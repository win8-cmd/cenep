"""电价与收益模块（规范 §26–§35、§43–§44、§146）。

职责
----
1. 电价解析（固定 / 峰平谷 / 市场 / 自定义）；
2. 光伏收益：自用收益 + 上网收益 + 其他收益；
3. 储能收益：套利 + 容量 + 辅助服务 + 其他。

**规范 §33**：不得在代码中硬编码任何一个市场价格，所有价格均来自参数或政策 Profile。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError


@dataclass(frozen=True)
class TariffResolution:
    """电价解析结果（统一为 元/kWh）。"""

    avoided_electricity_price: float
    """光伏自用/储能放电替代的用户边际购电电价（规范 §27）。"""

    charge_price: float
    """储能充电电价（规范 §43）。"""

    export_price: float
    """余电上网电价（规范 §28）。"""

    average_tou_price: float
    """峰平谷综合电价（规范 §32）。"""

    basis: str
    """电价解析依据，用于报告中的口径说明。"""


def average_tou_price(
    peak_price: float,
    flat_price: float,
    valley_price: float,
    peak_ratio: float,
    flat_ratio: float,
    valley_ratio: float,
) -> float:
    """峰平谷综合电价（规范 §32）。

    ``AverageTOUPrice = Peak×PeakRatio + Flat×FlatRatio + Valley×ValleyRatio``。

    规范 §31 要求三个比例之和为 1；此处做归一化校验并按原比例计算。
    """
    total = float(peak_ratio) + float(flat_ratio) + float(valley_ratio)
    if abs(total - 1.0) > 1e-9:
        raise ValidationError(
            f"峰、平、谷电量比例之和必须等于 1（当前为 {total:.6f}）",
            field="tou_ratios",
        )
    return (
        float(peak_price) * float(peak_ratio)
        + float(flat_price) * float(flat_ratio)
        + float(valley_price) * float(valley_ratio)
    )


def resolve_tariff(
    tariff_mode: str,
    average_price: float = 0.0,
    peak_price: float = 0.0,
    flat_price: float = 0.0,
    valley_price: float = 0.0,
    peak_ratio: float = 0.0,
    flat_ratio: float = 0.0,
    valley_ratio: float = 0.0,
    market_price: float = 0.0,
    custom_avoided_price: float | None = None,
    custom_charge_price: float | None = None,
    export_price: float = 0.0,
    avoided_price_override: float | None = None,
    charge_price_override: float | None = None,
) -> TariffResolution:
    """按电价模式解析出三个关键电价（规范 §29–§33）。

    推导规则（全部写死在此处，便于审计）：

    ==========  ==============================  ==========================
    模式        替代电价 avoided                充电电价 charge
    ==========  ==============================  ==========================
    FIXED       ``average_price``                ``average_price``
    TOU         ``AverageTOUPrice``（§32）       ``valley_price``（谷充）
    MARKET      ``market_price``                 ``market_price``
    CUSTOM      ``custom_avoided_price``         ``custom_charge_price``
    ==========  ==============================  ==========================

    显式覆盖：``avoided_price_override`` / ``charge_price_override`` 优先于上述推导结果，
    保证用户可以自行指定（规范 §85：用户明确修改的值优先）。
    """
    mode = str(tariff_mode).upper()
    tou_avg = 0.0
    if mode == "FIXED":
        avoided, charge, basis = average_price, average_price, "固定电价"
    elif mode == "TOU":
        tou_avg = average_tou_price(
            peak_price, flat_price, valley_price, peak_ratio, flat_ratio, valley_ratio
        )
        avoided, charge, basis = tou_avg, valley_price, "峰平谷电价（替代价取综合电价，充电取谷价）"
    elif mode == "MARKET":
        avoided, charge, basis = market_price, market_price, "市场电价"
    elif mode == "CUSTOM":
        if custom_avoided_price is None or custom_charge_price is None:
            raise ValidationError(
                "自定义电价模式必须同时提供替代电价与充电电价", field="custom_tariff"
            )
        avoided, charge, basis = custom_avoided_price, custom_charge_price, "自定义电价"
    else:
        raise ValidationError(f"不支持的电价模式：{tariff_mode}", field="tariff_mode")

    if avoided_price_override is not None:
        avoided = avoided_price_override
        basis += "；替代电价已被用户覆盖"
    if charge_price_override is not None:
        charge = charge_price_override
        basis += "；充电电价已被用户覆盖"

    return TariffResolution(
        avoided_electricity_price=float(avoided),
        charge_price=float(charge),
        export_price=float(export_price),
        average_tou_price=float(tou_avg),
        basis=basis,
    )


def self_use_revenue(self_use_energy_kwh: float, avoided_electricity_price: float) -> float:
    """光伏自用收益（规范 §27）：``PVSelfUse × AvoidedElectricityPrice``。"""
    return float(self_use_energy_kwh) * float(avoided_electricity_price)


def export_revenue(export_energy_kwh: float, export_price: float) -> float:
    """余电上网收益（规范 §28）：``PVExport × ExportPrice``。"""
    return float(export_energy_kwh) * float(export_price)


def storage_arbitrage_revenue(
    discharge_energy_kwh: float,
    discharge_avoided_price: float,
    charge_energy_kwh: float,
    charge_price: float,
) -> float:
    """储能套利收益（规范 §43）。

    ``StorageArbitrageRevenue = Edis × DischargeAvoidedPrice - Echg × ChargePrice``

    口径说明（写入报告）：V1 按规范 §43 原文实现——**全部充电量按 ChargePrice 计价**，
    包含由光伏转入储能的电量；该部分电量在光伏侧不计自用收益（规范 §47），因此不存在重复计算。
    """
    return float(discharge_energy_kwh) * float(discharge_avoided_price) - float(
        charge_energy_kwh
    ) * float(charge_price)


def storage_total_revenue(
    arbitrage_revenue: float,
    capacity_revenue: float,
    ancillary_revenue: float,
    other_revenue: float,
) -> float:
    """储能总收益（规范 §44），四类收益必须分开记录。"""
    return (
        float(arbitrage_revenue)
        + float(capacity_revenue)
        + float(ancillary_revenue)
        + float(other_revenue)
    )
