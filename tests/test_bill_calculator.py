"""账单基准数据计算与校验测试（V2.1 §3.1、§2.1、§5.5）。

覆盖规格书 §3.1 的四条公式：年度总用电量、账单平均综合电价、账单分项合计差 ΔC、
分时电量合计差 ΔE，以及容差可配置、差异必须显式报告、负数/两部制等质量规则。
"""

from __future__ import annotations

from datetime import date

import pytest

from cenep.calculation.bill_calculator import (
    apply_quality,
    annual_bill_summary,
    average_comprehensive_price,
    energy_period_breakdown,
    energy_sub_charge_components,
    level1_charge_components,
    monthly_summary,
    reconcile_bill,
    sum_period_energy,
    validate_bill_fields,
)
from cenep.domain.bill_models import (
    BillTolerance,
    ElectricityBill,
    make_bill_id,
)
from cenep.domain.enums import BillEnergyPeriod, BillQualityStatus, TariffStructure


def _bill(
    start: date = date(2026, 1, 1),
    end: date = date(2026, 1, 31),
    meter: str | None = "M1",
    **kwargs,
) -> ElectricityBill:
    payload: dict = {
        "bill_id": make_bill_id(start, end, meter),
        "project_id": "P1",
        "billing_period_start": start,
        "billing_period_end": end,
        "meter_id": meter,
    }
    payload.update(kwargs)
    return ElectricityBill(**payload)


def _consistent_bill(**kwargs) -> ElectricityBill:
    """一条完全自洽的账单：分时合计 = 总电量，分项合计 = 电度合计 = 账单总额。"""
    payload: dict = {
        "energy_total_kwh": 100_000.0,
        "energy_sharp_kwh": 10_000.0,
        "energy_peak_kwh": 30_000.0,
        "energy_flat_kwh": 40_000.0,
        "energy_valley_kwh": 20_000.0,
        "energy_charge_yuan": 65_000.0,
        "market_purchase_charge_yuan": 45_000.0,
        "transmission_distribution_charge_yuan": 15_000.0,
        "line_loss_charge_yuan": 1_000.0,
        "system_operation_charge_yuan": 500.0,
        "government_fund_charge_yuan": 3_500.0,
        "bill_total_yuan": 65_000.0,
        "voltage_level": "10kV",
        "tariff_structure": TariffStructure.SINGLE_PART,
    }
    payload.update(kwargs)
    return _bill(**payload)


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


class TestPeriodEnergyDifference:
    """§3.1：ΔE = E_total − Σ E_period。"""

    def test_consistent_periods(self):
        outcome = reconcile_bill(_consistent_bill())
        assert outcome.energy_period_sum_kwh == pytest.approx(100_000.0)
        assert outcome.energy_difference_kwh == pytest.approx(0.0)
        assert outcome.energy_consistent is True
        # 未提供深谷电量 → 只就比较到的时段求和，必须标记为"部分时段"
        assert outcome.energy_partial is True
        assert outcome.energy_missing_periods == ["深谷电量（kWh）"]

    def test_all_five_periods_given_is_not_partial(self):
        bill = _consistent_bill(energy_offpeak_kwh=0.0)
        outcome = reconcile_bill(bill)
        assert outcome.energy_partial is False
        assert outcome.energy_missing_periods == []
        assert outcome.energy_consistent is True

    def test_deep_valley_counts_into_sum(self):
        """深谷（offpeak）与低谷是两个时段，都会计入分时合计。"""
        bill = _consistent_bill(
            energy_valley_kwh=10_000.0, energy_offpeak_kwh=10_000.0
        )
        provided, missing = energy_period_breakdown(bill)
        assert provided[BillEnergyPeriod.OFFPEAK] == pytest.approx(10_000.0)
        assert missing == []
        assert reconcile_bill(bill).energy_consistent is True

    def test_difference_beyond_tolerance_is_warning(self):
        bill = _consistent_bill(energy_total_kwh=50_000.0)
        outcome = reconcile_bill(bill)
        assert outcome.energy_difference_kwh == pytest.approx(-50_000.0)
        assert outcome.energy_consistent is False
        assert "B04" in _codes(outcome.issues)
        assert any("超出容差" in message for message in outcome.messages)

    def test_difference_within_tolerance_is_reported_but_ok(self):
        """§2.1：容差内的差异也必须显示，不得隐藏。"""
        bill = _consistent_bill(energy_total_kwh=100_100.0, bill_total_yuan=65_000.0)
        outcome = reconcile_bill(bill)
        assert outcome.energy_consistent is True
        assert outcome.energy_difference_kwh == pytest.approx(100.0)
        assert any("ΔE" in message and "100.00" in message for message in outcome.messages)

    def test_partial_periods_marked(self):
        """§3.1：账单未提供的时段视为未知，不按 0 计入。"""
        bill = _bill(
            energy_total_kwh=70_000.0,
            energy_peak_kwh=30_000.0,
            energy_flat_kwh=40_000.0,
        )
        outcome = reconcile_bill(bill)
        assert outcome.energy_partial is True
        assert outcome.energy_consistent is True
        assert "尖峰电量（kWh）" in outcome.energy_missing_periods
        assert "低谷电量（kWh）" in outcome.energy_missing_periods
        assert any("未提供时段" in message or "未提供，按未知处理" in message for message in outcome.messages)

    def test_no_period_energy_means_unknown(self):
        outcome = reconcile_bill(_bill(energy_total_kwh=1000.0))
        assert outcome.energy_period_sum_kwh is None
        assert outcome.energy_consistent is None
        assert any("未提供任何分时电量" in message for message in outcome.messages)

    def test_missing_total_means_unknown(self):
        outcome = reconcile_bill(_consistent_bill(energy_total_kwh=None))
        assert outcome.energy_consistent is None
        assert outcome.energy_difference_kwh is None
        assert any("未提供总购电量" in message for message in outcome.messages)

    def test_sum_period_energy_returns_labels(self):
        total, missing = sum_period_energy(_consistent_bill())
        assert total == pytest.approx(100_000.0)
        assert missing == ["深谷电量（kWh）"]


class TestChargeDifference:
    """§3.1：ΔC = C_bill,total − Σ C_i。"""

    def test_consistent_components(self):
        outcome = reconcile_bill(_consistent_bill())
        assert outcome.amount_component_sum_yuan == pytest.approx(65_000.0)
        assert outcome.amount_difference_yuan == pytest.approx(0.0)
        assert outcome.amount_consistent is True

    def test_sub_components_are_not_double_counted(self):
        """电度电费合计与其 5 个明细是合计与明细关系，不能同时计入总额。"""
        outcome = reconcile_bill(_consistent_bill())
        assert outcome.amount_component_sum_yuan == pytest.approx(65_000.0)
        assert outcome.amount_components_used == ["energy_charge_yuan"]
        assert outcome.energy_sub_sum_yuan == pytest.approx(65_000.0)
        assert outcome.energy_sub_consistent is True

    def test_missing_energy_charge_uses_sub_sum_with_disclosure(self):
        outcome = reconcile_bill(_consistent_bill(energy_charge_yuan=None))
        assert outcome.amount_component_sum_yuan == pytest.approx(65_000.0)
        assert set(outcome.amount_components_used) == set(energy_sub_charge_components(_consistent_bill()))
        assert any("替代" in note for note in outcome.assumptions)

    def test_difference_beyond_tolerance_is_warning(self):
        outcome = reconcile_bill(_consistent_bill(bill_total_yuan=70_000.0))
        assert outcome.amount_difference_yuan == pytest.approx(5_000.0)
        assert outcome.amount_consistent is False
        assert "B14" in _codes(outcome.issues)

    def test_sub_components_mismatch_is_warning(self):
        outcome = reconcile_bill(
            _consistent_bill(market_purchase_charge_yuan=40_000.0)
        )
        assert outcome.energy_sub_difference_yuan == pytest.approx(5_000.0)
        assert outcome.energy_sub_consistent is False
        assert "B15" in _codes(outcome.issues)

    def test_no_total_means_unknown(self):
        outcome = reconcile_bill(_consistent_bill(bill_total_yuan=None))
        assert outcome.amount_consistent is None
        assert any("未提供账单总额" in message for message in outcome.messages)

    def test_no_components_means_unknown(self):
        bill = _bill(bill_total_yuan=500.0)
        outcome = reconcile_bill(bill)
        assert outcome.amount_component_sum_yuan is None
        assert outcome.amount_consistent is None
        assert "B08" in _codes(outcome.issues)

    def test_tolerance_is_configurable(self):
        """§2.1：容差可配置，但差异永远要报出来。"""
        bill = _consistent_bill(bill_total_yuan=66_000.0)  # 差异 1000 元，占 1.5%
        assert reconcile_bill(bill).amount_consistent is False
        loose = BillTolerance(amount_relative=0.05)
        assert reconcile_bill(bill, tolerance=loose).amount_consistent is True
        assert reconcile_bill(bill, tolerance=loose).amount_difference_yuan == pytest.approx(1_000.0)

    def test_level1_components_exclude_sub_items(self):
        assert set(level1_charge_components(_consistent_bill())) == {"energy_charge_yuan"}


class TestFieldValidation:
    def test_negative_energy_is_error(self):
        issues = validate_bill_fields(_bill(energy_total_kwh=-1.0))
        assert "B01" in _codes(issues)
        assert all(issue.level == "ERROR" for issue in issues if issue.code == "B01")

    def test_negative_amount_is_error(self):
        issues = validate_bill_fields(_bill(bill_total_yuan=-3.0))
        assert "B02" in _codes(issues)

    def test_negative_adjustment_is_allowed_with_info(self):
        """§2.1：调整/返还类字段明确允许负值。"""
        issues = validate_bill_fields(_bill(adjustment_charge_yuan=-200.0))
        assert "B03" in _codes(issues)
        assert not [i for i in issues if i.level == "ERROR"]
        assert not [i for i in issues if i.code == "B02"]

    def test_negative_power_factor_adjustment_allowed(self):
        issues = validate_bill_fields(_bill(power_factor_adjustment_yuan=-150.0))
        assert "B03" in _codes(issues)
        assert not [i for i in issues if i.level == "ERROR"]

    def test_two_part_without_capacity_or_demand_warns(self):
        issues = validate_bill_fields(_bill(tariff_structure=TariffStructure.TWO_PART))
        assert "B04" in _codes(issues)

    def test_two_part_with_demand_is_ok(self):
        issues = validate_bill_fields(
            _bill(tariff_structure=TariffStructure.TWO_PART, billing_demand_kw=450.0)
        )
        assert "B04" not in _codes(issues)

    def test_demand_over_contract_capacity_warns(self):
        issues = validate_bill_fields(
            _bill(
                tariff_structure=TariffStructure.TWO_PART,
                contract_capacity_kva=400.0,
                billing_demand_kw=450.0,
            )
        )
        assert "B05" in _codes(issues)

    def test_single_part_with_basic_fee_warns(self):
        issues = validate_bill_fields(
            _bill(tariff_structure=TariffStructure.SINGLE_PART, demand_charge_yuan=1000.0)
        )
        assert "B06" in _codes(issues)

    def test_zero_total_with_energy_warns(self):
        issues = validate_bill_fields(_bill(energy_total_kwh=1000.0, bill_total_yuan=0.0))
        assert "B13" in _codes(issues)

    def test_cross_month_is_informational(self):
        issues = validate_bill_fields(
            _bill(start=date(2026, 1, 26), end=date(2026, 2, 25))
        )
        assert "B10" in _codes(issues)
        assert all(i.level == "INFO" for i in issues if i.code == "B10")

    def test_excel_source_without_row_is_informational(self):
        from cenep.domain.enums import BillSourceType

        issues = validate_bill_fields(_bill(source_type=BillSourceType.EXCEL))
        assert "B12" in _codes(issues)

    def test_issue_carries_source_row(self):
        issues = validate_bill_fields(_bill(energy_total_kwh=-1.0, source_row_number=7))
        assert issues[0].source_row_number == 7

    def test_no_error_for_clean_bill(self):
        issues = validate_bill_fields(_consistent_bill())
        assert not [i for i in issues if i.level == "ERROR"]


class TestQualityStatus:
    def test_clean_bill_is_valid(self):
        assert reconcile_bill(_consistent_bill()).quality_status is BillQualityStatus.VALID

    def test_inconsistent_bill_is_warning(self):
        outcome = reconcile_bill(_consistent_bill(bill_total_yuan=70_000.0))
        assert outcome.quality_status is BillQualityStatus.WARNING

    def test_negative_bill_is_invalid(self):
        outcome = reconcile_bill(_bill(energy_total_kwh=-5.0, bill_total_yuan=10.0))
        assert outcome.quality_status is BillQualityStatus.INVALID
        assert outcome.passed is False

    def test_apply_quality_returns_copy_without_touching_facts(self):
        """§0.2：账单事实与校验结果分开保存。"""
        bill = _consistent_bill(bill_total_yuan=70_000.0)
        before = bill.model_dump()
        updated = apply_quality(bill)
        assert updated is not bill
        for name in ("energy_total_kwh", "bill_total_yuan", "energy_charge_yuan"):
            assert getattr(updated, name) == before[name], f"校验改动了账单事实字段 {name}"
        assert updated.bill_total_yuan == pytest.approx(70_000.0)
        assert updated.quality_status is BillQualityStatus.WARNING
        assert updated.quality_messages
        assert bill.quality_status is BillQualityStatus.VALID  # 原对象未被改动


class TestAveragePrice:
    """§3.1：P_avg = C_bill / E_grid（仅账单统计口径）。"""

    def test_formula(self):
        assert average_comprehensive_price(65_000.0, 100_000.0) == pytest.approx(0.65)

    @pytest.mark.parametrize(
        "amount,energy",
        [(None, 1000.0), (1000.0, None), (1000.0, 0.0), (None, None)],
    )
    def test_not_applicable_returns_none(self, amount, energy):
        assert average_comprehensive_price(amount, energy) is None

    def test_monthly_summary_uses_formula(self):
        items = monthly_summary([_consistent_bill()])
        assert items[0].average_price_yuan_per_kwh == pytest.approx(0.65)


class TestMonthlySummary:
    def test_grouping_and_totals(self):
        bills = [
            _consistent_bill(meter="M1"),
            _consistent_bill(meter="M2", bill_total_yuan=70_000.0, energy_charge_yuan=70_000.0),
        ]
        items = monthly_summary(bills)
        assert len(items) == 1
        assert items[0].billing_month == "2026-01"
        assert items[0].bill_count == 2
        assert items[0].energy_total_kwh == pytest.approx(200_000.0)
        assert items[0].amount_total_yuan == pytest.approx(135_000.0)
        assert items[0].average_price_yuan_per_kwh == pytest.approx(0.675)
        assert any("重复录入" in message for message in items[0].messages)

    def test_cross_month_flagged(self):
        bill = _bill(
            start=date(2026, 1, 26),
            end=date(2026, 2, 25),
            energy_total_kwh=1000.0,
            bill_total_yuan=700.0,
        )
        items = monthly_summary([bill])
        assert items[0].has_cross_month is True
        assert items[0].billing_month == "2026-01"
        assert any("跨月" in message for message in items[0].messages)

    def test_missing_energy_gives_none_average(self):
        items = monthly_summary([_bill(bill_total_yuan=500.0)])
        assert items[0].energy_total_kwh is None
        assert items[0].average_price_yuan_per_kwh is None
        assert items[0].energy_missing_bills == 1
        assert any("未提供总电量" in message for message in items[0].messages)

    def test_empty_input(self):
        assert monthly_summary([]) == []


def _monthly_bills(count: int, year: int = 2026, **kwargs) -> list[ElectricityBill]:
    """构造 ``count`` 个连续自然月账单（1 月起）。"""
    import calendar

    out = []
    for month in range(1, count + 1):
        last_day = calendar.monthrange(year, month)[1]
        out.append(
            _consistent_bill(
                start=date(year, month, 1),
                end=date(year, month, last_day),
                energy_total_kwh=10_000.0,
                energy_sharp_kwh=1_000.0,
                energy_peak_kwh=3_000.0,
                energy_flat_kwh=4_000.0,
                energy_valley_kwh=2_000.0,
                energy_charge_yuan=7_000.0,
                market_purchase_charge_yuan=5_000.0,
                transmission_distribution_charge_yuan=1_500.0,
                line_loss_charge_yuan=100.0,
                system_operation_charge_yuan=50.0,
                government_fund_charge_yuan=350.0,
                bill_total_yuan=7_000.0,
                **kwargs,
            )
        )
    return out


class TestAnnualSummary:
    """§3.1：年度总用电量只在周期完整且不重叠时才能直接相加。"""

    def test_full_year_can_be_summed(self):
        outcome = annual_bill_summary(_monthly_bills(12))
        assert outcome.year == 2026
        assert outcome.coverage_ratio == pytest.approx(1.0)
        assert outcome.missing_months == []
        assert outcome.can_sum_directly is True
        assert outcome.total_energy_kwh == pytest.approx(120_000.0)
        assert outcome.total_amount_yuan == pytest.approx(84_000.0)
        assert outcome.average_price_yuan_per_kwh == pytest.approx(0.7)

    def test_missing_months_reported(self):
        outcome = annual_bill_summary(_monthly_bills(11))
        assert outcome.coverage_ratio == pytest.approx(11 / 12)
        assert outcome.can_sum_directly is False
        assert outcome.missing_months == ["2026-12"]
        assert any("缺失月份" in message for message in outcome.messages)
        assert any("不得声称完整年度" in message for message in outcome.messages)

    def test_overlapping_periods_forbid_summing(self):
        bills = _monthly_bills(2)
        bills.append(
            _consistent_bill(
                start=date(2026, 1, 15),
                end=date(2026, 2, 15),
                meter="M9",
                energy_total_kwh=5_000.0,
                bill_total_yuan=3_500.0,
            )
        )
        outcome = annual_bill_summary(bills)
        assert outcome.can_sum_directly is False
        assert any("重叠" in message for message in outcome.messages)

    def test_duplicate_month_reported(self):
        bills = [*_monthly_bills(1), _consistent_bill(meter="M2")]
        outcome = annual_bill_summary(bills)
        assert outcome.duplicate_months == ["2026-01"]
        assert outcome.can_sum_directly is False
        assert any("同月多条账单" in message for message in outcome.messages)

    def test_non_natural_month_detected(self):
        """计费周期不等于自然月（例如两月一结）必须被指出。"""
        bills = _monthly_bills(2)
        bills.append(
            _consistent_bill(
                start=date(2026, 3, 1),
                end=date(2026, 4, 30),
                meter="M5",
                energy_total_kwh=20_000.0,
                energy_sharp_kwh=2_000.0,
                energy_peak_kwh=6_000.0,
                energy_flat_kwh=8_000.0,
                energy_valley_kwh=4_000.0,
                energy_charge_yuan=14_000.0,
                bill_total_yuan=14_000.0,
            )
        )
        outcome = annual_bill_summary(bills)
        assert outcome.non_natural_month_bills, "两月一结的账单应被识别为计费周期非自然月"
        assert outcome.can_sum_directly is False

    def test_year_defaults_to_most_common(self):
        bills = [*_monthly_bills(3, year=2025), *_monthly_bills(2, year=2026)]
        assert annual_bill_summary(bills).year == 2025

    def test_explicit_year_filters(self):
        bills = [*_monthly_bills(3, year=2025), *_monthly_bills(2, year=2026)]
        outcome = annual_bill_summary(bills, year=2026)
        assert outcome.coverage_ratio == pytest.approx(2 / 12)
        assert outcome.total_energy_kwh == pytest.approx(20_000.0)

    def test_sums_only_provided_values(self):
        bills = _monthly_bills(2)
        bills.append(
            _bill(
                start=date(2026, 3, 1),
                end=date(2026, 3, 31),
                meter="M3",
                energy_total_kwh=None,
                bill_total_yuan=1_000.0,
            )
        )
        outcome = annual_bill_summary(bills)
        assert outcome.total_energy_kwh == pytest.approx(20_000.0)
        assert outcome.total_amount_yuan == pytest.approx(15_000.0)
        assert any("未提供总电量" in message for message in outcome.messages)

    def test_annual_equals_sum_of_monthly(self):
        """§3.1：E_load,annual = Σ_m E_bill,m（逐月合计必须自洽）。"""
        outcome = annual_bill_summary(_monthly_bills(12))
        monthly_sum = sum(item.energy_total_kwh for item in outcome.monthly)
        assert outcome.total_energy_kwh == pytest.approx(monthly_sum)

    def test_assumptions_disclose_caliber(self):
        outcome = annual_bill_summary(_monthly_bills(12))
        text = "".join(outcome.assumptions)
        assert "Σ" in text or "相加" in text
        assert "边际节省电价" in text

    def test_empty_input(self):
        outcome = annual_bill_summary([], year=2026)
        assert outcome.coverage_ratio == 0.0
        assert outcome.can_sum_directly is False
        assert outcome.total_energy_kwh is None
