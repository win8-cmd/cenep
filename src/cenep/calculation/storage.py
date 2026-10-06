"""储能模块（规范 §37–§45、§146）。

V1 采用**年度等效循环模型**，不要求 8760 小时仿真（规范 §37）。

公式对应关系
------------
* 储能时长        §39  ``StorageDuration = StorageEnergy / StoragePower``
* 效率关系        §40  双向换算，且 ``round_trip = charge × discharge``
* 年放电量        §41  ``Edis = AvailableEnergy × DoD × Cycles × η_dis``
* 年充电量        §42  ``Echg = Edis / η_dis / η_chg``
* 逐年衰减        §45  ``Available_n = Initial × (1-d)^(n-1)``
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True)
class StorageYear:
    """储能某一年度的等效循环结果（单位：kWh）。"""

    year: int
    available_energy_kwh: float
    discharge_energy_kwh: float
    charge_energy_kwh: float
    is_replacement_year: bool


def storage_duration_hours(storage_energy_kwh: float, storage_power_kw: float) -> float:
    """储能时长（规范 §39）。功率为 0 时返回 0.0。"""
    if storage_power_kw <= 0:
        return 0.0
    return float(storage_energy_kwh) / float(storage_power_kw)


def resolve_efficiencies(
    charge_efficiency: float | None,
    discharge_efficiency: float | None,
    round_trip_efficiency: float | None,
) -> tuple[float, float, float]:
    """效率换算（规范 §40）。

    * 同时给出充电/放电效率 → ``round_trip = charge × discharge``
    * 只给往返效率 → ``charge = discharge = sqrt(round_trip)``

    返回 ``(charge_eff, discharge_eff, round_trip_eff)``。
    """
    if charge_efficiency is not None and discharge_efficiency is not None:
        c, d = float(charge_efficiency), float(discharge_efficiency)
        return c, d, c * d
    if round_trip_efficiency is not None:
        r = float(round_trip_efficiency)
        s = sqrt(r)
        return s, s, r
    if charge_efficiency is not None:
        c = float(charge_efficiency)
        return c, c, c * c
    if discharge_efficiency is not None:
        d = float(discharge_efficiency)
        return d, d, d * d
    raise ValueError("必须提供充电效率/放电效率，或往返效率")


def annual_discharge_energy(
    available_energy_kwh: float,
    depth_of_discharge: float,
    annual_cycles: float,
    discharge_efficiency: float,
) -> float:
    """年放电量（规范 §41）：``Available × DoD × Cycles × η_dis``，交流侧口径 kWh。"""
    return (
        max(float(available_energy_kwh), 0.0)
        * max(float(depth_of_discharge), 0.0)
        * max(float(annual_cycles), 0.0)
        * float(discharge_efficiency)
    )


def annual_charge_energy(
    discharge_energy_kwh: float,
    discharge_efficiency: float,
    charge_efficiency: float,
) -> float:
    """年充电量（规范 §42）：``Edis / η_dis / η_chg``，交流侧口径 kWh。"""
    if discharge_efficiency <= 0 or charge_efficiency <= 0:
        raise ValueError("充放电效率必须大于 0")
    return float(discharge_energy_kwh) / float(discharge_efficiency) / float(charge_efficiency)


def available_energy_for_year(
    initial_energy_kwh: float,
    annual_degradation_rate: float,
    year: int,
    replacement_year: int | None = None,
) -> tuple[float, bool]:
    """第 ``year`` 年可用储能容量（规范 §45）。

    ``Available_n = Initial × (1-d)^(n-1)``。

    关于 ``replacement_year``（规范 §38）：V1 的明确口径为——**更换电芯当年容量恢复至初始值**，
    并从该年起重新开始衰减；该年同时计入 ``ReplacementCAPEX``。这是与 §45 兼容的最简实现。

    返回 ``(可用容量 kWh, 是否更换年)``。
    """
    if year < 1:
        raise ValueError("year 必须从 1 开始（Year 0 为建设期）")
    is_replacement = replacement_year is not None and year == int(replacement_year)
    if replacement_year is not None and year >= int(replacement_year):
        base_year = int(replacement_year)
    else:
        base_year = 1
    available = float(initial_energy_kwh) * (1.0 - float(annual_degradation_rate)) ** (year - base_year)
    return max(available, 0.0), is_replacement


def storage_year_result(
    year: int,
    initial_energy_kwh: float,
    depth_of_discharge: float,
    annual_cycles: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    annual_degradation_rate: float,
    replacement_year: int | None = None,
) -> StorageYear:
    """把 §41、§42、§45 串起来，得到某一年储能等效循环结果。"""
    available, is_replacement = available_energy_for_year(
        initial_energy_kwh, annual_degradation_rate, year, replacement_year
    )
    discharge = annual_discharge_energy(available, depth_of_discharge, annual_cycles, discharge_efficiency)
    charge = annual_charge_energy(discharge, discharge_efficiency, charge_efficiency)
    return StorageYear(
        year=year,
        available_energy_kwh=available,
        discharge_energy_kwh=discharge,
        charge_energy_kwh=charge,
        is_replacement_year=is_replacement,
    )
