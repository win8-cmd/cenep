"""版本化电价计划的时段匹配、覆盖/冲突检测与价格校验（V2.3 §4.2、§7.3、§7.7）。

本模块是**电价时段与价格的唯一公式来源**（§0.2：核心公式在 ``calculation/``，UI 不得实现计算）。

口径总览
--------
===============================  ==========================================================================
公式 / 检查                       口径与出处
===============================  ==========================================================================
时段解析                          ``priority`` 大者优先；相同优先级时**后声明的规则覆盖先声明的**（与既有
                                  :mod:`cenep.calculation.tariff_series` 语义一致）
基础电价                          基础电价 = Σ 参与峰谷浮动的分项（``adjustable_by_tou=True``）
固定分项                          固定分项 = Σ 不参与浮动但已含在电度电价里的分项
分时电价（浮动口径）                ``P_period = 基础电价 × price_multiplier + 固定分项``
分时电价（直接单价口径）            ``P_period = direct_price_yuan_per_kwh``（官方价格表的绝对值，**权威**）
24 小时覆盖检测                   按 ``slot_minutes``（默认 30 分钟 = 48 槽）逐月检测"缺口 / 重叠 / 是否覆盖 24 小时"
===============================  ==========================================================================

**为什么以"直接单价"为权威**：官方价格表直接公布每个时段的绝对电价（元/千瓦时）。
浮动系数是**由绝对电价反算**出来的口径，且实测存在 ±0.002 元/kWh 的内部不一致
（尖峰 2.0、低谷 0.45 完全吻合，高峰 ≈1.49~1.50 不完全吻合）。因此：
直接单价用于计费；浮动系数只作**参考值与交叉校验**，任何不一致都作为 ``T10`` 问题
写进校验结论与报告，**不得**为了让公式对上而改动官方公布的绝对电价。

模块内所有异常与问题清单均为**中文**（§0.2）。
"""

from __future__ import annotations

import logging
from datetime import date

from ..domain.enums import (
    PriceBasis,
    TariffComponentUnit,
    TariffPeriod,
    TariffPlanStatus,
    TariffStructure,
)
from ..domain.tariff_models import (
    DEFAULT_SLOT_MINUTES,
    MINUTES_PER_DAY,
    TariffPlan,
    TariffPlanComparison,
    TariffPlanDiffItem,
    TariffPlanIssue,
    TariffPlanValidation,
    TariffTimePeriodRule,
    TimePeriodCoverage,
    format_hhmm,
)
from .errors import ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "PRICE_CONSISTENCY_TOLERANCE_YUAN_PER_KWH",
    "base_price_yuan_per_kwh",
    "compare_tariff_plans",
    "compose_period_price",
    "effective_period_prices",
    "fixed_price_yuan_per_kwh",
    "hourly_price_series",
    "implied_multiplier_from_prices",
    "month_coverage",
    "period_hours_of",
    "period_rule_text",
    "planned_period_hours",
    "require_formal_use",
    "resolve_slot_period",
    "select_tariff_plan",
    "validate_tariff_plan",
    "voltage_level_applies",
]

#: 直接单价与"基础电价 × 浮动系数"反算值的允许偏差（元/kWh）。
#:
#: 取 5e-4：远小于官方价格表内部一致性水平（实测最大 1.982e-3），足以把真实的口径差异
#: 暴露出来，又不会被浮点误差误报。
PRICE_CONSISTENCY_TOLERANCE_YUAN_PER_KWH = 5e-4


# --------------------------------------------------------------------------- #
# 基础电价 / 固定分项（§4.2、§2.3）
# --------------------------------------------------------------------------- #
def _components_total(plan: TariffPlan, *, adjustable: bool) -> tuple[float | None, list[str]]:
    """按"是否参与峰谷浮动"合计电价分项（元/kWh）。

    :return: ``(合计 元/kWh 或 None, 参与合计的分项名称)``；没有任何可用分项时返回 ``(None, [])``。
    """
    total = 0.0
    names: list[str] = []
    for component in plan.price_components:
        if component.unit is not TariffComponentUnit.YUAN_PER_KWH:
            continue
        if not component.included_in_tou_price:
            continue
        if bool(component.adjustable_by_tou) != bool(adjustable):
            continue
        if component.value is None:
            continue
        total += float(component.value)
        names.append(component.name)
    if not names:
        return None, []
    return total, names


def base_price_yuan_per_kwh(plan: TariffPlan) -> float | None:
    """**基础电价**（元/kWh）= Σ 参与峰谷浮动的分项（§4.2 的浮动基础）。

    湖北官方口径下，基础电价 = 代理购电价 + 上网环节线损费用折价；输配电价、系统运行费折价、
    政府性基金及附加**不参与**浮动（见 :mod:`cenep.policy.hubei_commercial`）。
    无可用分项时返回 ``None``，绝不返回 0（否则会造出"0 元电价"这种假数据，§0.2）。
    """
    total, _ = _components_total(plan, adjustable=True)
    return total


def fixed_price_yuan_per_kwh(plan: TariffPlan) -> float | None:
    """**固定分项**（元/kWh）= Σ 不参与浮动但已含在电度电价里的分项。"""
    total, _ = _components_total(plan, adjustable=False)
    return total


def implied_multiplier_from_prices(
    *, period_price: float, flat_price: float, fixed_price: float
) -> float | None:
    """由"平段电价 + 固定分项 + 某时段绝对电价"反算**隐含浮动系数**（仅作参考值）。

    ``基础电价 = 平段电价 − 固定分项``；``系数 = (某时段电价 − 固定分项) / 基础电价``。

    这是本模块提供给 :mod:`cenep.policy.hubei_commercial` 的唯一反算入口——政策数据模块
    只存放**官方公布的绝对电价**，反算出来的系数一律由本函数产生并标记为参考值
    （§0.2：核心公式在 ``calculation/``，政策模块不得各写一套公式）。

    :return: 系数；基础电价非正或入参为 ``None`` 时返回 ``None``。
    """
    base = float(flat_price) - float(fixed_price)
    if base <= 0.0:
        return None
    return (float(period_price) - float(fixed_price)) / base


def compose_period_price(
    *, base_price_yuan_per_kwh: float, price_multiplier: float, fixed_price_yuan_per_kwh: float
) -> float:
    """浮动口径合成某时段的电度电价：``P = 基础电价 × 浮动系数 + 固定分项``（元/kWh）。

    这是"基础电价 × 浮动倍率"两种配置方式的**唯一实现处**（§4.2）：
    :func:`_rule_price`、服务层的"按用户账单反算电价"、市场化直购变体全部调用本函数，
    避免同一公式在多处各写一遍（§0.2）。
    """
    return (
        float(base_price_yuan_per_kwh) * float(price_multiplier) + float(fixed_price_yuan_per_kwh)
    )


def _rule_price(plan: TariffPlan, rule: TariffTimePeriodRule) -> float | None:
    """按规则的 ``price_basis`` 取该时段的**最终**电度电价（元/kWh）。

    * ``DIRECT_PRICE``：直接取官方公布的绝对电价；缺失时返回 ``None``（不得回退成 0）；
    * ``MULTIPLIER``：``基础电价 × 浮动系数 + 固定分项``；基础电价或系数缺失时返回 ``None``。
    """
    if rule.price_basis is PriceBasis.DIRECT_PRICE:
        if rule.direct_price_yuan_per_kwh is None:
            return None
        return float(rule.direct_price_yuan_per_kwh)
    if rule.price_multiplier is None:
        return None
    base = base_price_yuan_per_kwh(plan)
    fixed = fixed_price_yuan_per_kwh(plan)
    if base is None or fixed is None:
        return None
    return compose_period_price(
        base_price_yuan_per_kwh=base,
        price_multiplier=float(rule.price_multiplier),
        fixed_price_yuan_per_kwh=fixed,
    )


def effective_period_prices(plan: TariffPlan) -> dict[TariffPeriod, float | None]:
    """每个时段的**最终**电度电价（元/kWh），键为 :class:`TariffPeriod`。

    同一时段被多条规则覆盖时，取 ``priority`` 最大、声明最靠后的那条（与时段解析同口径）。
    """
    prices: dict[TariffPeriod, float | None] = {}
    indexed = list(enumerate(plan.time_period_rules))
    for _, rule in sorted(indexed, key=lambda item: (item[1].priority, item[0])):
        prices[rule.period] = _rule_price(plan, rule)
    return prices


# --------------------------------------------------------------------------- #
# 时段解析（§4.2：月份范围、日类型、跨午夜、优先级）
# --------------------------------------------------------------------------- #
def _applicable_rules(
    plan: TariffPlan, month: int, minute: int, day_type=None
) -> list[TariffTimePeriodRule]:
    """给定"月份 + 当日第几分钟"（可选日类型）命中的全部规则，按声明顺序。"""
    return [
        rule
        for rule in plan.time_period_rules
        if rule.applies_on(month, day_type) and rule.covers_minute(minute)
    ]


def resolve_slot_period(
    plan: TariffPlan, month: int, slot_index: int, *, slot_minutes: int = DEFAULT_SLOT_MINUTES, day_type=None
) -> TariffPeriod | None:
    """某个时间槽命中的时段；没有任何规则命中时返回 ``None``（**不兜底成平段**）。

    为什么不像 :mod:`cenep.calculation.tariff_series` 那样用平段兜底：账单复算是**正式计费口径**，
    未定义时段必须阻断并让用户补齐（§4.2、§7.7），不能静默按平段计价。
    （旧的 ``tariff_series`` 兜底行为保持不变，供 V2 既有测算路径使用。）
    """
    minute = int(slot_index) * int(slot_minutes)
    matched = _applicable_rules(plan, month, minute, day_type)
    if not matched:
        return None
    best = max(range(len(matched)), key=lambda index: (matched[index].priority, index))
    return matched[best].period


def month_coverage(
    plan: TariffPlan, month: int, *, slot_minutes: int = DEFAULT_SLOT_MINUTES, day_type=None
) -> TimePeriodCoverage:
    """逐槽扫描一个月，产出缺口 / 重叠 / 各时段小时数（§4.2 中文报告）。"""
    slots = MINUTES_PER_DAY // int(slot_minutes)
    resolved: list[TariffPeriod | None] = []
    conflict_slots = 0
    overlap_texts: list[str] = []

    for slot in range(slots):
        minute = slot * int(slot_minutes)
        matched = _applicable_rules(plan, month, minute, day_type)
        if not matched:
            resolved.append(None)
            continue
        best = max(range(len(matched)), key=lambda index: (matched[index].priority, index))
        resolved.append(matched[best].period)
        distinct = {rule.period for rule in matched}
        if len(distinct) > 1:
            conflict_slots += 1
            slot_text = format_hhmm(minute)
            if slot_text not in overlap_texts:
                rules_text = "、".join(
                    f"{rule.period.label}({rule.months_text()} {rule.time_range_text()})" for rule in matched
                )
                overlap_texts.append(f"{slot_text} 起：{rules_text}")

    period_hours: dict[str, float] = {}
    covered = 0
    for period in resolved:
        if period is None:
            continue
        covered += 1
        period_hours[period.value] = period_hours.get(period.value, 0.0) + slot_minutes / 60.0

    # 连续未覆盖槽位合并成"缺口区间"，方便用户直接对照官方文件补时段
    gaps: list[str] = []
    gap_hours = 0.0
    start: int | None = None
    for slot, period in enumerate(resolved):
        if period is None:
            gap_hours += slot_minutes / 60.0
            if start is None:
                start = slot
        elif start is not None:
            gaps.append(
                f"{format_hhmm(start * slot_minutes)}-{format_hhmm(slot * slot_minutes)}"
                f"（{(slot - start) * slot_minutes / 60.0:g} 小时）"
            )
            start = None
    if start is not None:
        gaps.append(
            f"{format_hhmm(start * slot_minutes)}-24:00（{(slots - start) * slot_minutes / 60.0:g} 小时）"
        )

    messages: list[str] = []
    if not plan.time_period_rules:
        messages.append(f"{month} 月：该电价计划未声明任何分时时段规则，24 小时全部无定义")
    else:
        messages.append(
            f"{month} 月：已覆盖 {covered * slot_minutes / 60.0:g} / 24 小时"
            + ("（完整）" if not gaps else f"，**缺 {gap_hours:g} 小时**")
        )
    if gaps:
        messages.append(f"{month} 月未定义时段：" + "、".join(gaps) + "；请对照政策文件补齐，不得按平段兜底计价")
    if conflict_slots:
        messages.append(
            f"{month} 月存在 {conflict_slots} 个 {slot_minutes} 分钟槽位落在多条不同时段的规则上，"
            "已按『优先级高者优先、同级后声明者覆盖』解析；请确认是否符合政策原文"
        )

    return TimePeriodCoverage(
        month=int(month),
        slot_minutes=int(slot_minutes),
        period_hours=period_hours,
        covered_hours=covered * slot_minutes / 60.0,
        gaps=gaps,
        gap_hours=gap_hours,
        overlaps=overlap_texts,
        conflict_slots=conflict_slots,
        messages=messages,
    )


def planned_period_hours(plan: TariffPlan, month: int) -> dict[str, float]:
    """:func:`month_coverage` 的便捷封装：仅取"各时段合计小时数"。"""
    return dict(month_coverage(plan, month).period_hours)


def period_hours_of(plan: TariffPlan) -> dict[str, float]:
    """按**月份分组**给出各时段小时数，键形如 ``"7-8月"`` / ``"其他月份"``。

    分组依据是"各月的**时段划分（含起止时刻）**是否完全相同"，而不是只看小时数——
    否则"7、8 月与其他月份尖峰时刻不同但小时数相同"会被错误地合并成"全年"（§4.2 要求表达分月规则）。
    """
    per_month = {month: planned_period_hours(plan, month) for month in range(1, 13)}
    signature_of = {month: _month_signature(plan, month) for month in range(1, 13)}
    groups: dict[tuple, list[int]] = {}
    for month in range(1, 13):
        groups.setdefault(signature_of[month], []).append(month)
    out: dict[str, float] = {}
    for months in groups.values():
        out[_months_key(months)] = per_month[months[0]]
    return out


def _month_signature(plan: TariffPlan, month: int, *, slot_minutes: int = DEFAULT_SLOT_MINUTES) -> tuple:
    """某月"时段划分"的规范化签名：``((period_value, start, end), …)``，用于按月分组。"""
    slots = MINUTES_PER_DAY // int(slot_minutes)
    runs: list[tuple[str, str, str]] = []
    start: int | None = None
    current: TariffPeriod | None = None
    for slot in range(slots + 1):
        period = None if slot >= slots else resolve_slot_period(plan, month, slot, slot_minutes=slot_minutes)
        if period is not current:
            if current is not None and start is not None:
                runs.append((current.value, format_hhmm(start * slot_minutes), format_hhmm(slot * slot_minutes)))
            start = slot
            current = period
    return tuple(sorted(runs))


def _months_key(months: list[int]) -> str:
    ordered = sorted(months)
    if ordered == list(range(1, 13)):
        return "全年"
    if ordered == [7, 8]:
        return "7-8月"
    if ordered == [1, 2, 3, 4, 5, 6, 9, 10, 11, 12]:
        return "其他月份（1-6、9-12 月）"
    return "、".join(f"{m}月" for m in ordered)


# --------------------------------------------------------------------------- #
# 时段规则文本与计划对比（§4.2、§7.3）
# --------------------------------------------------------------------------- #
def period_rule_text(plan: TariffPlan) -> dict[str, list[str]]:
    """``{时段中文名: [『月份 时段（共 X 小时）』, …]}``，用于逐项对比与报告。"""
    out: dict[str, list[str]] = {}
    for period in TariffPeriod:
        texts = [f"{rule.months_text()} {rule.time_range_text()}" for rule in plan.time_period_rules if rule.period is period]
        if texts:
            out[period.label] = texts
    return out


def compare_tariff_plans(plan_a: TariffPlan, plan_b: TariffPlan) -> TariffPlanComparison:
    """逐项比较两套电价计划（§4.2、§7.3）。

    本项目明确要求官方版与项目资料版**并存**；本函数把差异逐条列出，供用户选择、核对、覆盖。
    """
    items: list[TariffPlanDiffItem] = []
    rules_a = period_rule_text(plan_a)
    rules_b = period_rule_text(plan_b)

    for period in TariffPeriod:
        text_a = "；".join(rules_a.get(period.label, [])) or "未定义"
        text_b = "；".join(rules_b.get(period.label, [])) or "未定义"
        if text_a != text_b:
            items.append(
                TariffPlanDiffItem(
                    category="时段",
                    item=f"{period.label}时段",
                    value_a=text_a,
                    value_b=text_b,
                    difference=_text_difference(text_a, text_b),
                )
            )
    same_time_periods = not any(item.category == "时段" for item in items)

    multipliers_a = _multiplier_map(plan_a)
    multipliers_b = _multiplier_map(plan_b)
    for period in TariffPeriod:
        value_a = multipliers_a.get(period)
        value_b = multipliers_b.get(period)
        if value_a is None and value_b is None:
            continue
        if value_a != value_b:
            items.append(
                TariffPlanDiffItem(
                    category="浮动系数",
                    item=f"{period.label}浮动系数",
                    value_a=_pct(value_a),
                    value_b=_pct(value_b),
                    difference=_pct_difference(value_a, value_b),
                )
            )
    same_multipliers = not any(item.category == "浮动系数" for item in items)

    prices_a = effective_period_prices(plan_a)
    prices_b = effective_period_prices(plan_b)
    for period in TariffPeriod:
        value_a = prices_a.get(period)
        value_b = prices_b.get(period)
        if value_a is None and value_b is None:
            continue
        if value_a != value_b:
            items.append(
                TariffPlanDiffItem(
                    category="电度电价",
                    item=f"{period.label}电度电价（元/千瓦时）",
                    value_a=_price(value_a),
                    value_b=_price(value_b),
                    difference=_price_difference(value_a, value_b),
                )
            )

    for label, value_a, value_b in (
        ("最大需量电价（元/千瓦·月）", plan_a.demand_charge_yuan_per_kw_month, plan_b.demand_charge_yuan_per_kw_month),
        ("变压器容量电价（元/千伏安·月）", plan_a.capacity_charge_yuan_per_kva_month, plan_b.capacity_charge_yuan_per_kva_month),
    ):
        if value_a != value_b:
            items.append(
                TariffPlanDiffItem(
                    category="容需量电价",
                    item=label,
                    value_a=_num(value_a),
                    value_b=_num(value_b),
                    difference=_num_difference(value_a, value_b),
                )
            )

    for label, value_a, value_b in (
        ("适用电压等级", "、".join(plan_a.applicable_voltage_levels) or "未限定",
         "、".join(plan_b.applicable_voltage_levels) or "未限定"),
        ("购电模式", plan_a.market_mode.label, plan_b.market_mode.label),
        ("基本电费计费方式", plan_a.demand_billing_mode.label, plan_b.demand_billing_mode.label),
        ("来源", plan_a.source_text, plan_b.source_text),
        ("政策文号", plan_a.source_document_number or "未提供", plan_b.source_document_number or "未提供"),
        ("核验状态", plan_a.status.label, plan_b.status.label),
    ):
        if value_a != value_b:
            items.append(
                TariffPlanDiffItem(
                    category="适用范围" if "适用" in label or "模式" in label or "计费" in label else "来源",
                    item=label,
                    value_a=str(value_a),
                    value_b=str(value_b),
                    difference=f"计划 A：{value_a}；计划 B：{value_b}",
                )
            )

    messages = [
        f"计划 A（{plan_a.name}）与计划 B（{plan_b.name}）共 {len(items)} 项差异；"
        f"时段划分{'一致' if same_time_periods else '不一致'}，"
        f"浮动系数{'一致' if same_multipliers else '不一致'}。",
        "两套计划必须并存：软件不得只保留一套，也不得用其中一套静默替换另一套（用户明确要求）。",
    ]
    assumptions = [
        "『浮动系数』口径 = 分时电价相对『基础电价』的倍数；基础电价口径见各计划的 base_price_definition。",
        "时段划分差异比系数差异影响更大：同一小时在两套计划下可能属于不同时段（例如 12:00-14:00 "
        "官方为低谷、部分项目资料为高峰）。",
    ]
    return TariffPlanComparison(
        plan_a_id=plan_a.tariff_plan_id,
        plan_a_name=plan_a.name,
        plan_b_id=plan_b.tariff_plan_id,
        plan_b_name=plan_b.name,
        items=items,
        same_time_periods=same_time_periods,
        same_float_multipliers=same_multipliers,
        messages=messages,
        assumptions=assumptions,
    )


def _multiplier_map(plan: TariffPlan) -> dict[TariffPeriod, float | None]:
    out: dict[TariffPeriod, float | None] = {}
    for rule in plan.time_period_rules:
        if rule.price_multiplier is not None:
            out.setdefault(rule.period, float(rule.price_multiplier))
    return out


def _text_difference(text_a: str, text_b: str) -> str:
    return f"计划 A：{text_a}｜计划 B：{text_b}"


def _pct(value: float | None) -> str:
    return "未提供" if value is None else f"{float(value) * 100:.0f}%"


def _pct_difference(value_a: float | None, value_b: float | None) -> str:
    if value_a is None or value_b is None:
        return "一方未提供浮动系数，无法比较"
    return (
        f"计划 A 比计划 B 高 {(float(value_a) - float(value_b)) * 100:+.0f} 个百分点"
        f"（{float(value_a) * 100:.0f}% vs {float(value_b) * 100:.0f}%）"
    )


def _price(value: float | None) -> str:
    return "未提供" if value is None else f"{float(value):.6f}"


def _price_difference(value_a: float | None, value_b: float | None) -> str:
    if value_a is None or value_b is None:
        return "一方未提供电价，无法比较"
    diff = float(value_a) - float(value_b)
    rate = diff / float(value_b) if float(value_b) else 0.0
    return f"计划 A − 计划 B = {diff:+.6f} 元/千瓦时（{rate:+.2%}）"


def _num(value: float | None) -> str:
    return "未提供" if value is None else f"{float(value):g}"


def _num_difference(value_a: float | None, value_b: float | None) -> str:
    if value_a is None or value_b is None:
        return "一方未提供数值，无法比较"
    return f"计划 A − 计划 B = {float(value_a) - float(value_b):+g}"


# --------------------------------------------------------------------------- #
# 价格校验（§4.2、§7.4）
# --------------------------------------------------------------------------- #
def _price_consistency_issues(plan: TariffPlan) -> list[TariffPlanIssue]:
    """直接单价 vs "基础电价 × 浮动系数" 的交叉校验（§2.3 关键约束）。

    两者不一致时**不改动直接单价**，只输出 ``T10`` 告警并披露差额——因为直接单价来自官方
    价格表（有来源、可追溯），而浮动系数是反算口径（无独立来源）。
    """
    issues: list[TariffPlanIssue] = []
    base = base_price_yuan_per_kwh(plan)
    fixed = fixed_price_yuan_per_kwh(plan)
    if base is None or fixed is None:
        return issues
    # 同一时段可能有多条规则（例如高峰分"7-8 月"与"其他月份"两段），只报一次并说明条数
    seen: dict[TariffPeriod, list[tuple[float, float, int]]] = {}
    for rule in plan.time_period_rules:
        if rule.direct_price_yuan_per_kwh is None or rule.price_multiplier is None:
            continue
        derived = base * float(rule.price_multiplier) + fixed
        diff = float(rule.direct_price_yuan_per_kwh) - derived
        seen.setdefault(rule.period, []).append(
            (float(rule.direct_price_yuan_per_kwh), diff, int(rule.price_multiplier * 100))
        )
    for period, entries in seen.items():
        worst = max(entries, key=lambda item: abs(item[1]))
        if abs(worst[1]) <= PRICE_CONSISTENCY_TOLERANCE_YUAN_PER_KWH:
            continue
        multiplier = worst[2] / 100
        derived = base * multiplier + fixed
        issues.append(
            TariffPlanIssue(
                level="WARNING",
                code="T10",
                field="time_period_rules",
                message=(
                    f"{period.label}时段（{len(entries)} 条规则）：官方公布直接单价 {worst[0]:.6f} 元/千瓦时，"
                    f"而按『基础电价 {base:.6f} × {multiplier:g} + 固定分项 {fixed:.6f}』"
                    f"反算为 {derived:.6f} 元/千瓦时，差 {worst[1]:+.6f} 元/千瓦时。"
                    "计费以**直接单价**为准（有来源、可追溯）；该差额属口径待确认，已在报告中披露，"
                    "不得为对上公式而改动官方公布的绝对电价"
                ),
                blocked=False,
            )
        )
    return issues


def validate_tariff_plan(
    plan: TariffPlan,
    *,
    as_of: date | None = None,
    require_verified: bool = True,
    months: list[int] | None = None,
    slot_minutes: int = DEFAULT_SLOT_MINUTES,
) -> TariffPlanValidation:
    """电价计划的完整校验：结构、覆盖、价格、来源、有效期（V2.3 §4.2、§7.4、§7.7）。

    规则编号（写在 ``code`` 与消息内，便于测试断言与报告检索）::

        T01 未声明任何分时时段规则                        ERROR（阻断）
        T02 存在未覆盖时段（不满足 24 小时覆盖）           ERROR（阻断）
        T03 存在同时段冲突（同一槽位落在不同时段规则上）     ERROR（阻断）
        T04 某已声明时段缺少价格                          ERROR（阻断）
        T05 计划未核验（status != verified）              ERROR（require_verified=True）/ INFO
        T06 基准日超出计划失效日期（计划已过期）            ERROR（阻断）
        T07 缺少来源（文件名 / 链接 / 文号全缺）            WARNING
        T08 未声明适用范围（电压等级 / 计费方式）           WARNING
        T09 采用浮动系数口径但缺少参与浮动的分项            ERROR（阻断）
        T10 直接单价与浮动系数反算值不一致                  WARNING（口径待确认）
        T11 两部制但未提供容量电价与需量电价                WARNING
        T12 计划被标记为示例数据                            WARNING（不得用于正式结论）
        T13 含按日类型（工作日/周末）区分的规则，覆盖检测按全年同一时段表口径 INFO

    :param as_of: 校验基准日；默认取 ``effective_from``（即"该版本自身是否自洽"）。
        传入账单账期日期即为"该计划能否用于这次复算"。
    :param require_verified: 是否要求计划必须是"已核验"才可用于正式复算（§4.2、§7.4）。
    """
    day = as_of or plan.effective_from
    issues: list[TariffPlanIssue] = []
    messages: list[str] = []
    assumptions: list[str] = []

    def add(level: str, code: str, field: str, message: str, *, blocked: bool) -> None:
        issues.append(
            TariffPlanIssue(level=level, code=code, field=field, message=message, blocked=blocked)
        )

    # ---- T01 时段规则 ----
    if not plan.time_period_rules:
        add(
            "ERROR",
            "T01",
            "time_period_rules",
            "该电价计划未声明任何分时时段规则，无法解析任何一小时属于尖峰/高峰/平段/低谷，"
            "不能用于账单复算；请补录官方时段划分（如『鄂发改价管〔2024〕77 号』的时段规定）",
            blocked=True,
        )

    # ---- T02/T03 覆盖与冲突（逐月检测）----
    target_months = months or list(range(1, 13))
    coverage = [month_coverage(plan, month, slot_minutes=slot_minutes) for month in target_months]
    for item in coverage:
        messages.extend(item.messages)
        if item.gaps:
            add(
                "ERROR",
                "T02",
                "time_period_rules",
                f"{item.month} 月共缺 {item.gap_hours:g} 小时未定义时段（"
                + "、".join(item.gaps)
                + "）；未定义时段不得按平段兜底计价（§4.2、§7.7）",
                blocked=True,
            )
        if item.overlaps:
            add(
                "ERROR",
                "T03",
                "time_period_rules",
                f"{item.month} 月存在同时段冲突：" + "；".join(item.overlaps),
                blocked=True,
            )

    # ---- T04 价格完整性 ----
    missing_price: list[str] = []
    for rule in plan.time_period_rules:
        if _rule_price(plan, rule) is None:
            missing_price.append(f"{rule.months_text()} {rule.period.label}（{rule.time_range_text()}）")
    if missing_price:
        add(
            "ERROR",
            "T04",
            "time_period_rules",
            "以下时段缺少价格（直接单价与浮动系数均未填写）：" + "、".join(missing_price)
            + "。价格缺失时禁止正式账单模拟，仅允许保存草稿（§4.2）",
            blocked=True,
        )

    # ---- T09 浮动系数口径缺分项 ----
    if any(rule.price_basis is PriceBasis.MULTIPLIER for rule in plan.time_period_rules) and (
        base_price_yuan_per_kwh(plan) is None or fixed_price_yuan_per_kwh(plan) is None
    ):
        add(
            "ERROR",
            "T09",
            "price_components",
            "计划采用『基础电价 × 浮动系数』口径，但未提供完整的价格组成分项"
            "（需要至少一个参与峰谷浮动的分项与一个固定分项），无法推导分时电价",
            blocked=True,
        )

    # ---- T05 核验状态 ----
    if plan.status is not TariffPlanStatus.VERIFIED:
        add(
            "ERROR" if require_verified else "INFO",
            "T05",
            "status",
            f"该电价计划当前状态为『{plan.status.label}』；"
            + (
                "未核验的计划不得用于正式账单复算，只能保存草稿（§4.2、§7.4）"
                if require_verified
                else "本次校验不要求已核验，仅作提示"
            ),
            blocked=bool(require_verified),
        )

    # ---- T06 有效期 ----
    if plan.is_expired_on(day):
        add(
            "ERROR",
            "T06",
            "effective_to",
            f"基准日 {day.isoformat()} 已超出该电价计划失效日期 "
            f"{plan.effective_to.isoformat() if plan.effective_to else '未标注'}，计划已过期，"
            "不得用于新测算；如需复现历史结果请显式选择该版本并在报告中披露",
            blocked=True,
        )
    elif day < plan.effective_from:
        add(
            "ERROR",
            "T06",
            "effective_from",
            f"基准日 {day.isoformat()} 早于该电价计划生效日期 {plan.effective_from.isoformat()}，"
            "该版本尚未生效，请选择正确的电价版本",
            blocked=True,
        )

    # ---- T07 来源 ----
    if not (plan.source_name or plan.source_url or plan.source_document_number):
        add(
            "WARNING",
            "T07",
            "source_name",
            "该电价计划未提供任何来源（文件名 / 链接 / 文号）。没有来源的电价一律标『待确认』，"
            "不得在报告中写成『湖北现行标准』（§0.2 红线、§4.1）",
            blocked=False,
        )

    # ---- T08 适用范围 ----
    if not plan.declares_voltage_level or not plan.applicable_tariff_structures:
        add(
            "WARNING",
            "T08",
            "applicable_voltage_levels",
            "该电价计划未同时声明『适用电压等级』与『适用计费方式（单一制/两部制）』；"
            "不允许仅凭『湖北省』就断定用户必然执行某一种电价（§4.1 第 6 条）",
            blocked=False,
        )

    # ---- T10 价格口径一致性 ----
    issues.extend(_price_consistency_issues(plan))

    # ---- T11 两部制容需量 ----
    is_two_part = any(str(item).startswith("two_part") for item in plan.applicable_tariff_structures)
    if is_two_part and plan.demand_charge_yuan_per_kw_month is None and plan.capacity_charge_yuan_per_kva_month is None:
        add(
            "WARNING",
            "T11",
            "demand_charge_yuan_per_kw_month",
            "该计划适用于两部制，但既未提供最大需量电价（元/千瓦·月）也未提供变压器容量电价"
            "（元/千伏安·月），基本电费无法复算（§3.4、§7.5）",
            blocked=False,
        )

    # ---- T12 示例数据 ----
    if plan.is_example:
        add(
            "WARNING",
            "T12",
            "is_example",
            "该电价计划被标记为**示例数据**，不得作为真实电价用于正式结论（§0.2 红线）",
            blocked=False,
        )

    # ---- T13 日类型规则提示 ----
    if any(rule.day_types for rule in plan.time_period_rules):
        add(
            "INFO",
            "T13",
            "time_period_rules",
            "该计划含按工作日/周末区分的时段规则；24 小时覆盖检测按『全年同一时段表』口径执行，"
            "实际按月/日类型匹配时请为每个月单独复核",
            blocked=False,
        )

    blocked = any(issue.blocked for issue in issues)
    usable = not blocked
    if usable:
        messages.append(
            f"电价计划『{plan.name}』校验通过：{len(plan.time_period_rules)} 条时段规则、"
            f"{len(plan.price_components)} 个价格分项，{len(target_months)} 个月均满足 24 小时覆盖且无冲突，"
            "可用于正式账单复算"
        )
    else:
        blockers = [issue for issue in issues if issue.blocked]
        messages.append(
            f"电价计划『{plan.name}』**未通过校验**，共 {len(blockers)} 项阻断问题："
            + "；".join(f"[{issue.code}] {issue.message}" for issue in blockers)
        )
    messages.append(f"校验基准日：{day.isoformat()}（{'计划生效起始日' if as_of is None else '账单/测算账期'}）")
    messages.append(plan.source_text)
    if not blocked:
        messages.extend(plan.rule_summary())

    assumptions.append(
        "分时时段与数值分离：时段规则可定义尖峰/高峰/平段/低谷时间；具体单价来自官方价格表、"
        "账单或用户输入（§1 设计边界）"
    )
    assumptions.append(
        "直接单价（有来源）为计费权威值；浮动系数为反算参考值，用于交叉校验，"
        f"不一致阈值为 {PRICE_CONSISTENCY_TOLERANCE_YUAN_PER_KWH:g} 元/千瓦时"
    )
    assumptions.append("未定义时段一律阻断，不得按平段兜底计价（§4.2、§7.7）")

    logger.info(
        "电价计划校验：%s（%s）→ 可用于正式复算=%s，问题 %d 项",
        plan.tariff_plan_id,
        plan.name,
        usable,
        len(issues),
    )
    return TariffPlanValidation(
        tariff_plan_id=plan.tariff_plan_id,
        plan_name=plan.name,
        status=plan.status,
        usable_for_formal_use=usable,
        as_of=day,
        coverage=coverage,
        issues=issues,
        messages=messages,
        assumptions=assumptions,
    )


def require_formal_use(plan: TariffPlan, *, as_of: date | None = None) -> TariffPlanValidation:
    """校验并要求计划可用于正式复算；否则抛出**中文** :class:`ValidationError`（§4.2、§7.4）。

    这是服务层"不允许用未核验/缺价计划跑正式模拟"的统一入口。
    """
    validation = validate_tariff_plan(plan, as_of=as_of, require_verified=True)
    if not validation.usable_for_formal_use:
        blockers = [issue for issue in validation.issues if issue.blocked]
        raise ValidationError(
            f"电价计划『{plan.name}』不能用于正式账单复算：" + "；".join(
                f"[{issue.code}] {issue.message}" for issue in blockers
            ),
            field="tariff_plan",
        )
    return validation


# --------------------------------------------------------------------------- #
# 逐时电价序列（供阶段 6 调度与报告复用）
# --------------------------------------------------------------------------- #
def hourly_price_series(
    plan: TariffPlan, month: int, *, day_type=None, slot_minutes: int = 60
) -> list[float | None]:
    """某月"典型日"的逐小时电价（元/kWh），长度 = ``24 * 60 / slot_minutes``。

    返回 ``None`` 表示该时间点**未定义时段或该时段无价格**——调用方必须显式处理，
    不得当成 0（§0.2、§4.2）。
    """
    prices = effective_period_prices(plan)
    slots = MINUTES_PER_DAY // int(slot_minutes)
    out: list[float | None] = []
    for slot in range(slots):
        period = resolve_slot_period(plan, month, slot, slot_minutes=slot_minutes, day_type=day_type)
        out.append(None if period is None else prices.get(period))
    return out


def select_tariff_plan(
    plans: list[TariffPlan],
    day: date,
    *,
    voltage_level: str | None = None,
    tariff_structure: TariffStructure | str | None = None,
    require_verified: bool = True,
) -> TariffPlan:
    """按**计费日期 + 适用范围**选取电价版本（V2.3 §2.3、§4.1 第 6、7 条）。

    筛选顺序：

    1. 生效期间覆盖 ``day``（``effective_from <= day <= effective_to``）；
    2. 若计划声明了 ``applicable_voltage_levels`` 且调用方给出电压等级，则须命中；
    3. 若计划声明了 ``applicable_tariff_structures`` 且调用方给出计费方式，则须命中；
    4. 优先取"已核验"，其次取 ``effective_from`` 最新者。

    :raises ValidationError: 没有可用版本 / 存在多个同等版本需要用户选择（**中文**提示）
    """
    if not plans:
        raise ValidationError("没有可用的电价计划，请先录入或载入湖北工商业电价计划", field="tariff_plan")

    def structure_text(value: TariffStructure | str | None) -> str:
        if value is None:
            return ""
        return value.value if isinstance(value, TariffStructure) else str(value)

    candidates = [plan for plan in plans if plan.covers_date(day)]
    if not candidates:
        available = "、".join(f"{p.name}（{p.effective_from}~{p.effective_to or '未标注'}）" for p in plans)
        raise ValidationError(
            f"没有覆盖 {day.isoformat()} 的电价计划版本；可用版本：" + available
            + "。跨政策版本的测算必须按日期选取版本，或显式固定一个版本并在报告中披露（§2.3）",
            field="effective_from",
        )

    if voltage_level:
        filtered = [
            plan
            for plan in candidates
            if not plan.applicable_voltage_levels or voltage_level in plan.applicable_voltage_levels
        ]
        if filtered:
            candidates = filtered

    wanted_structure = structure_text(tariff_structure)
    if wanted_structure:
        filtered = [
            plan
            for plan in candidates
            if not plan.applicable_tariff_structures or wanted_structure in plan.applicable_tariff_structures
        ]
        if filtered:
            candidates = filtered

    if require_verified:
        verified = [plan for plan in candidates if plan.status is TariffPlanStatus.VERIFIED]
        if verified:
            candidates = verified

    candidates.sort(key=lambda plan: (plan.effective_from, plan.tariff_plan_id), reverse=True)
    best = candidates[0]
    if len(candidates) > 1 and candidates[1].effective_from == best.effective_from:
        names = "、".join(plan.name for plan in candidates if plan.effective_from == best.effective_from)
        logger.warning("同一生效日期存在多个电价版本，已取其中之一：%s", names)
    logger.info("选取电价计划：%s（%s），基准日 %s", best.tariff_plan_id, best.name, day.isoformat())
    return best


def voltage_level_applies(plan: TariffPlan, voltage_level: str | None) -> bool:
    """电压等级是否落在计划声明范围内（未声明 = 未限定，返回 ``True``）。"""
    if not plan.applicable_voltage_levels or not voltage_level:
        return True
    return voltage_level in plan.applicable_voltage_levels


