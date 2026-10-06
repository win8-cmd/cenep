"""光伏模块（规范 §18–§25、§146）。

本模块只做纯函数计算：输入标量，输出标量/数据类，不依赖 Pydantic、不依赖 GUI。

公式对应关系
------------
* 容量解析            §19
* 首年发电量          §20  ``G1 = Ppv × H × PR × (1 - C)``
* 逐年衰减            §22  ``Gn = G1 × (1 - d)^(n-1)``
* 电量分配            §23  ``G = 直接自用 + 进储能 + 余电上网 + 损耗``
* 自发自用比例        §24  ``PVSelfUse = min(G × ratio, Load)``
"""

from __future__ import annotations

from dataclasses import dataclass

# 电量分配守恒判定阈值（规范 §113）
ENERGY_BALANCE_TOLERANCE = 1e-6


@dataclass(frozen=True)
class PvAllocation:
    """光伏年度电量分配结果（单位：kWh）。"""

    generation: float
    direct_use: float
    to_storage: float
    export: float
    loss: float

    @property
    def balance_error(self) -> float:
        """分配误差：``generation - (direct_use + to_storage + export + loss)``。"""
        return self.generation - (self.direct_use + self.to_storage + self.export + self.loss)

    def is_balanced(self, tolerance: float = ENERGY_BALANCE_TOLERANCE) -> bool:
        return abs(self.balance_error) <= tolerance


def resolve_pv_capacity(
    pv_capacity_kwp: float | None,
    usable_roof_area_m2: float | None,
    area_per_kwp: float | None,
) -> tuple[float, str]:
    """解析光伏装机容量（规范 §19）。

    优先使用直接输入的 ``pv_capacity_kwp``；否则按面积换算 ``UsableRoofArea / AreaPerKWp``。

    返回 ``(容量 kWp, 取值依据)``，依据取值 ``"INPUT"`` 或 ``"AREA"``。
    """
    if pv_capacity_kwp is not None and pv_capacity_kwp > 0:
        return float(pv_capacity_kwp), "INPUT"
    if usable_roof_area_m2 is not None and usable_roof_area_m2 > 0 and area_per_kwp is not None and area_per_kwp > 0:
        return float(usable_roof_area_m2) / float(area_per_kwp), "AREA"
    return 0.0, "NONE"


def first_year_generation(
    pv_capacity_kwp: float,
    equivalent_hours: float,
    performance_ratio: float,
    curtailment_rate: float,
) -> float:
    """首年光伏发电量（规范 §20）。

    ``G1 = Ppv × H × PR × (1 - C)``；单位 kWh。

    规范 §21 提醒：若用户输入的等效利用小时已是最终可利用小时，则 ``PR = 1``，
    本函数不做任何额外扣减，完全由入参决定。
    """
    return float(pv_capacity_kwp) * float(equivalent_hours) * float(performance_ratio) * (
        1.0 - float(curtailment_rate)
    )


def degradation_factor(annual_degradation_rate: float, year: int) -> float:
    """第 ``year`` 年的衰减系数 ``(1 - d)^(year-1)``（规范 §22、§45）。"""
    if year < 1:
        raise ValueError("year 必须从 1 开始（Year 0 为建设期）")
    return (1.0 - float(annual_degradation_rate)) ** (year - 1)


def generation_for_year(first_year_energy: float, annual_degradation_rate: float, year: int) -> float:
    """第 ``year`` 年发电量（规范 §22）: ``Gn = G1 × (1 - d)^(n-1)``。"""
    return float(first_year_energy) * degradation_factor(annual_degradation_rate, year)


def allocate_pv_energy(
    generation: float,
    load: float,
    self_consumption_ratio: float,
    storage_charge_headroom: float = 0.0,
    loss_ratio: float = 0.0,
) -> PvAllocation:
    """光伏电量分配（规范 §23–§25、§47）。

    分配顺序（规范 §46）：**直接自用 → 储能 → 余电上网**。

    1. ``target_direct = generation × self_consumption_ratio``
    2. ``direct_use = min(target_direct, load)`` —— 不能超过用户负荷
    3. ``remaining = generation - direct_use``
    4. ``to_storage = min(remaining, storage_charge_headroom)`` —— 受储能年充电能力限制
    5. ``export = remaining - to_storage - loss``
    6. ``loss = generation × loss_ratio``

    规范 §47 保证：进入储能的电量**不计**自用收益，自用电量**不计**储能收益，
    因此同一度电只会产生一条收益。
    """
    generation = max(float(generation), 0.0)
    load = max(float(load), 0.0)
    ratio = min(max(float(self_consumption_ratio), 0.0), 1.0)
    headroom = max(float(storage_charge_headroom), 0.0)

    loss = generation * max(float(loss_ratio), 0.0)
    usable = max(generation - loss, 0.0)

    target_direct = usable * ratio
    direct_use = min(target_direct, load)
    remaining = max(usable - direct_use, 0.0)
    to_storage = min(remaining, headroom)
    export = max(remaining - to_storage, 0.0)

    return PvAllocation(
        generation=generation,
        direct_use=direct_use,
        to_storage=to_storage,
        export=export,
        loss=loss,
    )
