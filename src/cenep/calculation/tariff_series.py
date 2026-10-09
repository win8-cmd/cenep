"""V2 分时电价引擎（V2 §3 P0.4、§17、§18）。

把 :class:`cenep.domain.timeseries.TariffProfile` 的**时段规则**解析为逐周期电价，
取代 V1「单一平均电价」的核心地位（V2 §17）。本模块是电价的唯一公式来源
（V1 §104、V2 §61）。

.. code-block:: text

    Price_t   = price_for_period(时段规则命中的时段)                （§17.1）
    Price_n[t] = Price_1[t] × (1 + tariff_growth_rate)^(n-1)        （§17.2）

**规则优先级（明确写死，避免歧义）**

多条规则可能命中同一小时（例如"7—8 月尖峰"与"全年高峰"）。本引擎采用：

    按 ``time_periods`` 的**声明顺序**求值，**后面的规则覆盖前面的**。

因此用户应把**更具体/更特殊**的规则写在**后面**（尖峰 > 高峰 > 平段 > 谷段、
季节性规则 > 全年规则）。命中数组合法但语义上与"先匹配先生效"相反，
测试已固化该行为。

**未命中规则的小时**

用 ``FLAT`` 兜底，**不抛异常**，但必须能被上层感知：
:func:`unmatched_mask` 返回这些小时的布尔掩码，报告层据此提示用户"有 N 个小时未定义时段"。
这样既不阻断测算，也不会静默改变口径。

**上网电价不随购电价增长**（§17.2 只定义购电电价的年度变化）

``export_price`` 由政策单独决定（脱硫煤基准价 / 市场化交易价），
因此**不乘**年度增长因子。需量电价与基本电费属于购电侧费用，**随**增长因子变化。

实现约束：只对**规则条数**循环（通常 ≤ 数十条），对 8760 个时间点全部向量化
（V2 §86、§87）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ..domain.enums import DayType, TariffPeriod
from ..domain.timeseries import TariffProfile, TariffSeriesConfig, TimePeriodRule
from .errors import ValidationError
from .timeseries_engine import TimeAxis, annual_growth_factor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TariffSeries:
    """逐周期电价序列及其它电价口径（V2 §17、§18）。"""

    price: np.ndarray
    export_price: np.ndarray
    period_code: np.ndarray
    demand_charge: float = 0.0
    basic_charge: float = 0.0
    demand_charge_enabled: bool = False
    basic_charge_enabled: bool = False

    def __len__(self) -> int:
        return int(self.price.size)

    def period_share(self) -> dict[str, float]:
        """各时段**小时占比**（分钟粒度下为周期占比），用于报告（V2 §66）。"""
        total = len(self)
        if total == 0:
            return {}
        codes, counts = np.unique(np.asarray(self.period_code, dtype=str), return_counts=True)
        return {str(c): float(n) / total for c, n in zip(codes, counts)}

    def energy_cost(self, energy: np.ndarray) -> float:
        """给定逐周期购电量（kWh）的购电费用（元）。"""
        return float(np.sum(np.asarray(energy, dtype=float) * self.price))


def price_for_period(profile: TariffProfile, period: TariffPeriod) -> float:
    """取某时段对应的电价（元/kWh）（§17.1）。"""
    return float(
        {
            TariffPeriod.SHARP_PEAK: profile.sharp_peak_price,
            TariffPeriod.PEAK: profile.peak_price,
            TariffPeriod.FLAT: profile.flat_price,
            TariffPeriod.VALLEY: profile.valley_price,
            TariffPeriod.DEEP_VALLEY: profile.deep_valley_price,
            TariffPeriod.CUSTOM: profile.custom_price,
        }[period]
    )


def _day_type_strings(axis: TimeAxis) -> np.ndarray:
    return np.asarray(axis.day_type).astype(str)


def _rule_mask(rule: TimePeriodRule, axis: TimeAxis, day_types: np.ndarray) -> np.ndarray:
    """一条规则命中的时间点掩码；维度上的空列表表示"该维度不限制"。"""
    mask = np.ones(axis.point_count, dtype=bool)
    if rule.months:
        mask &= np.isin(np.asarray(axis.month, dtype=np.int64), rule.months)
    if rule.day_types:
        wanted = [d.value if isinstance(d, DayType) else str(d) for d in rule.day_types]
        mask &= np.isin(day_types, wanted)
    if rule.hours:
        mask &= np.isin(np.asarray(axis.hour, dtype=np.int64), rule.hours)
    return mask


def resolve_period_codes(
    profile: TariffProfile, axis: TimeAxis
) -> tuple[np.ndarray, np.ndarray]:
    """解析时段码（字符串数组）与命中掩码。

    :returns: ``(codes, matched)`` —— ``codes`` 为 ``TariffPeriod.value`` 字符串数组，
        ``matched`` 为是否被任一规则命中的布尔掩码。
    """
    n = axis.point_count
    codes = np.full(n, TariffPeriod.FLAT.value, dtype=object)
    matched = np.zeros(n, dtype=bool)
    if n == 0 or not profile.time_periods:
        return codes.astype(str), matched

    day_types = _day_type_strings(axis)
    for rule in profile.time_periods:
        mask = _rule_mask(rule, axis, day_types)
        if not mask.any():
            continue
        codes[mask] = rule.period.value   # 后声明的规则覆盖先声明的
        matched |= mask
    return codes.astype(str), matched


def resolve_period_array(profile: TariffProfile, axis: TimeAxis) -> np.ndarray:
    """每个时间点所属的 :class:`TariffPeriod`（object 数组，未命中为 ``FLAT``）。"""
    codes, _ = resolve_period_codes(profile, axis)
    return np.array([TariffPeriod(c) for c in codes], dtype=object)


def unmatched_mask(profile: TariffProfile, axis: TimeAxis) -> np.ndarray:
    """未被任何时段规则命中的时间点掩码（这些点按 ``FLAT`` 兜底）。"""
    _, matched = resolve_period_codes(profile, axis)
    return ~matched


def resolve_price_series(
    config: TariffSeriesConfig, axis: TimeAxis, year_index: int = 1
) -> tuple[np.ndarray, np.ndarray]:
    """解析逐周期购电电价与上网电价（元/kWh）。

    :returns: ``(price, export_price)``，均为 ``float64``，长度 = ``axis.point_count``
    :raises ValidationError: 出现负电价时（中文报错）
    """
    profile = config.profile
    codes, _ = resolve_period_codes(profile, axis)
    price = np.fromiter(
        (price_for_period(profile, TariffPeriod(c)) for c in codes),
        dtype=float,
        count=axis.point_count,
    )

    factor = annual_growth_factor(config.annual_growth_rate, year_index)
    price = price * factor
    # §17.2：上网电价由政策单独决定，**不随**购电电价增长
    export = np.full(price.shape, float(profile.export_price), dtype=float)

    if np.any(price < 0.0) or np.any(export < 0.0):
        raise ValidationError(
            "电价出现负值，请检查分时电价与上网电价参数", field="timeseries.tariff.profile"
        )

    unmatched = int((~_matched_for(profile, axis)).sum())
    if unmatched:
        logger.warning(
            "分时电价有 %d 个时间点未被任何时段规则覆盖，已按平段兜底（V2 §52）", unmatched
        )
    logger.debug(
        "电价解析：年序=%d 点数=%d 购电均价=%.4f 元/kWh 上网价=%.4f 元/kWh 未覆盖=%d",
        year_index, axis.point_count, float(price.mean()),
        float(export[0]) if export.size else 0.0, unmatched,
    )
    return price, export


def _matched_for(profile: TariffProfile, axis: TimeAxis) -> np.ndarray:
    _, matched = resolve_period_codes(profile, axis)
    return matched


def resolve_tariff(
    config: TariffSeriesConfig, axis: TimeAxis, year_index: int = 1
) -> TariffSeries:
    """解析为 :class:`TariffSeries`（供调度引擎与报表使用，V2 §17、§18）。"""
    profile = config.profile
    price, export = resolve_price_series(config, axis, year_index)
    codes, _ = resolve_period_codes(profile, axis)
    factor = annual_growth_factor(config.annual_growth_rate, year_index)
    return TariffSeries(
        price=price,
        export_price=export,
        period_code=codes,
        # 需量电价 / 基本电费属购电侧费用，随电价增长因子变化（§17.2、§18）
        demand_charge=float(profile.demand_charge) * factor,
        basic_charge=float(profile.basic_charge) * factor,
        demand_charge_enabled=bool(config.demand_charge_enabled),
        basic_charge_enabled=bool(config.basic_charge_enabled),
    )


def weighted_average_price(series: TariffSeries, energy: np.ndarray) -> float:
    """按电量加权的购电均价（元/kWh），用于报告与自查。"""
    total = float(np.sum(energy))
    if total <= 0.0:
        return 0.0
    return float(np.sum(np.asarray(energy, dtype=float) * series.price) / total)


def peak_valley_spread(series: TariffSeries) -> float:
    """峰谷价差（元/kWh）= 最高时段电价 − 最低时段电价。"""
    if series.price.size == 0:
        return 0.0
    return float(np.max(series.price) - np.min(series.price))


#: 向后兼容别名（旧名 ``price_of``）
price_of = price_for_period


# --------------------------------------------------------------------------- #
# V2.3 §7.2：版本化电价计划 → V2 TariffProfile 的**追加式适配器**
# --------------------------------------------------------------------------- #
# 说明（追加式扩展，不改动本模块任何既有函数的行为）：
#   * V2.3 的 `TariffPlan`（用户侧工商业购电电价计划，policy/hubei_commercial.py）
#     是"版本化 + 有来源 + 有适用范围"的新模型；
#   * V2 既有的 `TariffProfile` 是储能调度与时序引擎的电价输入口径；
#   * 阶段 6 需要在**同一套电价**下跑方案对比，因此这里提供单向适配器
#     `TariffPlan → TariffProfile`，让既有 dispatch/timeseries 引擎直接复用，
#     **不新建第二套调度或电价引擎**（§0.2、§7.2）。
# --------------------------------------------------------------------------- #
def tariff_profile_from_plan(
    plan,
    *,
    month: int,
    export_price: float = 0.0,
    demand_charge: float | None = None,
    basic_charge: float = 0.0,
    tariff_id_suffix: str = "",
):
    """把版本化电价计划转为某个月的 :class:`~cenep.domain.timeseries.TariffProfile`（§7.2）。

    * 时段规则：只取 **在 ``month`` 生效**的规则，逐小时展开为
      :class:`~cenep.domain.timeseries.TimePeriodRule`；声明顺序与优先级一致
      （``priority`` 升序、同级按声明顺序），与既有引擎"后声明覆盖先声明"的语义配合；
    * 时段价格：取 :func:`cenep.calculation.tariff_plan_engine.effective_period_prices`
      的**最终价格**（直接单价优先，缺失时为 0 并由调用方在校验阶段拦截）；
    * 需量电价（元/kW·月）默认取计划的 ``demand_charge_yuan_per_kw_month``；
      基本电费（元/月）不能由容量电价直接得出，必须由调用方显式给出。

    :param plan: :class:`~cenep.domain.tariff_models.TariffPlan`
    :param month: 目标月份 1~12（该计划可能对 7、8 月与其他月份采用不同时段）
    :param export_price: 上网电价（元/kWh）；本适配器**不臆造**，默认 0 并由调用方覆盖
    :raises ValidationError: 月份非法
    """
    from ..domain.timeseries import TariffProfile, TimePeriodRule
    from .errors import ValidationError
    from .tariff_plan_engine import effective_period_prices

    month = int(month)
    if not 1 <= month <= 12:
        raise ValidationError(f"月份必须在 1~12，实际为 {month}", field="month")

    prices = effective_period_prices(plan)
    rules: list[TimePeriodRule] = []
    ordered = sorted(enumerate(plan.time_period_rules), key=lambda item: (item[1].priority, item[0]))
    for _, rule in ordered:
        if rule.months and month not in {int(m) for m in rule.months}:
            continue
        hours = [hour for hour in range(24) if rule.covers_minute(hour * 60)]
        if not hours:
            continue
        rules.append(
            TimePeriodRule(period=rule.period, months=[month], hours=hours)
        )

    def price_of(period) -> float:
        value = prices.get(period)
        return float(value) if value is not None else 0.0

    profile = TariffProfile(
        tariff_id=f"{plan.tariff_plan_id}{tariff_id_suffix}",
        name=f"{plan.name}（{month} 月）",
        effective_date=plan.effective_from,
        region=plan.province,
        time_periods=rules,
        sharp_peak_price=price_of(TariffPeriod.SHARP_PEAK),
        peak_price=price_of(TariffPeriod.PEAK),
        flat_price=price_of(TariffPeriod.FLAT),
        valley_price=price_of(TariffPeriod.VALLEY),
        deep_valley_price=price_of(TariffPeriod.DEEP_VALLEY),
        custom_price=price_of(TariffPeriod.CUSTOM),
        export_price=float(export_price),
        demand_charge=float(
            demand_charge
            if demand_charge is not None
            else (plan.demand_charge_yuan_per_kw_month or 0.0)
        ),
        basic_charge=float(basic_charge),
    )
    logger.debug(
        "电价计划适配为 TariffProfile：%s → %d 条时段规则（%d 月）",
        plan.tariff_plan_id,
        len(rules),
        month,
    )
    return profile
