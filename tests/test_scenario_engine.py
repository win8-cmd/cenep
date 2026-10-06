"""方案比较、容量扫描与寻优入口测试（V2 §42–§48、§86）。

覆盖：

* §43 三种标准组合（PV Only / Storage Only / PV + Storage）都能评估，
  且 ``total_capex`` 与容量变化方向一致；
* §42 基准方案（无光伏、无储能）的年节省恒为 0、IRR 不可计算；
* §45/§46 单维扫描、网格扫描、参数扫描的候选条数、排序方向与目标函数；
* §47 六种优化目标各自可用，``MIN_*`` 与 ``MAX_*`` 的排序方向相反；
* §48 禁止黑盒：``explanation`` 非空且含中文与数值比较、``candidates`` 与评估次数一致；
* §86 性能：100 个候选的 ``scan_grid`` 必须在 30 秒以内完成（手动计时断言，
  环境未安装 ``pytest-timeout``）；
* 路径覆盖辅助函数：非法路径与类型不符的值必须抛**中文**错误。

测试统一用 :func:`make_project` 构造带 timeseries 的项目：行情用例（``golden_*``）
默认关闭时序，无法直接跑 V2 时序仿真。为控制测试时长，除性能用例外
默认把计算期压到 10 年（精确复核的代价与时长相乘）。
"""

from __future__ import annotations

import copy
import time

import pytest

from cenep.calculation import scenario_engine as se
from cenep.domain.enums import (
    DispatchStrategy,
    LoadProfileMode,
    OptimizationObjective,
    ProjectType,
    PVProfileMode,
    ScanVariable,
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


def _tou_rules() -> list[TimePeriodRule]:
    """峰段 10–12 时、谷段 0–6 时、其余平段。"""
    out: list[TimePeriodRule] = []
    for hour in range(24):
        if hour in (10, 11, 12):
            out.append(TimePeriodRule(period=TariffPeriod.PEAK, hours=[hour]))
        elif hour in range(7):
            out.append(TimePeriodRule(period=TariffPeriod.VALLEY, hours=[hour]))
        else:
            out.append(TimePeriodRule(period=TariffPeriod.FLAT, hours=[hour]))
    return out


def make_project(
    golden: Project,
    *,
    period: int = 10,
    roof_area_m2: float = 6500.0,
    allow_grid_charge: bool = True,
) -> Project:
    """在行情用例基础上补全 timeseries 配置，得到可跑 V2 时序仿真的项目。"""
    project = copy.deepcopy(golden)
    project.analysis_period = period
    project.pv.usable_roof_area_m2 = roof_area_m2
    project.timeseries = TimeSeriesConfig(
        enabled=True,
        base_year=2025,
        load=LoadProfileConfig(
            mode=LoadProfileMode.ANNUAL_SIMPLE,
            annual_energy_kwh=1_200_000.0,
        ),
        pv=PVProfileConfig(
            mode=PVProfileMode.EQUIVALENT_HOURS,
            equivalent_hours=1100.0,
            performance_ratio=1.0,
        ),
        tariff=TariffSeriesConfig(
            profile=TariffProfile(
                peak_price=1.0,
                flat_price=0.7,
                valley_price=0.4,
                export_price=0.35,
                time_periods=_tou_rules(),
            )
        ),
        dispatch=StorageDispatchConfig(
            strategy=DispatchStrategy.PEAK_VALLEY,
            allow_grid_charge=allow_grid_charge,
            charge_from_grid=allow_grid_charge,
        ),
    )
    return project


@pytest.fixture
def ts_project(golden_pv_storage) -> Project:
    """光储项目 + 有效时序配置（默认 10 年，控制测试时长）。"""
    return make_project(golden_pv_storage)


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


# --------------------------------------------------------------------------- #
# §42 基准方案
# --------------------------------------------------------------------------- #
class TestBaselineScenario:
    def test_baseline_has_no_saving_and_no_investment(self, ts_project):
        """§42：无光伏无储能时年节省为 0，且没有可投资对象。"""
        baseline = se.build_baseline_scenario(ts_project)
        assert baseline.is_baseline is True
        assert baseline.annual_saving == 0.0
        assert baseline.demand_saving == 0.0
        assert baseline.total_capex == 0.0
        assert baseline.project_npv == 0.0
        assert baseline.project_irr is None
        assert baseline.static_payback is None
        assert baseline.lcoe is None
        assert baseline.lcos is None

    def test_baseline_keeps_electricity_cost_as_reference(self, ts_project):
        """基准电费必须落在基准方案的 metrics 中，供方案比较引用。"""
        baseline = se.build_baseline_scenario(ts_project)
        assert baseline.metrics.baseline_electricity_cost > 0.0
        assert baseline.metrics.actual_electricity_cost == pytest.approx(
            baseline.metrics.baseline_electricity_cost
        )
        assert baseline.metrics.annual_pv_generation == 0.0
        assert baseline.metrics.annual_load > 0.0


# --------------------------------------------------------------------------- #
# §43 三种标准组合
# --------------------------------------------------------------------------- #
class TestStandardCombinations:
    def test_standard_variants_labels(self, ts_project):
        """§43：必须能比较 PV Only / Storage Only / PV + Storage 三种组合。"""
        labels = [label for label, _ in se.standard_variants(ts_project)]
        assert len(labels) == 3
        assert "PV Only" in labels[0]
        assert "Storage Only" in labels[1]
        assert "PV + Storage" in labels[2]

    def test_three_combinations_are_evaluable(self, ts_project):
        """三种组合都能跑通并给出完整结果（V2 §43）。"""
        results = se.compare_scenarios(
            ts_project, se.standard_variants(ts_project), full_period=True
        )
        assert len(results) == 3
        for scenario in results:
            assert scenario.metrics.annual_load > 0.0
            assert scenario.total_capex >= 0.0

    def test_capex_follows_capacity_direction(self, ts_project):
        """总投资与容量变化方向一致：光储 > 仅光伏，且光储 > 仅储能。"""
        pv_only, storage_only, both = se.compare_scenarios(
            ts_project, se.standard_variants(ts_project), full_period=True
        )
        assert pv_only.pv_capacity_kwp == pytest.approx(1000.0)
        assert pv_only.storage_energy_kwh == 0.0
        assert storage_only.pv_capacity_kwp == 0.0
        assert storage_only.storage_energy_kwh == pytest.approx(1000.0)
        assert both.pv_capacity_kwp == pytest.approx(1000.0)
        assert both.storage_energy_kwh == pytest.approx(1000.0)

        # 单价：光伏 3000 元/kWp、储能 1000 元/kWh（行情用例固定值）
        assert pv_only.total_capex == pytest.approx(3_000_000.0)
        assert storage_only.total_capex == pytest.approx(1_000_000.0)
        assert both.total_capex == pytest.approx(4_000_000.0)
        assert both.total_capex > pv_only.total_capex > storage_only.total_capex

    def test_zero_pv_combination_has_no_lcoe(self, ts_project):
        """无光伏时 LCOE 无法计算（``None``），有储能时 LCOS 可计算。"""
        pv_only, storage_only, both = se.compare_scenarios(
            ts_project, se.standard_variants(ts_project), full_period=True
        )
        assert pv_only.lcoe is not None and pv_only.lcos is None
        assert storage_only.lcoe is None and storage_only.lcos is not None
        assert both.lcoe is not None and both.lcos is not None


# --------------------------------------------------------------------------- #
# §45 单维扫描
# --------------------------------------------------------------------------- #
class TestSingleDimensionScan:
    def test_candidate_count_equals_value_count(self, ts_project):
        """§45：候选条数 = 取值个数。"""
        values = [0.0, 500.0, 1000.0]
        result = se.scan(
            ts_project, variable=ScanVariable.STORAGE_CAPACITY, values=values
        )
        assert len(result.candidates) == len(values)
        assert {c.variables["STORAGE_CAPACITY"] for c in result.candidates} == set(values)

    def test_best_run_id_is_objective_optimum(self, ts_project):
        """§47：``best_run_id`` 必须指向目标函数最优的候选。"""
        result = se.scan(
            ts_project,
            variable=ScanVariable.PV_CAPACITY,
            values=[500.0, 1000.0],
            objective=OptimizationObjective.MAX_NPV,
        )
        feasible = [c for c in result.candidates if c.feasible]
        best_value = max(c.objective_value for c in feasible)
        assert result.best_run_id
        assert result.best_scenario is not None
        assert result.best_scenario.project_npv == pytest.approx(
            max(c.scenario.project_npv for c in feasible)
        )
        assert result.best_variables["PV_CAPACITY"] in (500.0, 1000.0)
        assert best_value == pytest.approx(result.best_scenario.project_npv)

    def test_candidates_sorted_by_objective(self, ts_project):
        """候选必须按目标从优到劣排序（V2 §48 ④）。"""
        result = se.scan(
            ts_project,
            variable=ScanVariable.STORAGE_CAPACITY,
            values=[0.0, 500.0, 1000.0],
            objective=OptimizationObjective.MIN_LCOE,
        )
        lcoes = [c.scenario.lcoe for c in result.candidates]
        assert lcoes == sorted(lcoes, key=lambda v: (v is None, v))

    def test_default_values_are_spec_given(self):
        """§45 规范给定的候选值必须是模块常量。"""
        assert se.PV_CAPACITY_VALUES == (500.0, 750.0, 1000.0, 1250.0, 1500.0, 2000.0)
        assert se.STORAGE_ENERGY_VALUES == (0.0, 500.0, 1000.0, 1500.0, 2000.0)
        assert se.STORAGE_POWER_VALUES == (250.0, 500.0, 750.0, 1000.0)
        assert se.DEFAULT_SCAN_VALUES[ScanVariable.PV_CAPACITY] == se.PV_CAPACITY_VALUES

    def test_roof_limit_prunes_oversized_pv(self, ts_project):
        """PR3：光伏容量超过屋顶可装上限的候选必须判为不可行并说明原因。"""
        limit = se.roof_limit_kwp(ts_project)
        assert limit == pytest.approx(6500.0 / 6.0)
        result = se.scan(
            ts_project,
            variable=ScanVariable.PV_CAPACITY,
            values=[1000.0, 2000.0],
        )
        oversized = [c for c in result.candidates if c.variables["PV_CAPACITY"] == 2000.0]
        assert len(oversized) == 1
        assert oversized[0].feasible is False
        assert "PR3" in oversized[0].note
        assert _has_chinese(oversized[0].note)

    def test_empty_values_raise(self, ts_project):
        with pytest.raises(se.ScenarioError) as exc:
            se.scan(ts_project, variable=ScanVariable.PV_CAPACITY, values=[])
        assert _has_chinese(str(exc.value))


# --------------------------------------------------------------------------- #
# §46 八个参数扫描维度
# --------------------------------------------------------------------------- #
class TestParameterScan:
    def test_eight_dimensions_are_available(self):
        """§46：参数扫描共 8 个维度，每个维度都有规范给定的默认取值。"""
        assert len(ScanVariable) == 8
        assert set(se.DEFAULT_SCAN_VALUES) == set(ScanVariable)
        for values in se.DEFAULT_SCAN_VALUES.values():
            assert values

    def test_parameter_scan_changes_one_parameter_at_a_time(self, ts_project):
        """V1 §95：参数扫描一次只改一个参数，不做组合。"""
        scans = {
            ScanVariable.TARIFF: [-0.1, 0.1],
            ScanVariable.LOAD: [-0.1, 0.1],
            ScanVariable.PEAK_VALLEY_SPREAD: [0.7, 1.3],
            ScanVariable.STORAGE_PRICE: [-0.1, 0.1],
            ScanVariable.CAPEX: [-0.1, 0.1],
        }
        result = se.parameter_scan(ts_project, scans)
        assert len(result.candidates) == 10
        assert len(result.scan_variables) >= 5

    def test_all_eight_dimensions_run(self, ts_project):
        """§46：8 个维度各自至少跑一次。"""
        scans = {
            ScanVariable.PV_CAPACITY: [1000.0],
            ScanVariable.STORAGE_CAPACITY: [1000.0],
            ScanVariable.STORAGE_POWER: [500.0],
            ScanVariable.STORAGE_PRICE: [0.0],
            ScanVariable.TARIFF: [0.0],
            ScanVariable.PEAK_VALLEY_SPREAD: [1.0],
            ScanVariable.LOAD: [0.0],
            ScanVariable.CAPEX: [0.0],
        }
        result = se.parameter_scan(ts_project, scans)
        assert len(result.candidates) == 8
        assert all(c.feasible for c in result.candidates)

    def test_tariff_scan_moves_revenue(self, ts_project):
        """电价扫描必须真正影响时序仿真（时序侧电价同步缩放，V2 §105）。"""
        result = se.parameter_scan(ts_project, {ScanVariable.TARIFF: [-0.2, 0.2]})
        by_value = {c.variables["TARIFF"]: c for c in result.candidates}
        assert by_value[0.2].scenario.project_npv > by_value[-0.2].scenario.project_npv

    def test_peak_valley_spread_keeps_flat_price(self, ts_project):
        """§7.2：价差情景只改峰谷，平段电价保持不变。"""
        changes = se.scan_variant_changes(ts_project, ScanVariable.PEAK_VALLEY_SPREAD, 0.7)
        assert "tariff.flat_price" not in changes
        variant = se.apply_variant(ts_project, changes)
        assert variant.tariff.flat_price == pytest.approx(ts_project.tariff.flat_price)
        assert variant.tariff.peak_price < ts_project.tariff.peak_price
        assert variant.tariff.valley_price > ts_project.tariff.valley_price
        # 时序侧电价同步调整（否则仿真仍按原价计算，V2 §105）
        assert variant.timeseries.tariff.profile.peak_price < (
            ts_project.timeseries.tariff.profile.peak_price
        )


# --------------------------------------------------------------------------- #
# §45 网格扫描与 §86 性能
# --------------------------------------------------------------------------- #
class TestGridScan:
    def test_grid_candidate_count(self, ts_project):
        grid = {
            "pv.pv_capacity_kwp": [800.0, 1000.0],
            "storage.storage_energy_kwh": [0.0, 500.0, 1000.0],
        }
        result = se.scan_grid(ts_project, grid)
        assert len(result.candidates) == 6

    def test_grid_over_limit_raises_chinese_error(self, ts_project):
        grid = {
            "pv.pv_capacity_kwp": [500.0, 750.0, 1000.0],
            "storage.storage_energy_kwh": [0.0, 500.0, 1000.0, 1500.0],
        }
        with pytest.raises(se.ScenarioError) as exc:
            se.scan_grid(ts_project, grid, max_candidates=5)
        message = str(exc.value)
        assert _has_chinese(message)
        assert "超过上限" in message

    def test_empty_grid_raises(self, ts_project):
        with pytest.raises(se.ScenarioError):
            se.scan_grid(ts_project, {})


class TestScanPerformance:
    def test_scan_grid_100_candidates_under_30_seconds(self, golden_pv_storage):
        """§86：100 个候选的网格扫描必须在 30 秒内完成（手动计时断言）。

        时间预算：排名阶段 100 × 首年仿真（约 0.09 秒）+ 最优候选的精确复核。
        """
        project = make_project(golden_pv_storage, period=25, roof_area_m2=20000.0)
        grid = {
            "pv.pv_capacity_kwp": [
                500.0, 750.0, 1000.0, 1250.0, 1500.0,
                1750.0, 2000.0, 2250.0, 2500.0, 2750.0,
            ],
            "storage.storage_energy_kwh": [
                250.0, 400.0, 600.0, 800.0, 1000.0,
                1200.0, 1400.0, 1600.0, 1800.0, 2000.0,
            ],
        }
        started = time.perf_counter()
        result = se.scan_grid(project, grid)
        elapsed = time.perf_counter() - started
        assert len(result.candidates) == 100
        assert all(c.feasible for c in result.candidates)
        assert result.best_scenario is not None
        assert elapsed < 30.0, f"100 个候选耗时 {elapsed:.2f} 秒，超过 §86 的 30 秒上限"
        assert result.elapsed_seconds < 30.0


# --------------------------------------------------------------------------- #
# §47 六种优化目标
# --------------------------------------------------------------------------- #
class TestOptimizationObjectives:
    @pytest.mark.parametrize("objective", list(OptimizationObjective))
    def test_each_objective_runs_and_picks_direction_extreme(self, ts_project, objective):
        """§47：六种目标各自可用，且最优候选取该目标方向上的极值。"""
        result = se.scan(
            ts_project,
            variable=ScanVariable.STORAGE_CAPACITY,
            values=[0.0, 500.0, 1000.0],
            objective=objective,
        )
        assert result.objective is objective
        assert result.best_scenario is not None
        assert result.explanation and all(_has_chinese(line) for line in result.explanation)
        feasible = [c for c in result.candidates if c.feasible]
        values = [c.objective_value for c in feasible]
        if se.is_maximization(objective):
            assert result.best_scenario is not None
            assert result.candidates[0].objective_value == max(values)
        else:
            assert result.candidates[0].objective_value == min(values)

    def test_min_and_max_have_opposite_direction(self, ts_project):
        """§47：``MIN_*`` 目标的排序方向与 ``MAX_*`` 相反。"""
        values = [0.0, 500.0, 1000.0]
        max_npv = se.scan(
            ts_project,
            variable=ScanVariable.STORAGE_CAPACITY,
            values=values,
            objective=OptimizationObjective.MAX_NPV,
        )
        min_lcoe = se.scan(
            ts_project,
            variable=ScanVariable.STORAGE_CAPACITY,
            values=values,
            objective=OptimizationObjective.MIN_LCOE,
        )
        npvs = [c.scenario.project_npv for c in max_npv.candidates]
        lcoes = [c.scenario.lcoe for c in min_lcoe.candidates if c.scenario.lcoe is not None]
        assert max_npv.candidates[0].scenario.project_npv == pytest.approx(max(npvs))
        assert min_lcoe.candidates[0].scenario.lcoe == pytest.approx(min(lcoes))

    def test_annual_cost_objective_prefers_lower_bill(self, ts_project):
        """MIN_ANNUAL_COST 取年运行成本最低者，且该口径由 metrics 可直接复算。"""
        result = se.scan(
            ts_project,
            variable=ScanVariable.STORAGE_CAPACITY,
            values=[0.0, 1000.0],
            objective=OptimizationObjective.MIN_ANNUAL_COST,
        )
        costs = [se.annual_operating_cost(c.scenario) for c in result.candidates]
        assert result.candidates[0].objective_value == pytest.approx(min(costs))


# --------------------------------------------------------------------------- #
# §48 禁止黑盒
# --------------------------------------------------------------------------- #
class TestExplainability:
    def test_explanation_is_chinese_with_numbers(self, ts_project):
        """§48：``explanation`` 非空、含中文，且至少含一处数值比较。"""
        result = se.scan(
            ts_project,
            variable=ScanVariable.PV_CAPACITY,
            values=[800.0, 1000.0],
        )
        text = "\n".join(result.explanation)
        assert text.strip()
        assert _has_chinese(text)
        assert any(ch.isdigit() for ch in text)
        assert "较次优方案" in text or "次优" in text

    def test_five_sections_are_present(self, ts_project):
        """§48 五段式：输入参数、约束、候选、计算结果、最优方案缺一不可。"""
        result = se.scan(
            ts_project,
            variable=ScanVariable.STORAGE_CAPACITY,
            values=[0.0, 500.0],
        )
        assert result.scan_variables
        assert result.constraints and all(_has_chinese(c) for c in result.constraints)
        assert len(result.candidates) == 2
        assert result.best_run_id
        assert result.best_variables
        assert result.best_scenario is not None
        for candidate in result.candidates:
            assert candidate.objective is OptimizationObjective.MAX_NPV
            assert candidate.scenario is not None

    def test_candidates_match_evaluation_count(self, ts_project):
        """§48 X3：``candidates`` 条数与实际评估次数一致，禁止只输出最优。"""
        values = [0.0, 500.0, 1000.0]
        result = se.scan(
            ts_project, variable=ScanVariable.STORAGE_CAPACITY, values=values
        )
        assert len(result.candidates) == len(values)

    def test_notes_mark_approximation_and_verification(self, ts_project):
        """§86：候选必须标注「排名用（线性外推）」或「精确复核」。"""
        result = se.scan(
            ts_project,
            variable=ScanVariable.STORAGE_CAPACITY,
            values=[0.0, 500.0, 1000.0],
        )
        notes = [c.note for c in result.candidates]
        assert any(se.VERIFY_NOTE in note for note in notes)
        assert any(se.RANK_NOTE in note for note in notes)
        verified = [c for c in result.candidates if se.VERIFY_NOTE in c.note]
        assert len(verified) == 1
        assert verified[0].run_id == result.best_run_id

    def test_explanation_documents_linear_extrapolation(self, ts_project):
        """§86：近似说明必须写清哪些数字是近似、哪些是精确。"""
        result = se.scan(
            ts_project, variable=ScanVariable.STORAGE_CAPACITY, values=[0.0, 500.0]
        )
        text = "\n".join(result.explanation)
        assert "线性外推" in text
        assert "精确复核" in text
        assert "近似" in text


# --------------------------------------------------------------------------- #
# 两阶段评估：线性外推（§86）
# --------------------------------------------------------------------------- #
class TestRankingExtrapolation:
    def test_ranking_overrides_scale_with_documented_factors(self, ts_project):
        """外推因子必须与文档一致：光伏按 (1−d_pv)、储能按 (1−d_es)。"""
        axis = se.build_axis(ts_project)
        first_year = se.simulate_candidate_year(ts_project, axis, 1)
        overrides = se.build_ranking_overrides(ts_project, first_year, axis)
        assert set(overrides) == set(range(1, int(ts_project.analysis_period) + 1))
        assert overrides[1].pv_generation == pytest.approx(first_year.metrics.annual_pv_generation)

        pv_factor = 1.0 - float(ts_project.pv.annual_degradation_rate)
        assert overrides[2].pv_generation == pytest.approx(
            overrides[1].pv_generation * pv_factor
        )
        storage_factor = 1.0 - float(ts_project.storage.annual_degradation_rate)
        assert overrides[2].storage_discharge == pytest.approx(
            overrides[1].storage_discharge * storage_factor
        )

    def test_extrapolation_keeps_storage_balance_valid(self, ts_project):
        """外推必须保持 ``储能充电量 ≥ 光伏转入储能电量``（V2 §113）。"""
        axis = se.build_axis(ts_project)
        first_year = se.simulate_candidate_year(ts_project, axis, 1)
        overrides = se.build_ranking_overrides(ts_project, first_year, axis)
        for year, override in overrides.items():
            assert override.storage_charge >= override.pv_to_storage - 1e-6, f"第 {year} 年"

    def test_ranking_is_faster_than_full_period(self, ts_project):
        """排名阶段只跑首年，必须显著快于精确复核。"""
        axis = se.build_axis(ts_project)
        started = time.perf_counter()
        se.evaluate_scenario(ts_project, axis, full_period=False)
        ranking_seconds = time.perf_counter() - started
        started = time.perf_counter()
        se.evaluate_scenario(ts_project, axis, full_period=True)
        precise_seconds = time.perf_counter() - started
        assert ranking_seconds < precise_seconds


# --------------------------------------------------------------------------- #
# 路径辅助函数（OPTIMIZATION.md §11 兼容性红线）
# --------------------------------------------------------------------------- #
class TestPathHelpers:
    def test_set_and_get_round_trip(self, ts_project):
        se.set_by_path(ts_project, "storage.storage_energy_kwh", 1500.0)
        assert se.get_by_path(ts_project, "storage.storage_energy_kwh") == pytest.approx(1500.0)

    def test_unknown_path_raises_chinese_error(self, ts_project):
        with pytest.raises(se.ScenarioPathError) as exc:
            se.set_by_path(ts_project, "pv.not_a_field", 1.0)
        message = str(exc.value)
        assert _has_chinese(message)
        assert "参数路径不存在" in message

    def test_wrong_type_raises_chinese_error(self, ts_project):
        with pytest.raises(se.ScenarioPathError) as exc:
            se.set_by_path(ts_project, "pv.pv_capacity_kwp", "很大")
        message = str(exc.value)
        assert _has_chinese(message)
        assert "需要数值" in message

    def test_shallow_or_empty_path_raises(self, ts_project):
        for bad in ("", "   ", "pv"):
            with pytest.raises(se.ScenarioPathError) as exc:
                se.set_by_path(ts_project, bad, 1.0)
            assert _has_chinese(str(exc.value))

    def test_out_of_range_value_raises_chinese_error(self, ts_project):
        """领域模型校验失败也要包装成中文错误（如增值税率不得超过 100%）。"""
        with pytest.raises(se.ScenarioPathError) as exc:
            se.set_by_path(ts_project, "tax.vat_rate", 1.5)
        assert _has_chinese(str(exc.value))

    def test_zero_pv_capacity_is_legal(self, ts_project):
        """V2 §78：光伏容量允许为 0（退化为纯电网负荷项目）。"""
        variant = se.apply_variant(
            ts_project, {"pv.pv_capacity_kwp": 0.0, "storage.storage_energy_kwh": 1000.0}
        )
        assert variant.pv.pv_capacity_kwp == 0.0
        assert se.effective_pv_capacity(variant) == 0.0
        assert variant.basic_info.project_type is ProjectType.COMMERCIAL_STORAGE
        scenario = se.evaluate_scenario(variant, full_period=False)
        assert scenario.pv_capacity_kwp == 0.0
        assert scenario.metrics.annual_pv_generation == 0.0

    def test_apply_variant_does_not_mutate_original(self, ts_project):
        original = ts_project.pv.pv_capacity_kwp
        variant = se.apply_variant(
            ts_project, {"pv.pv_capacity_kwp": 800.0, "storage.storage_energy_kwh": 0.0}
        )
        assert ts_project.pv.pv_capacity_kwp == original
        assert variant.pv.pv_capacity_kwp == pytest.approx(800.0)
        # 储能置 0 后项目类型自动降为纯光伏（否则 V1 校验会要求储能容量 > 0）
        assert variant.basic_info.project_type is ProjectType.COMMERCIAL_PV

    def test_apply_variant_zeroes_storage_power_per_pr1(self, ts_project):
        variant = se.apply_variant(
            ts_project,
            {"storage.storage_energy_kwh": 0.0, "storage.storage_power_kw": 0.0},
        )
        assert variant.storage.storage_energy_kwh == 0.0
        assert variant.storage.storage_power_kw == 0.0
        assert variant.basic_info.project_type is ProjectType.COMMERCIAL_PV
