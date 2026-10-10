"""账单电价取值来源与**优先级**（V2.5 §5、§0.2）。

本模块是"从一张账单取哪个电价"的**唯一实现处**（§0.2 铁律：核心规则不得散落在界面或报告里）。
界面、报告与后续的消纳率收益计算都必须调用本模块，不得各自再写一套"取平均电价"的逻辑。

为什么需要"优先级"这件事（需求方已确认的口径）
--------------------------------------------
同一个户可能有完全不同的两种电价形成机制，混用会直接算错收益：

1. **市场化直购客户**——账单第 4 页给出「24 小时电量电价」表，逐时列明
   直接交易价格与上网环节线损价格。**逐时价格才是真实的替代电价**
   （2025-10 样本实测：1–9 时直接交易价格 0.416~0.437 元/kWh，
   10–13 时 0.239~0.295 元/kWh，全月逐时波动）。
   对这类客户**不得**套用湖北政府峰谷系数（尖峰 200% / 高峰 150% / 低谷 45%）——
   那描述的是"代理购电"客户的目录电价形成方式，与市场化成交价无关。
2. **电网代理购电客户**——没有逐时交易价格，账单只有一个综合水平，
   此时才允许按政府峰谷分时系数拆分时段价格。

取值优先级（**写死在此，调用方不得调整**）
----------------------------------------
========================================  ==========================================================
优先级                                     取值来源
========================================  ==========================================================
①（首选）                                  :data:`PRICE_SOURCE_HOURLY_TABLE` —— 账单 24 小时电量电价表
②（次选，必须披露降级原因）                 :data:`PRICE_SOURCE_BILL_AVERAGE` —— 账单平均综合电价
③（**仅代理购电**；市场化直购客户调用即报错）  :data:`PRICE_SOURCE_GOV_TOU_FACTOR` —— 政府峰谷分时系数
========================================  ==========================================================

口径边界（§0.2）
---------------
* **替代电价的默认构成** = 账单「24 小时电量电价」表的**逐时直接交易价格**
  （需求方确认口径；样本实测 1–9 时 0.416~0.437、10–13 时 0.239~0.295 元/kWh）。
  「上网环节线损价格」是**可选**叠加项：传 ``include_line_loss=True`` 才得到
  "直接交易价格 + 线损价格"的全额成本口径，且必须在报告中披露，不得当成默认值。
* 本模块只**读**账单事实（逐时电量、逐时价格、金额、电量），**不写回**账单任何字段；
* 本模块不做"收益"计算，只回答"用哪个电价、为什么用它、还有哪些来源被降级了"；
* 账单未提供的数据一律视为**未知**，不臆造、不填 0；缺失到无法取价时抛
  :class:`~cenep.calculation.errors.ValidationError` 并给出**中文**修复指引。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

from ..domain.bill_models import HOURLY_PRICE_HOURS, ElectricityBill
from ..domain.enums import TariffPeriod
from .bill_calculator import average_comprehensive_price, effective_energy_charge
from .errors import ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "CUSTOMER_KIND_AGENCY_PURCHASE",
    "CUSTOMER_KIND_MARKET_DIRECT",
    "PRICE_PRIORITY",
    "PRICE_SOURCE_BILL_AVERAGE",
    "PRICE_SOURCE_GOV_TOU_FACTOR",
    "PRICE_SOURCE_HOURLY_TABLE",
    "BillEnergyPriceResolution",
    "hourly_avoided_prices",
    "resolve_bill_energy_price",
]

# --------------------------------------------------------------------------- #
# 客户类型（决定"能不能用政府峰谷系数"）
# --------------------------------------------------------------------------- #
#: 市场化直购客户：**逐时交易价格**才是真实替代电价，不得套用政府峰谷系数
CUSTOMER_KIND_MARKET_DIRECT = "市场化直购"
#: 电网代理购电客户：没有逐时成交价，才允许按政府峰谷分时系数拆分时段价格
CUSTOMER_KIND_AGENCY_PURCHASE = "代理购电"

#: 客户类型全集（供界面下拉框与中文校验共用）
CUSTOMER_KINDS: tuple[str, ...] = (CUSTOMER_KIND_MARKET_DIRECT, CUSTOMER_KIND_AGENCY_PURCHASE)

# --------------------------------------------------------------------------- #
# 电价来源标签（写进报告，用户一眼能看出"这个电价是哪来的"）
# --------------------------------------------------------------------------- #
#: ① 首选：账单第 4 页「24 小时电量电价」表
PRICE_SOURCE_HOURLY_TABLE = "账单「24 小时电量电价」表逐时价格（直接交易价格 + 上网环节线损价格）"
#: ② 次选：账单平均综合电价（电度电费 ÷ 总购电量）
PRICE_SOURCE_BILL_AVERAGE = "账单平均综合电价（电度电费 ÷ 总购电量，仅为账单统计口径）"
#: ③ 兜底：政府峰谷分时系数（**仅适用于代理购电客户**）
PRICE_SOURCE_GOV_TOU_FACTOR = "政府峰谷分时系数（**仅适用于代理购电客户**）"

#: 取值优先级（顺序即优先级，**不得调整**；报告按此顺序披露降级过程）
PRICE_PRIORITY: tuple[str, ...] = (
    PRICE_SOURCE_HOURLY_TABLE,
    PRICE_SOURCE_BILL_AVERAGE,
    PRICE_SOURCE_GOV_TOU_FACTOR,
)


@dataclass(frozen=True)
class BillEnergyPriceResolution:
    """一张账单的电价取值结果（V2.5 §5）。

    这是**取值口径的说明对象**，不是账单事实、也不是收益计算结果：
    它只回答"用了哪个来源、电价是多少、为什么没用其他来源"。

    ``hourly_prices_yuan_per_kwh`` / ``hourly_energy_kwh`` 与 ``hour`` 一一对应；
    逐时消纳收益计算应当使用本对象的逐时序列，而**不是**用 ``avoided_price`` 一个平均值
    乘全年电量——逐时价格波动很大（样本 0.239~0.437 元/kWh），用平均值会抹掉时段差异。
    """

    source: str
    """取值来源标签，取值必为 :data:`PRICE_PRIORITY` 中的一项。"""

    source_label: str
    """中文口径全文（供报告"口径与假设"章节原样披露）。"""

    avoided_price_yuan_per_kwh: float | None = None
    """按来源得到的**替代电价**（元/kWh）：光伏自用电量所避免支付的购电单价。"""

    hours: tuple[int, ...] = ()
    """逐时电价对应的小时编号（来自 24 小时表时恒为 ``(1, ..., 24)``，否则为空）。"""

    hourly_prices_yuan_per_kwh: tuple[float | None, ...] = ()
    """逐时替代电价（元/kWh），与 :attr:`hours` 一一对应；非逐时来源时为空。"""

    hourly_energy_kwh: tuple[float | None, ...] = ()
    """逐时电量（kWh），与 :attr:`hours` 一一对应；用于按电量加权。"""

    bill_average_price_yuan_per_kwh: float | None = None
    """账单平均综合电价（元/kWh）；无法计算时为 ``None``（**不是 0**）。"""

    gov_tou_prices_yuan_per_kwh: Mapping[str, float] = field(default_factory=dict)
    """政府峰谷系数得到的各时段电价（时段值 → 元/kWh）；未使用时为空。"""

    customer_kind: str = CUSTOMER_KIND_MARKET_DIRECT
    """客户类型（:data:`CUSTOMER_KIND_MARKET_DIRECT` / :data:`CUSTOMER_KIND_AGENCY_PURCHASE`）。"""

    messages: list[str] = field(default_factory=list)
    """中文说明（含降级过程与差异数值），供界面预览显示。"""

    assumptions: list[str] = field(default_factory=list)
    """口径假设（供报告"口径与假设"章节披露）。"""

    @property
    def used_hourly_table(self) -> bool:
        """是否采用了账单 24 小时电量电价表（消纳率电价的首选来源）。"""
        return self.source == PRICE_SOURCE_HOURLY_TABLE

    def price_at_hour(self, hour: int) -> float | None:
        """取某个小时的替代电价（元/kWh）；非逐时来源或该小时未提供时返回 ``None``。

        :raises ValidationError: 小时编号不在 1~24
        """
        if not 1 <= int(hour) <= 24:
            raise ValidationError(
                f"小时编号「{hour}」无效：必须是 1~24 的整数"
                "（1 时表示 0~1 时，24 时表示 23~24 时）",
                field="hour",
            )
        if not self.used_hourly_table:
            return None
        index = int(hour) - 1
        if index >= len(self.hourly_prices_yuan_per_kwh):
            return None
        return self.hourly_prices_yuan_per_kwh[index]


def _as_price(value: object) -> float | None:
    """账单价格 ``None`` = 未提供（**不是 0**），不得自动填 0（V2.1 §2.1）。"""
    return None if value is None else float(value)  # type: ignore[arg-type]


def _hour_price(
    point,
    *,
    include_line_loss: bool,
) -> float | None:
    """某一小时的替代电价。

    **默认口径（``include_line_loss=False``）= 直接交易价格**，因为需求方确认的
    "真实替代电价"就是账单第 4 页的**逐时交易价格**（样本实测 1–9 时 0.416~0.437、
    10–13 时 0.239~0.295 元/kWh 即指该列）。

    ``include_line_loss=True`` 是**全额口径**：把「上网环节线损价格」也加进来。
    该价格按账单备注"并入分时电价机制中的基础电价"，即每买 1 kWh 还要按它付线损费，
    因此自用 1 kWh 也会省下这份费用——它属于**可选**的完整成本口径，必须由调用方显式选择
    并在报告里写明，**不得**当成默认值悄悄改变电价水平。
    """
    direct = _as_price(point.direct_trade_price_yuan_per_kwh)
    loss = _as_price(point.line_loss_price_yuan_per_kwh)
    parts = [value for value in ((direct, loss) if include_line_loss else (direct,)) if value is not None]
    return float(sum(parts)) if parts else None


def hourly_avoided_prices(
    bill: ElectricityBill,
    *,
    include_line_loss: bool = False,
) -> tuple[tuple[int, ...], tuple[float, ...], tuple[float, ...]]:
    """账单 24 小时表的**逐时替代电价**（消纳率电价计算的首选输入，V2.5 §5）。

    这是消纳率收益逐时计算应当调用的入口：优先用逐时交易价格，
    **不得**退回平均电价或政府峰谷系数（那会抹掉逐时 0.239~0.437 元/kWh 的真实波动）。

    默认取**直接交易价格**（需求方确认的"真实替代电价"口径）；
    ``include_line_loss=True`` 时改取"直接交易价格 + 上网环节线损价格"的全额口径。

    :return: ``(小时编号, 逐时电价, 逐时电量)``；小时恒为 ``(1, ..., 24)``。
    :raises ValidationError: 账单没有 24 小时表 / 表不完整 / 某小时没有可用价格（中文报错）
    """
    points = {int(point.hour): point for point in bill.hourly_energy_tariff}
    missing_hours = [hour for hour in HOURLY_PRICE_HOURS if hour not in points]
    if missing_hours:
        raise ValidationError(
            "账单没有可用的「24 小时电量电价」表，或该表不完整（缺少 "
            + "、".join(f"{hour} 时" for hour in missing_hours)
            + "）。该表是消纳率电价的首选输入：市场化直购客户的逐时交易价格才是真实替代电价，"
            "不得退回账单平均电价或政府峰谷系数（V2.5 §5、§0.2）；"
            "请重新导入完整账单，或在电价参数中显式指定替代电价",
            field="hourly_energy_tariff",
        )
    no_price = [
        hour
        for hour in HOURLY_PRICE_HOURS
        if _hour_price(points[hour], include_line_loss=include_line_loss) is None
    ]
    if no_price:
        raise ValidationError(
            "账单 24 小时电量电价表缺少逐时价格（小时："
            + "、".join(f"{hour} 时" for hour in no_price)
            + "），无法得到逐时替代电价；请核对账单第 4 页是否被截断（V2.5 §5）",
            field="hourly_energy_tariff",
        )
    prices = tuple(
        float(_hour_price(points[hour], include_line_loss=include_line_loss))  # type: ignore[arg-type]
        for hour in HOURLY_PRICE_HOURS
    )
    energies = tuple(
        None if points[hour].energy_kwh is None else float(points[hour].energy_kwh)
        for hour in HOURLY_PRICE_HOURS
    )
    logger.debug(
        "逐时替代电价：%d 条，区间 %.6f ~ %.6f 元/kWh（含线损=%s）",
        len(prices),
        min(prices),
        max(prices),
        include_line_loss,
    )
    return HOURLY_PRICE_HOURS, prices, energies


def _weighted_price(prices: list[float], weights: list[float]) -> float:
    """按电量加权平均；权重合计为 0 时退回**算术平均**（不得返回 0 元/kWh）。

    权重合计为 0 意味着账单只给了价格、没给逐时电量：这时算术平均是唯一不臆造权重的做法，
    并且会在 ``messages`` 里明确说明"未按电量加权"。
    """
    total = float(sum(weights))
    if total <= 0.0:
        return float(sum(prices)) / len(prices)
    return float(sum(price * weight for price, weight in zip(prices, weights)) / total)


def _resolve_from_hourly_table(
    bill: ElectricityBill, *, include_line_loss: bool
) -> tuple[float, tuple[int, ...], tuple[float, ...], tuple[float | None, ...], list[str]]:
    """优先级①：从 24 小时电量电价表取价（返回 ``(电价, 小时, 逐时电价, 逐时电量, 说明)``）。"""
    hours, prices, energies = hourly_avoided_prices(bill, include_line_loss=include_line_loss)
    # 账单未提供逐时电量时权重全为 0 → ``_weighted_price`` 退回算术平均，并在说明里写明
    weights = [0.0 if value is None else float(value) for value in energies]
    avoided = _weighted_price(list(prices), weights)
    weighted = sum(weights) > 0.0
    messages = [
        f"电价取值来源：账单「24 小时电量电价」表（{len(prices)} 条，"
        f"{min(prices):.6f} ~ {max(prices):.6f} 元/kWh），"
        + ("按逐时电量加权" if weighted else "未按电量加权（账单未提供逐时电量，改取算术平均）")
    ]
    return avoided, hours, prices, energies, messages


def _resolve_from_bill_average(
    bill: ElectricityBill,
) -> tuple[float | None, list[str]]:
    """优先级②：账单平均综合电价 = 电度电费 ÷ 总购电量（返回 ``(电价, 说明)``）。

    分子优先取**电度电费**（``energy_charge_yuan``，缺失时用二层分项合计替代），
    这样平均电价只含"随电量变化"的部分，不会因为把固定基本电费摊进去而虚高。
    电度电费也无法取得时才退回 ``账单总额 ÷ 总购电量``，并在说明里写明差别。
    """
    messages: list[str] = []
    energy = None if bill.energy_total_kwh is None else float(bill.energy_total_kwh)
    energy_charge, charge_source = effective_energy_charge(bill)
    if energy_charge is not None and energy is not None and energy > 0.0:
        price = float(energy_charge) / energy
        messages.append(
            f"电价取值来源降级为账单平均电价：电度电费 {float(energy_charge):,.2f} 元"
            f"（{charge_source}）÷ 总购电量 {energy:,.0f} kWh = {price:.6f} 元/kWh。"
            "该值是**账单统计口径**，不能反映逐时价格波动（V2.1 §3.1）"
        )
        return price, messages
    fallback = average_comprehensive_price(bill.bill_total_yuan, bill.energy_total_kwh)
    if fallback is not None:
        messages.append(
            f"电价取值来源降级为账单平均电价：账单总额 {float(bill.bill_total_yuan):,.2f} 元 ÷ "
            f"总购电量 {energy:,.0f} kWh = {fallback:.6f} 元/kWh。"
            "注意该平均价含固定基本电费 / 力调 / 税费等不随电量变化的部分（V2.1 §3.1）"
        )
    return fallback, messages


def _normalize_period_key(key: object) -> str:
    """把时段键统一成 :class:`TariffPeriod` 的 ``value`` 字符串。"""
    if isinstance(key, TariffPeriod):
        return key.value
    return str(key)


def _resolve_from_gov_tou(
    prices: Mapping[object, float],
    energies: Mapping[object, float] | None,
) -> tuple[float | None, dict[str, float], list[str]]:
    """优先级③：政府峰谷分时系数得到的各时段电价 → 按电量加权（或无权重算术平均）。"""
    normalized = {_normalize_period_key(key): float(value) for key, value in prices.items()}
    if not normalized:
        return None, {}, []
    if not all(value >= 0.0 for value in normalized.values()):
        raise ValidationError(
            "政府峰谷分时电价存在负值，请检查电价参数（V2.5 §5）",
            field="gov_tou_prices",
        )
    weight_map = (
        {_normalize_period_key(key): float(value) for key, value in energies.items()}
        if energies
        else {}
    )
    ordered = [key for key in normalized if key in weight_map and weight_map[key] > 0.0]
    if ordered:
        avoided = _weighted_price(
            [normalized[key] for key in ordered], [weight_map[key] for key in ordered]
        )
        note = "按各时段电量加权"
    else:
        avoided = sum(normalized.values()) / len(normalized)
        note = "各时段算术平均（未提供分时电量，未臆造权重）"
    messages = [
        "电价取值来源为政府峰谷分时系数（" + note + "）："
        + "、".join(f"{label} {value:.6f} 元/kWh" for label, value in normalized.items())
        + "。该口径**仅适用于电网代理购电客户**；市场化直购客户必须使用账单逐时交易价格（V2.5 §5）"
    ]
    return avoided, normalized, messages


def resolve_bill_energy_price(
    bill: ElectricityBill,
    *,
    customer_kind: str = CUSTOMER_KIND_MARKET_DIRECT,
    include_line_loss: bool = False,
    gov_tou_prices: Mapping[object, float] | None = None,
    gov_tou_energy_by_period: Mapping[object, float] | None = None,
) -> BillEnergyPriceResolution:
    """按**写死的优先级**为一张账单确定替代电价（V2.5 §5、§0.2）。

    优先级：① 账单 24 小时电量电价表（逐时交易价格，**首选**）→
    ② 账单平均综合电价（降级，必须披露）→ ③ 政府峰谷分时系数
    （**仅代理购电客户**；市场化直购客户若走到这一步直接报错，而不是静默套用系数）。

    :param bill: 账单**事实**（只读，不会被修改）。
    :param customer_kind: 客户类型；取值见 :data:`CUSTOMER_KINDS`。
    :param include_line_loss: 逐时替代电价是否叠加「上网环节线损价格」。
        **默认 ``False``** = 需求方确认的"逐时交易价格"口径（样本实测 1–9 时 0.416~0.437、
        10–13 时 0.239~0.295 元/kWh）；改为 ``True`` 得到"直接交易价格 + 上网环节线损价格"
        的**全额成本**口径，必须显式选择并在报告中披露。
    :param gov_tou_prices: 兜底用的政府峰谷分时电价（时段 → 元/kWh）；
        仅当 ``customer_kind`` 为代理购电时才允许使用。
    :param gov_tou_energy_by_period: 兜底口径下各时段电量（时段 → kWh），用于加权；
        未提供时退回算术平均并在说明中写明。
    :raises ValidationError: 客户类型非法 / 无任何可用来源 / 市场化直购客户企图使用政府峰谷系数
    """
    kind = str(customer_kind or "").strip()
    if kind not in CUSTOMER_KINDS:
        raise ValidationError(
            f"客户类型「{customer_kind}」无法识别：必须是 "
            + " 或 ".join(f"『{value}』" for value in CUSTOMER_KINDS)
            + "（客户类型决定能否使用政府峰谷系数，V2.5 §5）",
            field="customer_kind",
        )

    messages: list[str] = []
    assumptions: list[str] = [
        "电价取值优先级（V2.5 §5，写死于 bill_price_source.resolve_bill_energy_price）："
        "① 账单 24 小时电量电价表逐时交易价格（首选）→ ② 账单平均综合电价（降级并披露）→ "
        "③ 政府峰谷分时系数（**仅代理购电客户**）",
        "该户为市场化直购客户时，逐时交易价格才是真实替代电价；"
        "**不得**对其套用湖北政府峰谷系数（尖峰 200% / 高峰 150% / 低谷 45%），"
        "该系数只描述代理购电客户的目录电价形成方式（V2.5 §5）",
    ]

    # ---------------- 优先级①：账单 24 小时电量电价表 ---------------- #
    if bill.hourly_energy_tariff:
        try:
            avoided, hours, prices, energies, hourly_messages = _resolve_from_hourly_table(
                bill, include_line_loss=include_line_loss
            )
        except ValidationError as exc:
            # 表存在但不完整 / 缺价格：这是必须让用户看见的输入问题，不得静默降级
            logger.warning("账单 %s 的 24 小时电价表不可用：%s", bill.bill_id, exc.message)
            raise
        messages.extend(hourly_messages)
        messages.append(
            f"{kind}客户的替代电价取逐时电价加权值 {avoided:.6f} 元/kWh"
            + (
                "（全额成本口径：直接交易价格 + 上网环节线损价格）"
                if include_line_loss
                else "（需求方确认口径：逐时直接交易价格，不含上网环节线损价格）"
            )
        )
        # 对照值：账单平均电价（仅用于披露"逐时口径与平均口径差多少"，不参与取价）
        bill_average, _average_messages = _resolve_from_bill_average(bill)
        if bill_average is not None:
            messages.append(
                f"对照：账单平均电价 {bill_average:.6f} 元/kWh，与逐时加权价相差 "
                f"{avoided - bill_average:+.6f} 元/kWh；**以逐时电价为准**（平均价会抹掉时段差异）"
            )
        assumptions.append(
            "消纳率电价按账单 24 小时电量电价表的**逐时价格**取值（"
            + ("全额成本口径：直接交易价格 + 上网环节线损价格" if include_line_loss
               else "口径：逐时**直接交易价格**（未叠加上网环节线损价格）")
            + "），随逐时电量加权；不使用账单平均电价，也不使用政府峰谷系数（V2.5 §5）"
        )
        return BillEnergyPriceResolution(
            source=PRICE_SOURCE_HOURLY_TABLE,
            source_label=PRICE_SOURCE_HOURLY_TABLE,
            avoided_price_yuan_per_kwh=avoided,
            hours=tuple(hours),
            hourly_prices_yuan_per_kwh=tuple(prices),
            hourly_energy_kwh=tuple(energies),
            bill_average_price_yuan_per_kwh=bill_average,
            customer_kind=kind,
            messages=messages,
            assumptions=assumptions,
        )

    # ---------------- 优先级②：账单平均综合电价 ---------------- #
    bill_average, average_messages = _resolve_from_bill_average(bill)
    if bill_average is not None:
        messages.extend(average_messages)
        messages.append(
            "账单未提供「24 小时电量电价」表，因此无法使用逐时电价（首选来源）；"
            "本次降级为账单平均电价，**结果精度低于逐时口径**，报告必须披露该降级"
        )
        assumptions.append(
            "本次未使用逐时电价：账单没有「24 小时电量电价」表，替代电价降级为账单平均综合电价；"
            "该平均价对固定费用做了摊薄，不能反映逐时价格波动（V2.1 §3.1、V2.5 §5）"
        )
        return BillEnergyPriceResolution(
            source=PRICE_SOURCE_BILL_AVERAGE,
            source_label=PRICE_SOURCE_BILL_AVERAGE,
            avoided_price_yuan_per_kwh=bill_average,
            bill_average_price_yuan_per_kwh=bill_average,
            customer_kind=kind,
            messages=messages,
            assumptions=assumptions,
        )

    # ---------------- 优先级③：政府峰谷分时系数（仅代理购电） ---------------- #
    if gov_tou_prices:
        if kind != CUSTOMER_KIND_AGENCY_PURCHASE:
            raise ValidationError(
                f"客户类型为『{kind}』时**不得**使用政府峰谷分时系数："
                "湖北政府峰谷系数（尖峰 200% / 高峰 150% / 低谷 45%）只描述『代理购电』客户的"
                "目录电价形成方式；市场化直购客户的真实替代电价是其逐时交易价格。"
                "请导入带「24 小时电量电价」表的完整账单，改为提供逐时电价，"
                "或把客户类型明确改为『代理购电』后再用系数口径（V2.5 §5、§0.2）",
                field="gov_tou_prices",
            )
        avoided, normalized, gov_messages = _resolve_from_gov_tou(
            gov_tou_prices, gov_tou_energy_by_period
        )
        messages.extend(gov_messages)
        weight_note = (
            "按各时段电量加权" if gov_tou_energy_by_period else "各时段算术平均（未提供分时电量）"
        )
        assumptions.append(
            "本次替代电价来自政府峰谷分时系数（**仅代理购电客户口径**）："
            "账单既无 24 小时电量电价表、也无法得到平均电价，因此只能按系数口径取价；"
            f"权重方式：{weight_note}；报告必须同时披露系数来源与权重方式（V2.5 §5）"
        )
        if avoided is None:  # pragma: no cover - gov_tou_prices 非空时必返回电价
            raise ValidationError(
                "政府峰谷分时系数未给出任何可用电价，无法确定替代电价（V2.5 §5）",
                field="gov_tou_prices",
            )
        return BillEnergyPriceResolution(
            source=PRICE_SOURCE_GOV_TOU_FACTOR,
            source_label=PRICE_SOURCE_GOV_TOU_FACTOR,
            avoided_price_yuan_per_kwh=avoided,
            gov_tou_prices_yuan_per_kwh=normalized,
            customer_kind=kind,
            messages=messages,
            assumptions=assumptions,
        )

    missing: list[str] = []
    if bill.energy_total_kwh is None:
        missing.append("总购电量")
    if effective_energy_charge(bill)[0] is None and bill.bill_total_yuan is None:
        missing.append("电度电费合计 / 账单总额")
    raise ValidationError(
        f"账单 {bill.bill_id} 无法确定替代电价：既没有「24 小时电量电价」表（首选来源），"
        "也缺少可计算平均电价的数据（"
        + ("、".join(missing) or "总购电量与电费金额")
        + "）。请导入完整账单，或显式提供替代电价参数；"
        "**不允许**在缺少数据时臆造电价，也不允许对市场化直购客户套用政府峰谷系数（V2.5 §5、§0.2）",
        field="energy_total_kwh",
    )
