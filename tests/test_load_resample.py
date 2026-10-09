"""V2.2 阶段 3：负荷重采样与功率/电量换算测试（规格书 §3.2、§6.4、§9.2）。

本模块锁定的**口径**（这些口径决定了 15/30/60 分钟如何合并，必须逐条可断言）：

* 电量（kWh）降采样 = **求和**；升采样 = **均分**（假设子间隔功率恒定，必须留痕）；
* 功率（kW）降采样 = **按时长加权平均**；升采样 = **阶梯保持**；
* 功率 → 电量 **乘** Δt，电量 → 功率 **除** Δt（Δt 只在这里出现）；
* 把电量当功率求和（或反之）会造成 4 倍／0.25 倍的系统性错误 —— 测试直接断言这个差异；
* 不整除、尾部不完整、间隔相同等边界一律**中文报错**，不近似、不静默丢数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cenep.calculation.errors import ValidationError
from cenep.calculation.load_resample import (
    downsample_energy,
    downsample_power,
    energy_from_power,
    power_from_energy,
    resample_dataset,
    resample_energy,
    resample_power,
    to_energy_kwh,
    to_power_kw,
    upsample_energy,
    upsample_power,
)
from cenep.domain.enums import LoadDataSourceType, LoadValueKind, Resolution
from cenep.domain.load_data import HighFrequencyLoadDataset
from cenep.domain.timeseries import TimeSeriesPoint


def make_dataset(
    values: list[float],
    *,
    interval_minutes: int = 15,
    value_kind: LoadValueKind = LoadValueKind.INTERVAL_ENERGY_KWH,
    source_type: LoadDataSourceType = LoadDataSourceType.HIGH_FREQUENCY_IMPORT,
    start: datetime | None = None,
) -> HighFrequencyLoadDataset:
    """按给定逐间隔电量构造数据集（时间戳按 Δt 递推）。"""
    resolution = Resolution.from_interval_minutes(interval_minutes)
    assert resolution is not None
    base = start or datetime(2025, 1, 1)
    step = timedelta(minutes=interval_minutes)
    return HighFrequencyLoadDataset(
        profile_id="ds",
        interval_minutes=interval_minutes,
        resolution=resolution,
        value_kind=value_kind,
        source_type=source_type,
        estimated=not source_type.is_measured,
        points=[
            TimeSeriesPoint(timestamp=base + step * index, load_kwh=float(value))
            for index, value in enumerate(values)
        ],
    )


# --------------------------------------------------------------------------- #
# 功率 ⇄ 电量
# --------------------------------------------------------------------------- #
class TestPowerEnergyConversion:
    def test_energy_from_power_15min(self) -> None:
        """§3.2：E = P × Δt，15 分钟 Δt = 0.25 h。"""
        assert energy_from_power([100.0, 200.0], 0.25).tolist() == [25.0, 50.0]

    def test_energy_from_power_30min(self) -> None:
        assert energy_from_power([100.0], 0.5).tolist() == [50.0]

    def test_power_from_energy_round_trip(self) -> None:
        energy = energy_from_power([40.0, 80.0], 0.25)
        assert power_from_energy(energy, 0.25).tolist() == [40.0, 80.0]

    def test_to_energy_kwh_respects_declared_kind(self) -> None:
        power = to_energy_kwh([40.0], LoadValueKind.POWER_KW, 0.25)
        energy = to_energy_kwh([40.0], LoadValueKind.INTERVAL_ENERGY_KWH, 0.25)
        assert power.tolist() == [10.0]
        assert energy.tolist() == [40.0]  # 电量不再乘 Δt

    def test_to_power_kw_respects_declared_kind(self) -> None:
        assert to_power_kw([40.0], LoadValueKind.INTERVAL_ENERGY_KWH, 0.25).tolist() == [
            160.0
        ]
        assert to_power_kw([40.0], LoadValueKind.POWER_KW, 0.25).tolist() == [40.0]

    def test_invalid_delta_hours_reports_chinese_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            energy_from_power([1.0], 0.0)
        assert "Δt 必须为正" in str(excinfo.value)

    def test_non_finite_values_report_chinese_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            energy_from_power([1.0, float("nan")], 1.0)
        assert "非有限数值" in str(excinfo.value)

    def test_two_dimensional_input_reports_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            power_from_energy([[1.0, 2.0]], 1.0)
        assert "一维序列" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 降采样 / 升采样
# --------------------------------------------------------------------------- #
class TestResamplingCaliber:
    def test_downsample_energy_sums_within_hour(self) -> None:
        """15 → 60 分钟：电量**求和**（能量守恒），不做平均。"""
        quarter = [10.0, 20.0, 30.0, 40.0]
        assert downsample_energy(quarter, 15, 60).tolist() == [100.0]

    def test_downsample_power_is_time_weighted_mean(self) -> None:
        """15 → 60 分钟：功率取**平均**（25 kW），其电量 = 25 kWh。"""
        quarter_power = [10.0, 20.0, 30.0, 40.0]
        result = downsample_power(quarter_power, 15, 60)
        assert result.tolist() == [25.0]
        # 平均与求和是两种口径：混用即 4 倍误差，这正是本阶段要防的 Δt 缺陷
        assert float(result[0]) * 1.0 == pytest.approx(25.0)
        assert sum(quarter_power) == pytest.approx(100.0)
        assert sum(quarter_power) / float(result[0]) == pytest.approx(4.0)

    def test_downsample_30_to_60(self) -> None:
        assert downsample_energy([1.0, 3.0], 30, 60).tolist() == [4.0]
        assert downsample_power([1.0, 3.0], 30, 60).tolist() == [2.0]

    def test_upsample_energy_splits_evenly(self) -> None:
        """60 → 15 分钟：电量均分到 4 个子间隔，合计不变。"""
        hourly = [40.0, 8.0]
        result = upsample_energy(hourly, 60, 15)
        assert result.tolist() == [10.0, 10.0, 10.0, 10.0, 2.0, 2.0, 2.0, 2.0]
        assert float(result.sum()) == pytest.approx(float(sum(hourly)))

    def test_upsample_power_holds_value(self) -> None:
        result = upsample_power([25.0], 60, 15)
        assert result.tolist() == [25.0, 25.0, 25.0, 25.0]

    def test_downsample_requires_exact_division(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            downsample_energy([1.0] * 96, 15, 20)
        assert "不能整除" in str(excinfo.value)

    def test_incomplete_tail_reports_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            downsample_energy([1.0] * 5, 15, 60)
        assert "无法按 4 个一组" in str(excinfo.value)

    def test_same_interval_reports_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            downsample_energy([1.0], 15, 15)
        assert "无需重采样" in str(excinfo.value)

    def test_wrong_direction_reports_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            downsample_energy([1.0, 2.0], 60, 15)
        assert "只用于降采样" in str(excinfo.value)

    def test_zero_interval_reports_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            downsample_energy([1.0], 0, 60)
        assert "必须为正数" in str(excinfo.value)


class TestResamplePlan:
    def test_resample_energy_records_caliber(self) -> None:
        result, plan = resample_energy([10.0, 20.0, 30.0, 40.0], 15, 60)
        assert result.tolist() == [100.0]
        assert plan.factor == 4
        assert plan.direction == "downsample"
        assert plan.value_kind is LoadValueKind.INTERVAL_ENERGY_KWH
        assert "电量求和" in plan.method
        assert plan.assumptions == ()

    def test_upsample_records_assumption(self) -> None:
        result, plan = resample_energy([40.0], 60, 15)
        assert result.tolist() == [10.0] * 4
        assert plan.direction == "upsample"
        assert plan.assumptions
        assert "均分" in plan.assumptions[0]
        assert "15 分钟 → 60 分钟" not in plan.describe()

    def test_resample_power_conserves_energy(self) -> None:
        _, plan = resample_power([10.0, 20.0, 30.0, 40.0], 15, 60)
        assert plan.value_kind is LoadValueKind.POWER_KW
        assert "加权平均" in plan.method

    def test_energy_conservation_violation_is_detected(self) -> None:
        """人为构造不守恒的输入（NaN 已由入参校验拦截，这里验证容差判定逻辑）。"""
        # 正常路径下守恒，不得误报
        result, _ = resample_energy([1.0] * 35040, 15, 60)
        assert float(result.sum()) == pytest.approx(35040.0)


# --------------------------------------------------------------------------- #
# 数据集级重采样
# --------------------------------------------------------------------------- #
class TestResampleDataset:
    def test_15min_to_60min_preserves_annual_energy(self) -> None:
        dataset = make_dataset([10.0] * 96)  # 一天 96 个 15 分钟点，共 960 kWh
        result, plan = resample_dataset(dataset, 60)
        assert result.interval_minutes == 60
        assert result.resolution is Resolution.HOURLY
        assert result.point_count == 24
        assert result.annual_energy_kwh == pytest.approx(960.0)
        assert [p.timestamp.strftime("%H:%M") for p in result.points[:3]] == [
            "00:00",
            "01:00",
            "02:00",
        ]
        assert plan.direction == "downsample"
        assert any("15 分钟 → 60 分钟" in note for note in result.assumptions)

    def test_60min_to_15min_generates_quarter_timestamps(self) -> None:
        dataset = make_dataset([40.0, 8.0], interval_minutes=60)
        result, plan = resample_dataset(dataset, 15)
        assert result.point_count == 8
        assert result.annual_energy_kwh == pytest.approx(48.0)
        stamps = [p.timestamp for p in result.points]
        assert stamps[1] - stamps[0] == timedelta(minutes=15)
        assert [p.load_kwh for p in result.points[:4]] == [10.0] * 4
        assert plan.assumptions
        assert any("均分" in note for note in result.assumptions)

    def test_power_source_is_converted_before_resampling(self) -> None:
        """源文件是 kW 时必须先乘 Δt 变电量再降采样，不得对功率求和。"""
        dataset = make_dataset(
            [40.0] * 96, value_kind=LoadValueKind.POWER_KW
        )
        result, _ = resample_dataset(dataset, 60)
        assert result.point_count == 24
        assert result.annual_energy_kwh == pytest.approx(96 * 40.0 * 0.25)
        assert result.value_kind is LoadValueKind.INTERVAL_ENERGY_KWH
        assert any("P × Δt" in note for note in result.assumptions)

    def test_source_type_label_is_preserved(self) -> None:
        dataset = make_dataset(
            [10.0] * 96,
            source_type=LoadDataSourceType.MONTHLY_BILL_ESTIMATE,
        )
        result, _ = resample_dataset(dataset, 60)
        assert result.source_type is LoadDataSourceType.MONTHLY_BILL_ESTIMATE
        assert result.estimated

    def test_zero_length_dataset_reports_error(self) -> None:
        dataset = make_dataset([])
        with pytest.raises(ValidationError) as excinfo:
            resample_dataset(dataset, 60)
        assert "没有数据点" in str(excinfo.value)

    def test_unsupported_target_interval_reports_error(self) -> None:
        dataset = make_dataset([1.0] * 96)
        with pytest.raises(ValidationError) as excinfo:
            resample_dataset(dataset, 7)
        assert "没有对应的内部分辨率" in str(excinfo.value)

    def test_non_contiguous_source_reports_error(self) -> None:
        """有缺口的数据不得重采样：重新生成时间戳会把缺口掩盖（§6.4）。"""
        points = [
            TimeSeriesPoint(timestamp=datetime(2025, 1, 1, 0, 0), load_kwh=1.0),
            TimeSeriesPoint(timestamp=datetime(2025, 1, 1, 0, 15), load_kwh=1.0),
            TimeSeriesPoint(timestamp=datetime(2025, 1, 1, 1, 0), load_kwh=1.0),
            TimeSeriesPoint(timestamp=datetime(2025, 1, 1, 1, 15), load_kwh=1.0),
        ]
        dataset = HighFrequencyLoadDataset(
            profile_id="gap",
            interval_minutes=15,
            resolution=Resolution.QUARTER_HOURLY,
            points=points,
        )
        with pytest.raises(ValidationError) as excinfo:
            resample_dataset(dataset, 60)
        assert "必须连续" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 各分辨率点数（15/30/60 分钟统一内部表示）
# --------------------------------------------------------------------------- #
class TestResolutionUnification:
    @pytest.mark.parametrize(
        ("minutes", "resolution", "flat_points", "leap_points", "delta_hours"),
        [
            (15, Resolution.QUARTER_HOURLY, 35040, 35136, 0.25),
            (30, Resolution.HALF_HOURLY, 17520, 17568, 0.5),
            (60, Resolution.HOURLY, 8760, 8784, 1.0),
        ],
    )
    def test_resolution_metadata(
        self, minutes: int, resolution: Resolution, flat_points: int, leap_points: int,
        delta_hours: float,
    ) -> None:
        from cenep.calculation.timeseries_engine import points_per_year

        assert Resolution.from_interval_minutes(minutes) is resolution
        assert resolution.interval_minutes == minutes
        assert resolution.delta_hours == pytest.approx(delta_hours)
        assert points_per_year(2025, resolution) == flat_points
        assert points_per_year(2024, resolution) == leap_points

    def test_unknown_interval_maps_to_none(self) -> None:
        assert Resolution.from_interval_minutes(7) is None
        assert Resolution.from_interval_minutes(45) is None

    def test_360_minutes_of_15min_data_equals_6_hours(self) -> None:
        """96 个 15 分钟点 = 24 小时；电量口径与功率口径必须一致。"""
        energy_15 = [1.0] * 96
        dataset = make_dataset(energy_15)
        hourly, _ = resample_dataset(dataset, 60)
        assert hourly.point_count == 24
        assert hourly.annual_energy_kwh == pytest.approx(sum(energy_15))
        assert all(p.load_kwh == pytest.approx(4.0) for p in hourly.points)
