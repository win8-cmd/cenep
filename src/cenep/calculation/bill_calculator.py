"""电费账单基准数据计算与校验（V2.1 §3.1、§2.1、§5.5）。

本模块是账单相关**核心公式的唯一实现处**（V2.1 §0.2：核心业务公式必须在
``calculation/``，UI 不得实现核心公式）。公式与口径逐条对应规格书：

============================  ============================================================
公式                          出处 / 口径
============================  ============================================================
``E_load,annual = Σ_m E_bill,m``  §3.1：仅当每月账单周期**完整且不重叠**时才允许直接相加
``P_avg = C_bill / E_grid``      §3.1：**仅作账单统计**，不得当成光伏自用量的边际节省电价
``ΔC = C_bill,total − Σ_i C_i``  §3.1：默认容差 ``max(1 元, 0.5% × 总额)``
``ΔE = E_total − (E_sharp+E_peak+E_flat+E_valley+…)``  §3.1：默认容差 ``max(1 kWh, 0.5% × 总电量)``
============================  ============================================================

关于 ``ΔE`` 的一处口径说明（必须披露，不得含糊）
------------------------------------------------
§3.1 的公式列出"尖 + 峰 + 平 + 谷"四项，而 §2.1 的字段清单还含 ``energy_offpeak_kwh``
（**深谷**，与低谷不是同一时段）。本实现的处理是：

* 参与 ``ΔE`` 的时段 = **账单实际提供了取值的全部时段**（含深谷）；
* 未提供的时段视为**未知**，绝不当作 0（§3.1 明确要求）；
* 只要有任一时段未提供，结果标记 ``energy_partial=True`` 并提示"只就比较到的时段求和"。

理由：若把深谷电量排除在合计之外，一份"深谷"与"低谷"分别列示的合法账单会被误报为
"分时合计与总电量不一致"。具体时段口径随结果一起返回，可在报告中原样披露。

本模块**不写回账单事实**：任何差异数值都不落进 :class:`~cenep.domain.bill_models.ElectricityBill`
的金额/电量字段，只影响 ``quality_status`` / ``quality_messages``（V2.1 §0.2、§5.5）。

V2.5 §5 账单数据分类口径（需求方已确认）在本模块的落点
----------------------------------------------------
* ``operation_fee_detail``（市场化运营费用，第 5 页 B/C 类）**只留档，绝不参与任何费用或电价
  计算**：它是市场化交易结算项，**已包含在账单总电费里**，重复计入即重复计算。本模块只在
  ``messages`` / ``assumptions`` 中如实披露它的存在与总合计，从不把它加进任何合计。
* ``hourly_energy_tariff``（24 小时电量电价表）是消纳率电价的**首选来源**；取价优先级与
  中文说明见 :mod:`cenep.calculation.bill_price_source`。
* ``meter_groups``（逐电能表计量分组）只用于校验与来源追溯，不单独参与电价计算。
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date

from ..domain.bill_models import (
    BILL_ENERGY_SUB_CHARGE_FIELDS,
    BILL_LEVEL1_CHARGE_FIELDS,
    ENERGY_PERIOD_FIELDS,
    SIGNED_CHARGE_FIELDS,
    BillAnnualSummary,
    BillMonthlySummary,
    BillReconciliation,
    BillTolerance,
    BillValidationIssue,
    ElectricityBill,
    label_of,
)
from ..domain.enums import BillEnergyPeriod, BillQualityStatus, TariffStructure

logger = logging.getLogger(__name__)

__all__ = [
    "NATURAL_MONTH_DAY_TOLERANCE",
    "apply_quality",
    "annual_bill_summary",
    "average_comprehensive_price",
    "energy_period_breakdown",
    "level1_charge_components",
    "energy_sub_charge_components",
    "monthly_summary",
    "reconcile_bill",
    "sum_period_energy",
    "validate_bill_fields",
]

#: 账期长度与自然月天数的允许偏差（天）。超出即提示"计费周期非自然月"（§3.1）
NATURAL_MONTH_DAY_TOLERANCE = 5


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _provided(bill: ElectricityBill, fields: tuple[str, ...]) -> dict[str, float]:
    """取**已提供**（非 ``None``）的字段值。

    ``None`` = 账单没有提供该分项，与 ``0.0`` 语义不同（V2.1 §2.1），
    因此这里绝不把 ``None`` 补成 0。
    """
    out: dict[str, float] = {}
    for name in fields:
        value = getattr(bill, name, None)
        if value is not None:
            out[name] = float(value)
    return out


def _fmt_kwh(value: float | None) -> str:
    return "未提供" if value is None else f"{value:,.2f} kWh"


def _fmt_yuan(value: float | None) -> str:
    return "未提供" if value is None else f"{value:,.2f} 元"


# --------------------------------------------------------------------------- #
# §3.1 分时电量合计差 ΔE
# --------------------------------------------------------------------------- #
def energy_period_breakdown(
    bill: ElectricityBill,
) -> tuple[dict[BillEnergyPeriod, float], list[BillEnergyPeriod]]:
    """返回 ``(已提供的分时电量, 未提供的时段)``（§2.1、§3.1）。"""
    provided: dict[BillEnergyPeriod, float] = {}
    missing: list[BillEnergyPeriod] = []
    for period, field in ENERGY_PERIOD_FIELDS.items():
        value = getattr(bill, field, None)
        if value is None:
            missing.append(period)
        else:
            provided[period] = float(value)
    return provided, missing


def sum_period_energy(bill: ElectricityBill) -> tuple[float | None, list[str]]:
    """分时电量合计与未提供时段的字段中文名（§3.1）。

    :return: ``(合计 kWh 或 None, 未提供时段字段中文名列表)``；一个时段都没提供时返回 ``None``。
    """
    provided, missing = energy_period_breakdown(bill)
    total = sum(provided.values()) if provided else None
    return total, [label_of(p.field_name) for p in missing]


# --------------------------------------------------------------------------- #
# §2.1、§3.1 费用分项与合计
# --------------------------------------------------------------------------- #
def level1_charge_components(bill: ElectricityBill) -> dict[str, float]:
    """**第一层**费用分项（已提供者），直接相加即为账单总额口径（§2.1、§3.1）。"""
    return _provided(bill, BILL_LEVEL1_CHARGE_FIELDS)


def energy_sub_charge_components(bill: ElectricityBill) -> dict[str, float]:
    """**第二层**费用分项：``energy_charge_yuan``（电度电费合计）的明细（§2.1）。

    这些字段**不重复计入账单总额**——它们与 ``energy_charge_yuan`` 是"明细与合计"的关系，
    同时相加会造成重复计算。
    """
    return _provided(bill, BILL_ENERGY_SUB_CHARGE_FIELDS)


def effective_energy_charge(bill: ElectricityBill) -> tuple[float | None, str]:
    """电度电费（用于账单总额核对的取值）与其来源。

    :return: ``(元 或 None, 来源)``，来源取值：

        * ``"declared"`` —— 账单直接给出了 ``energy_charge_yuan``（电度电费合计）；
        * ``"sub_sum"`` —— 账单未给合计，但给出了二层明细，按明细合计替代（会在结果中披露）；
        * ``"none"`` —— 既没有合计也没有明细。
    """
    if bill.energy_charge_yuan is not None:
        return float(bill.energy_charge_yuan), "declared"
    sub = energy_sub_charge_components(bill)
    if sub:
        return float(sum(sub.values())), "sub_sum"
    return None, "none"


# --------------------------------------------------------------------------- #
# §2.1 字段级校验
# --------------------------------------------------------------------------- #
def validate_bill_fields(bill: ElectricityBill) -> list[BillValidationIssue]:
    """字段级与口径级校验，返回**中文**问题清单（V2.1 §2.1、§0.2）。

    规则编号（写在 ``code`` 与消息开头，便于测试断言与报告检索）::

        B01 电量为负            ERROR
        B02 金额为负（非调整项）ERROR
        B03 调整/返还项为负     INFO（§2.1 允许负值）
        B04 两部制缺少容量与需量 WARNING
        B05 计费需量超过合同容量 WARNING
        B06 单一制却提供了基本电费 WARNING
        B07 未提供总电量        INFO
        B08 未提供任何费用分项  INFO
        B09 未提供账单总额      INFO
        B10 跨月账期            INFO（§2.1 要求在界面明确跨月）
        B11 未提供电压等级      INFO
        B12 Excel 导入缺少来源标记 INFO
        B13 总电量大于 0 但账单总额为 0 WARNING（可疑）
        B14 费用分项合计与账单总额差异超容差 WARNING（§3.1 的 ΔC）
        B15 电度电费二层分项合计与合计差超容差 WARNING
    """
    issues: list[BillValidationIssue] = []
    row = bill.source_row_number

    def add(level: str, code: str, field: str, message: str) -> None:
        issues.append(
            BillValidationIssue(level=level, code=code, field=field, message=message, source_row_number=row)
        )

    # ---- B01 电量不得为负（§2.1）----
    for name in (
        "energy_total_kwh",
        "energy_sharp_kwh",
        "energy_peak_kwh",
        "energy_flat_kwh",
        "energy_valley_kwh",
        "energy_offpeak_kwh",
    ):
        value = getattr(bill, name, None)
        if value is not None and float(value) < 0.0:
            add("ERROR", "B01", name, f"{label_of(name)} 不能为负（当前 {float(value):,.2f}），请核对账单数值")

    # ---- B02/B03 金额符号（§2.1）----
    for name in (*BILL_LEVEL1_CHARGE_FIELDS, *BILL_ENERGY_SUB_CHARGE_FIELDS, "bill_total_yuan"):
        value = getattr(bill, name, None)
        if value is None or float(value) >= 0.0:
            continue
        if name in SIGNED_CHARGE_FIELDS:
            add(
                "INFO",
                "B03",
                name,
                f"{label_of(name)} 为负值（{float(value):,.2f}），按调整/返还处理；"
                "该字段是唯一允许负值的费用项（V2.1 §2.1）",
            )
        else:
            add("ERROR", "B02", name, f"{label_of(name)} 不能为负（当前 {float(value):,.2f}），请核对账单数值")

    # ---- B04/B05/B06 计费方式与基本电费（§2.1、§3.4）----
    has_capacity_price = bill.basic_capacity_charge_yuan is not None
    has_demand_price = bill.demand_charge_yuan is not None
    if bill.tariff_structure is TariffStructure.TWO_PART:
        if bill.contract_capacity_kva is None and bill.billing_demand_kw is None:
            add(
                "WARNING",
                "B04",
                "tariff_structure",
                "计费方式为两部制，但未提供合同容量（kVA）与账单计费需量（kW），"
                "无法核对基本电费口径，请补填或改为单一制",
            )
    elif bill.tariff_structure is TariffStructure.SINGLE_PART and (has_capacity_price or has_demand_price):
        add(
            "WARNING",
            "B06",
            "tariff_structure",
            "计费方式为单一制，但提供了基本电费（容量/需量）金额，两者口径不一致，请核对",
        )

    if (
        bill.billing_demand_kw is not None
        and bill.contract_capacity_kva is not None
        and float(bill.billing_demand_kw) > float(bill.contract_capacity_kva)
    ):
        add(
            "WARNING",
            "B05",
            "billing_demand_kw",
            f"账单计费需量 {float(bill.billing_demand_kw):,.2f} kW 大于合同容量 "
            f"{float(bill.contract_capacity_kva):,.2f} kVA，可能触发超容加价，请核对",
        )

    # ---- B07 未提供总电量 ----
    if bill.energy_total_kwh is None:
        add("INFO", "B07", "energy_total_kwh", "账单未提供总购电量，年度总用电量与平均电价无法计算")

    # ---- B08/B09 费用完整性 ----
    components = level1_charge_components(bill)
    sub_components = energy_sub_charge_components(bill)
    if not components and not sub_components:
        add("INFO", "B08", "bill_total_yuan", "账单未提供任何费用分项，只能使用账单总额，无法核对分项合计")
    if bill.bill_total_yuan is None:
        add(
            "INFO",
            "B09",
            "bill_total_yuan",
            f"账单未提供总额，无法核对分项合计（已提供分项合计 {_fmt_yuan(sum(components.values()) if components else None)}）",
        )

    # ---- B13 总额为 0 但电量 > 0 ----
    if (
        bill.bill_total_yuan is not None
        and float(bill.bill_total_yuan) == 0.0
        and bill.energy_total_kwh is not None
        and float(bill.energy_total_kwh) > 0.0
    ):
        add(
            "WARNING",
            "B13",
            "bill_total_yuan",
            f"账单总额为 0 元，但总电量为 {float(bill.energy_total_kwh):,.2f} kWh，数据可疑，请核对",
        )

    # ---- B10 跨月账期 ----
    if bill.is_cross_month:
        add(
            "INFO",
            "B10",
            "billing_period_end",
            f"该账单为跨月账期（{bill.billing_period_start:%Y-%m-%d} ~ "
            f"{bill.billing_period_end:%Y-%m-%d}），月度聚合按起始月 {bill.billing_month} 归属，"
            "界面与报告必须标注跨月（V2.1 §2.1）",
        )

    # ---- B11 电压等级 ----
    if not (bill.voltage_level or "").strip():
        add("INFO", "B11", "voltage_level", "账单未提供电压等级，后续电价匹配需要该信息（V2.3 §7.3）")

    # ---- B12 导入来源标记 ----
    if bill.source_type.value == "excel" and (not bill.source_file_name or bill.source_row_number is None):
        add(
            "INFO",
            "B12",
            "source_file_name",
            "该账单标记为 Excel 导入，但缺少来源文件名或来源行号，无法回溯到原始行",
        )

    logger.debug("账单字段校验：%s → %d 条问题", bill.bill_id, len(issues))
    return issues


# --------------------------------------------------------------------------- #
# §2.1、§3.1 自洽性核对
# --------------------------------------------------------------------------- #
def reconcile_bill(
    bill: ElectricityBill, *, tolerance: BillTolerance | None = None
) -> BillReconciliation:
    """核对单条账单的自洽性：分时电量合计差 ΔE、费用分项合计差 ΔC、电度电费二层核对。

    差异**永远会被报告**；容差只决定是否升级为警告（V2.1 §2.1）。
    本函数不修改传入的账单对象。
    """
    tol = tolerance or BillTolerance()
    messages: list[str] = []
    assumptions: list[str] = []
    issues = validate_bill_fields(bill)

    # ---------------- ΔE：分时电量合计 vs 总电量（§3.1）---------------- #
    period_sum, missing_labels = sum_period_energy(bill)
    provided_periods, missing_periods = energy_period_breakdown(bill)
    total_energy = bill.energy_total_kwh
    tolerance_kwh = tol.energy_tolerance(total_energy)
    energy_difference: float | None = None
    energy_consistent: bool | None = None
    energy_partial = bool(missing_periods) and bool(provided_periods)

    if total_energy is None:
        messages.append(
            f"未提供总购电量，无法核对分时电量合计（已提供分时电量合计 {_fmt_kwh(period_sum)}）"
        )
    elif period_sum is None:
        messages.append("未提供任何分时电量，无法核对分时电量合计")
    else:
        energy_difference = float(total_energy) - float(period_sum)
        energy_consistent = abs(energy_difference) <= tolerance_kwh
        scope = "全部时段" if not missing_periods else f"已提供的 {len(provided_periods)} 个时段"
        messages.append(
            f"分时电量合计差 ΔE = {float(total_energy):,.2f} − {float(period_sum):,.2f} = "
            f"{energy_difference:+,.2f} kWh（{scope}；容差 {tolerance_kwh:,.2f} kWh）："
            + ("在容差内" if energy_consistent else "**超出容差**")
        )
        if not energy_consistent:
            issues.append(
                BillValidationIssue(
                    level="WARNING",
                    code="B04",
                    field="energy_total_kwh",
                    message=(
                        f"分时电量合计 {float(period_sum):,.2f} kWh 与总电量 "
                        f"{float(total_energy):,.2f} kWh 差异 {energy_difference:+,.2f} kWh，"
                        f"超出容差 {tolerance_kwh:,.2f} kWh，请核对账单；差异不会被静默覆盖"
                    ),
                    source_row_number=bill.source_row_number,
                )
            )
        if energy_partial:
            missing_text = "、".join(missing_labels)
            messages.append(f"以下时段未提供，按未知处理（未按 0 计入）：{missing_text}")
            assumptions.append(
                f"分时电量合计仅包含账单实际提供的时段；未提供时段（{missing_text}）视为未知而非 0（V2.1 §3.1）"
            )

    # ---------------- ΔC：费用分项合计 vs 账单总额（§3.1）---------------- #
    components = level1_charge_components(bill)
    sub_components = energy_sub_charge_components(bill)
    energy_charge, energy_charge_source = effective_energy_charge(bill)

    amount_components: dict[str, float] = dict(components)
    used_fields: list[str] = list(components)
    if energy_charge_source == "sub_sum" and energy_charge is not None:
        # 账单没给电度电费合计，但有明细：用明细合计替代，并明确披露
        amount_components.pop("energy_charge_yuan", None)
        used_fields = [f for f in used_fields if f != "energy_charge_yuan"] + list(sub_components)
        amount_components.update(sub_components)
        assumptions.append(
            f"账单未提供『电度电费合计』，账单总额核对按二层电度分项合计 {energy_charge:,.2f} 元替代，"
            "该替代值仅用于校验，不写回账单"
        )

    component_sum = float(sum(amount_components.values())) if amount_components else None
    total_amount = bill.bill_total_yuan
    tolerance_yuan = tol.amount_tolerance(total_amount)
    amount_difference: float | None = None
    amount_consistent: bool | None = None

    if total_amount is None:
        messages.append(f"未提供账单总额，无法核对分项合计（已提供分项合计 {_fmt_yuan(component_sum)}）")
    elif component_sum is None:
        messages.append("未提供任何费用分项，无法核对分项合计")
    else:
        amount_difference = float(total_amount) - component_sum
        amount_consistent = abs(amount_difference) <= tolerance_yuan
        messages.append(
            f"费用分项合计差 ΔC = {float(total_amount):,.2f} − {component_sum:,.2f} = "
            f"{amount_difference:+,.2f} 元（容差 {tolerance_yuan:,.2f} 元）："
            + ("在容差内" if amount_consistent else "**超出容差**")
        )
        if not amount_consistent:
            issues.append(
                BillValidationIssue(
                    level="WARNING",
                    code="B14",
                    field="bill_total_yuan",
                    message=(
                        f"费用分项合计 {component_sum:,.2f} 元与账单总额 {float(total_amount):,.2f} 元"
                        f"差异 {amount_difference:+,.2f} 元，超出容差 {tolerance_yuan:,.2f} 元，请核对；"
                        "差异不会被静默覆盖"
                    ),
                    source_row_number=bill.source_row_number,
                )
            )

    # ---------------- 电度电费二层明细核对（§2.1）---------------- #
    sub_sum: float | None = float(sum(sub_components.values())) if sub_components else None
    sub_difference: float | None = None
    sub_consistent: bool | None = None
    if bill.energy_charge_yuan is not None and sub_sum is not None:
        sub_difference = float(bill.energy_charge_yuan) - sub_sum
        sub_tolerance = tol.amount_tolerance(bill.energy_charge_yuan)
        sub_consistent = abs(sub_difference) <= sub_tolerance
        messages.append(
            f"电度电费二层分项合计差 = {float(bill.energy_charge_yuan):,.2f} − {sub_sum:,.2f} = "
            f"{sub_difference:+,.2f} 元（容差 {sub_tolerance:,.2f} 元）："
            + ("在容差内" if sub_consistent else "**超出容差**")
        )
        if not sub_consistent:
            issues.append(
                BillValidationIssue(
                    level="WARNING",
                    code="B15",
                    field="energy_charge_yuan",
                    message=(
                        f"电度电费合计 {float(bill.energy_charge_yuan):,.2f} 元与二层分项合计 "
                        f"{sub_sum:,.2f} 元差异 {sub_difference:+,.2f} 元，超出容差 {sub_tolerance:,.2f} 元，请核对"
                    ),
                    source_row_number=bill.source_row_number,
                )
            )
    elif bill.energy_charge_yuan is None and sub_sum is not None:
        messages.append(
            f"账单未提供『电度电费合计』，仅有二层分项合计 {sub_sum:,.2f} 元；"
            "无法核对两层口径，已在总额核对中说明替代口径"
        )

    # ---------------- 汇总状态 ---------------- #
    if any(i.level == "ERROR" for i in issues):
        status = BillQualityStatus.INVALID
    elif any(i.level == "WARNING" for i in issues):
        status = BillQualityStatus.WARNING
    else:
        status = BillQualityStatus.VALID

    for issue in issues:
        messages.append(f"[{issue.code}] {issue.message}")

    assumptions.append(
        "账单平均综合电价 P_avg = 账单总额 / 总购电量仅为**账单统计口径**，"
        "不可直接作为光伏自用电量的边际节省电价（V2.1 §3.1）"
    )

    # ---------------- V2.5 §5：账单数据分类口径的三条硬披露 ----------------
    # 这三条必须同时出现在**界面可见的中文说明**（经 quality_messages）与
    # **报告 assumptions** 中（规格书 §3.1 要求口径随结果披露，不得只在代码注释里写）。
    if bill.operation_fee_detail is not None:
        operation = bill.operation_fee_detail
        messages.append(
            "市场化运营费用（账单第 5 页 B 增加支出 / C 降低支出 / 虚拟电厂调峰 / 调频）"
            f"总合计 {('未提供' if operation.total_yuan is None else f'{operation.total_yuan:,.2f} 元')}："
            "**只作明细留档，不进入任何电价或电费计算口径**"
        )
        assumptions.append(
            "市场化运营费用（operation_fee_detail，第 5 页 B/C 类）**只留档，绝不进入电价或费用的"
            "计算口径**：这些是市场化交易结算项，**已包含在账单总电费里**，若同时计入本软件的"
            "电费/电价测算即构成**重复计算**。因此本软件的任何公式都不得消费该字段（V2.5 §5、§0.2）"
        )
    if bill.hourly_energy_tariff:
        assumptions.append(
            "消纳率电价的**首选来源**是账单「24 小时电量电价」表（逐时直接交易价格 + "
            "上网环节线损价格），按逐时电量加权取值；该户为**市场化直购客户**，其逐时交易价格"
            "才是真实的替代电价，**不得**套用湖北政府峰谷系数（尖峰 200% / 高峰 150% / 低谷 45%），"
            "该系数只适用于『代理购电』客户（V2.5 §5）"
        )
    if bill.meter_groups:
        assumptions.append(
            f"账单电量明细含 {len(bill.meter_groups)} 个计量分组（含定比分表），"
            "已随账单保存并用于核对『分时电量合计 = 各表计费电量之和』与来源追溯；"
            "分组明细**不单独参与电价计算**，示数/倍率/抄见电量/变损/线损/加减等中间过程量不入模型"
            "（V2.5 §5）"
        )

    return BillReconciliation(
        bill_id=bill.bill_id,
        billing_month=bill.billing_month,
        energy_total_kwh=total_energy,
        energy_period_sum_kwh=period_sum,
        energy_difference_kwh=energy_difference,
        energy_tolerance_kwh=tolerance_kwh,
        energy_consistent=energy_consistent,
        energy_partial=energy_partial,
        energy_missing_periods=[label_of(p.field_name) for p in missing_periods],
        amount_total_yuan=total_amount,
        amount_component_sum_yuan=component_sum,
        amount_difference_yuan=amount_difference,
        amount_tolerance_yuan=tolerance_yuan,
        amount_consistent=amount_consistent,
        amount_components_used=used_fields,
        energy_charge_declared_yuan=bill.energy_charge_yuan,
        energy_sub_sum_yuan=sub_sum,
        energy_sub_difference_yuan=sub_difference,
        energy_sub_consistent=sub_consistent,
        energy_sub_components_used=list(sub_components),
        quality_status=status,
        issues=issues,
        messages=messages,
        assumptions=assumptions,
    )


def apply_quality(
    bill: ElectricityBill, *, tolerance: BillTolerance | None = None
) -> ElectricityBill:
    """返回**带质量标记的账单副本**（不修改入参，也不改动任何金额/电量事实）。

    ``quality_status`` / ``quality_messages`` 是 §2.1 规定的字段，
    只描述"这条账单是否自洽"；差异数值本身留在
    :class:`~cenep.domain.bill_models.BillReconciliation` 中，不写回账单事实。
    """
    outcome = reconcile_bill(bill, tolerance=tolerance)
    updated = bill.model_copy(
        update={
            "quality_status": outcome.quality_status,
            "quality_messages": list(outcome.messages),
        }
    )
    logger.debug("账单质量标记：%s → %s", bill.bill_id, outcome.quality_status.label)
    return updated


# --------------------------------------------------------------------------- #
# §3.1 账单统计
# --------------------------------------------------------------------------- #
def average_comprehensive_price(
    total_amount_yuan: float | None, total_energy_kwh: float | None
) -> float | None:
    """账单平均综合电价 ``P_avg = C_bill / E_grid``（元/kWh，§3.1）。

    边界：购电量为 ``None`` 或 0 时返回 ``None``（**不返回 0**，避免把"不适用"显示成 0 元/kWh）；
    金额未提供时同样返回 ``None``。

    **口径警告（§3.1 原文）**：该指标仅作为账单统计，不可直接当成光伏自用电量的
    边际节省电价，因为固定基本电费、需量电费、税费和其他费用未必随购电量同比例变化。
    """
    if total_amount_yuan is None or total_energy_kwh is None:
        return None
    energy = float(total_energy_kwh)
    if energy <= 0.0:
        return None
    return float(total_amount_yuan) / energy


def _sum_provided(bills: list[ElectricityBill], field: str) -> tuple[float | None, int]:
    """对某字段求"已提供值之和"，返回 ``(和或 None, 未提供的条数)``。"""
    values = [float(getattr(b, field)) for b in bills if getattr(b, field) is not None]
    missing = len(bills) - len(values)
    return (float(sum(values)) if values else None), missing


def _has_overlap(bills: list[ElectricityBill]) -> bool:
    """账期是否重叠（含端点相同；§3.1：重叠时不允许直接相加）。"""
    ordered = sorted(bills, key=lambda b: (b.billing_period_start, b.billing_period_end))
    for previous, current in zip(ordered, ordered[1:]):
        if current.billing_period_start <= previous.billing_period_end:
            return True
    return False


def _is_natural_month(bill: ElectricityBill) -> bool:
    """账期长度是否与起始月自然月天数相符（§3.1 的"计费周期不一致"检测）。"""
    expected = calendar.monthrange(bill.billing_period_start.year, bill.billing_period_start.month)[1]
    return abs(bill.period_days - expected) <= NATURAL_MONTH_DAY_TOLERANCE


def _worst_status(statuses: list[BillQualityStatus]) -> BillQualityStatus:
    if BillQualityStatus.INVALID in statuses:
        return BillQualityStatus.INVALID
    if BillQualityStatus.WARNING in statuses:
        return BillQualityStatus.WARNING
    return BillQualityStatus.VALID


def monthly_summary(
    bills: list[ElectricityBill], *, tolerance: BillTolerance | None = None
) -> list[BillMonthlySummary]:
    """按 ``billing_month`` 聚合的月度汇总，供 UI 月度趋势与报告使用（§3.1、§5.1）。

    跨月账期按**起始月**归属，并在结果中标记 ``has_cross_month``（§2.1）。
    """
    grouped: dict[str, list[ElectricityBill]] = defaultdict(list)
    for bill in bills:
        grouped[bill.billing_month].append(bill)

    out: list[BillMonthlySummary] = []
    for month in sorted(grouped):
        group = grouped[month]
        energy, energy_missing = _sum_provided(group, "energy_total_kwh")
        amount, amount_missing = _sum_provided(group, "bill_total_yuan")
        cross = [b for b in group if b.is_cross_month]
        overlap = _has_overlap(group)
        messages: list[str] = []
        if cross:
            messages.append(
                f"该月含 {len(cross)} 条跨月账期账单（"
                + "、".join(f"{b.bill_id} {b.billing_period_start:%Y-%m-%d}~{b.billing_period_end:%Y-%m-%d}" for b in cross)
                + "），已按起始月归属，勿视为完整自然月"
            )
        if len(group) > 1:
            messages.append(f"该月有 {len(group)} 条账单记录，请确认是否为重复录入")
        if overlap:
            messages.append("该月存在账期重叠的账单，月度合计不可直接相加")
        if energy_missing:
            messages.append(f"该月有 {energy_missing} 条账单未提供总电量，合计仅包含已提供者")
        if amount_missing:
            messages.append(f"该月有 {amount_missing} 条账单未提供总额，合计仅包含已提供者")

        out.append(
            BillMonthlySummary(
                billing_month=month,
                bill_count=len(group),
                bill_ids=[b.bill_id for b in group],
                energy_total_kwh=energy,
                amount_total_yuan=amount,
                average_price_yuan_per_kwh=average_comprehensive_price(amount, energy),
                energy_missing_bills=energy_missing,
                amount_missing_bills=amount_missing,
                has_cross_month=bool(cross),
                has_overlap=overlap,
                quality_status=_worst_status([b.quality_status for b in group]),
                messages=messages,
            )
        )
    return out


def annual_bill_summary(
    bills: list[ElectricityBill],
    *,
    year: int | None = None,
    tolerance: BillTolerance | None = None,
) -> BillAnnualSummary:
    """年度账单基准数据（V2.1 §3.1）。

    ``E_load,annual = Σ_m E_bill,m`` **仅在每月账单周期完整且不重叠时**才允许直接相加：
    缺月、重叠、跨月或计费周期非自然月都会输出覆盖率与警告，
    并把 ``can_sum_directly`` 置为 ``False``。

    :param year: 统计年度（按账期起始日归属）；``None`` 时取账单中最常见的起始年份，
        没有账单时取当前年。
    """
    tol = tolerance or BillTolerance()
    if year is None:
        years: dict[int, int] = defaultdict(int)
        for bill in bills:
            years[bill.billing_period_start.year] += 1
        year = max(years, key=lambda y: years[y]) if years else date.today().year

    selected = [b for b in bills if b.billing_period_start.year == year]
    monthly = monthly_summary(selected, tolerance=tol)

    expected_months = [f"{year}-{m:02d}" for m in range(1, 13)]
    covered = [s.billing_month for s in monthly]
    missing = [m for m in expected_months if m not in set(covered)]
    duplicates = [s.billing_month for s in monthly if s.bill_count > 1]
    cross = [b.bill_id for b in selected if b.is_cross_month]
    non_natural = [b.bill_id for b in selected if not _is_natural_month(b)]
    overlap = _has_overlap(selected)

    energy, energy_missing = _sum_provided(selected, "energy_total_kwh")
    amount, amount_missing = _sum_provided(selected, "bill_total_yuan")
    coverage = len(covered) / 12.0
    can_sum = coverage >= 1.0 and not duplicates and not overlap

    messages: list[str] = [
        f"{year} 年账单月份覆盖率 {coverage * 100:.1f}%（{len(covered)}/12），"
        f"覆盖 {len(selected)} 条账单"
    ]
    if missing:
        messages.append("缺失月份：" + "、".join(missing) + "；年度合计不是完整年度实测值，不得声称完整年度")
    if duplicates:
        messages.append("同月多条账单：" + "、".join(duplicates) + "（疑似重复录入或分次结算）")
    if overlap:
        messages.append("存在账期重叠的账单，年度电量与电费不可直接相加")
    if cross:
        messages.append(f"含跨月账期账单 {len(cross)} 条：" + "、".join(cross[:5]) + ("…" if len(cross) > 5 else ""))
    if non_natural:
        messages.append(
            f"含计费周期非自然月的账单 {len(non_natural)} 条：" + "、".join(non_natural[:5])
            + ("…" if len(non_natural) > 5 else "")
        )
    if energy_missing:
        messages.append(f"有 {energy_missing} 条账单未提供总电量，年度电量合计仅包含已提供者")
    if amount_missing:
        messages.append(f"有 {amount_missing} 条账单未提供总额，年度电费合计仅包含已提供者")

    assumptions = [
        "年度总用电量 E_load,annual = Σ E_bill,m 仅在每月账单周期完整且不重叠时成立（V2.1 §3.1）",
        "账单平均综合电价 P_avg = 年度账单总额 / 年度总购电量，仅为账单统计口径，"
        "不等于光伏自用量的边际节省电价（V2.1 §3.1）",
        "账单未提供的分项按『未知』处理，不按 0 计入任何合计（V2.1 §2.1）",
    ]
    if not can_sum:
        assumptions.append(
            "本次统计不满足『周期完整且不重叠』条件，年度合计按覆盖率折算或仅用于参考，"
            "不得作为完整年度基准账单（V2.1 §3.1）"
        )

    logger.info(
        "年度账单汇总：%d 年，%d 条账单，覆盖率 %.1f%%，可直接相加=%s",
        year,
        len(selected),
        coverage * 100,
        can_sum,
    )
    return BillAnnualSummary(
        year=year,
        months_covered=covered,
        missing_months=missing,
        duplicate_months=duplicates,
        cross_month_bills=cross,
        non_natural_month_bills=non_natural,
        coverage_ratio=coverage,
        total_energy_kwh=energy,
        total_amount_yuan=amount,
        average_price_yuan_per_kwh=average_comprehensive_price(amount, energy),
        can_sum_directly=can_sum,
        monthly=monthly,
        messages=messages,
        assumptions=assumptions,
    )
