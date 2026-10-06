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
