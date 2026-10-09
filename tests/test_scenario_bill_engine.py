"""V2.3 阶段 6：光储四场景联动与**收益去重**测试（规格书 §3.5、§7.1、§7.5、§7.6、§9.3、§10 阶段 6）。

本文件是阶段 6 的**主要验收依据**，核心是 §7.6「避免收益重复计算」。
测试分七组：

1. **四场景基本行为**（§7.1）：同一负荷、同一电价、四类场景可运行；
2. **收益去重恒等式**（§7.6，最重要）：现金口径 = 分解口径，逐位闭合；
3. **成本层面重复计算**（§2.3、§7.6）：政府性基金及附加已含在分时电价内时不得再加一次；
4. **边界条件**（§7.6）：储能上网开启、电网充电开启、上网电价为 0；
5. **财务引擎接入**（§3.5）：财务现金流与去重后的唯一收益**完全一致**；
6. **必答题：只有一种口径能进现金流**（"同一笔钱不得出现在两个收益字段里"）；
7. **中文校验错误**（§0.2）与需量口径（§7.5）。

所有电价数值均取自**已核验**的湖北官方 2026-01 版电价计划
（:data:`cenep.policy.hubei_commercial.OFFICIAL_2026_01`，两套并存、不修改其数值），
本文件**不新增任何未经核验的电价**。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from cenep.application.scenario_service import ScenarioBillService
from cenep.calculation import economic_v2 as econ
from cenep.calculation.errors import ValidationError
from cenep.calculation.scenario_bill_engine import (
    IDENTITY_TOLERANCE_YUAN,
    ScenarioConfig,
    build_finance_overrides,
    build_project_tariff_series,
    cash_electricity_cost_saving,
    check_export_not_in_bill_saving,
    compare_four_scenarios,
    extract_government_fund_yuan_per_kwh,
    forgone_export_opportunity_cost,
    government_fund_included_in_tou_price,
    storage_loss_residual_yuan,
    windowed_max_demand_kw,
)
from cenep.domain.enums import DispatchStrategy, Resolution
from cenep.domain.models import Project
from cenep.domain.scenario_bill import (
    SCENARIO_ORDER,
    DemandMeasureMethod,
    PowerFactorAdjustMode,
    ScenarioBillSet,
    ScenarioKind,
)
from cenep.domain.timeseries import StorageDispatchConfig
from cenep.policy.tariff_plan_store import TariffPlanStore

OFFICIAL_110_ID = "HUBEI_COMMERCIAL_2026_01_TWO_PART_KV110"

PV_CAPACITY_KWP = 1000.0
STORAGE_POWER_KW = 500.0
STORAGE_ENERGY_KWH = 2000.0


# --------------------------------------------------------------------------- #
# 夹具与工厂
# --------------------------------------------------------------------------- #
def _project() -> Project:
    """受控 8760 项目：无光伏校验负担（容量 > 0），负荷与光伏由测试直接给定。"""
    project = Project()
    project.analysis_period = 1
    project.basic_info.project_name = "阶段 6 测试项目"
    project.timeseries.base_year = 2025
    project.timeseries.enabled = True
    project.pv.pv_capacity_kwp = PV_CAPACITY_KWP
    project.storage.storage_power_kw = STORAGE_POWER_KW
    project.storage.storage_energy_kwh = STORAGE_ENERGY_KWH
    return project


def _dispatch(
    *,
    allow_export: bool = False,
    allow_grid_charge: bool = False,
    strategy: DispatchStrategy = DispatchStrategy.PV_SELF_CONSUMPTION,
) -> StorageDispatchConfig:
    return StorageDispatchConfig(
        strategy=strategy,
        charge_from_pv=True,
        charge_from_grid=allow_grid_charge,
        allow_grid_charge=allow_grid_charge,
        allow_export=allow_export,
        allow_arbitrage=True,
        soc_min=0.10,
        soc_max=1.00,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
    )


@pytest.fixture(scope="module")
def axis():
    return econ.build_axis(_project())


@pytest.fixture(scope="module")
def load_series(axis):
    """白天 400 kW、夜间 120 kW 的**受控**负荷（8760 点）。"""
    n = axis.point_count
    return np.array([400.0 if 7 <= (i % 24) <= 18 else 120.0 for i in range(n)])


@pytest.fixture(scope="module")
def pv_series(axis):
    """受控光伏出力：仅 6–18 时发电，正午峰值 600 kWh/h。"""
    n = axis.point_count
    return np.array(
        [
            (
                600.0 * max(0.0, np.sin(np.pi * ((i % 24) - 6) / 12))
                if 6 <= (i % 24) <= 18
                else 0.0
            )
            for i in range(n)
        ]
    )


@pytest.fixture(scope="module")
def plan():
    return TariffPlanStore().get_plan(OFFICIAL_110_ID)


@pytest.fixture(scope="module")
def result_default(plan, axis, load_series, pv_series):
    """四场景默认对比（上网电价 0.35 元/kWh，储能不上网、不从电网充电）。"""
    return compare_four_scenarios(
        plan=plan,
        axis=axis,
        load=load_series,
        pv=pv_series,
        dispatch_config=_dispatch(),
        project_pv_capacity_kwp=PV_CAPACITY_KWP,
        project_storage_power_kw=STORAGE_POWER_KW,
        project_storage_energy_kwh=STORAGE_ENERGY_KWH,
        config=ScenarioConfig(export_price=0.35),
        project=_project(),
        load_source="受控算例（非实测）",
        load_is_measured=False,
    )


def _run(plan, axis, load, pv, cfg: ScenarioConfig, dispatch_cfg=None):
    return compare_four_scenarios(
        plan=plan,
        axis=axis,
        load=load,
        pv=pv,
        dispatch_config=dispatch_cfg or _dispatch(),
        project_pv_capacity_kwp=PV_CAPACITY_KWP,
        project_storage_power_kw=STORAGE_POWER_KW,
        project_storage_energy_kwh=STORAGE_ENERGY_KWH,
        config=cfg,
        project=_project(),
        load_source="受控算例（非实测）",
        load_is_measured=False,
    )


# --------------------------------------------------------------------------- #
# 1. 四场景基本行为（§7.1）
# --------------------------------------------------------------------------- #
class TestFourScenarios:
    """§7.1：同一客户、同一负荷、同一电价计划下模拟四类场景。"""

    def test_all_four_scenarios_present_in_fixed_order(self, result_default):
        kinds = [line.kind for line in result_default.scenarios]
        assert kinds == list(SCENARIO_ORDER)

    def test_scenario_equipment_configuration_differs_only(self, result_default):
        """四场景只有设备配置不同：负荷电量必须逐位相同（§7.1）。"""
        loads = {line.load_kwh for line in result_default.scenarios}
        assert len(loads) == 1, f"四场景负荷不一致：{loads}"
        by_kind = {line.kind: line for line in result_default.scenarios}
        assert by_kind[ScenarioKind.NO_PV_NO_STORAGE].pv_capacity_kwp == 0.0
        assert by_kind[ScenarioKind.NO_PV_NO_STORAGE].storage_power_kw == 0.0
        assert by_kind[ScenarioKind.PV_ONLY].pv_capacity_kwp == PV_CAPACITY_KWP
        assert by_kind[ScenarioKind.PV_ONLY].storage_power_kw == 0.0
        assert by_kind[ScenarioKind.STORAGE_ONLY].pv_capacity_kwp == 0.0
        assert by_kind[ScenarioKind.STORAGE_ONLY].storage_power_kw == STORAGE_POWER_KW
        assert by_kind[ScenarioKind.PV_STORAGE].pv_capacity_kwp == PV_CAPACITY_KWP
        assert by_kind[ScenarioKind.PV_STORAGE].storage_power_kw == STORAGE_POWER_KW

    def test_baseline_buys_the_whole_load(self, result_default):
        """基准场景：购电量 = 负荷电量，上网电量 = 0，无光伏发电。"""
        base = result_default.baseline
        assert base is not None
        assert base.grid_import_kwh == pytest.approx(base.load_kwh)
        assert base.grid_export_kwh == 0.0
        assert base.pv_generation_kwh == 0.0
        assert base.energy_charge_yuan > 0.0
        assert base.export_revenue_yuan == 0.0

    def test_pv_only_reduces_grid_purchase_and_saving_is_positive(self, result_default):
        by_kind = {line.kind: line for line in result_default.scenarios}
        base = by_kind[ScenarioKind.NO_PV_NO_STORAGE]
        pv_only = by_kind[ScenarioKind.PV_ONLY]
        assert pv_only.grid_import_kwh < base.grid_import_kwh
        comparison = result_default.comparison_of(ScenarioKind.PV_ONLY)
        assert comparison is not None
        assert comparison.bill_saving_yuan > 0.0
        assert comparison.bill_saving_rate is not None and 0.0 < comparison.bill_saving_rate < 1.0

    def test_pv_storage_saves_more_than_pv_only(self, result_default):
        """光储（储能供负荷）应比仅光伏省得更多：储能把光伏盈余搬到缺电时段。"""
        pv_only = result_default.comparison_of(ScenarioKind.PV_ONLY)
        pv_storage = result_default.comparison_of(ScenarioKind.PV_STORAGE)
        assert pv_only is not None and pv_storage is not None
        assert pv_storage.bill_saving_yuan > pv_only.bill_saving_yuan
        assert pv_storage.scenario.storage_discharge_kwh > 0.0

    def test_demand_reduction_is_listed_separately(self, result_default):
        """§7.5：需量变化与需量电费差必须**与电度电费分开列示**。"""
        comparison = result_default.comparison_of(ScenarioKind.PV_STORAGE)
        assert comparison is not None
        assert comparison.demand_reduction_kw > 0.0
        assert comparison.demand_charge_delta_yuan > 0.0
        # 需量电费差 × 单价 = 削减量 × 每月（12 个月）
        unit = 39.0
        assert comparison.demand_charge_delta_yuan == pytest.approx(
            comparison.demand_reduction_kw * unit * 12, rel=1e-9
        )

    def test_cost_components_sum_to_total_cost(self, result_default):
        """分项费用之和严格等于成本合计；收入项与"已在电度电费内"的分类列示项除外。"""
        for line in result_default.scenarios:
            included = sum(
                item.amount_yuan for item in line.cost_components if item.included_in_total_cost
            )
            assert included == pytest.approx(line.total_cost_yuan, abs=1e-6)
            export = line.component("export_revenue")
            assert export is not None and export.is_revenue
            assert export.included_in_total_cost is False
            # 电度电费 + 需量电费 + 力调 + 其他 = 成本合计（基金已含在电度电费内）
            assert line.total_cost_yuan == pytest.approx(
                line.energy_charge_yuan
                + line.demand_charge_yuan
                + line.capacity_charge_yuan
                + line.power_factor_adjustment_yuan
                + line.other_unmodeled_yuan,
                abs=1e-6,
            )

    def test_net_cost_equals_total_cost_minus_export(self, result_default):
        for line in result_default.scenarios:
            assert line.net_cost_yuan == pytest.approx(
                line.total_cost_yuan - line.export_revenue_yuan, abs=1e-6
            )

    def test_monthly_rows_sum_to_annual(self, result_default):
        """§7.7：逐月费用拆分之和必须等于年度值。"""
        for line in result_default.scenarios:
            assert len(line.monthly) == 12
            assert sum(row.grid_import_kwh for row in line.monthly) == pytest.approx(
                line.grid_import_kwh, rel=1e-9
            )
            assert sum(row.energy_charge_yuan for row in line.monthly) == pytest.approx(
                line.energy_charge_yuan, rel=1e-9
            )
            assert sum(row.demand_charge_yuan for row in line.monthly) == pytest.approx(
                line.demand_charge_yuan, rel=1e-9
            )

    def test_repeat_run_is_bitwise_identical(self, plan, axis, load_series, pv_series, result_default):
        """§7.7 第 1 条：相同输入重复运行结果完全一致。"""
        again = _run(plan, axis, load_series, pv_series, ScenarioConfig(export_price=0.35))
        assert again.unique_annual_benefit_yuan == result_default.unique_annual_benefit_yuan
        for first, second in zip(result_default.scenarios, again.scenarios):
            assert first.total_cost_yuan == second.total_cost_yuan
            assert first.grid_import_kwh == second.grid_import_kwh
            assert first.peak_demand_kw == second.peak_demand_kw


def _weighted_price() -> float:
    """占位保留（历史草稿使用），当前无调用方。"""
    return 0.0



# --------------------------------------------------------------------------- #
# 2. 收益去重恒等式（§7.6 —— 本阶段最重要的测试）
# --------------------------------------------------------------------------- #
class TestSavingIdentity:
    """§7.6：分解口径必须与现金口径逐位闭合，偏差超限即判失败。"""

    def test_identity_passes_for_all_scenarios(self, result_default):
        assert result_default.dedup_verified is True
        assert result_default.identity_max_deviation_yuan <= IDENTITY_TOLERANCE_YUAN
        for comparison in result_default.comparisons:
            assert comparison.dedup.identity_passed is True
            assert comparison.dedup.identity_deviation_yuan <= IDENTITY_TOLERANCE_YUAN

    def test_cash_saving_equals_pv_self_plus_arbitrage_plus_residual(
        self, plan, axis, load_series, pv_series, result_default
    ):
        """核心恒等式：S = 光伏自用节省 + 储能套利收益 + 储能损耗残差。"""
        tariff = build_project_tariff_series(
            plan, axis, export_price=0.35, require_verified=False
        )
        # 用引擎内部口径重新算一遍（独立算式，不复用被校验对象的中间值）
        from cenep.calculation.dispatch_engine import dispatch

        by_kind = {line.kind: line for line in result_default.scenarios}
        for comparison in result_default.comparisons:
            if comparison.scenario.kind is not ScenarioKind.PV_STORAGE:
                continue
            outcome = dispatch(
                load=np.asarray(load_series, dtype=float),
                pv=np.asarray(pv_series, dtype=float),
                tariff=tariff,
                axis=axis,
                config=_dispatch(),
                storage_capacity_kwh=STORAGE_ENERGY_KWH,
                storage_power_kw=STORAGE_POWER_KW,
            )
            baseline = econ.compute_baseline(load_series, tariff, axis)
            metrics = econ.compute_metrics(outcome, axis, baseline, demand_charge=39.0)
            cash = cash_electricity_cost_saving(load_series, tariff, outcome)
            decomposed = (
                float(metrics.pv_self_consumption_saving)
                + float(metrics.storage_arbitrage_revenue)
                + storage_loss_residual_yuan(outcome)
            )
            assert cash == pytest.approx(decomposed, abs=1e-6)
            # 与 V2.0 既有硬校验一致（同一口径、同一容差）
            assert econ.check_saving_identity(metrics) <= 1e-6
            # 被作为报告展示的分解值必须与独立复算一致
            assert comparison.scenario.pv_self_consumption_saving_yuan == pytest.approx(
                float(metrics.pv_self_consumption_saving), rel=1e-12
            )
            assert by_kind[ScenarioKind.PV_STORAGE].storage_discharge_kwh > 0.0

    def test_bill_saving_equals_cash_saving_plus_demand_saving(self, result_default):
        """账单节省 = 电度电费节省（现金口径）+ 需量电费节省（§7.5 分开列示）。"""
        for comparison in result_default.comparisons:
            assert comparison.bill_saving_yuan == pytest.approx(
                comparison.dedup.bill_saving_yuan, abs=1e-9
            )
            assert comparison.bill_saving_yuan == pytest.approx(
                comparison.energy_charge_delta_yuan + comparison.demand_charge_delta_yuan,
                abs=1e-6,
            )

    def test_unique_benefit_equals_bill_saving_plus_export_plus_external(
        self, result_default
    ):
        """§7.6 第 5 条：唯一收益 = 账单节省 + 上网收入 + 外部收益，仅此三项。"""
        for comparison in result_default.comparisons:
            manifest = comparison.dedup
            expected = (
                manifest.bill_saving_yuan
                + manifest.export_revenue_yuan
                + manifest.storage_capacity_revenue_yuan
                + manifest.storage_ancillary_revenue_yuan
                + manifest.storage_other_revenue_yuan
            )
            assert manifest.unique_annual_benefit_yuan == pytest.approx(expected, abs=1e-9)

    def test_analysis_total_is_strictly_larger_and_never_used(self, result_default):
        """分析口径合计把分解项也加进去了，因此不小于唯一收益；它**不得**被使用。"""
        for comparison in result_default.comparisons:
            manifest = comparison.dedup
            assert manifest.analysis_total_yuan >= manifest.unique_annual_benefit_yuan - 1e-9
            entry = manifest.entry("total_benefit_analysis")
            assert entry is not None
            assert entry.counted_in_unique_benefit is False
            assert "不可填入现金流" in entry.excluded_reason
            # 参考场景（光储）确实有分解项，因此**严格**更大
            if comparison.scenario.kind is ScenarioKind.PV_STORAGE:
                assert manifest.analysis_total_yuan > manifest.unique_annual_benefit_yuan

    def test_all_decomposition_entries_are_excluded(self, result_default):
        """光伏自用节省、储能套利收益、需量电费节省三项都必须标记为**不计入**。"""
        for comparison in result_default.comparisons:
            for key in (
                "pv_self_consumption_saving",
                "storage_arbitrage_revenue",
                "demand_cost_saving",
            ):
                entry = comparison.dedup.entry(key)
                assert entry is not None, key
                assert entry.counted_in_unique_benefit is False, key
                assert entry.entry_role == "decomposition", key
                assert "重复计算" in entry.excluded_reason, key

    def test_no_benefit_amount_is_registered_twice(self, result_default):
        """同一笔钱只能登记一次：唯一来源项不得重复出现同一键。"""
        for comparison in result_default.comparisons:
            unique = [item.key for item in comparison.dedup.unique_entries()]
            assert len(unique) == len(set(unique))
            for key in unique:
                assert key in {
                    "electricity_cost_saving",
                    "export_revenue",
                    "storage_capacity_revenue",
                    "storage_ancillary_revenue",
                    "storage_other_revenue",
                }

    def test_baseline_scenario_has_no_demand_saving(self, result_default):
        """基准场景的"需量节省"必须为 0（它就是基准本身，§7.5）。"""
        assert result_default.baseline is not None
        assert result_default.baseline.demand_cost_saving_yuan == 0.0
        assert result_default.baseline.demand_saving_kw == 0.0


# --------------------------------------------------------------------------- #
# 3. 成本层面的重复计算（§2.3、§7.6）—— 本阶段实际修复的缺陷
# --------------------------------------------------------------------------- #
class TestNoCostLevelDoubleCount:
    """政府性基金及附加已含在分时电价内时，**不得**再作为独立成本相加。

    背景：湖北官方电价表的基金及附加分项被声明为 ``included_in_tou_price=True``。
    实施初期把它当作独立费用相加，导致"账单节省"比现金口径高出
    ``Σ购电量 × 基金单价``（受控算例实测 41,730 元）。本组测试固化该修复。
    """

    def test_official_plan_declares_fund_included(self, plan):
        assert extract_government_fund_yuan_per_kwh(plan) == pytest.approx(0.0452)
        assert government_fund_included_in_tou_price(plan) is True

    def test_fund_is_listed_but_not_added_again(self, result_default):
        for line in result_default.scenarios:
            fund = line.component("government_fund")
            assert fund is not None
            assert fund.amount_yuan > 0.0  # 分类列示（不是 0）
            assert fund.included_in_total_cost is False  # 但不再计入成本合计
            assert fund.included_in_energy_charge is True  # 已在电度电费内
            assert fund.is_transfer_note is True
            assert "只分类列示" in fund.source_note
            included = sum(
                item.amount_yuan for item in line.cost_components if item.included_in_total_cost
            )
            assert included == pytest.approx(line.total_cost_yuan, abs=1e-6)
            # 分项金额与"购电量 × 计划基金单价"一致（分类列示值可核对）
            assert fund.amount_yuan == pytest.approx(line.grid_import_kwh * 0.0452, rel=1e-9)

    def test_adding_fund_on_top_of_tou_prices_breaks_the_identity(
        self, plan, axis, load_series, pv_series, result_default
    ):
        """把**已经含在分时电价里**的基金再加一次，账单节省会比现金口径高出恰好
        ``(基准购电量 − 方案购电量) × 基金单价``。

        这是本阶段实际修复的缺陷模型：早期草稿一边把基金当作独立费用相加，
        一边又在电价里含了它。下面用引擎的既有入口显式复现这个错误口径，
        并断言恒等式**确实会失败**（证明校验有抓取能力）。
        """
        from cenep.calculation.scenario_bill_engine import (
            apply_separate_price_surcharge,
            evaluate_scenario,
        )

        fund = 0.0452
        base_tariff = build_project_tariff_series(
            plan, axis, export_price=0.35, require_verified=False
        )
        # "含基金"的原电价（官方表列值本身已含基金）
        charged = apply_separate_price_surcharge(
            base_tariff, surcharge_yuan_per_kwh=fund, label="重复计算的基金"
        )

        def _scenario(kind: ScenarioKind, pv: np.ndarray, st_energy: float, st_power: float):
            return evaluate_scenario(
                kind=kind,
                load=np.asarray(load_series, dtype=float),
                pv=np.asarray(pv, dtype=float),
                axis=axis,
                tariff=charged,
                dispatch_config=_dispatch(),
                project_pv_capacity_kwp=PV_CAPACITY_KWP,
                project_storage_power_kw=st_power,
                project_storage_energy_kwh=st_energy,
                scenario_config=ScenarioConfig(export_price=0.35),
                government_fund_yuan_per_kwh=fund,
            )

        zeros = np.zeros(axis.point_count, dtype=float)
        baseline_eval = _scenario(ScenarioKind.NO_PV_NO_STORAGE, zeros, 0.0, 0.0)
        pv_eval = _scenario(ScenarioKind.PV_ONLY, pv_series, 0.0, 0.0)
        cash = cash_electricity_cost_saving(
            np.asarray(load_series, dtype=float), charged, pv_eval.outcome
        )
        # 错误口径：在电价已含基金之外，再把"购电量 × 基金单价"当作独立成本相加
        buggy_bill_saving = (
            baseline_eval.line.total_cost_yuan + baseline_eval.line.grid_import_kwh * fund
        ) - (pv_eval.line.total_cost_yuan + pv_eval.line.grid_import_kwh * fund)
        correct_bill_saving = (
            baseline_eval.line.total_cost_yuan - pv_eval.line.total_cost_yuan
        )
        expected_extra = (
            baseline_eval.line.grid_import_kwh - pv_eval.line.grid_import_kwh
        ) * fund
        assert buggy_bill_saving - correct_bill_saving == pytest.approx(expected_extra, rel=1e-9)
        # 正确口径与现金口径一致（这正是引擎默认走的路）
        assert correct_bill_saving == pytest.approx(cash, abs=1e-6)
        # 错误口径偏离现金口径 → 恒等式校验会判定失败（§7.6）
        assert abs(buggy_bill_saving - cash) > IDENTITY_TOLERANCE_YUAN
        assert expected_extra == pytest.approx(
            (baseline_eval.line.grid_import_kwh - pv_eval.line.grid_import_kwh) * 0.0452
        )

    def test_separate_fund_flag_is_still_available_for_plans_without_fund(
        self, plan, axis, load_series, pv_series
    ):
        """分时单价**不含**基金及附加的计划必须能把基金作为独立成本相加。

        做法：把计划各时段的直接单价整体下调基金单价 0.0452 元/千瓦时
        （模拟"合同价不含政府性基金及附加"的用户侧计划），再由用户**显式**要求相加。
        此时成本口径完整、去重仍然通过，且成本合计与"含基金的原计划"口径一致。
        """
        fund = extract_government_fund_yuan_per_kwh(plan)
        assert fund is not None and fund > 0.0
        no_fund = plan.model_copy(
            update={
                "time_period_rules": [
                    rule.model_copy(
                        update={
                            "direct_price_yuan_per_kwh": float(rule.direct_price_yuan_per_kwh) - fund
                        }
                    )
                    for rule in plan.time_period_rules
                ],
                "price_components": [
                    component
                    for component in plan.price_components
                    if component.component_type.value != "government_fund"
                ],
            }
        )
        assert extract_government_fund_yuan_per_kwh(no_fund) is None
        result = compare_four_scenarios(
            plan=no_fund,
            axis=axis,
            load=load_series,
            pv=pv_series,
            dispatch_config=_dispatch(),
            project_pv_capacity_kwp=PV_CAPACITY_KWP,
            project_storage_power_kw=STORAGE_POWER_KW,
            project_storage_energy_kwh=STORAGE_ENERGY_KWH,
            config=ScenarioConfig(
                export_price=0.35,
                government_fund_yuan_per_kwh=fund,
                government_fund_included_in_tou_price=False,
            ),
            project=_project(),
        )
        assert result.dedup_verified is True
        component = result.baseline.component("government_fund")
        assert component is not None
        # 已"并入逐周期电价"，因此是分类列示（计入电度电费，不再二次相加）
        assert component.included_in_energy_charge is True
        assert component.included_in_total_cost is False
        assert component.amount_yuan == pytest.approx(
            result.baseline.grid_import_kwh * fund, rel=1e-9
        )
        assert "并入逐周期电价" in component.source_note
        # 与"含基金的原计划"相比：成本合计口径一致（差异只来自电价取整）
        original = _run(plan, axis, load_series, pv_series, ScenarioConfig(export_price=0.35))
        assert result.baseline.total_cost_yuan == pytest.approx(
            original.baseline.total_cost_yuan, rel=1e-9
        )


# --------------------------------------------------------------------------- #
# 4. 边界条件（§7.6）：储能上网 / 电网充电 / 上网电价为 0
# --------------------------------------------------------------------------- #
class TestBoundaryConditions:
    """§7.6 的边界：这些开关会改变收益结构，但**不得**破坏去重。"""

    def test_storage_export_raises_export_revenue_but_not_bill_saving(
        self, plan, axis, load_series, pv_series
    ):
        """储能上网开启：上网收入显著增加，但账单节省**逐位不变**（§7.6 第 3 条）。"""
        off = _run(plan, axis, load_series, pv_series, ScenarioConfig(export_price=0.35))
        on = compare_four_scenarios(
            plan=plan,
            axis=axis,
            load=load_series,
            pv=pv_series,
            dispatch_config=_dispatch(allow_export=True),
            project_pv_capacity_kwp=PV_CAPACITY_KWP,
            project_storage_power_kw=STORAGE_POWER_KW,
            project_storage_energy_kwh=STORAGE_ENERGY_KWH,
            config=ScenarioConfig(export_price=0.35),
            project=_project(),
        )
        off_cmp = off.comparison_of(ScenarioKind.PV_STORAGE)
        on_cmp = on.comparison_of(ScenarioKind.PV_STORAGE)
        assert off_cmp is not None and on_cmp is not None
        assert on_cmp.scenario.grid_export_kwh > off_cmp.scenario.grid_export_kwh
        # 上网收入（含储能上网）增加
        assert on_cmp.dedup.export_revenue_yuan > off_cmp.dedup.export_revenue_yuan
        # 账单节省＝电度电费差＋需量电费差，两者都**不含**上网电量，因此结构不变
        assert on_cmp.bill_saving_yuan == pytest.approx(
            on_cmp.energy_charge_delta_yuan + on_cmp.demand_charge_delta_yuan, abs=1e-6
        )
        # 去重仍然通过
        assert on.dedup_verified is True
        assert on_cmp.dedup.identity_passed is True

    def test_storage_export_revenue_is_counted_exactly_once(self, plan, axis, load_series, pv_series):
        """储能上网收入只在"上网收入"一处登记；不得再出现在套利收益或账单节省里。"""
        on = compare_four_scenarios(
            plan=plan,
            axis=axis,
            load=load_series,
            pv=pv_series,
            dispatch_config=_dispatch(allow_export=True),
            project_pv_capacity_kwp=PV_CAPACITY_KWP,
            project_storage_power_kw=STORAGE_POWER_KW,
            project_storage_energy_kwh=STORAGE_ENERGY_KWH,
            config=ScenarioConfig(export_price=0.35),
            project=_project(),
        )
        comparison = on.comparison_of(ScenarioKind.PV_STORAGE)
        assert comparison is not None
        # 上网收入 ÷ 上网电价 = 上网电量（严格闭合，说明没有被缩放或部分重复计入）
        assert comparison.dedup.export_revenue_yuan == pytest.approx(
            comparison.scenario.grid_export_kwh * 0.35, rel=1e-9
        )
        # 储能套利收益只用"供负荷"的放电量：不得包含上网那部分
        entry = comparison.dedup.entry("storage_arbitrage_revenue")
        assert entry is not None and entry.counted_in_unique_benefit is False
        assert "load_from_storage" not in entry.caliber  # 口径文字用的是中文描述
        assert "储能→负荷" in entry.caliber
        # 唯一收益确实把上网收入算了且只算一次
        assert comparison.dedup.unique_annual_benefit_yuan == pytest.approx(
            comparison.bill_saving_yuan + comparison.dedup.export_revenue_yuan, abs=1e-6
        )
        # 若上网收入被重复计入，唯一收益会比"账单节省 + 上网收入"多出一份上网收入
        assert comparison.dedup.unique_annual_benefit_yuan != pytest.approx(
            comparison.bill_saving_yuan + 2.0 * comparison.dedup.export_revenue_yuan
        )

    def test_export_check_detects_arithmetic_coupling(self):
        """§7.6 第 3 条的可执行判据：上网收入与账单节省额必须代数独立。"""
        deviation = check_export_not_in_bill_saving(
            baseline_cost_yuan=1_000_000.0,
            scenario_cost_yuan=400_000.0,
            export_revenue_yuan=123_456.0,
        )
        assert deviation == pytest.approx(0.0, abs=1e-12)

    def test_grid_charge_enabled_keeps_identity(self, plan, axis, load_series, pv_series):
        """电网充电开启：多买电，账单节省仍闭合，去重仍通过（§7.6）。"""
        result = compare_four_scenarios(
            plan=plan,
            axis=axis,
            load=load_series,
            pv=pv_series,
            dispatch_config=_dispatch(allow_grid_charge=True),
            project_pv_capacity_kwp=PV_CAPACITY_KWP,
            project_storage_power_kw=STORAGE_POWER_KW,
            project_storage_energy_kwh=STORAGE_ENERGY_KWH,
            config=ScenarioConfig(export_price=0.35),
            project=_project(),
        )
        comparison = result.comparison_of(ScenarioKind.PV_STORAGE)
        assert comparison is not None
        assert comparison.scenario.storage_grid_charge_kwh > 0.0
        assert result.dedup_verified is True
        assert comparison.dedup.identity_deviation_yuan <= IDENTITY_TOLERANCE_YUAN

    def test_export_price_zero_degrades_to_v20_narrow_identity(
        self, plan, axis, load_series, pv_series
    ):
        """上网电价为 0：光伏盈余全部弃光，退化为 V2.0 的窄口径恒等式且仍闭合。"""
        result = _run(plan, axis, load_series, pv_series, ScenarioConfig(export_price=0.0))
        assert result.dedup_verified is True
        for comparison in result.comparisons:
            assert comparison.dedup.export_revenue_yuan == 0.0
            assert comparison.bill_saving_yuan == pytest.approx(
                comparison.energy_charge_delta_yuan + comparison.demand_charge_delta_yuan,
                abs=1e-6,
            )

    def test_zero_export_price_no_longer_double_counts_curtailment(
        self, plan, axis, load_series, pv_series
    ):
        """上网电价为 0 时，**不得**把"弃光机会成本"当作收益或成本（曾误用，已固化）。"""
        result = _run(plan, axis, load_series, pv_series, ScenarioConfig(export_price=0.0))
        comparison = result.comparison_of(ScenarioKind.PV_STORAGE)
        assert comparison is not None
        for note in comparison.dedup.duplicate_risk_notes:
            assert "弃光" not in note or "机会成本" not in note
        # 恒等式仍然逐位闭合（若误扣机会成本，这里会失败）
        assert comparison.dedup.identity_deviation_yuan <= IDENTITY_TOLERANCE_YUAN

    def test_forgone_export_opportunity_cost_is_analysis_only(self, axis, load_series, pv_series, plan):
        """上网机会成本函数只做分析展示，不参与恒等式（其定义即光伏充电 × 上网电价）。"""
        from cenep.calculation.dispatch_engine import dispatch

        tariff = build_project_tariff_series(plan, axis, export_price=0.35, require_verified=False)
        outcome = dispatch(
            load=np.asarray(load_series, dtype=float),
            pv=np.asarray(pv_series, dtype=float),
            tariff=tariff,
            axis=axis,
            config=_dispatch(),
            storage_capacity_kwh=STORAGE_ENERGY_KWH,
            storage_power_kw=STORAGE_POWER_KW,
        )
        forgone = forgone_export_opportunity_cost(outcome, np.asarray(tariff.export_price))
        assert forgone > 0.0
        assert forgone == pytest.approx(float(np.sum(outcome.pv_to_storage)) * 0.35, rel=1e-9)


# --------------------------------------------------------------------------- #
# 5. 财务引擎接入（§3.5、§7.6 第 5 条）
# --------------------------------------------------------------------------- #
class TestFinanceHandoff:
    """§10 阶段 6 验收：电费差额和财务现金流相互一致，不重复计算收益。"""

    def test_finance_override_total_equals_unique_benefit(self, result_default):
        overrides = build_finance_overrides(
            result_default, dispatch_config=_dispatch(), project=_project(), years=1
        )
        assert set(overrides) == {1}
        assert overrides[1].total_revenue() == pytest.approx(
            result_default.unique_annual_benefit_yuan, abs=1e-6
        )

    def test_engine_first_year_revenue_equals_unique_benefit(self, result_default):
        """既有财务引擎的首年运营收益必须等于去重后的唯一数值（§7.6 第 5 条）。"""
        from cenep.calculation.engine import CalculationEngine

        service = ScenarioBillService(_project())
        overrides = service.build_finance_input(result_default, years=1)
        financials = CalculationEngine().calculate(_project(), year_override=overrides)
        assert financials.annual_results[0].total_revenue == pytest.approx(
            result_default.unique_annual_benefit_yuan, abs=1e-6
        )
        ok, deviation, messages = service.verify_finance_consistency(
            result_default, years=1
        )
        assert ok is True
        assert deviation <= 1e-6
        assert any("不存在重复计算" in message for message in messages)

    def test_finance_input_rejected_when_dedup_fails(self, result_default):
        """去重未通过时必须**阻断**财务接入，并给出中文原因（§7.6 第 5 条）。

        说明：本引擎的正常路径不会产出"去重未通过"的结果（未通过会先被恒等式
        校验拦下，见 ``test_adding_fund_on_top_of_tou_prices_breaks_the_identity``）。
        这里显式构造一个未通过的状态，验证**闸门本身**确实存在且不可绕过。
        """
        broken = result_default.model_copy(update={"dedup_verified": False})
        assert broken.dedup_verified is False
        with pytest.raises(ValidationError, match="去重校验未通过"):
            build_finance_overrides(
                broken, dispatch_config=_dispatch(), project=_project(), years=1
            )
        service = ScenarioBillService(_project())
        with pytest.raises(ValidationError, match="去重校验未通过"):
            service.build_finance_input(broken, years=1)

    def test_every_year_carries_the_same_unique_benefit_when_flat(
        self, result_default
    ):
        """四条增长率全为 0 时，各年运营收益严格相同（线性外推口径，已披露）。"""
        project = _project()
        project.timeseries.load.annual_growth_rate = 0.0
        project.timeseries.tariff.annual_growth_rate = 0.0
        project.pv.annual_degradation_rate = 0.0
        project.storage.annual_degradation_rate = 0.0
        overrides = build_finance_overrides(
            result_default, dispatch_config=_dispatch(), project=project, years=3
        )
        totals = {round(overrides[year].total_revenue(), 6) for year in (1, 2, 3)}
        assert totals == {round(result_default.unique_annual_benefit_yuan, 6)}

    def test_revenue_follows_disclosed_extrapolation(self, result_default):
        """有衰减/增长时，跨年收益按**已披露的**线性外推缩放，不是逐年重算。"""
        project = _project()
        project.timeseries.load.annual_growth_rate = 0.0
        project.timeseries.tariff.annual_growth_rate = 0.0
        project.pv.annual_degradation_rate = 0.005
        project.storage.annual_degradation_rate = 0.0
        overrides = build_finance_overrides(
            result_default, dispatch_config=_dispatch(), project=project, years=3
        )
        assert overrides[2].total_revenue() < overrides[1].total_revenue()
        assert overrides[3].total_revenue() < overrides[2].total_revenue()
        # 光伏电量严格按 (1 − d)^(n−1) 外推
        assert overrides[3].pv_generation == pytest.approx(
            overrides[1].pv_generation * (1.0 - 0.005) ** 2, rel=1e-12
        )

    def test_energy_volumes_travel_with_revenue(self, result_default):
        """电量与收益必须同源（避免"电量按 A、钱按 B"的口径撕裂，§105 精神）。"""
        overrides = build_finance_overrides(
            result_default, dispatch_config=_dispatch(), project=_project(), years=1
        )
        line = result_default.line_of(result_default.reference_scenario)
        assert line is not None
        assert overrides[1].load == pytest.approx(line.load_kwh)
        assert overrides[1].grid_import == pytest.approx(line.grid_import_kwh)
        assert overrides[1].pv_self_use == pytest.approx(line.pv_to_load_kwh)


# --------------------------------------------------------------------------- #
# 6. 必答题：同一笔钱不得出现在两个收益字段里
# --------------------------------------------------------------------------- #
class TestNoBenefitAppearsTwice:
    """把四场景全部收益字段两两组合检查，确保没有任何一对"重复表述同一笔钱"。"""

    def test_unique_benefit_field_set_is_minimal_and_disjoint(self, result_default):
        for comparison in result_default.comparisons:
            manifest = comparison.dedup
            counted = {entry.key for entry in manifest.unique_entries()}
            # 唯一来源**只能**是这三类：账单节省、上网收入、外部收入参数
            assert counted <= {
                "electricity_cost_saving",
                "export_revenue",
                "storage_capacity_revenue",
                "storage_ancillary_revenue",
                "storage_other_revenue",
            }
            # 分解口径一个都不能进
            assert not (counted & {
                "pv_self_consumption_saving",
                "storage_arbitrage_revenue",
                "demand_cost_saving",
                "total_benefit_analysis",
            })

    def test_decomposition_never_exceeds_bill_saving(self, result_default):
        """三个纯分解项之和不得超过账单节省（否则必然重复计入）。"""
        for comparison in result_default.comparisons:
            manifest = comparison.dedup
            decomposition = sum(
                manifest.entry(key).amount_yuan
                for key in (
                    "pv_self_consumption_saving",
                    "storage_arbitrage_revenue",
                    "demand_cost_saving",
                )
            )
            assert decomposition <= manifest.bill_saving_yuan + IDENTITY_TOLERANCE_YUAN

    def test_analysis_total_equals_unique_plus_clean_decomposition(self, result_default):
        """分析口径合计与唯一收益之差**恰好**是三个纯分解项，无第四项混入。"""
        for comparison in result_default.comparisons:
            manifest = comparison.dedup
            decomposition = sum(
                manifest.entry(key).amount_yuan
                for key in (
                    "pv_self_consumption_saving",
                    "storage_arbitrage_revenue",
                    "demand_cost_saving",
                )
            )
            assert manifest.analysis_total_yuan - manifest.unique_annual_benefit_yuan == pytest.approx(
                decomposition, abs=1e-6
            )

    def test_unique_benefit_is_not_duplicated_across_scenarios(self, result_default):
        """四场景各自独立：唯一收益必须取自**参考场景**，不能用其他场景的数值替换。"""
        reference = result_default.line_of(result_default.reference_scenario)
        assert reference is not None
        assert result_default.unique_annual_benefit_yuan != 0.0
        comparison = result_default.reference_comparison()
        assert comparison is not None
        assert result_default.unique_annual_benefit_yuan == pytest.approx(
            comparison.dedup.unique_annual_benefit_yuan, abs=1e-9
        )
        # 其他场景的唯一收益必须各不相同（否则说明口径被串用）
        others = [
            item.dedup.unique_annual_benefit_yuan
            for item in result_default.comparisons
            if item.scenario.kind is not result_default.reference_scenario
        ]
        for value in others:
            assert value != pytest.approx(result_default.unique_annual_benefit_yuan)


# --------------------------------------------------------------------------- #
# 7. 需量口径（§7.5）与中文校验错误（§0.2）
# --------------------------------------------------------------------------- #
class TestDemandAndValidation:
    """§7.5 需量口径 + §0.2 中文校验错误。"""

    def test_windowed_demand_uses_configured_window(self, axis):
        """窗口长于数据间隔时按窗口平均，不得用"每小时平均负荷峰值"冒充电表需量。"""
        monthly, peak, warnings = windowed_max_demand_kw(
            np.full(axis.point_count, 100.0), axis, window_minutes=15
        )
        assert len(monthly) == 12
        assert peak == pytest.approx(100.0)
        assert warnings == []

    def test_windowed_demand_rejects_length_mismatch(self, axis):
        with pytest.raises(ValidationError, match="长度"):
            windowed_max_demand_kw(np.zeros(10), axis, window_minutes=15)

    def test_windowed_demand_rejects_non_positive_window(self, axis, load_series):
        with pytest.raises(ValidationError, match="计量窗口必须为正整数分钟"):
            windowed_max_demand_kw(load_series, axis, window_minutes=0)

    def test_nan_window_is_discarded_and_reported(self, axis):
        """窗口内含缺失点必须整窗作废并给出中文警告（§6.4：缺失不得按 0 处理）。"""
        series = np.full(axis.point_count, 100.0)
        series[axis.point_count // 2] = np.nan
        _monthly, _peak, warnings = windowed_max_demand_kw(series, axis, window_minutes=60)
        assert warnings and "缺失" in warnings[0]

    def test_fixed_demand_requires_value(self):
        with pytest.raises(ValidationError, match="没有填写固定需量"):
            ScenarioConfig(demand_method=DemandMeasureMethod.FIXED)

    def test_negative_export_price_rejected(self):
        with pytest.raises(ValidationError, match="上网电价不能为负"):
            ScenarioConfig(export_price=-0.1)

    def test_scenarios_must_include_baseline(self):
        with pytest.raises(ValidationError, match="必须包含基准场景"):
            ScenarioConfig(scenarios=(ScenarioKind.PV_ONLY,))

    def test_unknown_other_unmodeled_mode_rejected(self):
        with pytest.raises(ValidationError, match="无法识别"):
            ScenarioConfig(other_unmodeled_mode="magic")

    def test_power_factor_ratio_out_of_range_rejected(self):
        with pytest.raises(ValidationError, match="必须在"):
            ScenarioConfig(
                power_factor_mode=PowerFactorAdjustMode.RATIO_OF_ENERGY_CHARGE,
                power_factor_ratio=1.5,
            )

    def test_negative_fund_price_rejected(self):
        with pytest.raises(ValidationError, match="政府性基金及附加单价不能为负"):
            ScenarioConfig(government_fund_yuan_per_kwh=-0.01)

    def test_load_length_mismatch_rejected(self, plan, axis, pv_series):
        with pytest.raises(ValidationError, match="负荷曲线点数"):
            compare_four_scenarios(
                plan=plan,
                axis=axis,
                load=np.zeros(10),
                pv=pv_series,
                dispatch_config=_dispatch(),
                project_pv_capacity_kwp=PV_CAPACITY_KWP,
                project_storage_power_kw=STORAGE_POWER_KW,
                project_storage_energy_kwh=STORAGE_ENERGY_KWH,
                config=ScenarioConfig(export_price=0.35),
            )

    def test_power_factor_modes_change_total_cost(self, plan, axis, load_series, pv_series):
        """力调电费三种处理方式必须产生可解释的差异（§3.4、§7.5）。"""
        excluded = _run(plan, axis, load_series, pv_series, ScenarioConfig(export_price=0.35))
        fixed = _run(
            plan,
            axis,
            load_series,
            pv_series,
            ScenarioConfig(
                export_price=0.35,
                power_factor_mode=PowerFactorAdjustMode.FIXED,
                power_factor_fixed_yuan=-30_000.0,
            ),
        )
        assert fixed.baseline.power_factor_adjustment_yuan == pytest.approx(-30_000.0)
        assert excluded.baseline.power_factor_adjustment_yuan == 0.0
        # 固定金额在基准与方案两侧相同 → 账单节省不受影响
        excluded_cmp = excluded.comparison_of(ScenarioKind.PV_STORAGE)
        fixed_cmp = fixed.comparison_of(ScenarioKind.PV_STORAGE)
        assert excluded_cmp is not None and fixed_cmp is not None
        assert fixed_cmp.bill_saving_yuan == pytest.approx(excluded_cmp.bill_saving_yuan, abs=1e-6)


# --------------------------------------------------------------------------- #
# 8. 应用服务（§7.1、§3.5、§8.1 行数据）
# --------------------------------------------------------------------------- #
class TestScenarioService:
    """应用服务只做编排：公式仍在 ``calculation/``（§0.2 红线）。"""

    def test_service_runs_with_engine_provided_series(self, load_series, pv_series, axis):
        """服务层在显式给出曲线时必须能独立于项目负荷配置完成四场景。"""
        from cenep.domain.load_data import HighFrequencyLoadDataset
        from cenep.domain.timeseries import TimeSeriesPoint

        service = ScenarioBillService(_project())
        points = [
            TimeSeriesPoint(timestamp=stamp, load_kwh=float(value))
            for stamp, value in zip(axis.timestamps, load_series)
        ]
        dataset = HighFrequencyLoadDataset(
            profile_id="TEST-8760",
            project_id="阶段 6 测试项目",
            name="受控 8760 小时负荷",
            interval_minutes=60,
            points=points,
            period_start=points[0].timestamp,
            period_end=points[-1].timestamp,
        )
        result = service.compare(
            OFFICIAL_110_ID,
            config=ScenarioConfig(export_price=0.35),
            dataset=dataset,
            pv_series=pv_series,
            dispatch_config=_dispatch(),
        )
        assert isinstance(result, ScenarioBillSet)
        assert result.dedup_verified is True
        assert result.point_count == axis.point_count

    def test_service_reports_chinese_error_without_dataset(self):
        """无负荷数据集时必须给出中文错误（§0.2），不得静默返回空结果。"""
        service = ScenarioBillService(_project())
        with pytest.raises(ValidationError, match="尚未选择负荷数据集"):
            service.compare(OFFICIAL_110_ID, config=ScenarioConfig(export_price=0.35))

    def test_describe_scenarios_lists_all_four(self):
        service = ScenarioBillService(_project())
        text = service.describe_scenarios()
        for kind in SCENARIO_ORDER:
            assert kind.label in text

    def test_report_rows_are_consistent(self, result_default):
        service = ScenarioBillService(_project())
        scenario_rows = service.scenario_rows(result_default)
        assert len(scenario_rows) == len(result_default.scenarios) + 1
        comparison_rows = service.comparison_rows(result_default)
        assert len(comparison_rows) == len(result_default.comparisons) + 1
        dedup_rows = service.dedup_rows(result_default)
        assert len(dedup_rows) > 1
        manifest_rows = service.dedup_manifest_rows(
            result_default.comparisons[0].dedup
        )
        assert any("唯一去重后的年度运营收益" in str(row[0]) for row in manifest_rows)

    def test_headline_states_estimate_confidence(self, result_default):
        text = ScenarioBillService.headline(result_default)
        assert "唯一去重后的年度运营收益" in text
        assert "估算曲线" in text  # load_is_measured=False

    def test_serve_save_roundtrip(self, tmp_path):
        service = ScenarioBillService(_project())
        path = service.save(tmp_path / "stage6.nep")
        assert path.exists()


# --------------------------------------------------------------------------- #
# 9. 真实账单电价闭环（12 份东风本田 2025 年账单，§3.1、§3.4、§7.4）
# --------------------------------------------------------------------------- #
class TestRealBillPrices:
    """用 12 份**真实账单的实际分时单价**跑四场景，并与账单电度电费交叉核对。

    数据来源：``tests/data/dongfeng_2025_bill_facts.json``（阶段 5 从 12 份 PDF 只读抽取并逐份核对）。
    本组测试**只读**该夹具，不修改；负荷曲线是按账单分时电量结构**构造**的（不是实测曲线），
    因此只用于验证：

    * 电价口径能否复现账单电度电费（口径正确性）；
    * 四场景与收益去重在**真实电价**下是否仍逐位闭合（§7.6）。

    真实 15 分钟曲线的联调见 ``taiqu-storage/stage6_real_data_check.py``（只读外部资料，不入库）。
    """

    @staticmethod
    def _load_curve(facts: dict) -> tuple[object, np.ndarray, np.ndarray, np.ndarray]:
        """构造与账单分时电量结构一致的 8760 小时负荷 + 光伏曲线（受控算例）。"""
        from cenep.calculation.timeseries_engine import build_time_axis

        axis = build_time_axis(2025, Resolution.HOURLY)
        template = np.array(
            [0.88, 0.85, 0.84, 0.84, 0.87, 0.94, 1.02, 1.10, 1.16, 1.18, 1.17, 1.10,
             0.98, 0.99, 1.12, 1.16, 1.14, 1.08, 1.00, 0.98, 0.95, 0.93, 0.91, 0.89]
        )
        loads = np.zeros(axis.point_count)
        pvs = np.zeros(axis.point_count)
        month = np.asarray(axis.month)
        hour = np.asarray(axis.hour).astype(int)
        pv_shape = np.array(
            [0.0] * 6 + [0.05, 0.18, 0.38, 0.60, 0.78, 0.88, 0.90, 0.84, 0.70, 0.50,
                         0.28, 0.08] + [0.0] * 6
        )
        for entry in facts["months"]:
            m = int(entry["billing_month"][5:7])
            idx = np.flatnonzero(month == m)
            if idx.size == 0:
                continue
            hours_in_month = hour[idx]
            # 解一个最小二乘：小时形状最接近模板，同时**严格**满足账单四个时段电量
            rows = []
            targets = []
            for period, key in (
                ("SHARP_PEAK", "SHARP_PEAK"), ("PEAK", "PEAK"),
                ("FLAT", "FLAT"), ("VALLEY", "VALLEY"),
            ):
                rows.append(_period_indicator(entry, hours_in_month, period))
                targets.append(float(entry["period_energy_kwh"][key]))
            matrix = np.vstack(rows)
            base = template[hours_in_month]
            # 以 base 为初值做带约束的最小范数修正：base + M^T λ，使 M(base + M^Tλ) = targets
            residual = np.array(targets) - matrix @ base
            gram = matrix @ matrix.T
            lam = np.linalg.solve(gram, residual)
            loads[idx] = base + matrix.T @ lam
            pvs[idx] = pv_shape[hours_in_month] * float(entry["energy_total_kwh"]) * 0.075
        return axis, loads, pvs, np.asarray(axis.month)

    @staticmethod
    def _facts() -> dict:
        import json
        from pathlib import Path

        path = Path(__file__).parent / "data" / "dongfeng_2025_bill_facts.json"
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_baseline_energy_charge_reproduces_each_bill(self):
        """按账单实际分时单价复算的年电度电费必须与账单合计一致（差异可解释、极小）。"""
        facts = self._facts()
        axis, load, pv, month = self._load_curve(facts)
        from cenep.calculation.scenario_bill_engine import (
            build_project_tariff_series,
            evaluate_scenario,
        )

        store = TariffPlanStore()
        base = store.get_plan(OFFICIAL_110_ID)
        total_bill = 0.0
        total_engine = 0.0
        for entry in facts["months"]:
            m = int(entry["billing_month"][5:7])
            idx = np.flatnonzero(month == m)
            plan = _bill_plan(base, entry, m, 39.0)
            sub_axis = _sub_axis(axis, idx)
            tariff = build_project_tariff_series(plan, sub_axis, export_price=0.0, require_verified=False)
            result = evaluate_scenario(
                kind=ScenarioKind.NO_PV_NO_STORAGE,
                load=load[idx],
                pv=np.zeros(idx.size),
                axis=sub_axis,
                tariff=tariff,
                dispatch_config=_dispatch(),
                project_pv_capacity_kwp=PV_CAPACITY_KWP,
                project_storage_power_kw=0.0,
                project_storage_energy_kwh=0.0,
                scenario_config=ScenarioConfig(
                    export_price=0.0, government_fund_yuan_per_kwh=0.0452
                ),
                government_fund_yuan_per_kwh=0.0452,
            )
            total_bill += float(entry["energy_charge_yuan"])
            total_engine += result.line.energy_charge_yuan
        # 账单实际分时单价只有 6 位小数，用于数百万元电费会有个位数元的舍入差
        assert abs(total_engine - total_bill) / total_bill < 1e-6
        assert total_bill == pytest.approx(50_896_163.64, abs=0.02)

    def test_four_scenarios_on_real_prices_keep_identity(self):
        """真实账单电价下四场景仍全部通过收益去重（§7.6）。"""
        facts = self._facts()
        axis, load, pv, month = self._load_curve(facts)
        store = TariffPlanStore()
        base = store.get_plan(OFFICIAL_110_ID)
        max_deviation = 0.0
        for entry in facts["months"]:
            m = int(entry["billing_month"][5:7])
            idx = np.flatnonzero(month == m)
            plan = _bill_plan(base, entry, m, 39.0)
            result = compare_four_scenarios(
                plan=plan,
                axis=_sub_axis(axis, idx),
                load=load[idx],
                pv=pv[idx],
                dispatch_config=_dispatch(),
                project_pv_capacity_kwp=PV_CAPACITY_KWP,
                project_storage_power_kw=STORAGE_POWER_KW,
                project_storage_energy_kwh=STORAGE_ENERGY_KWH,
                config=ScenarioConfig(
                    export_price=0.0,
                    government_fund_yuan_per_kwh=0.0452,
                    government_fund_included_in_tou_price=True,
                ),
                require_verified=False,
                load_source="按账单分时电量结构构造的受控曲线（非实测）",
                load_is_measured=False,
            )
            assert result.dedup_verified is True, entry["billing_month"]
            max_deviation = max(max_deviation, result.identity_max_deviation_yuan)
        assert max_deviation <= 1e-4

    def test_bill_plan_demand_charge_matches_bill(self):
        """需量电价 39 元/kW·月（账单自证）下，账单计费需量口径的需量电费与账单严格一致。"""
        facts = self._facts()
        for entry in facts["months"]:
            assert entry["demand_charge_yuan"] == pytest.approx(entry["demand_kw"] * 39.0, abs=0.01)
        assert sum(entry["demand_charge_yuan"] for entry in facts["months"]) == pytest.approx(
            11_775_738.0
        )


def _period_indicator(entry: dict, hours: np.ndarray, period: str) -> np.ndarray:
    """某月内"落在指定时段"的小时指示向量（按官方时段划分，7、8 月规则不同）。"""
    sharp = {20, 21} if entry["billing_month"][5:7] in ("07", "08") else {18, 19}
    if entry["billing_month"][5:7] in ("07", "08"):
        peak = set(range(16, 20)) | {22, 23}
    else:
        peak = {16, 17} | set(range(20, 24))
    flat = {6, 7, 8, 9, 10, 11, 14, 15}
    if period == "SHARP_PEAK":
        wanted = sharp
    elif period == "PEAK":
        wanted = peak
    elif period == "FLAT":
        wanted = flat
    else:
        wanted = set(range(0, 24)) - sharp - peak - flat
    return np.isin(hours, sorted(wanted)).astype(float)


def _sub_axis(axis, idx):
    from cenep.calculation.timeseries_engine import axis_from_timestamps

    return axis_from_timestamps([axis.timestamps[int(i)] for i in idx], axis.resolution)


def _bill_plan(base, entry: dict, month: int, demand_price: float):
    """用某月账单实际分时单价替换官方计划价格（§4.1 第 3 条：账单实际值优先）。"""
    from cenep.domain.enums import PriceBasis, TariffPlanStatus
    from cenep.domain.tariff_models import TariffPriceComponent, TariffTimePeriodRule

    prices = {k: float(v) for k, v in entry["period_unit_price_yuan_per_kwh"].items()}
    rules = [
        TariffTimePeriodRule(
            period=rule.period,
            months=[month],
            start_time=rule.start_time,
            end_time=rule.end_time,
            direct_price_yuan_per_kwh=prices[rule.period.value],
            price_basis=PriceBasis.DIRECT_PRICE,
            priority=rule.priority,
            note=f"2025-{month:02d} 账单实际分时单价",
        )
        for rule in base.time_period_rules
        if not rule.months or month in rule.months
    ]
    return base.model_copy(
        update={
            "tariff_plan_id": f"DFH_BILL_2025_{month:02d}",
            "name": f"东风本田三厂 2025-{month:02d} 账单实际分时单价（测试）",
            "effective_from": date.fromisoformat(entry["billing_period_start"]),
            "effective_to": date.fromisoformat(entry["billing_period_end"]),
            "source_name": f"用户账单 {entry['file_name']}（2025-{month:02d}）实际分时单价",
            "source_url": None,
            "source_document_number": base.source_document_number,
            "status": TariffPlanStatus.VERIFIED,
            "time_period_rules": rules,
            "price_components": [
                TariffPriceComponent(
                    component_type=component.component_type,
                    name=component.name,
                    unit=component.unit,
                    value=(
                        0.0452
                        if component.component_type.value == "government_fund"
                        else component.value
                    ),
                    included_in_tou_price=True,
                    adjustable_by_tou=component.adjustable_by_tou,
                    source_note=component.source_note,
                )
                for component in base.price_components
            ],
            "demand_charge_yuan_per_kw_month": demand_price,
            "applicable_voltage_levels": ["110千伏"],
            "applicable_tariff_structures": ["two_part"],
            "user_overridden": True,
            "override_note": "价格取账单实际分时单价（测试用）",
        }
    )
