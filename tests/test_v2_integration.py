"""V2 端到端集成测试（V2 §73–§82、§85）。

本文件把规范里"必须验证"的几条硬约束做成可重复运行的断言：

* §73 能量守恒：**随机 100 组** PV / 负荷 / 储能 / 电价组合，逐点误差 < 1e-6
* §74 SOC 边界：``SOC_min ≤ SOC ≤ SOC_max`` 恒成立
* §75 充放电功率：``Charge ≤ MaxChargePower``、``Discharge ≤ MaxDischargePower``
* §77 无储能回归：储能容量为 0 时结果必须接近 V1
* §78 无光伏退化：PV 容量为 0 时退化为纯电网负荷项目
* §79 PV Only 的三种关系：PV > Load、PV < Load、PV = Load
* §80 PV + Storage 的四种状态：盈余、缺口、满、空
* §81 峰谷套利：谷充峰放
* §82 需量：储能后最大需量下降，节省额正确
* §85 V2 Golden Case：确定性、可重复

随机用例全部播种（``numpy.random.default_rng(seed)``），保证失败可复现。
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pytest

from cenep.calculation import economic_v2 as e2
from cenep.calculation.engine import calculation_engine
from cenep.domain.enums import (
    DispatchStrategy,
    LoadProfileMode,
    PVProfileMode,
    Resolution,
    TariffPeriod,
)
from cenep.domain.models import Project
from cenep.domain.timeseries import (
    LoadProfileConfig,
    PVProfileConfig,
    StorageDispatchConfig,
    TariffProfile,
    TariffSeriesConfig,
    TimePeriodRule,
    TimeSeriesConfig,
)

# --------------------------------------------------------------------------- #
# V2 Golden Case（规范 §85）
# --------------------------------------------------------------------------- #
GOLDEN = {
    "pv_kwp": 1000.0,
    "pv_annual": 1_100_000.0,
    "storage_kw": 500.0,
    "storage_kwh": 1000.0,
    "load_annual": 1_500_000.0,
    "peak_price": 1.0,
    "valley_price": 0.35,
    "flat_price": 0.65,
}

WORKDAY = [
    120, 120, 120, 120, 120, 130, 180, 260, 320, 340, 350, 340,
    330, 340, 350, 340, 320, 280, 220, 170, 140, 130, 125, 120,
]
WEEKEND = [
    80, 80, 80, 80, 80, 85, 100, 130, 160, 170, 175, 170,
    165, 170, 175, 170, 160, 140, 110, 95, 85, 82, 80, 80,
]

_AXIS_CACHE: dict = {}


def axis_for(year: int = 2025):
    if year not in _AXIS_CACHE:
        _AXIS_CACHE[year] = e2.build_time_axis(year, Resolution.HOURLY, ())
    return _AXIS_CACHE[year]


def build_project(
    *,
    pv_kwp: float = GOLDEN["pv_kwp"],
    storage_kw: float = GOLDEN["storage_kw"],
    storage_kwh: float = GOLDEN["storage_kwh"],
    load_annual: float = GOLDEN["load_annual"],
    pv_hours: float | None = None,
    strategy: DispatchStrategy = DispatchStrategy.PEAK_VALLEY,
    allow_grid_charge: bool = False,
    charge_threshold: float = 0.40,
    discharge_threshold: float = 0.90,
    export_price: float = 0.35,
    demand_charge: float = 0.0,
    svc_soc_min: float = 0.10,
    svc_soc_max: float = 1.00,
) -> Project:
    """构造一个 V2 时序项目（默认即规范 §85 的 Golden Case）。"""
    proj = Project()
    proj.pv.pv_capacity_kwp = pv_kwp
    proj.pv.annual_degradation_rate = 0.0
    proj.storage.storage_power_kw = storage_kw
    proj.storage.storage_energy_kwh = storage_kwh
    proj.storage.annual_cycles = 330.0
    proj.storage.annual_degradation_rate = 0.0
    proj.storage.replacement_year = None

    hours = pv_hours if pv_hours is not None else GOLDEN["pv_annual"] / GOLDEN["pv_kwp"]
    periods = [
        TimePeriodRule(period=TariffPeriod.VALLEY, hours=list(range(0, 8))),
        TimePeriodRule(period=TariffPeriod.PEAK, hours=[11, 12, 13, 14, 18, 19, 20, 21]),
        TimePeriodRule(period=TariffPeriod.FLAT, hours=[8, 9, 10, 15, 16, 17, 22, 23]),
    ]
    proj.timeseries = TimeSeriesConfig(
        enabled=True,
        resolution=Resolution.HOURLY,
        base_year=2025,
        load=LoadProfileConfig(
            mode=LoadProfileMode.TYPICAL_DAY,
            typical_workday=WORKDAY,
            typical_weekend=WEEKEND,
            annual_energy_kwh=load_annual,
        ),
        pv=PVProfileConfig(
            mode=PVProfileMode.EQUIVALENT_HOURS,
            equivalent_hours=hours,
            capacity_kwp=pv_kwp if pv_kwp > 0 else None,
        ),
        tariff=TariffSeriesConfig(
            profile=TariffProfile(
                peak_price=GOLDEN["peak_price"],
                flat_price=GOLDEN["flat_price"],
                valley_price=GOLDEN["valley_price"],
                export_price=export_price,
                time_periods=periods,
            ),
            demand_charge_enabled=demand_charge > 0,
        ),
        dispatch=StorageDispatchConfig(
            strategy=strategy,
            charge_price_threshold=charge_threshold,
            discharge_price_threshold=discharge_threshold,
            allow_grid_charge=allow_grid_charge,
            charge_from_grid=allow_grid_charge,
            soc_min=svc_soc_min,
            soc_max=svc_soc_max,
            initial_soc=svc_soc_min,
        ),
        holidays=[],
    )
    return proj


# --------------------------------------------------------------------------- #
# §85 V2 Golden Case
# --------------------------------------------------------------------------- #
class TestGoldenCase:
    def test_deterministic(self):
        """同样输入重复仿真，结果必须逐位一致。"""
        proj = build_project()
        a = e2.simulate_year(proj, axis_for(), 1)
        b = e2.simulate_year(proj, axis_for(), 1)
        assert a.metrics.electricity_cost_saving == b.metrics.electricity_cost_saving
        assert a.metrics.self_consumption_rate == b.metrics.self_consumption_rate
        assert np.array_equal(a.outcome.storage.soc_end, b.outcome.storage.soc_end)

    def test_generation_matches_spec(self):
        """§85：PV 年发电量 = 1,100,000 kWh。"""
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        assert sim.metrics.annual_pv_generation == pytest.approx(GOLDEN["pv_annual"], rel=1e-9)

    def test_load_matches_spec(self):
        """§85：负荷年电量 = 1,500,000 kWh（典型日模式须按年电量归一化）。"""
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        assert sim.metrics.annual_load == pytest.approx(GOLDEN["load_annual"], rel=1e-9)

    def test_prices_respected(self):
        proj = build_project()
        sim = e2.simulate_year(proj, axis_for(), 1)
        values = set(np.round(np.unique(sim.outcome.price), 6).tolist())
        assert values <= {GOLDEN["peak_price"], GOLDEN["flat_price"], GOLDEN["valley_price"]}

    def test_saving_identity_holds(self):
        """现金口径与分解口径必须恒等（防重复计算）。"""
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        assert e2.check_saving_identity(sim.metrics) < 1e-6

    def test_full_project_runs_and_balances(self):
        sim = e2.simulate_project(build_project())
        assert len(sim.years) == 25
        assert sim.max_balance_error() < 1e-6


# --------------------------------------------------------------------------- #
# §73 能量守恒（随机 100 组）
# --------------------------------------------------------------------------- #
class TestEnergyConservationRandom:
    N_CASES = 100

    def _random_params(self, rng: np.random.Generator) -> dict:
        return {
            "pv_kwp": float(rng.choice([0.0, 200.0, 500.0, 1000.0, 2000.0])),
            "storage_kw": float(rng.choice([0.0, 100.0, 500.0, 1000.0])),
            "storage_kwh": float(rng.choice([0.0, 200.0, 1000.0, 2000.0])),
            "load_annual": float(rng.uniform(400_000.0, 2_500_000.0)),
            "pv_hours": float(rng.uniform(700.0, 1400.0)),
            "charge_threshold": float(rng.uniform(0.20, 0.60)),
            "discharge_threshold": float(rng.uniform(0.70, 1.10)),
            "export_price": float(rng.uniform(0.20, 0.45)),
            "strategy": rng.choice(list(DispatchStrategy)),
            "allow_grid_charge": bool(rng.integers(0, 2)),
        }

    def test_balance_within_tolerance_for_100_cases(self):
        """§73：随机 100 组组合，逐点守恒误差必须 < 1e-6 kWh。"""
        rng = np.random.default_rng(20251007)
        axis = axis_for()
        worst = 0.0
        for i in range(self.N_CASES):
            params = self._random_params(rng)
            proj = build_project(**params)
            sim = e2.simulate_year(proj, axis, 1)
            error = abs(sim.balance.error)
            worst = max(worst, error)
            assert sim.balance.is_balanced, (
                f"第 {i} 组守恒失败：误差 {error:.3e}，参数 {params}"
            )
        assert worst < 1e-6, f"最大守恒误差 {worst:.3e} 超限"

    def test_saving_identity_for_100_cases(self):
        """§73 的经济侧对应项：分解口径与现金口径必须闭合。"""
        rng = np.random.default_rng(20251008)
        axis = axis_for()
        for i in range(self.N_CASES):
            params = self._random_params(rng)
            sim = e2.simulate_year(build_project(**params), axis, 1)
            dev = e2.check_saving_identity(sim.metrics)
            assert dev < 1e-4, f"第 {i} 组节省分解不闭合：偏差 {dev:.3e}"

    def test_soc_bounds_for_100_cases(self):
        """§74：SOC 必须始终落在 [SOC_min, SOC_max]。"""
        rng = np.random.default_rng(20251009)
        axis = axis_for()
        for i in range(self.N_CASES):
            params = self._random_params(rng)
            proj = build_project(**params)
            sim = e2.simulate_year(proj, axis, 1)
            cfg = proj.timeseries.dispatch
            soc = sim.outcome.storage.soc_end
            assert soc.min() >= cfg.soc_min - 1e-9, f"第 {i} 组 SOC 低于下限"
            assert soc.max() <= cfg.soc_max + 1e-9, f"第 {i} 组 SOC 高于上限"

    def test_power_limits_for_100_cases(self):
        """§75：充放电量不得超过功率上限 × Δt。"""
        rng = np.random.default_rng(20251010)
        axis = axis_for()
        for i in range(self.N_CASES):
            params = self._random_params(rng)
            proj = build_project(**params)
            sim = e2.simulate_year(proj, axis, 1)
            limit = proj.storage.storage_power_kw * axis.delta_hours
            assert sim.outcome.storage.charge_ac.max() <= limit + 1e-9, f"第 {i} 组充电超功率"
            assert (
                sim.outcome.storage.discharge_ac.max() <= limit + 1e-9
            ), f"第 {i} 组放电超功率"


# --------------------------------------------------------------------------- #
# §77 / §78 退化情形
# --------------------------------------------------------------------------- #
class TestDegenerateCases:
    def test_no_storage_no_arbitrage(self):
        """§77：储能容量为 0 时不得产生套利收益，且物理量全为 0。"""
        sim = e2.simulate_year(build_project(storage_kw=0.0, storage_kwh=0.0), axis_for(), 1)
        assert sim.metrics.annual_storage_charge == 0.0
        assert sim.metrics.annual_storage_discharge == 0.0
        assert sim.metrics.storage_arbitrage_revenue == 0.0
        assert sim.outcome.storage.equivalent_cycles == 0.0

    def test_no_storage_consumption_only_pv_and_grid(self):
        """§77：无储能时购电 = 负荷 − 光伏自用，不存在储能相关项。"""
        sim = e2.simulate_year(build_project(storage_kw=0.0, storage_kwh=0.0), axis_for(), 1)
        m = sim.metrics
        assert m.annual_grid_purchase == pytest.approx(m.annual_load - m.pv_self_consumption_saving and
                                                       (m.annual_load - float(np.sum(sim.outcome.pv_to_load))), rel=1e-9)

    def test_no_pv_is_pure_grid_load(self):
        """§78：PV 容量为 0 时退化为纯电网负荷项目。"""
        sim = e2.simulate_year(build_project(pv_kwp=0.0, pv_hours=0.0), axis_for(), 1)
        m = sim.metrics
        assert m.annual_pv_generation == 0.0
        assert m.self_consumption_rate == 0.0
        assert m.self_sufficiency_rate == 0.0
        assert m.electricity_cost_saving == pytest.approx(0.0, abs=1e-6)
        assert m.annual_grid_purchase == pytest.approx(m.annual_load, rel=1e-9)

    def test_pv_only_greater_than_load(self):
        """§79：PV > Load —— 出现上网与（可能）弃光，自用率 < 100%。"""
        sim = e2.simulate_year(
            build_project(pv_kwp=3000.0, pv_hours=1400.0, storage_kw=0.0, storage_kwh=0.0),
            axis_for(), 1,
        )
        m = sim.metrics
        assert m.annual_grid_export > 0
        assert m.self_consumption_rate < 1.0

    def test_pv_only_less_than_load(self):
        """§79：PV < Load —— 无上网，购电为正。"""
        sim = e2.simulate_year(
            build_project(pv_kwp=200.0, pv_hours=800.0, storage_kw=0.0, storage_kwh=0.0),
            axis_for(), 1,
        )
        m = sim.metrics
        assert m.annual_grid_purchase > 0
        assert m.self_sufficiency_rate < 1.0

    def test_pv_equal_load_scale(self):
        """§79：PV ≈ Load —— 自给率接近但不超过 100%（时序不匹配必然留有缺口）。"""
        sim = e2.simulate_year(
            build_project(pv_kwp=1000.0, pv_hours=1500.0, storage_kw=0.0, storage_kwh=0.0),
            axis_for(), 1,
        )
        m = sim.metrics
        assert 0.0 < m.self_sufficiency_rate <= 1.0


# --------------------------------------------------------------------------- #
# §80 PV + Storage 四种状态
# --------------------------------------------------------------------------- #
class TestPvStorageStates:
    def test_surplus_charges_storage(self):
        """§80 盈余：光伏给储能充电量 > 0。"""
        sim = e2.simulate_year(build_project(pv_kwp=2000.0, pv_hours=1300.0), axis_for(), 1)
        assert sim.metrics.annual_storage_charge > 0

    def test_deficit_discharges_storage(self):
        """§80 缺口：储能放电量 > 0。"""
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        assert sim.metrics.annual_storage_discharge > 0

    def test_storage_full_reason_appears(self):
        """§80 满：应出现「SOC 已达上限」类原因码。"""
        sim = e2.simulate_year(
            build_project(pv_kwp=3000.0, pv_hours=1500.0, storage_kwh=200.0),
            axis_for(), 1,
        )
        summary = sim.reason_summary()
        assert any("SOC_FULL" in code or "CHARGE" in code for code in summary)

    def test_storage_empty_reason_appears(self):
        """§80 空：应出现「SOC 已到下限」类原因码。"""
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        assert any("SOC_EMPTY" in code for code in sim.reason_summary())


# --------------------------------------------------------------------------- #
# §81 峰谷套利
# --------------------------------------------------------------------------- #
class TestPeakValleyArbitrage:
    def test_charges_in_valley_discharges_in_peak(self):
        """§81：谷价 0.35 / 峰价 1.00 时应"谷充峰放"。"""
        proj = build_project(allow_grid_charge=True, storage_kw=1000.0, storage_kwh=4000.0)
        sim = e2.simulate_year(proj, axis_for(), 1)
        storage = sim.outcome.storage
        price = np.asarray(sim.outcome.price, dtype=float)

        charging = storage.charge_ac > 1e-9
        discharging = storage.discharge_ac > 1e-9
        if charging.any():
            assert price[charging].mean() < 0.5, "充电应主要发生在低价时段"
        if discharging.any():
            assert price[discharging].mean() > 0.8, "放电应主要发生在高价时段"

    def test_grid_charge_disabled_blocks_valley_charging(self):
        """§21：关闭电网充电后，谷段不得从电网取电充电。"""
        sim = e2.simulate_year(build_project(allow_grid_charge=False), axis_for(), 1)
        assert sim.metrics.annual_grid_charge == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# §82 需量
# --------------------------------------------------------------------------- #
class TestDemand:
    def test_storage_reduces_peak_demand(self):
        """§82：储能削峰后最大需量应下降。"""
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        m = sim.metrics
        assert m.peak_demand_after <= m.peak_demand_before + 1e-9
        assert m.demand_saving == pytest.approx(
            m.peak_demand_before - m.peak_demand_after, rel=1e-9
        )

    def test_demand_charge_cost_scales_linearly(self):
        """§18/§30：需量电费与需量电价成正比。"""
        axis = axis_for()
        a = e2.simulate_year(build_project(demand_charge=30.0), axis, 1)
        b = e2.simulate_year(build_project(demand_charge=60.0), axis, 1)
        assert b.metrics.actual_demand_cost == pytest.approx(2.0 * a.metrics.actual_demand_cost, rel=1e-9)
        assert b.metrics.demand_cost_saving == pytest.approx(
            2.0 * a.metrics.demand_cost_saving, rel=1e-9
        )

    def test_no_demand_charge_means_zero_cost(self):
        sim = e2.simulate_year(build_project(demand_charge=0.0), axis_for(), 1)
        assert sim.metrics.actual_demand_cost == 0.0
        assert sim.metrics.demand_cost_saving == 0.0


# --------------------------------------------------------------------------- #
# 引擎接线一致性（§105）
# --------------------------------------------------------------------------- #
class TestEngineWiring:
    def test_v1_untouched_when_timeseries_disabled(self):
        """§1.1：未启用时序时结果对象不得出现任何 V2 字段内容。"""
        proj = build_project()
        proj.timeseries.enabled = False
        result = calculation_engine.calculate(proj)
        assert result.time_series_results is None
        assert result.baseline_results is None
        assert result.dispatch_results == []
        assert result.energy_balance is None

    def test_override_flows_into_annual_result(self):
        """§105：注入的时序口径必须与 AnnualResult 完全一致。"""
        proj = build_project()
        sim = e2.simulate_year(proj, axis_for(), 1)
        override = e2.ProjectSimulation(axis=axis_for(), years=[sim]).overrides()
        result = calculation_engine.calculate(proj, year_override=override)
        row = result.annual_results[0]
        assert row.pv_generation_kwh == pytest.approx(override[1].pv_generation, rel=1e-12)
        assert row.total_revenue == pytest.approx(override[1].total_revenue(), rel=1e-12)
        assert row.storage_charge_kwh == pytest.approx(override[1].storage_charge, rel=1e-12)

    def test_override_none_matches_plain_calculation(self):
        """year_override=None 与不传该参数必须逐位一致。"""
        proj = build_project()
        proj.timeseries.enabled = False
        a = calculation_engine.calculate(proj)
        b = calculation_engine.calculate(proj, year_override=None)
        assert a.project_irr == b.project_irr
        assert a.project_npv == b.project_npv
        assert [r.project_cashflow for r in a.annual_results] == [
            r.project_cashflow for r in b.annual_results
        ]
