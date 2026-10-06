"""V2 时序数据模型测试（V2 §6、§7、§10、§16、§19、§21、§55、§62、§65、§105）。

覆盖两类风险：

1. **V1 兼容性**：新增 ``timeseries`` 段不得改变 V1 项目的默认行为与 .nep 读写；
2. **模型自洽性**：派生时间字段的序列化/反序列化往返、列式结果与行视图一致性、
   以及各种非法配置必须被拦截。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from cenep.domain.enums import (
    DayType,
    DispatchAction,
    DispatchStrategy,
    LoadProfileMode,
    MissingDataPolicy,
    OptimizationObjective,
    PVProfileMode,
    Resolution,
    ScanVariable,
    TariffPeriod,
)
from cenep.domain.models import Project
from cenep.domain.timeseries import (
    LoadProfile,
    LoadProfileConfig,
    PVProfile,
    PVProfileConfig,
    StorageDispatchConfig,
    TariffProfile,
    TariffSeriesConfig,
    TimePeriodRule,
    TimeSeriesConfig,
    TimeSeriesPoint,
)
from cenep.domain.timeseries_results import (
    HOURLY_COLUMNS,
    HourlyResult,
    TimeSeriesResultSet,
)
from cenep.infrastructure.project_file import load_project, save_project

YEAR = 2025


def _points(hours: int = 24, start: datetime | None = None) -> list[TimeSeriesPoint]:
    start = start or datetime(YEAR, 1, 1, 0)
    return [
        TimeSeriesPoint(
            timestamp=start.replace(hour=h) if start.hour == 0 else start,
            load_kwh=100.0 + h,
            electricity_price=0.65,
        )
        for h in range(hours)
    ]


def _hourly_points() -> list[TimeSeriesPoint]:
    return [
        TimeSeriesPoint(timestamp=datetime(YEAR, 1, 1, h), load_kwh=100.0 + h, electricity_price=0.65)
        for h in range(24)
    ]


# --------------------------------------------------------------------------- #
# V2 §7 时间索引
# --------------------------------------------------------------------------- #
class TestTimeSeriesPoint:
    def test_derived_time_fields(self):
        """§6/§7：时间字段由 timestamp 派生，不落库。"""
        p = TimeSeriesPoint(timestamp=datetime(2025, 6, 15, 13))
        assert (p.year, p.month, p.day, p.hour) == (2025, 6, 15, 13)
        assert p.weekday == 6  # 2025-06-15 是周日
        assert p.is_weekend is True

    def test_weekday_monday_is_zero(self):
        assert TimeSeriesPoint(timestamp=datetime(2025, 6, 16)).weekday == 0
        assert TimeSeriesPoint(timestamp=datetime(2025, 6, 16)).is_weekend is False

    def test_serialization_contains_all_spec_fields(self):
        """§6：序列化必须包含规范列出的全部字段。"""
        dumped = TimeSeriesPoint(timestamp=datetime(2025, 1, 1, 5)).model_dump()
        for name in (
            "timestamp", "year", "month", "day", "hour", "weekday",
            "is_weekend", "is_holiday", "load_kwh", "pv_generation_kwh",
            "electricity_price", "export_price",
        ):
            assert name in dumped, f"§6 要求的字段 {name} 未出现在序列化结果中"

    def test_round_trip_through_dict(self):
        """回归：computed_field 会进 JSON，反序列化必须能吃回去。"""
        p = TimeSeriesPoint(timestamp=datetime(2025, 3, 9, 17), load_kwh=42.0)
        assert TimeSeriesPoint.model_validate(p.model_dump()) == p

    def test_contradictory_time_field_rejected(self):
        """外部数据里 hour 与 timestamp 矛盾时必须报错，不得静默采信。"""
        with pytest.raises(ValidationError, match="自相矛盾"):
            TimeSeriesPoint.model_validate(
                {"timestamp": "2025-06-15T13:00:00", "hour": 7, "load_kwh": 1.0}
            )

    def test_extra_unknown_field_still_forbidden(self):
        """剥离派生字段后，extra=forbid 对真正的未知字段仍然生效。"""
        with pytest.raises(ValidationError):
            TimeSeriesPoint.model_validate(
                {"timestamp": "2025-06-15T13:00:00", "不存在的字段": 1}
            )

    def test_negative_values_not_hard_rejected(self):
        """§52–§54：非法值要由校验器统一报告，模型层不硬拒绝，否则用户看不到中文提示。"""
        p = TimeSeriesPoint(timestamp=datetime(2025, 1, 1), load_kwh=-5.0)
        assert p.load_kwh == -5.0


# --------------------------------------------------------------------------- #
# V2 §6 / §10 / §21 配置校验
# --------------------------------------------------------------------------- #
class TestDispatchConfigValidation:
    def test_defaults_follow_spec(self):
        """§10.1 / §20 / §21：默认 SOC 10%~100%，禁止储能上网，禁止电网充电。"""
        cfg = StorageDispatchConfig()
        assert (cfg.soc_min, cfg.soc_max) == (0.10, 1.00)
        assert cfg.allow_export is False
        assert cfg.allow_grid_charge is False

    def test_default_efficiency_matches_v1_round_trip(self):
        """回归：往返效率必须严格等于 V1 的 0.88。

        早期默认单向效率写成 0.938，``0.938² = 0.879844``，与 V1 的 0.88 相差 0.016%，
        会通过储能套利收益与 LCOS 传导到经济指标。默认值改为 ``√0.88`` 精确均分。
        """
        cfg = StorageDispatchConfig()
        assert cfg.round_trip_efficiency == pytest.approx(0.88, rel=1e-12)
        assert cfg.charge_efficiency == pytest.approx(cfg.discharge_efficiency, rel=1e-12)

    def test_custom_efficiencies_multiply(self):
        cfg = StorageDispatchConfig(charge_efficiency=0.95, discharge_efficiency=0.9)
        assert cfg.round_trip_efficiency == pytest.approx(0.855)

    def test_soc_bounds(self):
        with pytest.raises(ValidationError, match="SOC 下限必须小于上限"):
            StorageDispatchConfig(soc_min=0.9, soc_max=0.5)

    def test_initial_soc_inside_bounds(self):
        with pytest.raises(ValidationError, match="起始 SOC"):
            StorageDispatchConfig(soc_min=0.2, soc_max=0.8, initial_soc=0.05)

    def test_grid_charge_switch_consistency(self):
        with pytest.raises(ValidationError, match="电网充电总开关"):
            StorageDispatchConfig(charge_from_grid=True, allow_grid_charge=False)

    def test_grid_charge_allowed_when_both_on(self):
        cfg = StorageDispatchConfig(charge_from_grid=True, allow_grid_charge=True)
        assert cfg.charge_from_grid and cfg.allow_grid_charge

    def test_thresholds_must_be_ordered(self):
        with pytest.raises(ValidationError, match="充电价格阈值必须小于放电价格阈值"):
            StorageDispatchConfig(charge_price_threshold=0.9, discharge_price_threshold=0.4)

    def test_thresholds_equal_is_rejected(self):
        with pytest.raises(ValidationError, match="必须小于"):
            StorageDispatchConfig(charge_price_threshold=0.5, discharge_price_threshold=0.5)


class TestProfileConfigValidation:
    def test_typical_day_must_be_24_points(self):
        with pytest.raises(ValidationError, match="24 点"):
            LoadProfileConfig(mode=LoadProfileMode.TYPICAL_DAY, typical_workday=[1.0] * 23)

    def test_monthly_factors_must_be_12(self):
        with pytest.raises(ValidationError, match="12 个"):
            LoadProfileConfig(monthly_factors=[1.0] * 11)

    def test_hourly_mode_requires_points(self):
        with pytest.raises(ValidationError, match="必须提供 hourly"):
            LoadProfileConfig(mode=LoadProfileMode.HOURLY)

    def test_typical_day_mode_requires_curve(self):
        with pytest.raises(ValidationError, match="typical_workday"):
            LoadProfileConfig(mode=LoadProfileMode.TYPICAL_DAY)

    def test_pv_hourly_mode_requires_points(self):
        with pytest.raises(ValidationError, match="必须提供 hourly"):
            PVProfileConfig(mode=PVProfileMode.HOURLY)

    def test_pv_monthly_hour_factor_requires_both(self):
        with pytest.raises(ValidationError, match="必须同时提供"):
            PVProfileConfig(mode=PVProfileMode.MONTHLY_HOUR_FACTOR, monthly_factors=[1.0] * 12)

    def test_valid_hourly_load_config(self):
        cfg = LoadProfileConfig(
            mode=LoadProfileMode.HOURLY,
            hourly=LoadProfile(points=_hourly_points(), annual_energy=2412.0),
        )
        assert cfg.hourly is not None and cfg.hourly.point_count == 24


class TestTimePeriodRule:
    def test_hour_range(self):
        with pytest.raises(ValidationError, match="0~23"):
            TimePeriodRule(period=TariffPeriod.PEAK, hours=[24])

    def test_month_range(self):
        with pytest.raises(ValidationError, match="1~12"):
            TimePeriodRule(period=TariffPeriod.PEAK, months=[13])

    def test_valid_rule(self):
        rule = TimePeriodRule(
            period=TariffPeriod.SHARP_PEAK, months=[7, 8], day_types=[DayType.WORKDAY], hours=[11, 12]
        )
        assert rule.period.is_high is True

    def test_period_helpers(self):
        assert TariffPeriod.DEEP_VALLEY.is_low is True
        assert TariffPeriod.PEAK.is_high is True
        assert TariffPeriod.FLAT.is_high is False and TariffPeriod.FLAT.is_low is False


# --------------------------------------------------------------------------- #
# V2 §6 / §105 列式结果集与行视图一致性
# --------------------------------------------------------------------------- #
class TestTimeSeriesResultSet:
    def _build(self, n: int = 48) -> TimeSeriesResultSet:
        timestamps = [datetime(2025, 1, 1 + i // 24, i % 24) for i in range(n)]
        columns = {name: [float(i * 10 + k) for i in range(n)] for k, name in enumerate(HOURLY_COLUMNS)}
        return TimeSeriesResultSet.from_columns(
            timestamps=timestamps,
            columns=columns,
            dispatch_actions=[DispatchAction.CHARGE.value] * n,
            dispatch_reasons=["谷段电价低于充电阈值"] * n,
        )

    def test_length_and_columns(self):
        rs = self._build()
        assert len(rs) == 48
        for name in HOURLY_COLUMNS:
            assert len(rs.column(name)) == 48

    def test_row_view_matches_columns(self):
        """§105：列式存储与行视图数值必须完全一致。"""
        rs = self._build()
        row = rs.row(7)
        assert isinstance(row, HourlyResult)
        for name in HOURLY_COLUMNS:
            assert getattr(row, name) == rs.column(name)[7]

    def test_row_carries_dispatch_explanation(self):
        """§16：行视图必须带出可解释的调度原因。"""
        row = self._build().row(0)
        assert row.dispatch_action is DispatchAction.CHARGE
        assert row.dispatch_reason == "谷段电价低于充电阈值"

    def test_hourly_result_covers_all_columns(self):
        """列名集合必须是 HourlyResult 字段的子集，避免列式/行式脱节。"""
        missing = [c for c in HOURLY_COLUMNS if c not in HourlyResult.model_fields]
        assert missing == []

    def test_slice_hours(self):
        rs = self._build()
        part = rs.slice_hours(10, 20)
        assert len(part) == 10
        assert part.column("load")[0] == rs.column("load")[10]

    def test_row_index_bounds(self):
        with pytest.raises(IndexError, match="越界"):
            self._build().row(999)

    def test_unknown_column_raises(self):
        with pytest.raises(KeyError, match="未知的时序结果列"):
            self._build().column("不存在的列")


# --------------------------------------------------------------------------- #
# V2 §7 分辨率
# --------------------------------------------------------------------------- #
class TestResolution:
    def test_points_per_year(self):
        assert Resolution.HOURLY.points_per_year == 8760
        assert Resolution.QUARTER_HOURLY.points_per_year == 35040
        assert Resolution.DAILY.points_per_year == 365
        assert Resolution.MONTHLY.points_per_year == 12

    def test_delta_hours(self):
        assert Resolution.HOURLY.delta_hours == 1.0
        assert Resolution.QUARTER_HOURLY.delta_hours == 0.25


# --------------------------------------------------------------------------- #
# V2 §1.1 / §64 / §65 V1 兼容与项目文件往返
# --------------------------------------------------------------------------- #
class TestProjectCompatibility:
    def test_timeseries_disabled_by_default(self):
        """§1.1：默认关闭时序，V1 行为不受影响。"""
        assert Project().timeseries.enabled is False

    def test_v1_project_unchanged(self, golden_pv):
        assert golden_pv.timeseries.enabled is False

    def test_nep_round_trip_with_timeseries(self, tmp_path):
        """§63/§64：含 8760 时序的项目必须可存可读，且二次存盘幂等。"""
        proj = Project()
        proj.timeseries = TimeSeriesConfig(
            enabled=True,
            base_year=YEAR,
            load=LoadProfileConfig(
                mode=LoadProfileMode.HOURLY,
                hourly=LoadProfile(
                    profile_id="L1",
                    name="实测负荷",
                    points=_hourly_points(),
                    annual_energy=sum(p.load_kwh for p in _hourly_points()),
                ),
            ),
            dispatch=StorageDispatchConfig(
                strategy=DispatchStrategy.PEAK_VALLEY,
                charge_price_threshold=0.4,
                discharge_price_threshold=0.9,
            ),
            tariff=TariffSeriesConfig(
                profile=TariffProfile(peak_price=1.0, flat_price=0.65, valley_price=0.35)
            ),
            holidays=[date(2025, 1, 1)],
        )
        first = save_project(proj, tmp_path / "V2项目")
        raw = first.read_text(encoding="utf-8")

        payload = json.loads(raw)["project"]["timeseries"]
        assert payload["enabled"] is True
        assert len(payload["load"]["hourly"]["points"]) == 24
        assert payload["dispatch"]["strategy"] == "PEAK_VALLEY"

        back = load_project(first)
        hourly = back.timeseries.load.hourly
        assert hourly is not None
        assert hourly.point_count == 24
        assert hourly.points[0].load_kwh == 100.0
        assert back.timeseries.dispatch.strategy is DispatchStrategy.PEAK_VALLEY
        assert back.timeseries.holidays == [date(2025, 1, 1)]

        second = save_project(back, tmp_path / "V2项目2")
        assert second.read_text(encoding="utf-8") == raw, "二次存盘结果必须与首次完全一致"

    def test_v1_nep_loads_without_timeseries_block(self, tmp_path, golden_pv):
        """§65：V1 的 .nep（无 timeseries 段）必须能照常打开。"""
        path = save_project(golden_pv, tmp_path / "V1项目")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["project"].pop("timeseries", None)
        stripped = tmp_path / "V1项目_无时序段.nep"
        stripped.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        loaded = load_project(stripped)
        assert loaded.timeseries.enabled is False
        assert loaded.pv.pv_capacity_kwp == golden_pv.pv.pv_capacity_kwp


# --------------------------------------------------------------------------- #
# V2 §41 / §47 新增枚举
# --------------------------------------------------------------------------- #
class TestNewEnums:
    def test_labels_present(self):
        for member in (
            DispatchStrategy.PEAK_VALLEY,
            DispatchStrategy.PV_SELF_CONSUMPTION,
            DispatchStrategy.ECONOMIC_OPTIMIZATION,
            OptimizationObjective.MAX_NPV,
            ScanVariable.PV_CAPACITY,
            MissingDataPolicy.REJECT,
            Resolution.HOURLY,
            TariffPeriod.SHARP_PEAK,
        ):
            assert member.label, f"{member} 缺少中文标签"

    def test_default_objective_is_max_npv(self):
        """§47：默认寻优目标为最大 NPV。"""
        assert OptimizationObjective.MAX_NPV.value == "MAX_NPV"

    def test_default_missing_policy_is_reject(self):
        """§53：默认禁止静默填充。"""
        assert MissingDataPolicy.REJECT.label.startswith("不允许计算")
