"""贪心经济优化器（V2 §45、§46；OPTIMIZATION.md §5）。

**定位**：**逐维坐标下降**（coordinate descent）——先扫光伏容量取最优，
再在该容量下扫储能容量，再扫储能功率，必要时重复若干轮。
评估次数远小于全网格（``6×5×4 = 120`` → 约 ``6+5+4 = 15`` 次），
在给定候选值上仍是"逐点评估后取最优"，不引入近似最优性损失。

性能（V2 §86）
--------------
每个候选走 :class:`~cenep.calculation.scenario_engine.CandidateEvaluator` 的
**排名阶段**（首年仿真 + 线性外推，约 0.08 秒/候选），
最后对**最优候选**做一次**精确复核**（完整运营期仿真）。
``max_evaluations`` 为**硬上限**：达到上限后立即停止搜索，并把截断情况写入
``constraints`` 与 ``explanation``（OPTIMIZATION.md 原则 O4：降级必须留痕）。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..calculation import scenario_engine as se
from ..calculation.timeseries_engine import TimeAxis
from ..domain.enums import OptimizationObjective
from ..domain.models import Project
from ..domain.timeseries_results import OptimizationCandidate, OptimizationResult
from ..infrastructure.logging_setup import get_logger
from . import rule_based

logger = get_logger()

#: 默认评估次数上限（V2 §86：100 方案 < 30 秒）
DEFAULT_MAX_EVALUATIONS = 100


def _spec(pv: float, energy: float, power: float, stage: str, index: int) -> se.CandidateSpec:
    effective_power = power if energy > 0.0 else 0.0
    code = se.scenario_code(pv, energy, effective_power)
    return se.CandidateSpec(
        run_id=f"GREEDY-{stage}{index}-{code}",
        variables={
            "pv.pv_capacity_kwp": float(pv),
            "storage.storage_energy_kwh": float(energy),
            "storage.storage_power_kw": float(effective_power),
        },
        changes={
            "pv.pv_capacity_kwp": float(pv),
            "storage.storage_energy_kwh": float(energy),
            "storage.storage_power_kw": float(effective_power),
        },
        label=f"贪心候选（{stage} 阶段）{code}",
    )


def _is_better(
    objective: OptimizationObjective,
    candidate: OptimizationCandidate,
    incumbent: OptimizationCandidate | None,
) -> bool:
    """在**可行**候选之间比较优劣（含并列打破规则；OPTIMIZATION.md §3.3）。"""
    if not candidate.feasible:
        return False
    if incumbent is None or not incumbent.feasible:
        return True
    return se.rank_key(objective, 0, candidate.scenario) < se.rank_key(
        objective, 0, incumbent.scenario
    )


def optimize(
    project: Project,
    *,
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
    max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
    pv_values: Sequence[float] | None = None,
    storage_energy_values: Sequence[float] | None = None,
    storage_power_values: Sequence[float] | None = None,
    rounds: int = 1,
    axis: TimeAxis | None = None,
    budget: float | None = None,
    verify_top_n: int = 1,
    seed: tuple[float, float, float] | None = None,
) -> OptimizationResult:
    """逐维坐标下降的贪心寻优（V2 §45、§46）。

    :param max_evaluations: 实际评估次数的**硬上限**（默认 100）。
    :param rounds: 坐标下降轮数（默认 1 轮：光伏 → 储能容量 → 储能功率）。
    :param seed: 起始容量 ``(光伏 kWp, 储能 kWh, 储能功率 kW)``；
        ``None`` 时取规则型建议（:func:`~cenep.optimization.rule_based.recommend`）。
    :param verify_top_n: 复核阶段跑精确全周期仿真的候选个数（默认仅最优 1 个）。
    """
    if int(max_evaluations) <= 0:
        raise se.ScenarioError(f"max_evaluations 必须为正整数，收到 {max_evaluations!r}")
    resolved_axis = axis if axis is not None else se.build_axis(project)

    recommendation = rule_based.recommend(project, axis=resolved_axis)
    default_pv, default_energy, default_power = rule_based.default_scan_values(
        project, recommendation
    )
    pv_grid = [
        float(v) for v in (pv_values if pv_values is not None else default_pv)
    ]
    energy_grid = [
        float(v) for v in (storage_energy_values if storage_energy_values is not None else default_energy)
    ]
    power_grid = [
        float(v) for v in (storage_power_values if storage_power_values is not None else default_power)
    ]
    for name, grid in (
        ("pv_values", pv_grid),
        ("storage_energy_values", energy_grid),
        ("storage_power_values", power_grid),
    ):
        if not grid:
            raise se.ScenarioError(f"贪心寻优的候选值 {name} 不能为空（V2 §45）")

    if seed is not None:
        current = (float(seed[0]), float(seed[1]), float(seed[2]))
    else:
        current = (
            recommendation.pv_capacity_kwp,
            recommendation.storage_energy_kwh,
            recommendation.storage_power_kw,
        )

    evaluator = se.CandidateEvaluator(
        project, objective=objective, axis=resolved_axis, budget=budget
    )
    truncated = False
    evaluated_stages: list[str] = []
    best_overall: OptimizationCandidate | None = None

    stage_plan = (
        ("PV", 0),
        ("E", 1),
        ("P", 2),
    )
    for round_index in range(max(int(rounds), 1)):
        for stage_name, position in stage_plan:
            grid = (pv_grid, energy_grid, power_grid)[position]
            stage_best: OptimizationCandidate | None = None
            for index, value in enumerate(grid):
                if evaluator.evaluations >= int(max_evaluations):
                    truncated = True
                    break
                trial = list(current)
                trial[position] = float(value)
                candidate = evaluator.evaluate(
                    _spec(trial[0], trial[1], trial[2], f"R{round_index + 1}{stage_name}", index)
                )
                if _is_better(objective, candidate, stage_best):
                    stage_best = candidate
                if _is_better(objective, candidate, best_overall):
                    best_overall = candidate
            if truncated:
                break
            if stage_best is not None and stage_best.feasible:
                current = (
                    float(stage_best.variables.get("pv.pv_capacity_kwp", current[0])),
                    float(stage_best.variables.get("storage.storage_energy_kwh", current[1])),
                    float(stage_best.variables.get("storage.storage_power_kw", current[2])),
                )
            evaluated_stages.append(
                f"第 {round_index + 1} 轮 · {stage_name} 阶段最优 "
                f"{stage_best.run_id if stage_best else '（无可行候选）'}"
            )
        if truncated:
            break

    constraints = list(evaluator.constraints)
    if truncated:
        constraints.append(
            f"评估次数硬上限 max_evaluations = {int(max_evaluations)} 已触发，"
            "剩余候选未评估（OPTIMIZATION.md 原则 O4：降级已留痕）"
        )
    explanation = [
        "贪心经济优化（OPTIMIZATION.md §5）：逐维坐标下降 —— 先扫光伏容量取最优，"
        "再在该容量下扫储能容量，最后扫储能功率"
        + (f"，共 {int(rounds)} 轮" if int(rounds) > 1 else "")
        + f"；实际评估 {evaluator.evaluations} 次（硬上限 {int(max_evaluations)} 次），"
        f"远小于全网格评估次数。",
        "起始容量（种子）："
        f"光伏 {current[0]:,.2f} kWp、储能 {current[1]:,.2f} kWh / {current[2]:,.2f} kW"
        + ("（来自规则型建议）" if seed is None else "（调用方指定）")
        + "。",
        "坐标下降轨迹：" + "；".join(evaluated_stages) + "。",
    ]
    if truncated:
        explanation.append(
            f"注意：本次搜索在评估 {int(max_evaluations)} 次后**提前停止**"
            "（max_evaluations 硬上限），因此不保证网格内严格最优；"
            "如需完整搜索请提高 max_evaluations。"
        )

    return _assemble(evaluator, constraints, explanation, verify_top_n)


def _assemble(
    evaluator: se.CandidateEvaluator,
    constraints: Sequence[str],
    explanation: Sequence[str],
    verify_top_n: int,
) -> OptimizationResult:
    """生成最终结果（复核最优 + §48 五段式输出 + 贪心说明）。"""
    result = evaluator.finalize(
        scan_variables=[
            "pv.pv_capacity_kwp（光伏容量）",
            "storage.storage_energy_kwh（储能容量）",
            "storage.storage_power_kw（储能功率）",
        ],
        verify_top_n=verify_top_n,
        extra_explanation=list(explanation),
    )
    result.constraints = list(constraints)
    return result


__all__ = ["DEFAULT_MAX_EVALUATIONS", "optimize"]
