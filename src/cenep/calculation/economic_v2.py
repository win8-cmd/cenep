"""时序驱动的经济评价（V2 §27–§31、§41、§42、§86）。

本模块把 8760 时序结果汇总成年度经济口径，并**严格避免重复计算**。

口径的权威定义（唯一有现金意义的一条）
------------------------------------
==============================  ============================================================
基准电费（无 PV、无储能）        ``BaselineCost = Σ_t (Load_t × Price_t)``
项目实际电费                    ``ActualCost   = Σ_t (GridImport_t × Price_t)``
**电费节省**                    ``Saving = BaselineCost − ActualCost``
==============================  ============================================================

分解口径（供报告展示，V2 §27、§28、§31）
--------------------------------------
::

    电费节省 = 光伏自用节省 + 储能套利收益

* ``PVSelfConsumptionSaving = Σ_t (PV_to_load_t × Price_t)``
* ``StorageArbitrageRevenue = Σ_t (LoadFromStorage_t × Price_t)
                              − Σ_t (GridToStorage_t × Price_t)``

**关键计价规则（避免重复计算）**：光伏给储能充电的电量按 **0 计价**，
电网给储能充电的电量按当期购电价计价。理由：

* 光伏电量若去了储能，其价值**只在放电时**通过"替代购电"体现一次；
  若同时在充电侧再记一次收入或成本，就会与 ``PVSelfConsumptionSaving`` 重复；
* 按此规则，上述两项之和**恒等于**总电费节省额（本模块用
  :func:`check_saving_identity` 强制校验，误差超限即判定计算失败）。
* 光伏→储能的**机会成本**（本可上网的收益）不在分解口径中扣除，
  属于口径选择而非错误；该取舍已在 ``notes`` 中披露。

其余收益项（V2 §27、§31）
------------------------
储能容量收益、辅助服务收益、其他收益来自项目参数（沿用 V1 口径）；
上网收入 ``PVExportRevenue = Σ_t (GridExport_t × ExportPrice_t)``；
需量节省 ``DemandSaving`` 按 §30 单独计算（容量电费与电量电费分开）。

性能（V2 §86、§87）
------------------
全部汇总用 NumPy 向量化；25 年 × 8760 点的编排见 :func:`simulate_project`。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from ..domain.enums import DispatchAction, Resolution
from ..domain.models import Project
from ..domain.timeseries_results import (
    BaselineResult,
    DataQualityScore,
    DispatchDecision,
    EnergyBalance,
    TimeSeriesMetrics,
    TimeSeriesResultSet,
)
from . import energy_balance as balance_mod
from . import load_profile as load_mod
from . import pv_profile as pv_mod
from . import storage_soc as soc_mod
from . import tariff_series as tariff_mod
from .dispatch_engine import DispatchOutcome, dispatch
from .timeseries_engine import TimeAxis, build_time_axis

#: 分解口径与总节省额允许的最大偏差（元）。超过即判定计算失败（V2 §19 精神）
SAVING_IDENTITY_TOLERANCE = 1e-6


@dataclass
class YearSimulation:
    """单个运营年度的时序仿真结果。

    **不**持有 8760 个 :class:`DispatchDecision` 对象（V2 §87 内存要求）：
    逐时动作与原因以列式数组存放在 ``outcome.action_codes`` / ``outcome.reasons``，
    需要行视图时用 :meth:`decisions` 按区间物化。
    25 年 × 8760 个 Pydantic 对象会占约 110 MB，没有必要。
    """

    year_index: int
    axis: TimeAxis
    load: np.ndarray
    pv: np.ndarray
    tariff: tariff_mod.TariffSeries
    outcome: DispatchOutcome
    balance: EnergyBalance
    metrics: TimeSeriesMetrics
    baseline: BaselineResult

    @property
    def usable_capacity_kwh(self) -> float:
        return float(self.outcome.storage.usable_energy_kwh)

    @property
    def dispatch_action_codes(self) -> list[str]:
        return list(self.outcome.action_codes)

    @property
    def dispatch_reasons(self) -> list[str]:
        return list(self.outcome.reasons)

    def decisions(self, start: int = 0, end: int | None = None) -> list[DispatchDecision]:
        """按区间物化逐时调度决策（V2 §16）。

        默认区间取**首 168 小时（一周）**——报告与界面明细的常用窗口；
        传 ``end=len(self)`` 可取整年，但会创建 8760 个对象。
        """
        stop = 168 if end is None else end
        stop = min(stop, self.axis.point_count)
        codes = self.outcome.action_codes
        return [
            DispatchDecision(
                timestamp=self.axis.timestamps[i],
                action=DispatchAction(codes[i]) if codes[i] else DispatchAction.IDLE,
                reason=self.outcome.reasons[i],
                reason_code=self.outcome.reason_codes[i],
                charge_energy=float(self.outcome.storage.charge_ac[i]),
                discharge_energy=float(self.outcome.storage.discharge_ac[i]),
            )
            for i in range(max(start, 0), stop)
        ]

    def reason_summary(self) -> dict[str, int]:
        """各原因码出现的次数，用于报告中的调度行为统计。"""
        summary: dict[str, int] = {}
        for code in self.outcome.reason_codes:
            summary[code] = summary.get(code, 0) + 1
        return dict(sorted(summary.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- #
# 基准方案（V2 §42、§28、§30）
# --------------------------------------------------------------------------- #
def compute_baseline(
    load: np.ndarray, tariff: tariff_mod.TariffSeries, axis: TimeAxis
) -> BaselineResult:
    """基准方案：无 PV、无储能，全部负荷由电网供应（V2 §42）。

    需量按 §18 逐月取最大购电功率；电量电费与需量电费分开统计（§28、§30）。
    """
    price = np.asarray(tariff.price, dtype=float)
    energy = np.asarray(load, dtype=float)
    delta = axis.delta_hours if axis.resolution is not Resolution.MONTHLY else 1.0

    energy_cost = float(np.sum(energy * price))
    power = energy / delta if delta > 0 else energy
    monthly = [float(power[a:b].max()) if b > a else 0.0 for a, b in axis.month_index_ranges()]
    demand_charge = float(tariff.demand_charge) if tariff.demand_charge_enabled else 0.0

    return BaselineResult(
        annual_load=float(energy.sum()),
        annual_electricity_cost=energy_cost,
        annual_demand_cost=float(sum(d * demand_charge for d in monthly)),
        peak_demand_kw=max(monthly) if monthly else 0.0,
        monthly_max_demand=monthly,
    )


# --------------------------------------------------------------------------- #
# 指标汇总（V2 §41、§22、§23、§24、§27–§31）
# --------------------------------------------------------------------------- #
def compute_metrics(
    outcome: DispatchOutcome,
    axis: TimeAxis,
    baseline: BaselineResult,
    *,
    configured_cycles: float = 0.0,
    demand_charge: float = 0.0,
    capacity_revenue: float = 0.0,
    ancillary_revenue: float = 0.0,
    other_revenue: float = 0.0,
) -> TimeSeriesMetrics:
    """把逐时结果汇总为 V2 §41 的关键指标。

    :param demand_charge: 需量电价 元/kW·月，取自电价模板（V2 §18）；
        为 0 表示不计需量电费。
    """
    storage = outcome.storage
    price = np.asarray(outcome.price, dtype=float)
    export_price = np.asarray(outcome.export_price, dtype=float)
    delta = axis.delta_hours if axis.resolution is not Resolution.MONTHLY else 1.0

    pv_generation = float(np.sum(outcome.pv))
    pv_to_load = float(np.sum(outcome.pv_to_load))
    load_total = float(np.sum(outcome.load))
    grid_import = float(np.sum(outcome.grid_import))
    grid_export = float(np.sum(outcome.grid_export))
    action_codes = list(outcome.action_codes)

    actual_energy_cost = float(np.sum(outcome.grid_import * price))
    saving = baseline.annual_electricity_cost - actual_energy_cost

    pv_self_use_saving = float(np.sum(outcome.pv_to_load * price))
    # 光伏→储能按 0 计价、电网→储能按购电价计价（见模块文档，避免重复计算）
    arbitrage = float(
        np.sum(outcome.load_from_storage * price) - np.sum(outcome.grid_to_storage * price)
    )
    export_revenue = float(np.sum(outcome.grid_export * export_price))

    power = np.asarray(outcome.grid_import, dtype=float) / delta if delta > 0 else outcome.grid_import
    monthly_after = [
        float(power[a:b].max()) if b > a else 0.0 for a, b in axis.month_index_ranges()
    ]
    actual_demand_cost = float(sum(d * float(demand_charge) for d in monthly_after))

    metrics = TimeSeriesMetrics(
        annual_pv_generation=pv_generation,
        annual_pv_curtailment=float(np.sum(outcome.pv_curtailed)),
        annual_grid_purchase=grid_import,
        annual_grid_export=grid_export,
        annual_load=load_total,
        annual_storage_charge=storage.total_charge,
        annual_storage_discharge=storage.total_discharge,
        annual_grid_charge=storage.total_grid_charge,
        configured_cycles=float(configured_cycles),
        equivalent_cycles=storage.equivalent_cycles,
        self_consumption_rate=(pv_to_load / pv_generation) if pv_generation > 0 else 0.0,
        self_sufficiency_rate=(pv_to_load / load_total) if load_total > 0 else 0.0,
        peak_demand_before=baseline.peak_demand_kw,
        peak_demand_after=max(monthly_after) if monthly_after else 0.0,
        demand_saving=baseline.peak_demand_kw - (max(monthly_after) if monthly_after else 0.0),
        monthly_max_demand_before=list(baseline.monthly_max_demand),
        monthly_max_demand_after=monthly_after,
        baseline_electricity_cost=baseline.annual_electricity_cost,
        actual_electricity_cost=actual_energy_cost,
        electricity_cost_saving=saving,
        baseline_demand_cost=baseline.annual_demand_cost,
        actual_demand_cost=actual_demand_cost,
        demand_cost_saving=baseline.annual_demand_cost - actual_demand_cost,
        pv_self_consumption_saving=pv_self_use_saving,
        pv_export_revenue=export_revenue,
        storage_arbitrage_revenue=arbitrage,
        storage_capacity_revenue=float(capacity_revenue),
        storage_ancillary_revenue=float(ancillary_revenue),
        other_revenue=float(other_revenue),
    )
    metrics.total_benefit = (
        pv_self_use_saving
        + export_revenue
        + arbitrage
        + float(capacity_revenue)
        + float(ancillary_revenue)
        + float(other_revenue)
        + metrics.demand_cost_saving
    )
    return metrics


def check_saving_identity(metrics: TimeSeriesMetrics, tolerance: float = SAVING_IDENTITY_TOLERANCE) -> float:
    """校验「总电费节省 = 光伏自用节省 + 储能套利收益」（避免重复计算）。

    返回偏差（元）；调用方应在偏差超过 ``tolerance`` 时判定计算失败。
    """
    decomposed = metrics.pv_self_consumption_saving + metrics.storage_arbitrage_revenue
    return abs(decomposed - metrics.electricity_cost_saving)


# --------------------------------------------------------------------------- #
# 单年仿真编排（V2 §7、§8、§9、§17、§3 P0.5–P0.8）
# --------------------------------------------------------------------------- #
def simulate_year(
    project: Project,
    axis: TimeAxis,
    year_index: int = 1,
    *,
    quality: DataQualityScore | None = None,
) -> YearSimulation:
    """对给定年度跑一次完整 8760 时序仿真。"""
    ts = project.timeseries
    capacity_kwp = float(ts.pv.capacity_kwp or project.pv.pv_capacity_kwp or 0.0)

    load = load_mod.resolve_load(ts.load, axis, year_index)
    pv = pv_mod.resolve_pv(
        ts.pv, axis, capacity_kwp, float(project.pv.annual_degradation_rate), year_index
    )
    tariff = tariff_mod.resolve_tariff(ts.tariff, axis, year_index)

    outcome = dispatch(
        load=load,
        pv=pv,
        tariff=tariff,
        axis=axis,
        config=ts.dispatch,
        storage_capacity_kwh=float(project.storage.storage_energy_kwh),
        storage_power_kw=float(project.storage.storage_power_kw),
        storage_degradation_rate=float(project.storage.annual_degradation_rate),
        replacement_year=project.storage.replacement_year,
        year_index=year_index,
    )

    balance = balance_mod.check_balance(outcome, tolerance=float(ts.balance_tolerance))
    baseline = compute_baseline(load, tariff, axis)
    metrics = compute_metrics(
        outcome,
        axis,
        baseline,
        configured_cycles=float(project.storage.annual_cycles),
        demand_charge=float(tariff.demand_charge) if tariff.demand_charge_enabled else 0.0,
        capacity_revenue=float(project.storage.annual_capacity_revenue),
        ancillary_revenue=float(project.storage.annual_ancillary_revenue),
        other_revenue=float(project.storage.annual_other_revenue),
    )
    deviation = check_saving_identity(metrics)
    if deviation > max(SAVING_IDENTITY_TOLERANCE, 1e-9) * max(
        1.0, abs(metrics.electricity_cost_saving)
    ):
        raise ValueError(
            f"第 {year_index} 年电费节省分解不闭合："
            f"总节省 {metrics.electricity_cost_saving:.6f} 元，"
            f"分解之和 {metrics.pv_self_consumption_saving + metrics.storage_arbitrage_revenue:.6f} 元，"
            f"偏差 {deviation:.6f} 元 —— 存在重复计算或漏计"
        )

    _ = quality
    return YearSimulation(
        year_index=year_index,
        axis=axis,
        load=load,
        pv=pv,
        tariff=tariff,
        outcome=outcome,
        balance=balance,
        metrics=metrics,
        baseline=baseline,
    )


def to_result_set(sim: YearSimulation) -> TimeSeriesResultSet:
    """把单年仿真结果转成列式结果集（V2 §6、§62）。"""
    o = sim.outcome
    columns: dict[str, list[float]] = {
        "load": sim.load.tolist(),
        "pv_generation": o.pv.tolist(),
        "pv_to_load": o.pv_to_load.tolist(),
        "pv_to_storage": o.pv_to_storage.tolist(),
        "pv_to_grid": o.pv_to_grid.tolist(),
        "pv_curtailed": o.pv_curtailed.tolist(),
        "grid_to_load": o.grid_to_load.tolist(),
        "grid_to_storage": o.grid_to_storage.tolist(),
        "storage_charge": o.storage.charge_ac.tolist(),
        "storage_discharge": o.storage.discharge_ac.tolist(),
        "storage_soc_start": o.storage.soc_start.tolist(),
        "storage_soc_end": o.storage.soc_end.tolist(),
        "load_from_storage": o.load_from_storage.tolist(),
        "grid_import": o.grid_import.tolist(),
        "grid_export": o.grid_export.tolist(),
        "electricity_cost": o.electricity_cost.tolist(),
        "export_revenue": o.export_revenue.tolist(),
        "storage_revenue": o.storage_revenue.tolist(),
        "total_revenue": o.total_revenue.tolist(),
        "net_energy_cost": o.net_energy_cost.tolist(),
        "electricity_price": o.price.tolist(),
        "export_price": o.export_price.tolist(),
    }
    return TimeSeriesResultSet.from_columns(
        timestamps=list(sim.axis.timestamps),
        columns=columns,
        resolution=sim.axis.resolution,
        base_year=sim.axis.year,
        dispatch_actions=list(o.action_codes),
        dispatch_reasons=list(o.reasons),
    )


def build_axis(project: Project) -> TimeAxis:
    """按项目配置构造时间轴（V2 §7）。"""
    ts = project.timeseries
    return build_time_axis(ts.base_year, ts.resolution, tuple(ts.holidays))


# --------------------------------------------------------------------------- #
# 逐年收益项（供 V1 年度经济模型注入，V2 §27–§32）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class YearRevenue:
    """某一运营年度的收益分解（V2 §27、§31）。

    引擎用这些项**替换**年度模型里的收益计算，其余（OPEX、折旧、税、融资、
    现金流、IRR/NPV）仍由 V1 的同一套代码计算，避免第二套财务实现（V2 §61）。
    """

    year_index: int
    pv_self_use: float
    pv_export: float
    storage_arbitrage: float
    storage_capacity: float
    storage_ancillary: float
    storage_other: float
    demand_saving: float

    @property
    def total(self) -> float:
        return (
            self.pv_self_use
            + self.pv_export
            + self.storage_arbitrage
            + self.storage_capacity
            + self.storage_ancillary
            + self.storage_other
            + self.demand_saving
        )


@dataclass(frozen=True)
class YearOverride:
    """单年度的**完整**时序口径覆盖（电量 + 收益），供 V1 年度模型注入。

    为什么要连电量一起覆盖：若只替换收益、却沿用 V1 的年度等效电量，
    报告里会出现"电量按 V1、钱按 V2"的口径撕裂（违反 V2 §105 结果一致性）。
    因此电量与收益必须同源。
    """

    year_index: int

    # ---- 电量（kWh）----
    load: float
    pv_generation: float
    pv_self_use: float
    pv_export: float
    pv_to_storage: float
    grid_import: float
    grid_export: float
    storage_charge: float
    storage_discharge: float
    grid_charge: float

    # ---- 收益（元）----
    pv_self_use_revenue: float
    pv_export_revenue: float
    storage_arbitrage_revenue: float
    storage_capacity_revenue: float
    storage_ancillary_revenue: float
    storage_other_revenue: float
    demand_saving: float

    def total_revenue(self) -> float:
        """总收益 = 光伏自用 + 上网 + 储能套利 + 储能三类收益 + 需量节省（V2 §31）。"""
        return (
            self.pv_self_use_revenue
            + self.pv_export_revenue
            + self.storage_arbitrage_revenue
            + self.storage_capacity_revenue
            + self.storage_ancillary_revenue
            + self.storage_other_revenue
            + self.demand_saving
        )


@dataclass
class ProjectSimulation:
    """一个项目的完整时序仿真（V2 §32：Year 0 投资，Year 1..N 运营）。"""

    axis: TimeAxis
    years: list[YearSimulation] = field(default_factory=list)
    reused_first_year: int = 0
    elapsed_seconds: float = 0.0

    @property
    def first_year(self) -> YearSimulation:
        return self.years[0]

    def revenues(self) -> list[YearRevenue]:
        """逐年收益分解，可直接注入年度经济模型。"""
        out: list[YearRevenue] = []
        for sim in self.years:
            m = sim.metrics
            out.append(
                YearRevenue(
                    year_index=sim.year_index,
                    pv_self_use=m.pv_self_consumption_saving,
                    pv_export=m.pv_export_revenue,
                    storage_arbitrage=m.storage_arbitrage_revenue,
                    storage_capacity=m.storage_capacity_revenue,
                    storage_ancillary=m.storage_ancillary_revenue,
                    storage_other=m.other_revenue,
                    demand_saving=m.demand_cost_saving,
                )
            )
        return out

    def total_energy_cost_saving(self) -> float:
        """全周期电费+需量节省合计（元），用于报告展示。"""
        return sum(
            s.metrics.electricity_cost_saving + s.metrics.demand_cost_saving for s in self.years
        )

    def overrides(self) -> dict[int, YearOverride]:
        """逐年覆盖表 ``{年份: YearOverride}``，直接传给 ``calculation_engine.calculate``。"""
        out: dict[int, YearOverride] = {}
        for sim in self.years:
            m = sim.metrics
            o = sim.outcome
            out[sim.year_index] = YearOverride(
                year_index=sim.year_index,
                load=m.annual_load,
                pv_generation=m.annual_pv_generation,
                pv_self_use=float(np.sum(o.pv_to_load)),
                pv_export=m.annual_grid_export,
                pv_to_storage=float(np.sum(o.pv_to_storage)),
                grid_import=m.annual_grid_purchase,
                grid_export=m.annual_grid_export,
                storage_charge=m.annual_storage_charge,
                storage_discharge=m.annual_storage_discharge,
                grid_charge=m.annual_grid_charge,
                pv_self_use_revenue=m.pv_self_consumption_saving,
                pv_export_revenue=m.pv_export_revenue,
                storage_arbitrage_revenue=m.storage_arbitrage_revenue,
                storage_capacity_revenue=m.storage_capacity_revenue,
                storage_ancillary_revenue=m.storage_ancillary_revenue,
                storage_other_revenue=m.other_revenue,
                demand_saving=m.demand_cost_saving,
            )
        return out

    def max_balance_error(self) -> float:
        return max((s.balance.error for s in self.years), default=0.0)


def _shapes_are_static(project: Project) -> bool:
    """负荷、光伏、电价三条曲线是否逐年等比例（决定能否复用首年仿真）。"""
    ts = project.timeseries
    return (
        abs(float(ts.load.annual_growth_rate)) < 1e-12
        and abs(float(project.pv.annual_degradation_rate)) < 1e-12
        and abs(float(ts.tariff.annual_growth_rate)) < 1e-12
        and abs(float(project.storage.annual_degradation_rate)) < 1e-12
    )


def simulate_project(project: Project, *, max_years: int | None = None) -> ProjectSimulation:
    """对一个项目跑完整运营期时序仿真（V2 §32、§86）。

    性能策略（V2 §86：单项目 8760 计算 < 2 秒）
    ------------------------------------------
    逐年仿真的成本约 130 ms/年，25 年将超过预算。因此：

    * 若负荷增长率、光伏衰减率、电价增长率、储能衰减率**全为 0**，
      则各年曲线形状与水平完全相同，**只跑首年**并复用其余年份
      （``reused_first_year`` 记录复用次数），结果与逐年仿真**逐位一致**；
    * 任一增长率非 0 时必须逐年仿真——此时曲线逐年变化，套利机会与自用率都会改变，
      复用会引入实质误差。
    """
    import time as _time

    ts = project.timeseries
    period = int(max_years if max_years is not None else project.analysis_period)
    axis = build_axis(project)
    start = _time.perf_counter()

    years: list[YearSimulation] = []
    static = _shapes_are_static(project)

    first = simulate_year(project, axis, 1)
    years.append(first)

    if static:
        # 各年曲线形状与水平完全相同（四条增长率全为 0），年份之间**逐位等价**，
        # 因此直接复用首年结果对象，仅改写年份编号（数组只读共享）。
        for year_index in range(2, period + 1):
            years.append(replace(first, year_index=year_index))
        reused = period - 1
    else:
        for year_index in range(2, period + 1):
            years.append(simulate_year(project, axis, year_index))
        reused = 0

    return ProjectSimulation(
        axis=axis,
        years=years,
        reused_first_year=reused,
        elapsed_seconds=_time.perf_counter() - start,
    )


__all__ = [
    "SAVING_IDENTITY_TOLERANCE",
    "YearSimulation",
    "build_axis",
    "check_saving_identity",
    "compute_baseline",
    "compute_metrics",
    "simulate_year",
    "to_result_set",
]
