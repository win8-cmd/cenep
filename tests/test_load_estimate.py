"""V2.2 阶段 4：月账单估算负荷测试（规格书 §3.2、§6.5、§9.2、§0.2）。

覆盖：

* 可编辑典型负荷模板（内置 5 套、用户覆盖、权重校验）；
* **每月估算电量严格回归到输入月电量**（§6.5：误差 ≤ 1e-6 kWh）；
* 月电量的四种来源（账单 / 手工 / 年电量按比例拆分 / 年电量均匀分摊）；
* 日类型（工作日 / 周末 / 节假日 / 停产检修日）与比例参数、班次裁剪；
* 白天/夜间电量比例；
* 子小时展开假设（15/30 分钟）与假设披露；
* 来源标签红线（§0.2）：估算结果无法被标成实测。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest
from pydantic import ValidationError as PydanticValidationError

from cenep.calculation.errors import ValidationError
from cenep.calculation.load_estimate import (
    DAY_TYPE_HOLIDAY,
    DAY_TYPE_MAINTENANCE,
    DAY_TYPE_WEEKEND,
    DAY_TYPE_WORKDAY,
    estimate_from_monthly_energy,
    expand_hourly_weights,
    monthly_energies_from_annual,
    regress_month_to_energy,
    resolve_day_type_weights,
)
from cenep.domain.enums import (
    LoadDataSourceType,
    LoadEstimateSource,
    LoadQualityStatus,
    LoadValueKind,
    Resolution,
)
from cenep.domain.load_estimate import (
    BUILTIN_LOAD_TEMPLATES,
    HOURS_PER_DAY,
    LoadEstimateParams,
    LoadEstimateResult,
    MonthlyLoadEnergy,
    TypicalLoadTemplate,
    builtin_template,
    template_ids,
)


def _monthly(energy: float = 100_000.0, year: int = 2025) -> list[MonthlyLoadEnergy]:
    return [MonthlyLoadEnergy(year=year, month=m, energy_kwh=energy) for m in range(1, 13)]


def _params(**overrides) -> LoadEstimateParams:
    base = dict(year=2025, interval_minutes=60, template_id="double_shift")
    base.update(overrides)
    return LoadEstimateParams(**base)


# --------------------------------------------------------------------------- #
# 模板（§6.5）
# --------------------------------------------------------------------------- #
class TestTemplates:
    def test_five_builtin_templates(self):
        """§6.5 列举：单班生产、双班生产、连续生产、工作日为主、办公型。"""
        assert set(template_ids()) == {
            "single_shift",
            "double_shift",
            "continuous",
            "workday_dominant",
            "office",
        }
        assert len(BUILTIN_LOAD_TEMPLATES) == 5

    def test_each_template_has_24_point_non_negative_weights(self):
        for template in BUILTIN_LOAD_TEMPLATES:
            assert len(template.workday_weights) == HOURS_PER_DAY
            assert all(value >= 0.0 for value in template.workday_weights)
            assert sum(template.workday_weights) > 0.0
            assert template.description, "模板必须有中文说明（用户需要知道它代表什么工况）"

    def test_template_copy_is_independent(self):
        first = builtin_template("double_shift")
        first.workday_weights[0] = 999.0
        second = builtin_template("double_shift")
        assert second.workday_weights[0] != 999.0

    def test_unknown_template_reports_chinese_error(self):
        with pytest.raises(KeyError):
            builtin_template("不存在的模板")

    def test_estimate_with_unknown_template_id_reports_chinese_error(self):
        with pytest.raises(ValidationError) as exc:
            estimate_from_monthly_energy(_monthly(), _params(template_id="不存在"))
        assert "未知的典型负荷模板" in str(exc.value)
        assert "single_shift" in str(exc.value)

    def test_weight_length_validated(self):
        with pytest.raises(PydanticValidationError) as exc:
            TypicalLoadTemplate(template_id="x", name="x", workday_weights=[1.0] * 23)
        assert "24 点" in str(exc.value)

    def test_negative_weight_validated(self):
        weights = [1.0] * HOURS_PER_DAY
        weights[3] = -1.0
        with pytest.raises(PydanticValidationError) as exc:
            TypicalLoadTemplate(template_id="x", name="x", workday_weights=weights)
        assert "负值" in str(exc.value)

    def test_all_zero_weight_validated(self):
        with pytest.raises(PydanticValidationError) as exc:
            TypicalLoadTemplate(template_id="x", name="x", workday_weights=[0.0] * HOURS_PER_DAY)
        assert "合计为 0" in str(exc.value)


class TestWeightExpansion:
    def test_hourly_is_identity(self):
        weights = list(range(1, 25))
        assert expand_hourly_weights(weights, 60).tolist() == [float(v) for v in weights]

    def test_half_hourly_and_quarter_hourly_repeat(self):
        weights = [1.0] + [0.0] * 23
        assert expand_hourly_weights(weights, 30).tolist() == [1.0, 1.0] + [0.0] * 46
        assert expand_hourly_weights(weights, 15).tolist() == [1.0] * 4 + [0.0] * 92

    def test_invalid_interval_rejected(self):
        with pytest.raises(ValidationError) as exc:
            expand_hourly_weights([1.0] * HOURS_PER_DAY, 20)
        assert "15 / 30 / 60" in str(exc.value)


# --------------------------------------------------------------------------- #
# 回归（§3.2、§6.5）
# --------------------------------------------------------------------------- #
class TestMonthlyRegression:
    def test_regress_is_exact(self):
        weights = np.array([1.0, 2.0, 3.0, 4.0])
        values, residual = regress_month_to_energy(weights, 12345.6789)
        assert abs(values.sum() - 12345.6789) <= 1e-6
        assert abs(residual) <= 1e-6
        # 形状保持：比例不变
        assert values[1] / values[0] == pytest.approx(2.0)

    def test_zero_weights_rejected(self):
        with pytest.raises(ValidationError) as exc:
            regress_month_to_energy(np.zeros(4), 100.0)
        assert "合计为 0" in str(exc.value)

    def test_non_positive_energy_rejected(self):
        with pytest.raises(ValidationError) as exc:
            regress_month_to_energy(np.ones(4), 0.0)
        assert "必须为正数" in str(exc.value)

    @pytest.mark.parametrize("interval", [15, 30, 60])
    def test_every_month_regresses_to_input(self, interval):
        """§6.5：每个月的估算间隔电量必须严格回归到输入月电量（≤1e-6 kWh）。"""
        monthly = [MonthlyLoadEnergy(year=2025, month=m, energy_kwh=90_000.0 + m * 137.0)
                   for m in range(1, 13)]
        result = estimate_from_monthly_energy(monthly, _params(interval_minutes=interval))
        assert len(result.monthly) == 12
        for row in result.monthly:
            assert abs(row.estimated_energy_kwh - row.input_energy_kwh) <= 1e-6
            assert row.within_tolerance is True
            assert row.residual_ratio == pytest.approx(0.0, abs=1e-12)
        assert result.all_months_regressed is True
        assert result.max_abs_residual_kwh <= 1e-6
        assert result.dataset.annual_energy_kwh == pytest.approx(
            sum(row.input_energy_kwh for row in result.monthly), abs=1e-3
        )

    def test_leap_year_point_counts(self):
        monthly = _monthly(year=2024)
        for interval, expected in ((60, 8784), (30, 17568), (15, 35136)):
            result = estimate_from_monthly_energy(
                monthly, _params(year=2024, interval_minutes=interval)
            )
            assert result.dataset.point_count == expected
            assert result.dataset.resolution is Resolution.from_interval_minutes(interval)

    def test_result_is_marked_estimated(self):
        result = estimate_from_monthly_energy(_monthly(), _params())
        assert result.is_estimate is True
        assert result.dataset.estimated is True
        assert result.dataset.source_type is LoadDataSourceType.MONTHLY_BILL_ESTIMATE
        assert result.dataset.value_kind is LoadValueKind.INTERVAL_ENERGY_KWH
        assert result.dataset.quality_status is LoadQualityStatus.WARNING
        assert any("不是实测" in message for message in result.dataset.quality_messages)
        assert "估算" in result.provenance_text

    def test_is_estimate_cannot_be_false(self):
        """结构性防线：估算结果不允许把 ``is_estimate`` 改成 False（§0.2 红线）。"""
        result = estimate_from_monthly_energy(_monthly(), _params())
        with pytest.raises(PydanticValidationError) as exc:
            LoadEstimateResult(
                dataset=result.dataset,
                params=result.params,
                template_id=result.template_id,
                is_estimate=False,
            )
        assert "不得为 False" in str(exc.value)

    def test_estimated_dataset_rejects_measured_label(self):
        """估算曲线不能标成"实测高频导入"（构造期即拦截）。"""
        from cenep.domain.load_data import HighFrequencyLoadDataset
        from cenep.domain.timeseries import TimeSeriesPoint
        from datetime import datetime

        points = [TimeSeriesPoint(timestamp=datetime(2025, 1, 1, h), load_kwh=1.0) for h in range(4)]
        with pytest.raises(PydanticValidationError) as exc:
            HighFrequencyLoadDataset(
                profile_id="x",
                source_type=LoadDataSourceType.HIGH_FREQUENCY_IMPORT,
                estimated=True,
                interval_minutes=60,
                points=points,
                period_start=points[0].timestamp,
                period_end=points[-1].timestamp,
            )
        assert "不得同时标记为估算" in str(exc.value)


# --------------------------------------------------------------------------- #
# 月电量来源（§6.5）
# --------------------------------------------------------------------------- #
class TestMonthlyEnergySources:
    def test_manual_energy_kept(self):
        result = estimate_from_monthly_energy(_monthly(), _params())
        assert {row.source for row in result.monthly} == {LoadEstimateSource.MANUAL}

    def test_annual_uniform_split_is_explicit_default(self):
        """只给年电量：均匀分摊，并作为**明确的默认估算假设**披露。"""
        result = estimate_from_monthly_energy(
            [], _params(annual_energy_kwh=1_200_000.0)
        )
        assert len(result.monthly) == 12
        assert {row.source for row in result.monthly} == {LoadEstimateSource.UNIFORM_DEFAULT}
        assert all(row.input_energy_kwh == pytest.approx(100_000.0) for row in result.monthly)
        assert any("均匀分摊" in line for line in result.assumptions)

    def test_annual_with_monthly_ratios(self):
        ratios = [2.0] + [1.0] * 11
        result = estimate_from_monthly_energy(
            [], _params(annual_energy_kwh=1_300_000.0, monthly_split_ratios=ratios)
        )
        assert {row.source for row in result.monthly} == {LoadEstimateSource.ANNUAL_SPLIT}
        assert result.monthly[0].input_energy_kwh == pytest.approx(
            1_300_000.0 * 2.0 / 13.0
        )
        assert any("月度比例" in line for line in result.assumptions)

    def test_missing_months_filled_only(self):
        partial = [
            MonthlyLoadEnergy(year=2025, month=m, energy_kwh=50_000.0) for m in range(1, 7)
        ]
        result = estimate_from_monthly_energy(partial, _params(annual_energy_kwh=720_000.0))
        sources = {row.month: row.source for row in result.monthly}
        for month in range(1, 7):
            assert sources[month] is LoadEstimateSource.MANUAL
        for month in range(7, 13):
            assert sources[month] is LoadEstimateSource.UNIFORM_DEFAULT

    def test_no_monthly_and_no_annual_reports_chinese_error(self):
        with pytest.raises(ValidationError) as exc:
            estimate_from_monthly_energy([], _params())
        assert "年用电量" in str(exc.value)

    def test_duplicate_month_rejected(self):
        monthly = [
            MonthlyLoadEnergy(year=2025, month=3, energy_kwh=1000.0),
            MonthlyLoadEnergy(year=2025, month=3, energy_kwh=2000.0),
        ]
        with pytest.raises(ValidationError) as exc:
            estimate_from_monthly_energy(monthly, _params(annual_energy_kwh=120_000.0))
        assert "重复" in str(exc.value)

    def test_year_mismatch_rejected(self):
        monthly = [MonthlyLoadEnergy(year=2024, month=1, energy_kwh=1000.0)]
        with pytest.raises(ValidationError) as exc:
            estimate_from_monthly_energy(monthly, _params(year=2025, annual_energy_kwh=1.0))
        assert "年份" in str(exc.value)

    def test_monthly_energies_from_annual_helper(self):
        params = _params(annual_energy_kwh=120_000.0)
        items = monthly_energies_from_annual(params, missing_months=[1, 2])
        assert [item.month for item in items] == [1, 2]
        assert all(item.energy_kwh == pytest.approx(10_000.0) for item in items)


# --------------------------------------------------------------------------- #
# 运行参数（§6.5）
# --------------------------------------------------------------------------- #
class TestOperatingParameters:
    def test_day_type_levels(self):
        weights = resolve_day_type_weights(
            builtin_template("double_shift"),
            _params(
                weekend_run_ratio=0.4,
                holiday_run_ratio=0.2,
                maintenance_run_ratio=0.05,
            ),
        )
        workday_sum = float(weights[DAY_TYPE_WORKDAY].sum())
        assert float(weights[DAY_TYPE_WEEKEND].sum()) == pytest.approx(workday_sum * 0.4)
        assert float(weights[DAY_TYPE_HOLIDAY].sum()) == pytest.approx(workday_sum * 0.2)
        assert float(weights[DAY_TYPE_MAINTENANCE].sum()) == pytest.approx(workday_sum * 0.05)
        # 形状不变（比例一致）
        assert weights[DAY_TYPE_WEEKEND][10] / weights[DAY_TYPE_WORKDAY][10] == pytest.approx(0.4)

    def test_weekend_weights_override(self):
        weekend = [1.0] + [0.0] * 23
        weights = resolve_day_type_weights(
            builtin_template("double_shift"),
            _params(weekend_weights=weekend, weekend_run_ratio=1.0),
        )
        assert weights[DAY_TYPE_WEEKEND][0] == pytest.approx(1.0)
        assert weights[DAY_TYPE_WEEKEND][1] == pytest.approx(0.0)

    def test_shift_clipping_overrides_night_shape(self):
        weights = resolve_day_type_weights(
            builtin_template("double_shift"),
            _params(offshift_run_ratio=0.1, shift_start_hour=8, shift_end_hour=20),
        )
        workday = weights[DAY_TYPE_WORKDAY]
        peak_inside = float(workday[8:20].max())
        assert float(workday[0]) == pytest.approx(peak_inside * 0.1)
        assert float(workday[23]) == pytest.approx(peak_inside * 0.1)

    def test_no_shift_clipping_when_ratio_is_none(self):
        weights = resolve_day_type_weights(
            builtin_template("double_shift"), _params(offshift_run_ratio=None)
        )
        template = builtin_template("double_shift")
        assert weights[DAY_TYPE_WORKDAY].tolist() == [float(v) for v in template.workday_weights]

    def test_shift_window_with_zero_inside_weight_rejected(self):
        zero_day = [0.0] * 8 + [1.0] * 16
        template = TypicalLoadTemplate(
            template_id="t", name="t", workday_weights=zero_day, editable=True
        )
        with pytest.raises(ValidationError) as exc:
            resolve_day_type_weights(
                template, _params(offshift_run_ratio=0.1, shift_start_hour=0, shift_end_hour=8)
            )
        assert "全为 0" in str(exc.value)

    def test_holiday_and_shutdown_days_change_shape(self):
        result = estimate_from_monthly_energy(
            _monthly(),
            _params(
                interval_minutes=60,
                holidays=[date(2025, 1, 1)],
                shutdown_days=[date(2025, 1, 2)],
                holiday_run_ratio=0.2,
                maintenance_run_ratio=0.0,
            ),
        )
        by_stamp = {point.timestamp: point.load_kwh for point in result.dataset.points}
        from datetime import datetime

        holiday_energy = by_stamp[datetime(2025, 1, 1, 10)]
        shutdown_energy = by_stamp[datetime(2025, 1, 2, 10)]
        workday_energy = by_stamp[datetime(2025, 1, 3, 10)]
        assert shutdown_energy == pytest.approx(0.0)
        assert 0.0 < holiday_energy < workday_energy

    def test_workdays_per_week(self):
        """每周生产 6 天时，周六按工作日处理（水平高于周日）。"""
        result = estimate_from_monthly_energy(
            _monthly(),
            _params(workdays_per_week=6, weekend_run_ratio=0.1, interval_minutes=60),
        )
        by_stamp = {point.timestamp: point.load_kwh for point in result.dataset.points}
        from datetime import datetime

        saturday = by_stamp[datetime(2025, 1, 4, 10)]  # 周六
        sunday = by_stamp[datetime(2025, 1, 5, 10)]  # 周日
        assert saturday > sunday

    def test_day_night_ratio(self):
        """白天 70% / 夜间 30% 必须精确落到两组电量上（§6.5）。"""
        result = estimate_from_monthly_energy(
            _monthly(energy=100_000.0),
            _params(
                interval_minutes=60,
                daytime_load_ratio=0.7,
                nighttime_load_ratio=0.3,
            ),
        )
        day_total = 0.0
        night_total = 0.0
        for point in result.dataset.points:
            if point.timestamp.month != 1:
                continue
            if 6 <= point.timestamp.hour < 18:
                day_total += point.load_kwh
            else:
                night_total += point.load_kwh
        assert day_total == pytest.approx(70_000.0, rel=1e-9)
        assert night_total == pytest.approx(30_000.0, rel=1e-9)

    def test_day_night_requires_both(self):
        with pytest.raises(PydanticValidationError) as exc:
            _params(daytime_load_ratio=0.7)
        assert "同时给出" in str(exc.value)

    def test_day_night_must_sum_to_one(self):
        with pytest.raises(PydanticValidationError) as exc:
            _params(daytime_load_ratio=0.7, nighttime_load_ratio=0.4)
        assert "合计必须为 1" in str(exc.value)

    def test_interval_outside_supported_rejected(self):
        with pytest.raises(PydanticValidationError) as exc:
            _params(interval_minutes=45)
        assert "15 / 30 / 60" in str(exc.value)

    def test_timezone_must_be_shanghai(self):
        with pytest.raises(PydanticValidationError) as exc:
            _params(timezone="UTC")
        assert "Asia/Shanghai" in str(exc.value)


# --------------------------------------------------------------------------- #
# 假设披露（§6.5、§0.2）
# --------------------------------------------------------------------------- #
class TestAssumptions:
    def test_caliber_and_missing_data_disclosed(self):
        result = estimate_from_monthly_energy([], _params(annual_energy_kwh=1_200_000.0))
        lines = result.caliber_lines()
        joined = "\n".join(lines)
        assert "E_i = E_month" in joined or "E_month" in joined
        assert "不是 kW" in joined
        assert "均匀分摊" in joined
        assert "估算" in joined
        assert "1e-6" in joined or "容差" in joined

    def test_sub_hour_expansion_assumption_for_15min(self):
        result = estimate_from_monthly_energy(_monthly(), _params(interval_minutes=15))
        assert any("子小时展开假设" in line for line in result.assumptions)
        assert any("均分" in line for line in result.assumptions)

    def test_no_sub_hour_assumption_for_hourly(self):
        result = estimate_from_monthly_energy(_monthly(), _params(interval_minutes=60))
        assert not any("子小时展开假设" in line for line in result.assumptions)

    def test_shift_clipping_assumption_disclosed(self):
        result = estimate_from_monthly_energy(
            _monthly(), _params(offshift_run_ratio=0.1, shift_start_hour=8, shift_end_hour=20)
        )
        assert any("覆盖模板自带的夜间形状" in line for line in result.assumptions)

    def test_dataset_carries_same_assumptions(self):
        result = estimate_from_monthly_energy(_monthly(), _params())
        assert result.dataset.assumptions == result.assumptions
