"""V2.2 阶段 4：光伏消纳率纯计算模块测试（规格书 §3.3、§9.2、§6.4、§0.2、§8.3）。

覆盖：

* §9.2 的全部**可手算案例**（负荷 10/光伏 6、负荷 4/光伏 6、PV=0、负荷=0、两者皆 0）；
* 四项指标的**口径**（分母到底是光伏发电量还是负荷电量）与恒等式
  ``R_self + R_export = 1``、``R_coverage + R_grid = 1``；
* 逐间隔能量平衡（§3.3、§19）与容差；
* 边界：分母为 0 时返回 ``None``（"不适用"）而不是 0%；
* 缺失数据不得默认按 0（§6.4）；
* 来源标签红线（§0.2）：估算负荷算出的结果必须携带"基于估算"标记；
* 含储能场景**复用**既有 dispatch / energy_balance 引擎（§3.3、§6.6）；
* 性能：35040 点一次完整分析远快于 §8.3 的 5 秒预算。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from cenep.calculation.dispatch_engine import dispatch
from cenep.calculation.errors import ValidationError
from cenep.calculation.self_consumption import (
    BALANCE_TOLERANCE_KWH,
    CALIBERS,
    analyze_dispatch_outcome,
    analyze_self_consumption,
    caliber_of,
    interval_energy_flows,
    ratio_or_none,
)
from cenep.calculation.tariff_series import resolve_tariff
from cenep.calculation.timeseries_engine import build_time_axis
from cenep.domain.enums import (
    LoadDataSourceType,
    LoadQualityStatus,
    MissingDataPolicy,
    Resolution,
)
from cenep.domain.self_consumption_result import METRIC_ORDER, SelfConsumptionResult
from cenep.domain.timeseries import StorageDispatchConfig, TariffSeriesConfig, TariffProfile


# --------------------------------------------------------------------------- #
# §9.2 手算案例
# --------------------------------------------------------------------------- #
class TestHandCalculatedCases:
    def test_load_10_pv_6(self):
        """§9.2：每小时负荷 10 kWh、PV 6 kWh → 自用 6、上网 0、购电 4。"""
        load = np.full(24, 10.0)
        pv = np.full(24, 6.0)
        result = analyze_self_consumption(load, pv, interval_minutes=60)
        assert result.pv_used_on_site_kwh == pytest.approx(6.0 * 24)
        assert result.pv_export_kwh == pytest.approx(0.0)
        assert result.grid_import_kwh == pytest.approx(4.0 * 24)
        assert result.self_consumption_rate == pytest.approx(1.0)
        assert result.load_coverage_rate == pytest.approx(0.6)
        assert result.export_rate == pytest.approx(0.0)
        assert result.grid_dependency_rate == pytest.approx(0.4)

    def test_load_4_pv_6(self):
        """§9.2：每小时负荷 4 kWh、PV 6 kWh → 自用 4、上网 2、购电 0。"""
        load = np.full(24, 4.0)
        pv = np.full(24, 6.0)
        result = analyze_self_consumption(load, pv, interval_minutes=60)
        assert result.pv_used_on_site_kwh == pytest.approx(4.0 * 24)
        assert result.pv_export_kwh == pytest.approx(2.0 * 24)
        assert result.grid_import_kwh == pytest.approx(0.0)
        assert result.self_consumption_rate == pytest.approx(4.0 / 6.0)
        assert result.export_rate == pytest.approx(2.0 / 6.0)

    def test_pv_zero(self):
        """§9.2：PV=0 → 自用率与上网率为"不适用"；购电量等于负荷。"""
        load = np.array([3.0, 5.0, 0.0, 7.0])
        pv = np.zeros(4)
        result = analyze_self_consumption(load, pv, interval_minutes=60)
        assert result.self_consumption_rate is None
        assert result.export_rate is None
        assert result.grid_import_kwh == pytest.approx(15.0)
        assert result.grid_dependency_rate == pytest.approx(1.0)

    def test_load_zero(self):
        """§9.2：负荷=0 → 覆盖率与电网依赖率为"不适用"；PV 全部上网。"""
        load = np.zeros(4)
        pv = np.array([2.0, 4.0, 6.0, 8.0])
        result = analyze_self_consumption(load, pv, interval_minutes=60)
        assert result.load_coverage_rate is None
        assert result.grid_dependency_rate is None
        assert result.pv_export_kwh == pytest.approx(20.0)
        assert result.export_rate == pytest.approx(1.0)

    def test_both_zero(self):
        """§9.2：负荷与 PV 同时为 0 → 所有比率"不适用"，不得除零。"""
        result = analyze_self_consumption(np.zeros(3), np.zeros(3), interval_minutes=60)
        for key in METRIC_ORDER:
            assert result.rate_of(key) is None
        assert result.energy_balance_error_kwh == pytest.approx(0.0)

    def test_interval_minutes_respected(self):
        """15/30/60 分钟都只是"一次求和"，不改变口径（电量口径与 Δt 无关）。"""
        load = np.full(96, 2.5)
        pv = np.full(96, 1.5)
        result = analyze_self_consumption(load, pv, interval_minutes=15)
        assert result.interval_minutes == 15
        assert result.point_count == 96
        assert result.pv_used_on_site_kwh == pytest.approx(1.5 * 96)
        assert result.grid_import_kwh == pytest.approx(1.0 * 96)


# --------------------------------------------------------------------------- #
# 口径（§3.3、§12）
# --------------------------------------------------------------------------- #
class TestCalibers:
    def test_four_metrics_declared_in_order(self):
        assert [caliber_of(key).key for key in METRIC_ORDER] == list(METRIC_ORDER)
        assert set(CALIBERS) == set(METRIC_ORDER)

    def test_denominators_are_explicit(self):
        """分母必须写清：自用率/上网率 ÷ 光伏发电量；覆盖率/电网依赖率 ÷ 负荷电量。"""
        assert "光伏发电量" in caliber_of("self_consumption").denominator
        assert "光伏发电量" in caliber_of("export").denominator
        assert "负荷电量" in caliber_of("load_coverage").denominator
        assert "负荷电量" in caliber_of("grid_dependency").denominator

    def test_load_coverage_is_energy_not_time(self):
        """负荷覆盖率必须是**电量**覆盖率，口径里必须写明"不是时间覆盖率"。"""
        caliber = caliber_of("load_coverage")
        assert "电量覆盖率" in caliber.note
        assert "不是时间覆盖率" in caliber.note

    def test_boundary_documented(self):
        for key in METRIC_ORDER:
            boundary = caliber_of(key).boundary
            assert "不适用" in boundary
            assert "不得显示 0%" in boundary

    def test_caliber_text_includes_all_parts(self):
        text = caliber_of("self_consumption").text()
        for token in ("＝", "分子＝", "分母＝", "单位＝", "边界："):
            assert token in text

    def test_complementary_identities(self):
        """同一分母下两个比例必须互补（口径自洽的强断言）。"""
        rng = np.random.default_rng(20251010)
        load = rng.uniform(0.0, 20.0, size=500)
        pv = rng.uniform(0.0, 20.0, size=500)
        result = analyze_self_consumption(load, pv, interval_minutes=60)
        assert result.self_consumption_rate + result.export_rate == pytest.approx(1.0)
        assert result.load_coverage_rate + result.grid_dependency_rate == pytest.approx(1.0)

    def test_result_carries_calibers_and_metric_rows(self):
        result = analyze_self_consumption(np.full(4, 10.0), np.full(4, 6.0), interval_minutes=60)
        assert len(result.calibers) == 4
        rows = result.metric_rows()
        assert len(rows) == 4
        assert [row[0] for row in rows] == list(METRIC_ORDER)
        assert all(row[2] for row in rows), "每一项都必须带口径文本（§3.3）"
        assert rows[0][1] == "100.00%"

    def test_caliber_lines_in_assumptions(self):
        result = analyze_self_consumption(np.full(4, 10.0), np.full(4, 6.0), interval_minutes=60)
        lines = result.assumption_lines()
        assert len(lines) >= len(result.assumptions) + 4
        joined = "\n".join(lines)
        assert "分母" in joined and "不适用" in joined

    def test_ratio_or_none_boundary(self):
        assert ratio_or_none(0.0, 0.0) is None
        assert ratio_or_none(5.0, 0.0) is None
        assert ratio_or_none(5.0, 10.0) == pytest.approx(0.5)
        assert ratio_or_none(0.0, 10.0) == 0.0


# --------------------------------------------------------------------------- #
# 逐间隔公式与守恒
# --------------------------------------------------------------------------- #
class TestIntervalFlowsAndBalance:
    def test_interval_formula(self):
        flows = interval_energy_flows([10.0, 4.0, 0.0], [6.0, 6.0, 0.0])
        assert flows.used.tolist() == [6.0, 4.0, 0.0]
        assert flows.export.tolist() == [0.0, 2.0, 0.0]
        assert flows.grid_import.tolist() == [4.0, 0.0, 0.0]

    def test_balance_identities_hold(self):
        result = analyze_self_consumption(
            np.array([10.0, 4.0, 0.0]), np.array([6.0, 6.0, 0.0]), interval_minutes=60
        )
        assert result.is_balanced is True
        assert abs(result.max_interval_balance_error_kwh) <= BALANCE_TOLERANCE_KWH
        assert result.balance_tolerance_kwh == BALANCE_TOLERANCE_KWH

    def test_length_mismatch_reports_chinese_error(self):
        with pytest.raises(ValidationError) as exc:
            analyze_self_consumption(np.zeros(3), np.zeros(4), interval_minutes=60)
        assert "数量不一致" in str(exc.value)

    def test_negative_values_rejected(self):
        with pytest.raises(ValidationError) as exc:
            analyze_self_consumption(np.array([-1.0]), np.array([1.0]), interval_minutes=60)
        assert "负值" in str(exc.value)

    def test_non_finite_rejected(self):
        with pytest.raises(ValidationError) as exc:
            analyze_self_consumption(np.array([np.nan]), np.array([1.0]), interval_minutes=60)
        assert "非有限" in str(exc.value) or "缺失" in str(exc.value)


# --------------------------------------------------------------------------- #
# 缺失数据（§6.4）
# --------------------------------------------------------------------------- #
class TestMissingDataPolicy:
    def test_reject_by_default(self):
        load = np.array([1.0, np.nan, 3.0])
        with pytest.raises(ValidationError) as exc:
            analyze_self_consumption(load, np.full(3, 1.0), interval_minutes=60)
        assert "不得默认按 0 处理" in str(exc.value)

    def test_forward_fill_records_count(self):
        load = np.array([1.0, np.nan, 3.0])
        result = analyze_self_consumption(
            load,
            np.full(3, 1.0),
            interval_minutes=60,
            missing_policy=MissingDataPolicy.FORWARD_FILL,
        )
        assert result.interpolated_intervals == 1
        assert any("插补 1 个间隔" in line for line in result.assumptions)

    def test_linear_interpolation(self):
        load = np.array([0.0, np.nan, 4.0])
        result = analyze_self_consumption(
            load,
            np.full(3, 10.0),
            interval_minutes=60,
            missing_policy=MissingDataPolicy.LINEAR_INTERPOLATION,
        )
        assert result.load_energy_kwh == pytest.approx(0.0 + 2.0 + 4.0)


# --------------------------------------------------------------------------- #
# 来源标签红线（§0.2）
# --------------------------------------------------------------------------- #
class TestSourceLabelRedLine:
    def test_measured_source_no_estimate_flag(self):
        result = analyze_self_consumption(
            np.full(4, 10.0),
            np.full(4, 6.0),
            interval_minutes=60,
            load_source_type=LoadDataSourceType.HIGH_FREQUENCY_IMPORT,
        )
        assert result.is_based_on_estimate is False
        assert "实测" in result.provenance_text

    def test_estimate_source_sets_flag_and_badge(self):
        result = analyze_self_consumption(
            np.full(4, 10.0),
            np.full(4, 6.0),
            interval_minutes=60,
            load_source_type=LoadDataSourceType.MONTHLY_BILL_ESTIMATE,
        )
        assert result.is_based_on_estimate is True
        assert result.estimate_badge == "估算数据（不是实测）"
        assert any("基于估算" in line for line in result.assumption_lines())

    def test_estimate_source_without_flag_is_rejected(self):
        """结构性防线：非实测来源 + ``is_based_on_estimate=False`` 无法构造。"""
        with pytest.raises(ValueError) as exc:
            SelfConsumptionResult(
                load_energy_kwh=1.0,
                pv_generation_kwh=1.0,
                load_source_type=LoadDataSourceType.MONTHLY_BILL_ESTIMATE,
                is_based_on_estimate=False,
            )
        assert "基于估算" in str(exc.value)

    def test_measured_source_with_flag_is_rejected(self):
        with pytest.raises(ValueError):
            SelfConsumptionResult(
                load_energy_kwh=1.0,
                pv_generation_kwh=1.0,
                load_source_type=LoadDataSourceType.HIGH_FREQUENCY_IMPORT,
                is_based_on_estimate=True,
            )

    def test_time_coverage_is_separate_field(self):
        """时间覆盖率与"负荷覆盖率"是两个字段，必须分别说明（§3.3）。"""
        result = analyze_self_consumption(
            np.full(4, 10.0),
            np.full(4, 6.0),
            interval_minutes=60,
            coverage_ratio=0.5,
            data_quality_status=LoadQualityStatus.WARNING,
        )
        assert result.coverage_ratio == pytest.approx(0.5)
        assert result.load_coverage_rate == pytest.approx(0.6)  # 6 kWh ÷ 10 kWh
        assert any("时间覆盖率（≠ 负荷覆盖率）" in line for line in result.assumptions)


# --------------------------------------------------------------------------- #
# 含储能：复用既有 dispatch / energy_balance（§3.3、§6.6）
# --------------------------------------------------------------------------- #
class TestStorageScenarioReuse:
    @staticmethod
    def _dispatch_outcome():
        axis = build_time_axis(2025, Resolution.HOURLY)
        n = axis.point_count
        rng = np.random.default_rng(7)
        load = 400.0 + 300.0 * np.sin(np.arange(n) * 2.0 * np.pi / 24.0) ** 2
        pv = np.maximum(0.0, 900.0 * np.sin(np.arange(n) * 2.0 * np.pi / 24.0 - 1.0))
        del rng
        tariff = resolve_tariff(
            TariffSeriesConfig(
                profile=TariffProfile(
                    peak_price=1.2, flat_price=0.7, valley_price=0.4, export_price=0.35
                )
            ),
            axis,
        )
        outcome = dispatch(
            load=load,
            pv=pv,
            tariff=tariff,
            axis=axis,
            config=StorageDispatchConfig(allow_export=True),
            storage_capacity_kwh=1000.0,
            storage_power_kw=500.0,
        )
        return outcome

    def test_dispatch_outcome_summarised_and_balanced(self):
        outcome = self._dispatch_outcome()
        result = analyze_dispatch_outcome(outcome, interval_minutes=60)
        assert result.is_balanced is True
        assert result.point_count == 8760
        # 含储能时自称自用 = 光伏→负荷 + 光伏→储能
        assert result.pv_used_on_site_kwh == pytest.approx(
            float(np.sum(outcome.pv_to_load) + np.sum(outcome.pv_to_storage))
        )
        assert result.grid_import_kwh == pytest.approx(float(np.sum(outcome.grid_import)))
        assert any("未重写调度" in line for line in result.assumptions)

    def test_ratios_within_unit_range(self):
        outcome = self._dispatch_outcome()
        result = analyze_dispatch_outcome(outcome, interval_minutes=60)
        for key in METRIC_ORDER:
            value = result.rate_of(key)
            if value is not None:
                assert 0.0 <= value <= 1.0

    def test_no_storage_result_matches_engine_arrow(self):
        """同一批输入的"无储能"路径必须与"复用 dispatch 引擎"的结果一致。"""
        axis = build_time_axis(2025, Resolution.HOURLY)
        n = axis.point_count
        load = 400.0 + 300.0 * np.sin(np.arange(n) * 2.0 * np.pi / 24.0) ** 2
        pv = np.maximum(0.0, 900.0 * np.sin(np.arange(n) * 2.0 * np.pi / 24.0 - 1.0))
        tariff = resolve_tariff(TariffSeriesConfig(), axis)
        zero = dispatch(
            load=load,
            pv=pv,
            tariff=tariff,
            axis=axis,
            config=StorageDispatchConfig(),
            storage_capacity_kwh=0.0,
            storage_power_kw=0.0,
        )
        from_engine = analyze_dispatch_outcome(zero, interval_minutes=60)
        pure = analyze_self_consumption(load, pv, interval_minutes=60)
        assert from_engine.pv_used_on_site_kwh == pytest.approx(pure.pv_used_on_site_kwh)
        assert from_engine.grid_import_kwh == pytest.approx(pure.grid_import_kwh)
        assert from_engine.self_consumption_rate == pytest.approx(pure.self_consumption_rate)


# --------------------------------------------------------------------------- #
# 逐月明细
# --------------------------------------------------------------------------- #
class TestMonthlyBreakdown:
    def test_monthly_rows_sum_to_annual(self):
        axis = build_time_axis(2025, Resolution.HOURLY)
        n = axis.point_count
        load = 400.0 + 300.0 * np.sin(np.arange(n) * 2.0 * np.pi / 24.0) ** 2
        pv = np.maximum(0.0, 900.0 * np.sin(np.arange(n) * 2.0 * np.pi / 24.0 - 1.0))
        result = analyze_self_consumption(
            load, pv, interval_minutes=60, timestamps=list(axis.timestamps)
        )
        assert len(result.monthly) == 12
        assert sum(row.load_energy_kwh for row in result.monthly) == pytest.approx(
            result.load_energy_kwh
        )
        assert sum(row.pv_used_on_site_kwh for row in result.monthly) == pytest.approx(
            result.pv_used_on_site_kwh
        )
        for row in result.monthly:
            assert row.month_key.startswith("2025-")
            if row.pv_generation_kwh > 0.0:
                assert row.self_consumption_rate is not None

    def test_month_without_pv_reports_not_applicable(self):
        stamps = [np.datetime64("2025-01-01T00:00").astype(object)]
        del stamps
        from datetime import datetime

        stamps = [datetime(2025, 1, 1, h) for h in range(4)] + [
            datetime(2025, 2, 1, h) for h in range(4)
        ]
        load = np.full(8, 5.0)
        pv = np.array([1.0, 1.0, 1.0, 1.0] + [0.0, 0.0, 0.0, 0.0])
        result = analyze_self_consumption(
            load, pv, interval_minutes=60, timestamps=stamps
        )
        february = next(row for row in result.monthly if row.month == 2)
        assert february.self_consumption_rate is None
        assert february.rate_text("self_consumption") == "不适用"


# --------------------------------------------------------------------------- #
# 性能（§8.3）
# --------------------------------------------------------------------------- #
class TestPerformance:
    def test_35040_points_under_budget(self):
        """§8.3：35040 个 15 分钟点的基础消纳计算应在 5 秒内完成。"""
        axis = build_time_axis(2025, Resolution.QUARTER_HOURLY)
        n = axis.point_count
        assert n == 35040
        load = 400.0 + 300.0 * np.sin(np.arange(n) * 2.0 * np.pi / 96.0) ** 2
        pv = np.maximum(0.0, 900.0 * np.sin(np.arange(n) * 2.0 * np.pi / 96.0 - 1.0))
        started = time.perf_counter()
        result = analyze_self_consumption(
            load, pv, interval_minutes=15, timestamps=list(axis.timestamps)
        )
        elapsed = time.perf_counter() - started
        assert result.point_count == 35040
        assert len(result.monthly) == 12
        assert elapsed < 5.0, f"消纳计算耗时 {elapsed:.2f} 秒，超出 §8.3 的 5 秒预算"
