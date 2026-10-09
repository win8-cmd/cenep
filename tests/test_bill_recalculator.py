"""V2.3 阶段 5：基准账单复算与差异分解测试（规格书 §3.4、§3.1、§7.4、§7.5、§9.3）。

全部用例都**可手算复核**（§10 阶段 5 验收：给定已知电量、电价、费用参数，软件账单可手工复核）。
"""

from __future__ import annotations

from datetime import date

import pytest

from cenep.calculation.bill_recalculator import (
    DEMAND_METHOD_BILL,
    calibrate_bills,
    energy_by_period_from_hourly,
    recompute_bill,
    recompute_bills,
)
from cenep.calculation.errors import ValidationError
from cenep.domain.bill_models import BillTolerance, ElectricityBill
from cenep.domain.enums import (
    BillSourceType,
    DemandBillingMode,
    TariffComponentType,
    TariffComponentUnit,
    TariffPeriod,
    TariffPlanStatus,
    TariffStructure,
)
from cenep.domain.tariff_models import TariffPlan, TariffPriceComponent, TariffTimePeriodRule

# --------------------------------------------------------------------------- #
# 可手算的固定电价计划与账单
# --------------------------------------------------------------------------- #
#: 分时直接单价（元/kWh）：尖峰 1.00、高峰 0.80、平段 0.60、低谷 0.38
PRICES = {
    TariffPeriod.SHARP_PEAK: 1.00,
    TariffPeriod.PEAK: 0.80,
    TariffPeriod.FLAT: 0.60,
    TariffPeriod.VALLEY: 0.38,
}


def _plan(
    *,
    demand_billing_mode: DemandBillingMode = DemandBillingMode.DEMAND,
    demand_price: float | None = 39.0,
    capacity_price: float | None = 26.3,
    status: TariffPlanStatus = TariffPlanStatus.VERIFIED,
) -> TariffPlan:
    """24 小时闭合、四时段价格可手算的测试电价计划。"""
    rules = [
        TariffTimePeriodRule(
            period=TariffPeriod.VALLEY, start_time="00:00", end_time="06:00",
            direct_price_yuan_per_kwh=PRICES[TariffPeriod.VALLEY],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.FLAT, start_time="06:00", end_time="12:00",
            direct_price_yuan_per_kwh=PRICES[TariffPeriod.FLAT],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.VALLEY, start_time="12:00", end_time="14:00",
            direct_price_yuan_per_kwh=PRICES[TariffPeriod.VALLEY],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.FLAT, start_time="14:00", end_time="16:00",
            direct_price_yuan_per_kwh=PRICES[TariffPeriod.FLAT],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.PEAK, start_time="16:00", end_time="18:00",
            direct_price_yuan_per_kwh=PRICES[TariffPeriod.PEAK],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK, start_time="18:00", end_time="20:00",
            direct_price_yuan_per_kwh=PRICES[TariffPeriod.SHARP_PEAK],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.PEAK, start_time="20:00", end_time="24:00",
            direct_price_yuan_per_kwh=PRICES[TariffPeriod.PEAK],
        ),
    ]
    return TariffPlan(
        tariff_plan_id="TEST_RECOMPUTE",
        name="测试复算电价计划",
        province="湖北",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
        source_name="单元测试构造",
        source_document_number="测试〔2026〕0 号",
        source_fetched_at=date(2026, 1, 1),
        status=status,
        applicable_voltage_levels=["110千伏"],
        applicable_tariff_structures=["two_part"],
        time_period_rules=rules,
        price_components=[
            TariffPriceComponent(
                component_type=TariffComponentType.MARKET_ENERGY,
                name="测试基础分项",
                unit=TariffComponentUnit.YUAN_PER_KWH,
                value=0.40,
                adjustable_by_tou=True,
            ),
            TariffPriceComponent(
                component_type=TariffComponentType.TRANSMISSION_DISTRIBUTION,
                name="测试固定分项",
                unit=TariffComponentUnit.YUAN_PER_KWH,
                value=0.20,
                adjustable_by_tou=False,
            ),
        ],
        capacity_charge_yuan_per_kva_month=capacity_price,
        demand_charge_yuan_per_kw_month=demand_price,
        demand_billing_mode=demand_billing_mode,
        base_price_definition="测试口径",
        notes="仅用于测试",
    )


def _bill(**overrides: object) -> ElectricityBill:
    """可手算账单：分时电量 100/200/300/400（合计 1000 kWh），账单总额 4450 元。"""
    payload: dict[str, object] = {
        "bill_id": "BILL-TEST",
        "project_id": "测试项目",
        "billing_period_start": date(2026, 3, 1),
        "billing_period_end": date(2026, 3, 31),
        "voltage_level": "110千伏",
        "tariff_structure": TariffStructure.TWO_PART,
        "energy_total_kwh": 1000.0,
        "energy_sharp_kwh": 100.0,
        "energy_peak_kwh": 200.0,
        "energy_flat_kwh": 300.0,
        "energy_valley_kwh": 400.0,
        "energy_charge_yuan": 600.0,
        "demand_charge_yuan": 3900.0,
        "billing_demand_kw": 100.0,
        "contract_capacity_kva": 100.0,
        "power_factor_adjustment_yuan": -50.0,
        "bill_total_yuan": 4450.0,
        "source_type": BillSourceType.EXCEL,
        "source_file_name": "测试账单.xlsx",
        "source_row_number": 2,
    }
    payload.update(overrides)
    return ElectricityBill(**payload)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 电度电费：C_energy = Σ E_i × P_i（§3.4）
# --------------------------------------------------------------------------- #
def test_energy_charge_hand_computed() -> None:
    """电度电费可手算：100×1.00 + 200×0.80 + 300×0.60 + 400×0.38 = 592 元。"""
    outcome = recompute_bill(_bill(), _plan(), require_verified=False)
    assert outcome.recomputed_energy_charge_yuan == pytest.approx(592.0)
    details = {detail.period: detail for detail in outcome.period_details}
    assert details[TariffPeriod.SHARP_PEAK.value].amount_yuan == pytest.approx(100.0)
    assert details[TariffPeriod.PEAK.value].amount_yuan == pytest.approx(160.0)
    assert details[TariffPeriod.FLAT.value].amount_yuan == pytest.approx(180.0)
    assert details[TariffPeriod.VALLEY.value].amount_yuan == pytest.approx(152.0)
    assert details[TariffPeriod.SHARP_PEAK.value].unit_price_yuan_per_kwh == pytest.approx(1.00)


def test_demand_charge_hand_computed() -> None:
    """两部制需量计费：100 kW × 39 元/千瓦·月 = 3900 元；复算合计 = 592 + 3900 = 4492 元。"""
    outcome = recompute_bill(_bill(), _plan(), require_verified=False)
    assert outcome.recomputed_demand_charge_yuan == pytest.approx(3900.0)
    assert outcome.recomputed_capacity_charge_yuan is None
    assert outcome.recomputed_subtotal_yuan == pytest.approx(4492.0)
    assert outcome.demand_billing_mode is DemandBillingMode.DEMAND
    assert outcome.demand_method == DEMAND_METHOD_BILL


def test_capacity_and_demand_are_mutually_exclusive() -> None:
    """容量计费与需量计费**默认互斥**：切换计费方式后只算其中一项（§7.5）。"""
    capacity = recompute_bill(
        _bill(), _plan(demand_billing_mode=DemandBillingMode.CAPACITY), require_verified=False
    )
    assert capacity.recomputed_demand_charge_yuan is None
    assert capacity.recomputed_capacity_charge_yuan == pytest.approx(100.0 * 26.3)
    assert capacity.recomputed_subtotal_yuan == pytest.approx(592.0 + 2630.0)

    none_mode = recompute_bill(
        _bill(), _plan(demand_billing_mode=DemandBillingMode.NONE), require_verified=False
    )
    assert none_mode.recomputed_subtotal_yuan == pytest.approx(592.0)


def test_demand_mode_can_be_overridden_per_call() -> None:
    """单次调用可覆盖计费方式，但不改动计划本身（§7.5 只在明确配置时允许切换）。"""
    plan = _plan(demand_billing_mode=DemandBillingMode.DEMAND)
    outcome = recompute_bill(
        _bill(), plan, demand_mode=DemandBillingMode.CAPACITY, require_verified=False
    )
    assert outcome.recomputed_capacity_charge_yuan == pytest.approx(2630.0)
    assert plan.demand_billing_mode is DemandBillingMode.DEMAND


# --------------------------------------------------------------------------- #
# 差异与分解（§3.4、§7.4）
# --------------------------------------------------------------------------- #
def test_difference_and_itemization_is_exact() -> None:
    """毛差异 −42 元；分解为 电度电费差 +8、基本电费差 0、未建模 −50、残差 0，严格闭合。"""
    outcome = recompute_bill(_bill(), _plan(), require_verified=False)
    assert outcome.difference_yuan == pytest.approx(4450.0 - 4492.0)
    by_category: dict[str, float] = {}
    for item in outcome.difference_items:
        by_category[item.category] = by_category.get(item.category, 0.0) + item.amount_yuan
    assert by_category["energy"] == pytest.approx(8.0)
    assert by_category["basic"] == pytest.approx(0.0)
    # 未建模费用 + 调整/补退费 两项都属于 unmodeled 类别
    assert by_category["unmodeled"] == pytest.approx(-50.0)
    assert by_category["residual"] == pytest.approx(0.0)
    assert sum(item.amount_yuan for item in outcome.difference_items) == pytest.approx(
        outcome.difference_yuan
    )


def test_unmodeled_fee_is_baseline_and_tolerance_uses_net_difference() -> None:
    """未建模费用按账单实际值作固定基准；容差按**净差异**判定，毛差异并行披露（§3.4、§7.4）。"""
    outcome = recompute_bill(_bill(), _plan(), require_verified=False)
    assert outcome.recomputed_unmodeled_baseline_yuan == pytest.approx(-50.0)
    assert outcome.recomputed_total_with_baseline_yuan == pytest.approx(4492.0 - 50.0)
    assert outcome.difference_after_baseline_yuan == pytest.approx(4450.0 - 4442.0)
    tolerance = BillTolerance().amount_tolerance(4450.0)
    assert outcome.tolerance_yuan == pytest.approx(tolerance)
    assert tolerance == pytest.approx(22.25)
    assert outcome.passed_tolerance is True  # 净差异 8 元 ≤ 22.25
    assert outcome.gross_passed_tolerance is False  # 毛差异 42 元 > 22.25
    assert outcome.bill_unmodeled_fields == ["power_factor_adjustment_yuan"]
    assert any("净差异" in message for message in outcome.messages)


def test_difference_over_tolerance_marks_failed_calibration() -> None:
    """净差异超出容差 → 明确提示"未通过账单校准"，方案节省额不得标高置信度（§7.4 第 6 条）。"""
    bill = _bill(bill_total_yuan=5000.0)
    outcome = recompute_bill(bill, _plan(), require_verified=False)
    assert outcome.passed_tolerance is False
    assert outcome.quality_status == "warning"
    assert any("未通过账单校准" in message for message in outcome.messages)


def test_within_tolerance_message() -> None:
    """净差异在容差内时必须显示"账单校准通过"。"""
    outcome = recompute_bill(_bill(), _plan(), require_verified=False)
    assert any("净差异在容差内" in message for message in outcome.messages)


def test_bill_facts_are_never_modified() -> None:
    """复算**绝不写回**账单事实（§2.5、§0.2）：原对象所有金额/电量字段保持不变。"""
    bill = _bill()
    snapshot = bill.model_dump()
    recompute_bill(bill, _plan(), require_verified=False)
    assert bill.model_dump() == snapshot


def test_adjustment_charge_sign_allowed() -> None:
    """调整/补退费可为负，并按账单实际值进入基准（§2.1）。"""
    bill = _bill(adjustment_charge_yuan=-100.0, bill_total_yuan=4350.0)
    outcome = recompute_bill(bill, _plan(), require_verified=False)
    assert outcome.bill_adjustment_yuan == pytest.approx(-100.0)
    assert outcome.recomputed_unmodeled_baseline_yuan == pytest.approx(-150.0)
    assert outcome.difference_after_baseline_yuan == pytest.approx(4350.0 - (4492.0 - 150.0))


# --------------------------------------------------------------------------- #
# 电量口径与缺数据（§3.1、§4.2）
# --------------------------------------------------------------------------- #
def test_energy_difference_and_missing_periods_are_reported() -> None:
    """账单缺某时段电量时视为**未知**（不按 0 计价），并报告 ΔE（§3.1）。"""
    bill = _bill(energy_valley_kwh=None, energy_total_kwh=1000.0)
    outcome = recompute_bill(bill, _plan(), require_verified=False)
    assert "低谷电量（kWh）" in outcome.energy_missing_periods
    assert outcome.recomputed_energy_charge_yuan == pytest.approx(100.0 + 160.0 + 180.0)
    assert outcome.energy_period_sum_kwh == pytest.approx(600.0)
    assert outcome.energy_difference_kwh == pytest.approx(400.0)
    assert any("未参与计价" in message for message in outcome.messages)


def test_no_priced_period_raises_chinese() -> None:
    """没有任何"既有电量又有价格"的时段时抛**中文**异常，不得静默算出 0 元电费。"""
    plan = _plan().model_copy(
        update={"time_period_rules": [], "price_components": []}
    )
    with pytest.raises(ValidationError) as excinfo:
        recompute_bill(_bill(), plan, require_verified=False)
    assert "既有电量又有价格" in str(excinfo.value)


def test_missing_price_blocks_formal_recompute() -> None:
    """formal 复算要求计划可用：未核验计划必须被阻断（§4.2、§7.4）。"""
    with pytest.raises(ValidationError):
        recompute_bill(_bill(), _plan(status=TariffPlanStatus.DRAFT), require_verified=True)


def test_expired_plan_blocks_bill_from_other_period() -> None:
    """电价规则按计费日期生效：账单账期不在计划有效期内 → 阻断（§2.3 第 3 条）。"""
    bill = _bill(
        billing_period_start=date(2027, 1, 1),
        billing_period_end=date(2027, 1, 31),
        billing_month="2027-01",
    )
    with pytest.raises(ValidationError) as excinfo:
        recompute_bill(bill, _plan(), require_verified=True)
    assert "已过期" in str(excinfo.value) or "不能用于正式账单复算" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 逐小时电量 → 分时电量（§3.4、§4.2）
# --------------------------------------------------------------------------- #
def test_energy_by_period_from_hourly_hand_computed() -> None:
    """24 点逐小时电量按官方时段归并：每小时 10 kWh，3 月的尖峰 2h / 高峰 6h / 平段 8h / 低谷 8h。"""
    hourly = [10.0] * 24
    by_period = energy_by_period_from_hourly(_plan(), 3, hourly)
    assert by_period[TariffPeriod.SHARP_PEAK] == pytest.approx(20.0)
    assert by_period[TariffPeriod.PEAK] == pytest.approx(60.0)
    assert by_period[TariffPeriod.FLAT] == pytest.approx(80.0)
    assert by_period[TariffPeriod.VALLEY] == pytest.approx(80.0)
    assert sum(by_period.values()) == pytest.approx(240.0)


def test_energy_by_period_requires_24_points() -> None:
    """逐小时电量点数不足必须是**中文**报错。"""
    with pytest.raises(ValidationError) as excinfo:
        energy_by_period_from_hourly(_plan(), 3, [1.0] * 12)
    assert "必须是 24 个点" in str(excinfo.value)


def test_energy_by_period_rejects_uncovered_hours() -> None:
    """存在未覆盖小时必须报错并指出小时序号，不得静默丢弃电量（§4.2、§7.7）。"""
    plan = _plan().model_copy(
        update={
            "time_period_rules": [
                TariffTimePeriodRule(
                    period=TariffPeriod.FLAT,
                    start_time="08:00",
                    end_time="09:00",
                    direct_price_yuan_per_kwh=0.6,
                )
            ]
        }
    )
    with pytest.raises(ValidationError) as excinfo:
        energy_by_period_from_hourly(plan, 3, [1.0] * 24)
    message = str(excinfo.value)
    assert "未被电价计划覆盖" in message and "小时" in message


def test_period_energies_override_bill_fields() -> None:
    """调用方提供的分时电量优先于账单字段，并在明细中披露来源（§7.4 第 1 条）。"""
    override = {
        TariffPeriod.SHARP_PEAK: 0.0,
        TariffPeriod.PEAK: 0.0,
        TariffPeriod.FLAT: 0.0,
        TariffPeriod.VALLEY: 1000.0,
    }
    outcome = recompute_bill(_bill(), _plan(), period_energies=override, require_verified=False)
    assert outcome.recomputed_energy_charge_yuan == pytest.approx(1000.0 * 0.38)
    valley = next(d for d in outcome.period_details if d.period == TariffPeriod.VALLEY.value)
    assert "调用方提供" in valley.energy_source
    assert outcome.energy_missing_periods == []


# --------------------------------------------------------------------------- #
# 多账单与年度校准（§3.1、§7.4）
# --------------------------------------------------------------------------- #
def _month_bill(month: int, *, energy: float = 1000.0) -> ElectricityBill:
    import calendar

    last = calendar.monthrange(2026, month)[1]
    return _bill(
        bill_id=f"BILL-2026{month:02d}",
        billing_period_start=date(2026, month, 1),
        billing_period_end=date(2026, month, last),
        billing_month=f"2026-{month:02d}",
        energy_total_kwh=energy,
        energy_sharp_kwh=energy * 0.1,
        energy_peak_kwh=energy * 0.2,
        energy_flat_kwh=energy * 0.3,
        energy_valley_kwh=energy * 0.4,
        energy_charge_yuan=energy * 0.592,
        demand_charge_yuan=3900.0,
        bill_total_yuan=energy * 0.592 + 3900.0 - 50.0,
    )


def test_recompute_bills_returns_one_result_per_bill() -> None:
    """多账单逐月复算：每个账单一个结果，顺序与入参一致。"""
    bills = [_month_bill(month) for month in (3, 4, 5)]
    outcomes = recompute_bills(bills, _plan(), require_verified=False)
    assert [item.bill_id for item in outcomes] == ["BILL-202603", "BILL-202604", "BILL-202605"]
    for item in outcomes:
        assert item.passed_tolerance is True
        assert item.difference_after_baseline_yuan == pytest.approx(0.0, abs=1e-6)


def test_calibrate_bills_annual_summary_and_coverage() -> None:
    """年度校准：覆盖率、毛/净差异、逐月通过数与差异因素合计都要给出。"""
    bills = [_month_bill(month) for month in range(1, 13)]
    summary = calibrate_bills(bills, _plan(), year=2026, require_verified=False)
    assert summary.bill_count == 12
    assert summary.coverage_ratio == pytest.approx(1.0)
    assert summary.can_sum_directly is True
    assert summary.missing_months == []
    assert summary.calibration_passed is True
    assert summary.total_net_difference_yuan == pytest.approx(0.0, abs=1e-6)
    assert summary.total_difference_yuan == pytest.approx(-600.0, abs=1e-6)  # 12 × (−50) 未建模费用
    assert summary.passed_bills == 12 and summary.failed_bills == 0
    assert any("差异因素年度合计" in message for message in summary.messages)
    assert summary.factor_totals["未建模费用（功率因数调整 / 其他 / 增值税）"] == pytest.approx(-600.0)


def test_calibrate_bills_reports_missing_months() -> None:
    """缺月必须降低可信度并提示，不得把不完整年度写成"完整年度实测"（§3.1）。"""
    bills = [_month_bill(month) for month in (1, 2, 3)]
    summary = calibrate_bills(bills, _plan(), year=2026, require_verified=False)
    assert summary.coverage_ratio == pytest.approx(0.25)
    assert summary.can_sum_directly is False
    assert summary.missing_months == [f"2026-{month:02d}" for month in range(4, 13)]
    assert any("缺失月份" in message for message in summary.messages)


def test_calibrate_bills_reports_overlap_as_not_sumable() -> None:
    """账期重叠时年度电量不可直接相加（§3.1）。"""
    first = _month_bill(3)
    second = _month_bill(3).model_copy(update={"bill_id": "BILL-DUP"})
    summary = calibrate_bills([first, second], _plan(), year=2026, require_verified=False)
    assert summary.can_sum_directly is False
    assert any("重叠" in message or "重复" in message or "同月" in message for message in summary.messages)


def test_calibrate_bills_with_period_energies_override() -> None:
    """年度校准支持按账单提供分时电量（账单无分时电量字段时的入口，§7.4 第 1 条）。"""
    bills = [_month_bill(3).model_copy(update={"energy_sharp_kwh": None, "energy_peak_kwh": None,
                                               "energy_flat_kwh": None, "energy_valley_kwh": None})]
    override = {
        bills[0].bill_id: {
            TariffPeriod.SHARP_PEAK: 100.0,
            TariffPeriod.PEAK: 200.0,
            TariffPeriod.FLAT: 300.0,
            TariffPeriod.VALLEY: 400.0,
        }
    }
    summary = calibrate_bills(
        bills, _plan(), year=2026, require_verified=False, period_energies_by_bill=override
    )
    assert summary.monthly[0].recomputed_energy_charge_yuan == pytest.approx(592.0)
