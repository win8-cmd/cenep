"""储能状态与 SOC 模型（V2 §10、§11、§24、§25、§26）。

这是 V2 最重要的新增模块：储能不再用「容量 × 循环次数 × 峰谷价差」估算，
而是建立真正的 SOC（State of Charge）时序模型。

核心公式（V2 §10）
-----------------
====================  ==================================================
可用容量              ``E_usable = E_rated × (SOC_max − SOC_min)``
充电（AC 侧输入）     ``E_storage_increase = P_charge × η_charge × Δt``
放电（AC 侧输出）     ``E_storage_decrease = P_discharge / η_discharge × Δt``
SOC 递推              ``SOC_end = SOC_start + (increase − decrease) / E_rated``
约束                  ``SOC_min ≤ SOC_end ≤ SOC_max``
====================  ==================================================

两个必须注意的方向性约定（V2 §10.3、§10.4）
------------------------------------------
* **充电乘效率**：给储能注入 ``E`` 度电，电池内部只增加 ``E × η_charge``；
* **放电除效率**：要从储能取出 ``E`` 度电，电池内部要减少 ``E / η_discharge``。
  这个方向常被写反，写反会让往返效率从 ``η²`` 变成 ``1/η²`` 量级。

功率约束（V2 §11）
-----------------
``ChargeEnergy ≤ MaxChargePower × Δt``、``DischargeEnergy ≤ MaxDischargePower × Δt``，
且同时受 SOC 边界限制。``max_*_power`` 为 0 时表示按储能额定功率取值。

衰减与更换（V2 §25、§26）
-----------------------
``AvailableCapacity_n = InitialCapacity × (1 − degradation_rate)^(n−1)``；
到达 ``replacement_year`` 后容量**复位**为初始值（V1 的 ``storage.available_energy_for_year``
已实现同一口径，本模块保持一致）。

本模块全部为**纯函数 + NumPy 向量化**，不持有状态、不逐时循环（V2 §86、§87）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .timeseries_engine import annual_growth_factor


# --------------------------------------------------------------------------- #
# 容量与可用电量
# --------------------------------------------------------------------------- #
def usable_energy(capacity_kwh: float, soc_min: float, soc_max: float) -> float:
    """可用容量 ``E_usable = E_rated × (SOC_max − SOC_min)``（V2 §10.2）。"""
    return float(capacity_kwh) * (float(soc_max) - float(soc_min))


def capacity_for_year(
    initial_capacity_kwh: float,
    degradation_rate: float,
    year_index: int,
    replacement_year: int | None = None,
    replacement_capacity_kwh: float | None = None,
) -> float:
    """第 n 年的储能可用容量（V2 §25、§26）。

    ``replacement_year`` 之后容量复位为 ``replacement_capacity_kwh``（默认回到初始容量）。
    ``year_index`` 从 1 开始，第 1 年恒为初始容量。
    """
    if replacement_year is not None and year_index >= replacement_year:
        base = (
            float(replacement_capacity_kwh)
            if replacement_capacity_kwh is not None
            else float(initial_capacity_kwh)
        )
        return base
    return float(initial_capacity_kwh) * annual_growth_factor(-float(degradation_rate), year_index)


def equivalent_cycles(annual_discharge_kwh: float, usable_energy_kwh: float) -> float:
    """等效循环次数 ``= 年放电量 / 可用容量``（V2 §24）。

    必须同时提供「配置循环次数」与「实际等效循环次数」（V2 §24），
    后者由本函数按真实放电量算出。
    """
    if usable_energy_kwh <= 0.0:
        return 0.0
    return float(annual_discharge_kwh) / float(usable_energy_kwh)


# --------------------------------------------------------------------------- #
# 单个周期的功率与 SOC 约束
# --------------------------------------------------------------------------- #
def max_charge_energy_ac(
    soc_start: np.ndarray,
    *,
    capacity_kwh: float,
    soc_max: float,
    eta_charge: float,
    max_charge_power_kw: float,
    delta_hours: float,
) -> np.ndarray:
    """单周期**AC 侧**最大可充电量（V2 §10.3、§11）。

    = ``min(功率上限 × Δt, 填满可用 SOC 空间所需注入的 AC 电量)``

    需要的 AC 注入量由「电池内部还能装多少」反推：
    ``E_ac = (SOC_max − SOC_start) × E_rated / η_charge``（因充电时内部增量 = E_ac × η）。
    """
    power_limit = max(float(max_charge_power_kw), 0.0) * float(delta_hours)
    room_kwh = np.maximum(float(soc_max) - soc_start, 0.0) * float(capacity_kwh)
    room_ac = room_kwh / float(eta_charge) if eta_charge > 0 else np.zeros_like(room_kwh)
    return np.minimum(room_ac, power_limit)


def max_discharge_energy_ac(
    soc_start: np.ndarray,
    *,
    capacity_kwh: float,
    soc_min: float,
    eta_discharge: float,
    max_discharge_power_kw: float,
    delta_hours: float,
) -> np.ndarray:
    """单周期**AC 侧**最大可放电量（V2 §10.4、§11）。

    = ``min(功率上限 × Δt, 放空可用 SOC 空间能送出的 AC 电量)``

    可送出的 AC 电量 = ``(SOC_start − SOC_min) × E_rated × η_discharge``
    （因放电时内部减少量 = ``E_ac / η``，故 ``E_ac = 内部可用量 × η``）。
    """
    power_limit = max(float(max_discharge_power_kw), 0.0) * float(delta_hours)
    available_kwh = np.maximum(soc_start - float(soc_min), 0.0) * float(capacity_kwh)
    return np.minimum(available_kwh * float(eta_discharge), power_limit)


def soc_after(
    soc_start: np.ndarray,
    charge_ac: np.ndarray,
    discharge_ac: np.ndarray,
    *,
    capacity_kwh: float,
    eta_charge: float,
    eta_discharge: float,
) -> np.ndarray:
    """SOC 递推（V2 §10.5）。

    ``SOC_end = SOC_start + (充电AC×η_charge − 放电AC/η_discharge) / E_rated``
    """
    if capacity_kwh <= 0:
        return np.array(soc_start, dtype=float)
    increase = np.asarray(charge_ac, dtype=float) * float(eta_charge)
    decrease = (
        np.asarray(discharge_ac, dtype=float) / float(eta_discharge)
        if eta_discharge > 0
        else np.zeros_like(increase)
    )
    return np.asarray(soc_start, dtype=float) + (increase - decrease) / float(capacity_kwh)


def clip_soc(soc: np.ndarray, soc_min: float, soc_max: float) -> np.ndarray:
    """把 SOC 夹在 ``[SOC_min, SOC_max]`` 内（V2 §10.5 约束）。"""
    return np.clip(soc, float(soc_min), float(soc_max))


def internal_energy_delta(
    charge_ac: np.ndarray, discharge_ac: np.ndarray, *, eta_charge: float, eta_discharge: float
) -> np.ndarray:
    """电池内部净增电量（kWh），用于能量平衡校验（V2 §19）。"""
    increase = np.asarray(charge_ac, dtype=float) * float(eta_charge)
    decrease = (
        np.asarray(discharge_ac, dtype=float) / float(eta_discharge)
        if eta_discharge > 0
        else np.zeros_like(increase)
    )
    return increase - decrease


# --------------------------------------------------------------------------- #
# 时序仿真结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StorageSeries:
    """储能逐时仿真结果（V2 §6 的储能相关列）。

    全部数组长度 = 时间轴点数。电量单位 kWh（AC 侧），SOC 为额定容量占比（0~1）。
    """

    capacity_kwh: float
    usable_energy_kwh: float
    soc_start: np.ndarray
    soc_end: np.ndarray
    charge_ac: np.ndarray
    discharge_ac: np.ndarray
    grid_charge_ac: np.ndarray
    pv_charge_ac: np.ndarray

    @property
    def total_charge(self) -> float:
        return float(np.sum(self.charge_ac))

    @property
    def total_discharge(self) -> float:
        return float(np.sum(self.discharge_ac))

    @property
    def total_grid_charge(self) -> float:
        """电网充电电量，必须单独统计，避免误算成"光伏储能"（V2 §21）。"""
        return float(np.sum(self.grid_charge_ac))

    @property
    def total_pv_charge(self) -> float:
        return float(np.sum(self.pv_charge_ac))

    @property
    def equivalent_cycles(self) -> float:
        """实际等效循环次数（V2 §24）。"""
        return equivalent_cycles(self.total_discharge, self.usable_energy_kwh)

    def soc_min_actual(self) -> float:
        return float(np.min(self.soc_end)) if self.soc_end.size else 0.0

    def soc_max_actual(self) -> float:
        return float(np.max(self.soc_end)) if self.soc_end.size else 0.0


def empty_series(point_count: int, capacity_kwh: float = 0.0, soc: float = 0.0) -> StorageSeries:
    """无储能项目使用的空序列，保持下游数组形状一致。"""
    zeros = np.zeros(point_count, dtype=float)
    socs = np.full(point_count, float(soc), dtype=float)
    return StorageSeries(
        capacity_kwh=float(capacity_kwh),
        usable_energy_kwh=0.0,
        soc_start=socs.copy(),
        soc_end=socs.copy(),
        charge_ac=zeros.copy(),
        discharge_ac=zeros.copy(),
        grid_charge_ac=zeros.copy(),
        pv_charge_ac=zeros.copy(),
    )


__all__ = [
    "StorageSeries",
    "capacity_for_year",
    "clip_soc",
    "empty_series",
    "equivalent_cycles",
    "internal_energy_delta",
    "max_charge_energy_ac",
    "max_discharge_energy_ac",
    "soc_after",
    "usable_energy",
]
