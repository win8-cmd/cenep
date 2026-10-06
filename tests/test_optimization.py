"""三个优化器测试（V2 §45–§48、§86；OPTIMIZATION.md §2、§4、§5、§6）。

覆盖：

* 规则型：R1–R7 规则轨迹完整、小规模网格扫描可运行、解释含规则内容；
* 贪心：逐维坐标下降可运行、评估次数远小于全网格、``max_evaluations`` 是**硬上限**；
* 线性规划：``scipy`` 可用时给出结果且解释含「线性近似」，
  ``scipy`` 不可用时**优雅降级**（不抛异常、留痕、回退到贪心）；
* 六种目标各自可用，``MIN_*`` 与 ``MAX_*`` 排序方向相反；
* §48：``explanation`` 非空且含中文，``candidates`` 条数与评估次数一致。
"""

from __future__ import annotations

import pytest

from cenep import optimization as opt
from cenep.calculation import scenario_engine as se
from cenep.domain.enums import OptimizationObjective
from cenep.domain.models import Project
from cenep.optimization import greedy_optimizer, lp_optimizer, rule_based
from cenep.optimization.rule_based import RuleRecommendation

from test_scenario_engine import make_project  # noqa: E402  （pytest 会把 tests/ 加入 sys.path）


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


@pytest.fixture
def opt_project(golden_pv_storage) -> Project:
    """光储项目 + 有效时序配置（10 年，控制测试时长）。"""
    return make_project(golden_pv_storage, period=10)


# --------------------------------------------------------------------------- #
# 规则型优化器（OPTIMIZATION.md §4）
# --------------------------------------------------------------------------- #
class TestRuleBasedOptimizer:
    def test_recommendation_covers_rules_r1_to_r7(self, opt_project):
        """规则轨迹必须覆盖 R1–R7，并给出输入、公式与结果（V2 §48）。"""
        recommendation = rule_based.recommend(opt_project)
        assert isinstance(recommendation, RuleRecommendation)
        rules = {trace.rule for trace in recommendation.rule_trace}
        assert rules == {"R1", "R2", "R3", "R4", "R5", "R6", "R7"}
        assert recommendation.pv_capacity_kwp > 0.0
        assert recommendation.storage_energy_kwh > 0.0
        assert recommendation.storage_power_kw > 0.0
        assert recommendation.binding_rule in {"R1", "R2", "R3"}
        for trace in recommendation.rule_trace:
            assert _has_chinese(trace.formula)

    def test_recommendation_respects_roof_limit(self, opt_project):
        """R1：建议光伏容量不得超过屋顶可装上限。"""
        recommendation = rule_based.recommend(opt_project)
        limit = se.roof_limit_kwp(opt_project)
        assert limit is not None
        assert recommendation.pv_capacity_kwp <= limit + 1e-9

    def test_optimize_runs_small_grid(self, opt_project):
        """规则型：小规模网格逐候选评估，结果满足 §48 五段式。"""
        result = rule_based.optimize(opt_project)
        assert len(result.candidates) >= 2
        assert result.best_run_id
        assert result.best_scenario is not None
        assert result.scan_variables
        text = "\n".join(result.explanation)
        assert _has_chinese(text)
        assert "规则型" in text
        assert any("R2" in line for line in result.explanation)

    def test_explicit_values_override_recommendation(self, opt_project):
        """显式候选值优先于规则建议值。"""
        result = rule_based.optimize(
            opt_project,
            pv_values=[600.0, 700.0],
            storage_energy_values=[0.0],
            storage_power_values=[0.0],
        )
        assert len(result.candidates) == 2
        assert {c.variables["pv.pv_capacity_kwp"] for c in result.candidates} == {600.0, 700.0}


# --------------------------------------------------------------------------- #
# 贪心经济优化器（OPTIMIZATION.md §5）
# --------------------------------------------------------------------------- #
class TestGreedyOptimizer:
    def test_coordinate_descent_runs(self, opt_project):
        """逐维坐标下降可运行，且最优方案有精确复核结果。"""
        result = greedy_optimizer.optimize(opt_project)
        assert result.candidates
        assert result.best_scenario is not None
        assert result.best_variables
        assert se.VERIFY_NOTE in result.candidates[0].note

    def test_evaluations_far_below_full_grid(self, opt_project):
        """评估次数必须远小于全网格（6×5×4 = 120）。"""
        result = greedy_optimizer.optimize(
            opt_project,
            pv_values=[500.0, 750.0, 1000.0],
            storage_energy_values=[0.0, 500.0, 1000.0],
            storage_power_values=[250.0, 500.0, 750.0],
        )
        assert len(result.candidates) < 3 * 3 * 3
        text = "\n".join(result.explanation)
        assert "评估" in text

    def test_max_evaluations_is_hard_cap(self, opt_project):
        """``max_evaluations`` 为硬上限：达到即停止，并留下降级痕迹（原则 O4）。"""
        result = greedy_optimizer.optimize(
            opt_project,
            max_evaluations=2,
            pv_values=[500.0, 750.0, 1000.0, 1250.0],
            storage_energy_values=[0.0, 500.0, 1000.0],
            storage_power_values=[250.0, 500.0],
        )
        assert len(result.candidates) <= 2
        text = "\n".join(result.explanation) + "\n" + "\n".join(result.constraints)
        assert "max_evaluations" in text
        assert "提前停止" in text

    def test_invalid_max_evaluations_raises(self, opt_project):
        with pytest.raises(se.ScenarioError):
            greedy_optimizer.optimize(opt_project, max_evaluations=0)


# --------------------------------------------------------------------------- #
# 线性规划优化器（OPTIMIZATION.md §6）
# --------------------------------------------------------------------------- #
class TestLinearProgrammingOptimizer:
    def test_linear_model_coefficients(self, opt_project):
        """线性化建模：单位容量的年度收益贡献与资本回收系数必须可复算。"""
        axis = se.build_axis(opt_project)
        model = lp_optimizer.build_linear_model(
            opt_project,
            axis,
            pv_reference=800.0,
            energy_reference=500.0,
            power_reference=250.0,
        )
        assert model["c_pv"] > 0.0
        assert model["crf"] > 0.0
        assert model["annual_net_pv"] == pytest.approx(
            model["c_pv"] - model["crf"] * model["unit_pv"]
        )
        assert model["annual_net_es"] == pytest.approx(
            model["c_es"] - model["crf"] * model["unit_es"]
        )

    def test_lp_runs_when_scipy_available(self, opt_project):
        """``scipy`` 可用时必须给出结果，且解释含「线性近似」。"""
        assert lp_optimizer._load_scipy() is not None, "测试环境应已安装 scipy"
        result = lp_optimizer.optimize(opt_project)
        assert result.candidates
        assert result.best_scenario is not None
        text = "\n".join(result.explanation)
        assert "线性近似" in text
        assert "复核" in text

    def test_lp_continuous_and_snapped_candidates(self, opt_project):
        """连续解与就近取整解都要出现在候选清单中。"""
        result = lp_optimizer.optimize(opt_project)
        run_ids = {c.run_id for c in result.candidates}
        assert "LP-连续解" in run_ids
        assert "LP-取整解" in run_ids

    def test_lp_unknown_solver_raises_chinese_error(self, opt_project):
        with pytest.raises(se.ScenarioError) as exc:
            lp_optimizer.optimize(opt_project, solver="not-a-solver")
        assert _has_chinese(str(exc.value))

    def test_lp_degrades_gracefully_without_scipy(self, opt_project, monkeypatch):
        """``scipy`` 不可用时必须优雅降级（不抛异常），且降级留痕（原则 O4）。"""
        monkeypatch.setattr(lp_optimizer, "_load_scipy", lambda: None)
        result = lp_optimizer.optimize(opt_project)
        text = "\n".join(result.explanation)
        assert "降级" in text
        assert "scipy" in text
        infeasible = [c for c in result.candidates if not c.feasible]
        assert infeasible, "降级必须留下一条 feasible=False 的记录"
        assert any("降级" in c.note for c in infeasible)
        assert result.best_scenario is not None
        assert "线性近似" in text

    def test_lp_degrades_without_fallback(self, opt_project, monkeypatch):
        """``fallback_to_greedy=False`` 时只返回降级说明，不抛异常。"""
        monkeypatch.setattr(lp_optimizer, "_load_scipy", lambda: None)
        result = lp_optimizer.optimize(opt_project, fallback_to_greedy=False)
        assert result.best_run_id == "LP-降级"
        assert any("降级" in line for line in result.explanation)

    def test_lp_budget_constraint_is_respected(self, opt_project):
        """提供投资预算时必须作为线性约束生效（L7）。"""
        result = lp_optimizer.optimize(opt_project, budget=2_500_000.0)
        text = "\n".join(result.constraints)
        assert "投资预算" in text
        assert all(
            (not c.feasible) or c.scenario.total_capex <= 2_500_000.0 + 1e-6
            for c in result.candidates
        )


# --------------------------------------------------------------------------- #
# 统一入口（OPTIMIZATION.md §2.2）
# --------------------------------------------------------------------------- #
class TestUnifiedEntry:
    def test_dispatch_by_name(self, opt_project):
        result = opt.optimize(opt_project, optimizer="rule_based")
        assert result.best_run_id
        assert opt.rule_based.optimize is opt.optimize_rule_based

    def test_unknown_optimizer_raises_chinese_error(self, opt_project):
        with pytest.raises(se.ScenarioError) as exc:
            opt.optimize(opt_project, optimizer="magic")
        assert _has_chinese(str(exc.value))
        assert "未知的优化器" in str(exc.value)

    def test_lp_is_never_called_implicitly(self, opt_project, monkeypatch):
        """§2.2：LP 永不被隐式调用。"""
        calls: list[str] = []

        def _forbidden(*args, **kwargs):  # pragma: no cover - 只用于断言未被调用
            calls.append("lp")
            raise AssertionError("LP 不应被隐式调用")

        monkeypatch.setattr(opt, "optimize_lp", _forbidden)
        opt.optimize(opt_project, optimizer="greedy", max_evaluations=3)
        assert calls == []


# --------------------------------------------------------------------------- #
# §47 六种目标 × 三个优化器
# --------------------------------------------------------------------------- #
class TestObjectivesAcrossOptimizers:
    @pytest.mark.parametrize("objective", list(OptimizationObjective))
    def test_rule_based_supports_every_objective(self, opt_project, objective):
        """六种目标各跑一次（规则型）。"""
        result = rule_based.optimize(opt_project, objective=objective)
        assert result.objective is objective
        assert result.best_scenario is not None
        feasible = [c for c in result.candidates if c.feasible]
        if se.is_maximization(objective):
            assert result.candidates[0].objective_value == max(
                c.objective_value for c in feasible
            )
        else:
            assert result.candidates[0].objective_value == min(
                c.objective_value for c in feasible
            )

    def test_greedy_supports_min_lcoe(self, opt_project):
        result = greedy_optimizer.optimize(
            opt_project,
            objective=OptimizationObjective.MIN_LCOE,
            pv_values=[700.0, 900.0],
            storage_energy_values=[0.0, 500.0],
            storage_power_values=[250.0, 500.0],
        )
        feasible = [c for c in result.candidates if c.feasible]
        lcoes = [c.scenario.lcoe for c in feasible if c.scenario.lcoe is not None]
        assert result.candidates[0].scenario.lcoe == pytest.approx(min(lcoes))

    def test_lp_supports_min_annual_cost(self, opt_project):
        result = lp_optimizer.optimize(
            opt_project, objective=OptimizationObjective.MIN_ANNUAL_COST
        )
        assert result.best_scenario is not None
        feasible = [c for c in result.candidates if c.feasible]
        assert result.candidates[0].objective_value == min(
            c.objective_value for c in feasible
        )


# --------------------------------------------------------------------------- #
# §48 可解释性（三个优化器共用同一套输出契约）
# --------------------------------------------------------------------------- #
class TestOptimizerExplainability:
    @pytest.mark.parametrize("name", list(opt.OPTIMIZER_NAMES))
    def test_explanation_and_candidates(self, opt_project, name):
        """§48：``explanation`` 非空且含中文，``candidates`` 与最优方案齐备。"""
        result = opt.optimize(opt_project, optimizer=name)
        assert result.explanation
        assert all(_has_chinese(line) for line in result.explanation)
        assert any(ch.isdigit() for ch in "\n".join(result.explanation))
        assert result.candidates
        assert result.best_run_id
        assert result.best_variables
        assert result.best_scenario is not None
        assert result.constraints
        assert result.scan_variables

    def test_notes_are_chinese(self, opt_project):
        result = opt.optimize(opt_project, optimizer="greedy", max_evaluations=6)
        for candidate in result.candidates:
            assert _has_chinese(candidate.note)
