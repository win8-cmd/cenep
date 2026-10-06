"""线性规划优化器（V2 §45、§47；OPTIMIZATION.md §6）。

**必须先做线性化建模**（本模块的核心）
------------------------------------
把容量作为**连续决策变量** ``x = [x_pv, x_e]``（kWp、kWh），
用「**单位容量的年度收益贡献**」做线性近似系数：

.. code-block:: text

    年化净收益(x) = Σ_i [ c_i − CRF × u_i ] × x_i

    c_i  = 单位容量的年度收益贡献（元/单位·年）    ← 由排名阶段的时序仿真差分求得
    u_i  = 单位容量的投资（元/单位）               ← project.investment 的单价
    CRF  = r(1+r)^n / ((1+r)^n − 1)               ← 资本回收系数（年金化）
    x_p  = x_e ÷ 目标时长                          ← 储能功率无独立单价，
                                                     由 R6（默认 2 小时）推导

**约束**（线性）：

.. code-block:: text

    L6  容量上下限： 0 ≤ x_pv ≤ PV_max， 0 ≤ x_e ≤ min(E_max, 2 h × P_max)
    L7  投资预算：   u_pv × x_pv + u_e × x_e ≤ Budget（提供预算时生效）
    L3  功率联动：   x_p = x_e ÷ 2 h（信息项，不作独立决策变量）

求解用 ``scipy.optimize.linprog``（``method="highs"``）。

优雅降级（原则 O4，**禁止静默**）
--------------------------------
``scipy`` 不可用、求解失败或无可行解时，**不抛异常**：
回退到贪心经济优化，并在 ``explanation`` 开头写明降级原因、
追加一条 ``feasible=False`` 的候选记录该降级（§9.1.1 降级信息的承载）。

**线性近似的结果必须复核**：返回结果里显式说明"这是线性近似，
结果需用规则型/贪心复核"，并通过 ``best_scenario``（精确复核）给出可用结论。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..calculation import scenario_engine as se
from ..calculation.timeseries_engine import TimeAxis
from ..domain.enums import OptimizationObjective
from ..domain.models import Project
from ..domain.timeseries_results import (
    OptimizationCandidate,
    OptimizationResult,
    ScenarioResult,
)
from ..infrastructure.logging_setup import get_logger
from . import greedy_optimizer, rule_based

logger = get_logger()

#: 支持的求解器名称 → ``scipy.optimize.linprog`` 的 ``method``
SUPPORTED_SOLVERS: dict[str, str] = {
    "scipy-highs": "highs",
    "highs": "highs",
    "highs-ds": "highs-ds",
    "highs-ipm": "highs-ipm",
}

#: 线性近似的免责说明（规范 §47 要求必须写进结果）
LINEAR_APPROX_NOTE = (
    "线性近似说明：本优化器把容量当作**连续决策变量**，用「单位容量的年度收益贡献」"
    "做线性近似系数（收益按首年仿真差分、投资按年金化 CRF 折算），"
    "因此得到的是**线性近似解**，不是时序仿真的精确最优；"
    "该结果需用规则型（rule_based）或贪心经济（greedy_optimizer）复核，"
    "本结果中的 best_scenario 已对最优候选做过精确复核。"
)


def _load_scipy() -> Any | None:
    """惰性导入 scipy（可选依赖）；不可用时返回 ``None``（不抛异常）。"""
    try:  # pragma: no cover - 环境相关
        from scipy.optimize import linprog  # noqa: PLC0415
    except Exception:  # pragma: no cover - 缺依赖/装坏
        logger.warning("线性规划不可用：未安装 scipy，将降级到贪心经济优化")
        return None
    return linprog


def _capital_recovery_factor(rate: float, years: int) -> float:
    """资本回收系数 ``CRF = r(1+r)^n / ((1+r)^n − 1)``（把投资折算为年等额）。"""
    r = float(rate)
    n = max(int(years), 1)
    if r <= 0.0:
        return 1.0 / n
    growth = (1.0 + r) ** n
    return r * growth / (growth - 1.0)


def _snap(value: float, candidates: Sequence[float]) -> float:
    """就近取整：把连续解映射回规范给定的候选网格（OPTIMIZATION.md §6.4）。"""
    return float(min(candidates, key=lambda c: abs(float(c) - float(value))))


def _anchor_benefit(
    project: Project,
    axis: TimeAxis,
    pv: float,
    energy: float,
    power: float,
) -> tuple[float, float]:
    """排名阶段评估一个锚点，返回 ``(年收益贡献, 总投资)``。"""
    changes: dict[str, Any] = {
        "pv.pv_capacity_kwp": pv if pv > 0 else None,
        "storage.storage_energy_kwh": float(energy),
        "storage.storage_power_kw": float(power) if energy > 0 else 0.0,
    }
    variant = se.apply_variant(project, changes)
    scenario = se.evaluate_scenario(variant, axis, label="LP 锚点", full_period=False)
    return float(scenario.metrics.total_benefit), float(scenario.total_capex)


def build_linear_model(
    project: Project,
    axis: TimeAxis,
    *,
    pv_reference: float,
    energy_reference: float,
    power_reference: float,
) -> dict[str, Any]:
    """构造线性化模型系数（单位容量的年度收益贡献与投资、CRF）。

    锚点（均为**首年 + 线性外推**的排名阶段评估，约 0.1 秒/个）：

    * ``A``：仅光伏 ``(pv_ref, 0, 0)``
    * ``C``：光储 ``(pv_ref, E_ref, P_ref)``

    于是 ``c_pv = 收益(A) ÷ pv_ref``、``c_es = (收益(C) − 收益(A)) ÷ E_ref``。
    """
    benefit_a, _ = _anchor_benefit(project, axis, pv_reference, 0.0, 0.0)
    benefit_c, _ = _anchor_benefit(project, axis, pv_reference, energy_reference, power_reference)

    unit_pv = float(project.investment.pv_capex_per_kw)
    unit_es = float(project.investment.storage_capex_per_kwh)
    crf = _capital_recovery_factor(float(project.discount_rate), int(project.analysis_period))

    c_pv = benefit_a / pv_reference if pv_reference > 0 else 0.0
    c_es = (benefit_c - benefit_a) / energy_reference if energy_reference > 0 else 0.0
    return {
        "c_pv": c_pv,
        "c_es": c_es,
        "unit_pv": unit_pv,
        "unit_es": unit_es,
        "crf": crf,
        "annual_net_pv": c_pv - crf * unit_pv,
        "annual_net_es": c_es - crf * unit_es,
        "benefit_pv_only": benefit_a,
        "benefit_pv_storage": benefit_c,
    }


def optimize(
    project: Project,
    *,
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
    solver: str = "scipy-highs",
    pv_max: float | None = None,
    energy_max: float | None = None,
    power_max: float | None = None,
    budget: float | None = None,
    axis: TimeAxis | None = None,
    verify_top_n: int = 1,
    fallback_to_greedy: bool = True,
    max_evaluations: int = greedy_optimizer.DEFAULT_MAX_EVALUATIONS,
) -> OptimizationResult:
    """线性规划寻优（V2 §47）：连续容量解 + 就近取整映射回候选网格。

    :param solver: 求解器名称，见 :data:`SUPPORTED_SOLVERS`；默认 ``"scipy-highs"``。
    :param pv_max: 光伏容量上限（kWp）；``None`` 时取屋顶可装上限（无屋顶信息则取参考容量 ×2）。
    :param energy_max: 储能容量上限（kWh）；``None`` 时取参考容量 ×2（至少 2000 kWh）。
    :param power_max: 储能功率上限（kW）；``None`` 时取参考功率 ×2（至少 1000 kW）。
    :param budget: 投资预算上限（元）；提供时作为线性约束 ``L7``。
    :param fallback_to_greedy: 求解不可用时是否回退到贪心经济优化（默认 ``True``，
        满足"优雅降级而不是抛异常"的要求）。
    """
    method = SUPPORTED_SOLVERS.get(str(solver))
    if method is None:
        raise se.ScenarioError(
            f"不支持的求解器 {solver!r}；可选：{'、'.join(sorted(SUPPORTED_SOLVERS))}"
        )
    resolved_axis = axis if axis is not None else se.build_axis(project)
    linprog = _load_scipy()
    if linprog is None:
        return _degrade(
            project,
            objective=objective,
            axis=resolved_axis,
            reason="线性规划不可用：未安装 scipy，已降级到贪心经济优化",
            fallback_to_greedy=fallback_to_greedy,
            verify_top_n=verify_top_n,
            budget=budget,
            max_evaluations=max_evaluations,
        )

    recommendation = rule_based.recommend(project, axis=resolved_axis)
    pv_reference = max(recommendation.pv_capacity_kwp, 1.0)
    energy_reference = max(recommendation.storage_energy_kwh, 0.0)
    power_reference = max(recommendation.storage_power_kw, 0.0)
    if energy_reference <= 0.0 or power_reference <= 0.0:
        energy_reference = max(float(project.storage.storage_energy_kwh), 1.0)
        power_reference = max(float(project.storage.storage_power_kw), 1.0)

    roof_limit = se.roof_limit_kwp(project)
    pv_upper = float(pv_max) if pv_max is not None else (
        roof_limit if roof_limit is not None else pv_reference * 2.0
    )
    energy_upper = float(energy_max) if energy_max is not None else max(
        energy_reference * 2.0, 2000.0
    )
    power_upper = float(power_max) if power_max is not None else max(
        power_reference * 2.0, 1000.0
    )
    # 功率联动约束（L3）：x_p = x_e ÷ 2h ≤ power_upper
    energy_upper = min(energy_upper, max(power_upper * 2.0, 0.0))

    model = build_linear_model(
        project,
        resolved_axis,
        pv_reference=pv_reference,
        energy_reference=energy_reference,
        power_reference=power_reference,
    )
    constraints = list(se.default_constraints(project, budget))
    constraints.extend(
        [
            f"LP L6 容量上下限：0 ≤ x_pv ≤ {pv_upper:,.2f} kWp，"
            f"0 ≤ x_e ≤ {energy_upper:,.2f} kWh（并受功率联动 x_p = x_e ÷ 2 h 限制）",
            f"LP L3 功率联动：x_p = x_e ÷ 2 小时（储能功率无独立单价，按 R6 目标时长推导）",
        ]
    )
    if budget is not None:
        constraints.append(
            f"LP L7 投资预算：{model['unit_pv']:,.2f} 元/kWp × x_pv + "
            f"{model['unit_es']:,.2f} 元/kWh × x_e ≤ {float(budget):,.2f} 元"
        )

    c = [-float(model["annual_net_pv"]), -float(model["annual_net_es"])]
    a_ub = None
    b_ub = None
    if budget is not None:
        a_ub = [[float(model["unit_pv"]), float(model["unit_es"])]]
        b_ub = [float(budget)]
    try:
        solution = linprog(
            c,
            A_ub=a_ub,
            b_ub=b_ub,
            bounds=[(0.0, pv_upper), (0.0, energy_upper)],
            method=method,
        )
    except Exception as exc:  # pragma: no cover - scipy 内部异常
        return _degrade(
            project,
            objective=objective,
            axis=resolved_axis,
            reason=f"线性规划求解异常（{exc}），已降级到贪心经济优化",
            fallback_to_greedy=fallback_to_greedy,
            verify_top_n=verify_top_n,
            budget=budget,
            max_evaluations=max_evaluations,
        )

    if not getattr(solution, "success", False) or solution.x is None:
        return _degrade(
            project,
            objective=objective,
            axis=resolved_axis,
            reason=(
                f"线性规划未求解成功（status={getattr(solution, 'status', '未知')}："
                f"{getattr(solution, 'message', '')}），已降级到贪心经济优化"
            ),
            fallback_to_greedy=fallback_to_greedy,
            verify_top_n=verify_top_n,
            budget=budget,
            max_evaluations=max_evaluations,
        )

    x_pv = float(solution.x[0])
    x_energy = float(solution.x[1])
    x_power = min(x_energy / 2.0, power_upper)

    snapped_pv = _snap(x_pv, se.PV_CAPACITY_VALUES) if x_pv > 0 else 0.0
    snapped_energy = _snap(x_energy, se.STORAGE_ENERGY_VALUES)
    snapped_power = _snap(x_power, se.STORAGE_POWER_VALUES) if x_energy > 0 else 0.0

    specs = [
        se.CandidateSpec(
            run_id="LP-连续解",
            variables={
                "pv.pv_capacity_kwp": x_pv,
                "storage.storage_energy_kwh": x_energy,
                "storage.storage_power_kw": x_power,
            },
            changes={
                "pv.pv_capacity_kwp": x_pv if x_pv > 0 else None,
                "storage.storage_energy_kwh": x_energy,
                "storage.storage_power_kw": x_power,
            },
            label="线性规划连续解（线性近似）",
            note="线性规划连续解",
        ),
        se.CandidateSpec(
            run_id="LP-取整解",
            variables={
                "pv.pv_capacity_kwp": snapped_pv,
                "storage.storage_energy_kwh": snapped_energy,
                "storage.storage_power_kw": snapped_power,
            },
            changes={
                "pv.pv_capacity_kwp": snapped_pv if snapped_pv > 0 else None,
                "storage.storage_energy_kwh": snapped_energy,
                "storage.storage_power_kw": snapped_power,
            },
            label="线性规划就近取整解（映射回 §45 候选网格）",
            note="线性规划取整解",
        ),
    ]

    explanation = [
        LINEAR_APPROX_NOTE,
        "线性规划模型（OPTIMIZATION.md §6）：决策变量为连续容量 x = [x_pv, x_e]；"
        f"单位容量年度收益贡献 c_pv = {model['c_pv']:,.2f} 元/kWp·年、"
        f"c_es = {model['c_es']:,.2f} 元/kWh·年（由首年仿真差分求得）；"
        f"资本回收系数 CRF = {model['crf']:.6f}"
        f"（折现率 {float(project.discount_rate):.4f}、计算期 {int(project.analysis_period)} 年）；"
        f"年化净收益系数：光伏 {model['annual_net_pv']:,.2f} 元/kWp·年、"
        f"储能 {model['annual_net_es']:,.2f} 元/kWh·年。",
        f"求解器 {solver}（scipy method={method}）返回 status="
        f"{getattr(solution, 'status', '未知')}：连续最优解为 x_pv = {x_pv:,.2f} kWp、"
        f"x_e = {x_energy:,.2f} kWh、推导功率 x_p = {x_power:,.2f} kW；"
        f"就近取整映射回 §45 候选网格为 {se.scenario_code(snapped_pv, snapped_energy, snapped_power)}。",
        "复核要求：线性近似解必须用规则型（rule_based）或贪心经济（greedy_optimizer）"
        "在同口径时序仿真下复核后再使用；本次已对最优候选执行**精确复核**"
        "（完整运营期时序仿真），best_scenario 即精确结果。",
        "本优化器只输出指标与最优方案，不输出「项目可行/不可行」的结论"
        "（V1 §106、OPTIMIZATION.md 原则 O6）。",
    ]
    return se.evaluate_candidates(
        project,
        specs,
        objective=objective,
        axis=resolved_axis,
        budget=budget,
        verify_top_n=verify_top_n,
        constraints=constraints,
        scan_variables=[
            "pv.pv_capacity_kwp（光伏容量，连续变量）",
            "storage.storage_energy_kwh（储能容量，连续变量）",
        ],
        extra_explanation=explanation,
    )


def _degrade(
    project: Project,
    *,
    objective: OptimizationObjective,
    axis: TimeAxis,
    reason: str,
    fallback_to_greedy: bool,
    verify_top_n: int,
    budget: float | None,
    max_evaluations: int,
) -> OptimizationResult:
    """优雅降级：留痕 + 回退到贪心经济优化（**不抛异常**，原则 O4）。"""
    worst = float("-inf") if se.is_maximization(objective) else float("inf")
    if fallback_to_greedy:
        logger.warning("%s", reason)
        result = greedy_optimizer.optimize(
            project,
            objective=objective,
            max_evaluations=max_evaluations,
            axis=axis,
            budget=budget,
            verify_top_n=verify_top_n,
        )
        result.explanation.insert(0, LINEAR_APPROX_NOTE)
        result.explanation.insert(0, f"降级说明：{reason}")
        result.constraints = list(result.constraints) + [f"降级：{reason}"]
    else:
        result = OptimizationResult(
            objective=objective,
            scan_variables=["LP（线性规划容量寻优）"],
            constraints=list(se.default_constraints(project, budget)) + [f"降级：{reason}"],
            candidates=[],
            explanation=[f"降级说明：{reason}", LINEAR_APPROX_NOTE],
        )
    result.candidates.append(
        OptimizationCandidate(
            run_id="LP-降级",
            variables={},
            objective=objective,
            objective_value=worst,
            feasible=False,
            note=reason,
            scenario=ScenarioResult(name="LP-降级", label="线性规划降级记录"),
        )
    )
    result.scan_variables = ["LP（线性规划容量寻优）"] + list(result.scan_variables)
    if result.best_run_id == "" and result.candidates:
        result.best_run_id = "LP-降级"
        result.best_variables = {}
        result.best_scenario = result.candidates[-1].scenario
    return result


__all__ = [
    "LINEAR_APPROX_NOTE",
    "SUPPORTED_SOLVERS",
    "build_linear_model",
    "optimize",
]
