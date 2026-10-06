"""V2 能量平衡引擎（V2 §19、§70）。

规范把能量守恒定为**最高级别测试条件**：

    PV + GridImport + StorageDischarge = Load + StorageCharge + GridExport + Curtailment

任何时点 ``BalanceError > tolerance``（默认 ``1e-6`` kWh）必须判定**计算失败**，
而不是给出一个"看起来差不多"的结果。

本模块同时产出 :class:`cenep.domain.timeseries_results.EnergyBalance` 汇总，
供「能源平衡页面」（§70）与报告使用。
"""

from __future__ import annotations

import logging

import numpy as np

from ..domain.timeseries_results import EnergyBalance
from .dispatch_engine import DispatchOutcome
from .errors import ValidationError

logger = logging.getLogger(__name__)

__all__ = ["balance_of", "check_balance", "supply_array", "demand_array"]


def supply_array(outcome: DispatchOutcome) -> np.ndarray:
    """供给侧逐时电量：``PV + 购电 + 储能放电``。"""
    return outcome.pv + outcome.grid_import + outcome.storage.discharge_ac


def demand_array(outcome: DispatchOutcome) -> np.ndarray:
    """需求侧逐时电量：``负荷 + 储能充电 + 上网 + 弃光``。"""
    return outcome.load + outcome.storage.charge_ac + outcome.grid_export + outcome.pv_curtailed


def balance_of(outcome: DispatchOutcome, tolerance: float = 1e-6) -> EnergyBalance:
    """计算能量平衡汇总（V2 §19、§70）。不抛异常，供报告与调试使用。"""
    supply = supply_array(outcome)
    demand = demand_array(outcome)
    diff = supply - demand
    error = float(np.sum(diff))
    max_hourly = float(np.max(np.abs(diff))) if diff.size else 0.0

    return EnergyBalance(
        pv_generation=float(np.sum(outcome.pv)),
        pv_to_load=float(np.sum(outcome.pv_to_load)),
        pv_to_storage=float(np.sum(outcome.pv_to_storage)),
        pv_to_grid=float(np.sum(outcome.pv_to_grid)),
        pv_curtailed=float(np.sum(outcome.pv_curtailed)),
        grid_to_load=float(np.sum(outcome.grid_to_load)),
        grid_to_storage=float(np.sum(outcome.grid_to_storage)),
        grid_import=float(np.sum(outcome.grid_import)),
        storage_charge=float(np.sum(outcome.storage.charge_ac)),
        storage_discharge=float(np.sum(outcome.storage.discharge_ac)),
        storage_to_load=float(np.sum(outcome.load_from_storage)),
        storage_to_grid=float(np.sum(outcome.storage_to_grid)),
        grid_export=float(np.sum(outcome.grid_export)),
        load_total=float(np.sum(outcome.load)),
        supply_total=float(np.sum(supply)),
        demand_total=float(np.sum(demand)),
        error=error,
        max_hourly_error=max_hourly,
        tolerance=tolerance,
        is_balanced=max_hourly <= tolerance,
    )


def check_balance(outcome: DispatchOutcome, tolerance: float = 1e-6) -> EnergyBalance:
    """校验能量守恒；超限即判定计算失败（V2 §19）。

    :raises ValidationError: 出现 ``BalanceError > tolerance`` 时，
        报错信息带出误差量级与首个越限时点，便于定位。
    """
    result = balance_of(outcome, tolerance)
    if result.is_balanced:
        logger.debug(
            "能量平衡校验通过：误差=%.3e（容差 %.1e）", result.max_hourly_error, tolerance
        )
        return result

    supply = supply_array(outcome)
    demand = demand_array(outcome)
    diff = np.abs(supply - demand)
    first = int(np.argmax(diff))
    raise ValidationError(
        f"能量平衡校验失败：最大逐时误差 {result.max_hourly_error:.6e} kWh "
        f"超过容差 {tolerance:.1e} kWh，首个越限时点下标 {first}"
        f"（供给侧 {supply[first]:.6f}，需求侧 {demand[first]:.6f}）",
        field="timeseries.energy_balance",
    )
