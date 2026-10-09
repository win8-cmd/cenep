"""基准账单复算与差异分析（V2.3 §3.4、§7.4、§7.5、§7.6）。

本模块是**账单复算相关核心公式的唯一实现处**（§0.2：核心公式在 ``calculation/``）。
公式与口径逐条对应规格书：

====================================  =============================================================
公式                                   出处 / 口径
====================================  =============================================================
``C_energy = Σ_i E_grid,i × P_i``       §3.4；``P_i`` 由日期、时刻、电价版本、适用时段与用户价格参数共同确定
``C_capacity = K_contract,kVA × P_capacity``  §3.4 容量计费（元/千伏安·月）
``C_demand = D_billable,kW × P_demand``       §3.4 需量计费（元/千瓦·月）；与容量计费**默认互斥**
``ΔC = C_bill,total − Σ_i C_i``          §3.1、§3.4：差异**必须被解释**，不得为匹配总额而改电价
``S_bill = C_baseline − C_scenario``     §3.4（阶段 5 只产出 ``C_baseline`` 一侧，阶段 6 才做方案对比）
====================================  =============================================================

三条不可违反的口径（§2.5、§7.4、§7.6）
------------------------------------
1. **账单事实与复算结果分开**：本模块返回 :class:`~cenep.domain.bill_recomputation.BillRecomputation`，
   **绝不写回** :class:`~cenep.domain.bill_models.ElectricityBill` 的任何金额/电量字段。
2. **差异必须被解释**：差异被拆成"电度电费差 + 基本电费差 + 未建模费用 + 调整/返还 + 口径残差"，
   各分项之和**严格等于**总差异（残差项永远列出，不允许出现无法解释的差额）。
3. **未建模费用不得凭空估算**：功率因数调整电费、其他费用、增值税一律取**账单实际值**作为固定基准
   并在结果中披露处理方式；本阶段不重建功率因数调整规则（§3.4 明确允许）。

需量口径（§3.4、§7.5）
--------------------
``D_billable`` **不等于**负荷曲线最大功率。V2.3 只支持两种来源：
① 用户输入的实际账单计费需量（默认，本阶段采用）；② 按配置计量窗口从高频曲线计算的模拟需量（阶段 6）。
结果中的 ``demand_method`` 永远标明用了哪一种。
"""

from __future__ import annotations

import logging
from calendar import monthrange
from collections import defaultdict
from datetime import date
from typing import Iterable, Mapping, Sequence

from ..domain.bill_models import BILL_LEVEL1_CHARGE_FIELDS, BillTolerance, ElectricityBill, label_of
from ..domain.bill_recomputation import (
    RECOMPUTATION_BASIS_LABELS,
    BillCalibrationSummary,
    BillDifferenceItem,
    BillRecomputation,
    PeriodPriceDetail,
)
from ..domain.enums import DemandBillingMode, TariffPeriod
from ..domain.tariff_models import TariffPlan
from .bill_calculator import NATURAL_MONTH_DAY_TOLERANCE, effective_energy_charge
from .errors import ValidationError
from .tariff_plan_engine import effective_period_prices, require_formal_use

logger = logging.getLogger(__name__)

__all__ = [
    "BILL_CHARGE_FIELDS",
    "DEMAND_METHOD_BILL",
    "DEMAND_METHOD_CURVE",
    "PERIOD_ENERGY_FIELDS",
    "calibrate_bills",
    "energy_by_period_from_hourly",
    "recompute_bill",
    "recompute_bills",
]

#: 需量取值方法（§3.4、§7.5 要求报告必须标注）
DEMAND_METHOD_BILL = "账单计费需量（账单事实，§7.5 默认）"
DEMAND_METHOD_CURVE = "按配置计量窗口从高频负荷曲线计算的模拟需量（阶段 6）"

#: 分时时段 → 账单电量字段（账单未提供的时段视为**未知**，绝不按 0 计价，§3.1）
PERIOD_ENERGY_FIELDS: dict[TariffPeriod, str] = {
    TariffPeriod.SHARP_PEAK: "energy_sharp_kwh",
    TariffPeriod.PEAK: "energy_peak_kwh",
    TariffPeriod.FLAT: "energy_flat_kwh",
    TariffPeriod.VALLEY: "energy_valley_kwh",
    TariffPeriod.DEEP_VALLEY: "energy_offpeak_kwh",
}


def _provided(value: object) -> float | None:
    """``None``（账单未提供）与 ``0.0``（确实为 0）严格区分（§2.1）。"""
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def _fmt(value: float | None, unit: str = "元") -> str:
    return "未提供" if value is None else f"{float(value):,.2f} {unit}"


def energy_by_period_from_hourly(
    plan: TariffPlan,
    month: int,
    hourly_energy_kwh: Sequence[float],
    *,
    day_type=None,
) -> dict[TariffPeriod, float]:
    """把 24 点逐小时电量按电价计划的时段规则归并到尖峰/高峰/平段/低谷（§3.4、§4.2）。

    逐小时电量与 ``[h, h+1)`` 小时一一对应，采用计划的月份规则（7、8 月与其他月份不同）。
    未被任何规则覆盖的小时**不会**被静默丢弃：全部漏掉时抛中文异常，部分漏掉时在结果中体现
    （该小时电量不计入任何时段，由调用方的覆盖校验负责阻断）。

    :raises ValidationError: 小时数不是 24 / 存在未覆盖小时 / 未提供任何电量
    """
    values = [float(item) for item in hourly_energy_kwh]
    if len(values) != 24:
        raise ValidationError(
            f"逐小时电量必须是 24 个点，实际 {len(values)} 个，无法按分时时段归并",
            field="hourly_energy_kwh",
        )
    if not values:
        raise ValidationError("逐小时电量为空，无法按分时时段归并", field="hourly_energy_kwh")
    from .tariff_plan_engine import resolve_slot_period

    out: dict[TariffPeriod, float] = defaultdict(float)
    uncovered: list[int] = []
    for hour, energy in enumerate(values):
        period = resolve_slot_period(plan, month, hour, slot_minutes=60, day_type=day_type)
        if period is None:
            uncovered.append(hour)
            continue
        out[period] += energy
    if uncovered:
        raise ValidationError(
            f"{month} 月有 {len(uncovered)} 个小时未被电价计划覆盖（小时："
            + "、".join(str(hour) for hour in uncovered[:12])
            + ("…" if len(uncovered) > 12 else "")
            + "），该部分电量无法定价；请先补齐时段规则（§4.2、§7.7）",
            field="time_period_rules",
        )
    logger.debug("按小时归并时段：%s 月 %d 点 → %s", month, len(values), dict(out))
    return dict(out)


def _plan_period_energies(
    bill: ElectricityBill,
    period_energies: Mapping[TariffPeriod, float] | None,
) -> tuple[dict[TariffPeriod, float], list[str]]:
    """确定"用于复算的分时电量"及其来源。

    优先使用调用方显式提供的分时电量（例如由高频/逐小时数据归并而来）；否则使用账单已提供的
    分时电量字段。账单未提供的时段**视为未知**并记入 ``missing``，绝不按 0 参与计价（§3.1）。
    """
    if period_energies is not None:
        return {period: float(value) for period, value in period_energies.items()}, []
    energies: dict[TariffPeriod, float] = {}
    missing: list[str] = []
    for period, field_name in PERIOD_ENERGY_FIELDS.items():
        value = _provided(getattr(bill, field_name, None))
        if value is None:
            missing.append(label_of(field_name))
        else:
            energies[period] = value
    return energies, missing


def recompute_bill(
    bill: ElectricityBill,
    plan: TariffPlan,
    *,
    period_energies: Mapping[TariffPeriod, float] | None = None,
    demand_mode: DemandBillingMode | None = None,
    tolerance: BillTolerance | None = None,
    require_verified: bool = True,
    as_of: date | None = None,
) -> BillRecomputation:
    """对单张账单做基准复算与差异分析（V2.3 §3.4、§7.4）。

    :param bill: 账单**事实**（不会被修改）。
    :param plan: 电价计划（版本化；时段与价格来自计划，不来自账单，§1 设计边界）。
    :param period_energies: 可选的分时电量覆盖（如由逐小时数据归并）；``None`` 时用账单字段。
    :param demand_mode: 基本电费计费方式；``None`` 时取计划默认值（容量与需量**默认互斥**，§7.5）。
    :param tolerance: 差异容差；``None`` 时用 §2.1 默认 ``max(1 元, 0.5% × 账单总额)``。
    :param require_verified: 是否要求计划可用于**正式**复算（未核验/缺价/过期/时段不闭合时抛中文异常）。
    :param as_of: 计费基准日；``None`` 时取账期起始日（用于按日期选取政策版本的场景）。
    :raises ValidationError: 电价计划不可用于正式复算、时段无价格、缺少可用于复算的电量。
    """
    tol = tolerance or BillTolerance()
    basis_day = as_of or bill.billing_period_start
    if require_verified:
        require_formal_use(plan, as_of=basis_day)

    prices = effective_period_prices(plan)
    energies, missing_periods = _plan_period_energies(bill, period_energies)

    # ---------------- 电度电费 C_energy = Σ E_p × P_p（§3.4）---------------- #
    details: list[PeriodPriceDetail] = []
    recomputed_energy = 0.0
    priced_any = False
    missing_prices: list[str] = []
    for period in TariffPeriod:
        energy = energies.get(period)
        price = prices.get(period)
        amount: float | None = None
        if energy is not None and price is not None:
            amount = float(energy) * float(price)
            recomputed_energy += amount
            priced_any = True
        elif energy is not None and price is None:
            missing_prices.append(period.label)
        if energy is None and price is None:
            continue
        details.append(
            PeriodPriceDetail(
                period=period.value,
                period_label=period.label,
                energy_kwh=energy,
                unit_price_yuan_per_kwh=price,
                amount_yuan=amount,
                price_source=f"{plan.name}（{'直接单价' if price is not None else '缺失'}）",
                energy_source=(
                    "调用方提供的分时电量（如逐小时归并）"
                    if period_energies is not None
                    else f"账单字段 {PERIOD_ENERGY_FIELDS.get(period, '')}"
                ),
                note="" if price is not None else "电价计划未提供该时段价格，未参与计价",
            )
        )

    if not priced_any:
        raise ValidationError(
            f"账单 {bill.bill_id} 复算失败：没有任何『既有电量又有价格』的时段，"
            "无法计算电度电费。请补录账单分时电量或电价计划价格（§3.4、§4.2）",
            field="energy_charge_yuan",
        )
    if missing_prices:
        logger.warning("以下时段有电量但计划无价格，未计价：%s", "、".join(missing_prices))

    # ---------------- 基本电费（容量 / 需量，互斥，§3.4、§7.5）---------------- #
    mode = demand_mode or plan.demand_billing_mode
    recomputed_capacity: float | None = None
    recomputed_demand: float | None = None
    if mode is DemandBillingMode.CAPACITY:
        if bill.contract_capacity_kva is None or plan.capacity_charge_yuan_per_kva_month is None:
            recomputed_capacity = None
        else:
            recomputed_capacity = float(bill.contract_capacity_kva) * float(
                plan.capacity_charge_yuan_per_kva_month
            )
    elif mode is DemandBillingMode.DEMAND:
        if bill.billing_demand_kw is None or plan.demand_charge_yuan_per_kw_month is None:
            recomputed_demand = None
        else:
            recomputed_demand = float(bill.billing_demand_kw) * float(plan.demand_charge_yuan_per_kw_month)

    recomputed_subtotal = (
        recomputed_energy + (recomputed_capacity or 0.0) + (recomputed_demand or 0.0)
    )

    # ---------------- 账单实录值（§2.1、§3.4）---------------- #
    bill_energy, energy_source = effective_energy_charge(bill)
    bill_capacity = _provided(bill.basic_capacity_charge_yuan)
    bill_demand = _provided(bill.demand_charge_yuan)
    unmodeled: dict[str, float] = {}
    for name in ("power_factor_adjustment_yuan", "other_charge_yuan", "vat_yuan"):
        value = _provided(getattr(bill, name, None))
        if value is not None:
            unmodeled[name] = value
    bill_unmodeled = float(sum(unmodeled.values())) if unmodeled else None
    bill_adjustment = _provided(bill.adjustment_charge_yuan)
    bill_total = _provided(bill.bill_total_yuan)

    difference: float | None = None
    difference_rate: float | None = None
    net_difference: float | None = None
    tolerance_yuan = tol.amount_tolerance(bill_total)
    passed: bool | None = None
    gross_passed: bool | None = None
    items: list[BillDifferenceItem] = []

    # 未建模费用按**账单实际值**作为固定基准保留（§3.4：不得凭空估算）；
    # 与复算合计相加得到与账单总额同口径可比的"复算总额"。
    baseline = float(bill_unmodeled or 0.0) + float(bill_adjustment or 0.0)
    recomputed_total_with_baseline = recomputed_subtotal + baseline

    if bill_total is not None:
        difference = float(bill_total) - recomputed_subtotal
        difference_rate = difference / recomputed_subtotal if recomputed_subtotal else None
        net_difference = float(bill_total) - recomputed_total_with_baseline
        passed = abs(net_difference) <= tolerance_yuan
        gross_passed = abs(difference) <= tolerance_yuan

        energy_delta = (float(bill_energy) - recomputed_energy) if bill_energy is not None else None
        if energy_delta is not None:
            items.append(
                BillDifferenceItem(
                    factor="电度电费差（账单实录 − 计划复算）",
                    category="energy",
                    amount_yuan=energy_delta,
                    unit_amount_yuan_per_kwh=(
                        energy_delta / float(bill.energy_total_kwh) if bill.energy_total_kwh else None
                    ),
                    explanation=(
                        f"账单电度电费 {_fmt(bill_energy)}（来源：{energy_source}）− 计划复算 "
                        f"{_fmt(recomputed_energy)}。差异来自『分时电量 × 电价』两端的口径不同："
                        "电价水平（代理购电 / 市场化 / 合同）、时段划分、以及是否含输配电价、"
                        "系统运行费与政府性基金及附加"
                    ),
                )
            )
        else:
            items.append(
                BillDifferenceItem(
                    factor="电度电费差（账单未提供电度电费分项）",
                    category="energy",
                    amount_yuan=0.0,
                    explanation=(
                        "账单既未提供『电度电费合计』也未提供电度电费二层分项，无法单独拆分电度电费差；"
                        "该部分差异全部落在口径残差项中，报告需说明账单分项缺失"
                    ),
                )
            )

        bill_basic = float(bill_capacity or 0.0) + float(bill_demand or 0.0)
        recomputed_basic = float(recomputed_capacity or 0.0) + float(recomputed_demand or 0.0)
        items.append(
            BillDifferenceItem(
                factor="基本电费差（容量 / 需量）",
                category="basic",
                amount_yuan=bill_basic - recomputed_basic,
                unit_amount_yuan_per_kwh=None,
                explanation=(
                    f"账单基本电费 {_fmt(bill_basic)} − 计划复算 {_fmt(recomputed_basic)}；"
                    f"计费方式：{mode.label}。"
                    + (
                        f"复算需量 = 账单计费需量 {_fmt(bill.billing_demand_kw, 'kW')} × 需量电价 "
                        f"{_fmt(plan.demand_charge_yuan_per_kw_month, '元/千瓦·月')}"
                        if mode is DemandBillingMode.DEMAND
                        else f"复算容量 = 合同容量 {_fmt(bill.contract_capacity_kva, 'kVA')} × 容量电价 "
                        f"{_fmt(plan.capacity_charge_yuan_per_kva_month, '元/千伏安·月')}"
                    )
                    + "。需量口径见 demand_method，不可用曲线最大功率替代账单计费需量（§7.5）"
                ),
            )
        )
        items.append(
            BillDifferenceItem(
                factor="未建模费用（功率因数调整 / 其他 / 增值税）",
                category="unmodeled",
                amount_yuan=float(bill_unmodeled or 0.0),
                explanation=(
                    "以下费用本阶段不重建规则，按账单实际金额作为**固定基准**保留并披露："
                    + ("、".join(f"{label_of(name)} {_fmt(value)}" for name, value in unmodeled.items()) or "无")
                ),
            )
        )
        items.append(
            BillDifferenceItem(
                factor="调整 / 补退费",
                category="unmodeled",
                amount_yuan=float(bill_adjustment or 0.0),
                explanation="账单调整/补退费（可为负），按账单实际值保留",
            )
        )
        explained = sum(item.amount_yuan for item in items)
        items.append(
            BillDifferenceItem(
                factor="口径残差（账单内部合计与复算口径之差）",
                category="residual",
                amount_yuan=float(difference) - explained,
                explanation=(
                    "残差 = 总差异 − 上述各项之和。残差非 0 时说明账单内部的分项结构与"
                    "『电度电费 + 基本电费 + 未建模费用 + 调整项』的口径不完全一致"
                    "（例如账单把某些费用列在电度电费之外，或分项合计与账单总额本身不闭合）；"
                    "该残差永远列出，不允许为了匹配总额而改动电价（§7.4 第 4 条）"
                ),
            )
        )

    # ---------------- 电量口径差 ΔE（§3.1）---------------- #
    period_sum = float(sum(energies.values())) if energies else None
    energy_difference = (
        float(bill.energy_total_kwh) - period_sum
        if bill.energy_total_kwh is not None and period_sum is not None
        else None
    )

    messages: list[str] = [
        f"{bill.billing_month} 账单 {bill.bill_id}：账单总额 {_fmt(bill_total)}，"
        f"复算合计 {_fmt(recomputed_subtotal)}（仅软件建模部分），毛差异 {_fmt(difference)}"
        + (f"（差异率 {difference_rate:+.2%}）" if difference_rate is not None else "")
        + f"；未建模费用基准 {_fmt(bill_unmodeled)}，调整项 {_fmt(bill_adjustment)}，"
        f"复算总额 {_fmt(recomputed_total_with_baseline)}，净差异 {_fmt(net_difference)}"
        + f"；容差 {_fmt(tolerance_yuan)}，"
        + ("净差异在容差内（账单校准通过）" if passed else "**净差异超出容差（未通过账单校准）**")
    ]
    if bill_total is not None and passed is not None and gross_passed is not None and passed != gross_passed:
        messages.append(
            f"口径提示：毛差异是否在容差内 = {gross_passed}，净差异 = {passed}。"
            "容差判定采用**净差异**（剔除账单已列但本阶段未建模的费用，如功率因数调整电费），"
            "因为这些费用按账单实际值固定保留、不参与建模（§3.4、§7.4）"
        )
    messages.append(
        "复算口径："
        + RECOMPUTATION_BASIS_LABELS["tou_energy_recompute"]
        + "（分时电量 × 电价计划分时单价），不是账单实录值，也不覆盖账单事实（§2.5）"
    )
    if missing_periods:
        messages.append(
            "账单未提供以下时段的电量，未参与计价（不是 0）：" + "、".join(missing_periods)
        )
    if energy_difference is not None:
        messages.append(
            f"分时电量合计差 ΔE = {float(bill.energy_total_kwh):,.2f} − {period_sum:,.2f} = "
            f"{energy_difference:+,.2f} kWh（§3.1）"
        )
    if missing_prices:
        messages.append(
            "以下时段有电量但电价计划未提供价格，未计价：" + "、".join(missing_prices)
        )
    if difference is not None and not passed:
        messages.append(
            "未通过账单校准（§7.4 第 6 条）：**净差异**超出容差，方案节省额不得标为高置信度"
        )
    messages.append(f"电价计划：{plan.display_name}；来源：{plan.source_text}")

    assumptions: list[str] = [
        "电量电费 C_energy = Σ_i E_grid,i × P_i；P_i 由月份、时刻、电价版本与适用时段共同确定（V2.3 §3.4）",
        "两部制容量计费与需量计费**默认互斥**；本次采用：" + mode.label + "（§7.5）",
        "需量取值方法：" + (DEMAND_METHOD_BILL if mode is DemandBillingMode.DEMAND else DEMAND_METHOD_CURVE),
        "未建模费用（功率因数调整、其他费用、增值税）按账单实际值保留为固定基准，不凭空估算（§3.4）",
        "账单未提供的分项视为**未知**，不按 0 计入任何合计（§2.1、§3.1）",
        "账单事实与本复算结果分开保存；本结果不写回账单任何字段（§2.5）",
    ]
    if not require_verified:
        assumptions.append("本次允许使用未核验的电价计划（require_verified=False），结果只能作口径对照用")
    if not (plan.effective_from <= basis_day and plan.covers_date(basis_day)):
        assumptions.append(
            f"注意：计费基准日 {basis_day.isoformat()} 不在电价计划生效期间"
            f"（{plan.effective_from} ~ {plan.effective_to or '未标注'}）内，本次属**跨期口径对照**，"
            "不代表该月实际执行的电价，报告必须标注"
        )

    quality = "valid" if passed else "warning"
    logger.info(
        "账单复算：%s（%s）账单 %.2f vs 复算 %.2f，差异 %s",
        bill.bill_id,
        plan.tariff_plan_id,
        float(bill_total or 0.0),
        recomputed_subtotal,
        "在容差内" if passed else "超容差",
    )
    return BillRecomputation(
        bill_id=bill.bill_id,
        billing_month=bill.billing_month,
        tariff_plan_id=plan.tariff_plan_id,
        tariff_plan_name=plan.name,
        plan_status=plan.status,
        voltage_level=bill.voltage_level,
        tariff_structure=bill.tariff_structure,
        basis="tou_energy_recompute",
        basis_label=RECOMPUTATION_BASIS_LABELS["tou_energy_recompute"],
        energy_total_kwh=_provided(bill.energy_total_kwh),
        energy_period_sum_kwh=period_sum,
        energy_difference_kwh=energy_difference,
        energy_missing_periods=missing_periods,
        period_details=details,
        recomputed_energy_charge_yuan=recomputed_energy,
        recomputed_capacity_charge_yuan=recomputed_capacity,
        recomputed_demand_charge_yuan=recomputed_demand,
        recomputed_subtotal_yuan=recomputed_subtotal,
        recomputed_unmodeled_baseline_yuan=(float(bill_unmodeled or 0.0) if bill_unmodeled is not None else 0.0)
        + float(bill_adjustment or 0.0),
        recomputed_total_with_baseline_yuan=recomputed_total_with_baseline,
        recomputed_average_price_yuan_per_kwh=(
            recomputed_subtotal / float(bill.energy_total_kwh)
            if bill.energy_total_kwh not in (None, 0)
            else None
        ),
        bill_energy_charge_yuan=bill_energy,
        bill_capacity_charge_yuan=bill_capacity,
        bill_demand_charge_yuan=bill_demand,
        bill_unmodeled_charge_yuan=bill_unmodeled,
        bill_unmodeled_fields=sorted(unmodeled),
        bill_adjustment_yuan=bill_adjustment,
        bill_total_yuan=bill_total,
        difference_yuan=difference,
        difference_rate=difference_rate,
        difference_after_baseline_yuan=net_difference,
        tolerance_yuan=tolerance_yuan,
        passed_tolerance=passed,
        gross_passed_tolerance=gross_passed,
        difference_items=items,
        demand_billing_mode=mode,
        billing_demand_kw=_provided(bill.billing_demand_kw),
        contract_capacity_kva=_provided(bill.contract_capacity_kva),
        demand_price_yuan_per_kw_month=plan.demand_charge_yuan_per_kw_month,
        capacity_price_yuan_per_kva_month=plan.capacity_charge_yuan_per_kva_month,
        demand_method=(
            DEMAND_METHOD_BILL if mode is DemandBillingMode.DEMAND else DEMAND_METHOD_CURVE
        ),
        quality_status=quality,
        messages=messages,
        assumptions=assumptions,
    )


def recompute_bills(
    bills: Iterable[ElectricityBill],
    plan: TariffPlan,
    *,
    require_verified: bool = True,
    tolerance: BillTolerance | None = None,
    period_energies_by_bill: Mapping[str, Mapping[TariffPeriod, float]] | None = None,
) -> list[BillRecomputation]:
    """对多张账单逐月复算（顺序与入参一致）。

    :param period_energies_by_bill: 可选的"账单编号 → 分时电量"映射；用于账单本身没有分时电量字段、
        但用户提供了逐小时/高频负荷数据的场景（§7.4 第 1 条）。未登记分时电量的账单按账单字段处理。
    """
    overrides = period_energies_by_bill or {}
    return [
        recompute_bill(
            bill,
            plan,
            period_energies=overrides.get(bill.bill_id),
            tolerance=tolerance,
            require_verified=require_verified,
        )
        for bill in bills
    ]


def _is_natural_month(bill: ElectricityBill) -> bool:
    """账期长度是否与起始月自然月天数相符（与 :mod:`cenep.calculation.bill_calculator` 同一容差口径）。"""
    expected = monthrange(bill.billing_period_start.year, bill.billing_period_start.month)[1]
    return abs(bill.period_days - expected) <= NATURAL_MONTH_DAY_TOLERANCE


def calibrate_bills(
    bills: Sequence[ElectricityBill],
    plan: TariffPlan,
    *,
    year: int | None = None,
    tolerance: BillTolerance | None = None,
    require_verified: bool = True,
    period_energies_by_bill: Mapping[str, Mapping[TariffPeriod, float]] | None = None,
) -> BillCalibrationSummary:
    """用真实账单校准复算结果并给出年度结论（V2.3 §7.4、§3.1）。

    年度电量只在**每月账单周期完整且不重叠**时才允许直接相加（§3.1）；
    覆盖率、缺月、重叠、跨月都会降低可信度并写入 ``messages``，
    整体是否通过校准由 ``calibration_passed`` 给出（§7.4 第 6 条）。

    :param period_energies_by_bill: 可选"账单编号 → 分时电量"映射，见 :func:`recompute_bills`。
    """
    tol = tolerance or BillTolerance()
    if year is None:
        counts: dict[int, int] = defaultdict(int)
        for bill in bills:
            counts[bill.billing_period_start.year] += 1
        year = max(counts, key=lambda item: counts[item]) if counts else date.today().year

    selected = [bill for bill in bills if bill.billing_period_start.year == year]
    monthly = recompute_bills(
        selected,
        plan,
        require_verified=require_verified,
        tolerance=tol,
        period_energies_by_bill=period_energies_by_bill,
    )

    covered = [item.billing_month for item in monthly]
    expected = [f"{year}-{month:02d}" for month in range(1, 13)]
    missing = [month for month in expected if month not in set(covered)]
    duplicate = [month for month in set(covered) if covered.count(month) > 1]
    ordered = sorted(selected, key=lambda bill: bill.billing_period_start)
    overlap = any(
        ordered[index].billing_period_start <= ordered[index - 1].billing_period_end
        for index in range(1, len(ordered))
    )
    non_natural = [bill.bill_id for bill in selected if not _is_natural_month(bill)]
    coverage = len(set(covered)) / 12.0
    can_sum = coverage >= 1.0 and not duplicate and not overlap

    total_energy = sum(float(bill.energy_total_kwh) for bill in selected if bill.energy_total_kwh is not None)
    total_bill = sum(float(bill.bill_total_yuan) for bill in selected if bill.bill_total_yuan is not None)
    total_recomputed = sum(
        float(item.recomputed_subtotal_yuan) for item in monthly if item.recomputed_subtotal_yuan is not None
    )
    total_recomputed_with_baseline = sum(
        float(item.recomputed_total_with_baseline_yuan)
        for item in monthly
        if item.recomputed_total_with_baseline_yuan is not None
    )
    total_difference = total_bill - total_recomputed
    total_net_difference = total_bill - total_recomputed_with_baseline
    bill_avg = total_bill / total_energy if total_energy else None
    recomputed_avg = total_recomputed / total_energy if total_energy else None

    factor_totals: dict[str, float] = defaultdict(float)
    for item in monthly:
        for diff_item in item.difference_items:
            factor_totals[diff_item.factor] += float(diff_item.amount_yuan)

    passed_bills = sum(1 for item in monthly if item.passed_tolerance is True)
    failed_bills = sum(1 for item in monthly if item.passed_tolerance is False)
    total_tolerance = tol.amount_tolerance(total_bill)
    calibration_passed = (
        (abs(total_net_difference) <= total_tolerance and failed_bills == 0) if monthly else None
    )

    messages: list[str] = [
        f"{year} 年共复算 {len(monthly)} 条账单，月份覆盖率 {coverage * 100:.1f}%（{len(set(covered))}/12）",
        f"账单合计 {total_bill:,.2f} 元；复算合计（仅软件建模部分：电度电费 + 基本电费）"
        f"{total_recomputed:,.2f} 元，毛差异 {total_difference:+,.2f} 元"
        + (f"（差异率 {total_difference / total_recomputed:+.2%}）" if total_recomputed else ""),
        f"复算总额（含未建模费用基准）{total_recomputed_with_baseline:,.2f} 元，"
        f"净差异 {total_net_difference:+,.2f} 元；容差 {total_tolerance:,.2f} 元",
        f"账单加权均价 {bill_avg:.6f} 元/千瓦时，复算加权均价 {recomputed_avg:.6f} 元/千瓦时"
        if bill_avg is not None and recomputed_avg is not None
        else "账单或复算均价无法计算（缺少电量）",
        f"逐月通过容差（按**净差异**）{passed_bills} 条，超容差 {failed_bills} 条；"
        + ("整体通过账单校准" if calibration_passed else "**整体未通过账单校准**，方案节省额不得标为高置信度"),
    ]
    if missing:
        messages.append("缺失月份：" + "、".join(missing) + "；年度合计不是完整年度实测值")
    if duplicate:
        messages.append("同月多条账单：" + "、".join(sorted(duplicate)))
    if overlap:
        messages.append("存在账期重叠的账单，年度电量与电费不可直接相加")
    if non_natural:
        messages.append(f"含计费周期非自然月的账单 {len(non_natural)} 条：" + "、".join(non_natural[:5]))
    messages.append(
        "差异因素年度合计（元）："
        + "；".join(f"{factor} {amount:+,.2f}" for factor, amount in factor_totals.items())
    )

    assumptions = [
        "复算口径为『按分时电量复算值』，与账单实录值、负荷曲线模拟值必须分别展示（§2.5、§8.1）",
        "差异**必须被解释**：全部差异被拆成电度电费差、基本电费差、未建模费用、调整/返还与口径残差，"
        "各分项之和严格等于**毛差异**（§7.4 第 4 条）",
        "容差判定采用**净差异** = 账单总额 −（复算合计 + 未建模费用基准 + 调整项）；"
        "未建模费用按账单实际值固定保留、不参与建模，因此不参与容差判定（§3.4、§7.4）",
        "未建模费用按账单实际值保留为固定基准，不凭空估算（§3.4）",
        "年度电量仅在每月账单周期完整且不重叠时才允许直接相加（§3.1）",
        f"电价计划：{plan.display_name}；来源：{plan.source_text}",
    ]
    if not require_verified:
        assumptions.append("本次允许使用未核验的电价计划，结论只能作口径对照用")

    logger.info(
        "账单校准：%d 年 %d 条，账单 %.2f vs 复算 %.2f，差异 %+.2f，通过=%s",
        year,
        len(monthly),
        total_bill,
        total_recomputed,
        total_difference,
        calibration_passed,
    )
    return BillCalibrationSummary(
        year=year,
        tariff_plan_id=plan.tariff_plan_id,
        tariff_plan_name=plan.name,
        basis="tou_energy_recompute",
        basis_label=RECOMPUTATION_BASIS_LABELS["tou_energy_recompute"],
        bill_count=len(monthly),
        months_covered=sorted(set(covered)),
        missing_months=missing,
        coverage_ratio=coverage,
        can_sum_directly=can_sum,
        total_energy_kwh=total_energy or None,
        total_bill_amount_yuan=total_bill or None,
        total_recomputed_amount_yuan=total_recomputed or None,
        total_recomputed_with_baseline_yuan=total_recomputed_with_baseline or None,
        total_difference_yuan=total_difference,
        total_difference_rate=(total_difference / total_recomputed) if total_recomputed else None,
        total_net_difference_yuan=total_net_difference,
        bill_average_price_yuan_per_kwh=bill_avg,
        recomputed_average_price_yuan_per_kwh=recomputed_avg,
        unit_price_difference_yuan_per_kwh=(
            recomputed_avg - bill_avg if bill_avg is not None and recomputed_avg is not None else None
        ),
        factor_totals=dict(factor_totals),
        passed_bills=passed_bills,
        failed_bills=failed_bills,
        monthly=monthly,
        tolerance_yuan=total_tolerance,
        calibration_passed=calibration_passed,
        messages=messages,
        assumptions=assumptions,
    )


# 账单费用字段清单在本模块重新导出，便于调用方核对"哪些字段算未建模"
BILL_CHARGE_FIELDS: tuple[str, ...] = BILL_LEVEL1_CHARGE_FIELDS
