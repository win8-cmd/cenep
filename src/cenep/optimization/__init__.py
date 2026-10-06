"""优化器集合（V2 §45–§47；OPTIMIZATION.md §2）。

三个优化器与优先级（OPTIMIZATION.md §2.2，方向为**执行顺序与稳定性**）：

.. code-block:: text

    规则型 rule_based  →  贪心经济 greedy_optimizer  →  线性规划 lp_optimizer

* :mod:`~cenep.optimization.rule_based`：按工程规则 R1–R7 给出容量建议，
  并在建议点附近做小规模网格扫描（**默认**，无依赖、秒级返回）；
* :mod:`~cenep.optimization.greedy_optimizer`：逐维坐标下降，网格内严格最优；
* :mod:`~cenep.optimization.lp_optimizer`：连续容量线性规划（**可选**依赖 ``scipy``），
  缺依赖或求解失败时**优雅降级**到贪心，绝不抛异常、绝不静默。

``optimize(...)`` 是按 §2.2 组织的统一入口；**LP 永不被隐式调用**——
只有显式指定 ``optimizer="lp"`` 时才会执行。
"""

from __future__ import annotations

from ..calculation import scenario_engine as se
from ..domain.enums import OptimizationObjective
from ..domain.models import Project
from ..domain.timeseries_results import OptimizationResult
from . import greedy_optimizer, lp_optimizer, rule_based
from .greedy_optimizer import optimize as optimize_greedy
from .lp_optimizer import optimize as optimize_lp
from .rule_based import optimize as optimize_rule_based

#: 统一入口支持的优化器名称
OPTIMIZER_NAMES: tuple[str, ...] = ("rule_based", "greedy", "lp")


def optimize(
    project: Project,
    *,
    optimizer: str = "rule_based",
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
    **kwargs: object,
) -> OptimizationResult:
    """统一寻优入口（OPTIMIZATION.md §2.2）。

    :param optimizer: ``"rule_based"``（默认）/ ``"greedy"`` / ``"lp"``。
        LP **不会**被隐式调用；选择 ``"lp"`` 时才加载 ``scipy``。
    :param kwargs: 透传给具体优化器的关键字参数（含 ``objective`` 的重复项会被忽略）。
    """
    name = str(optimizer).strip().lower()
    options = dict(kwargs)
    options.pop("objective", None)
    if name in ("rule_based", "rule", "rulebased"):
        return optimize_rule_based(project, objective=objective, **options)
    if name in ("greedy", "greedy_optimizer", "greedy_economic"):
        return optimize_greedy(project, objective=objective, **options)
    if name in ("lp", "lp_optimizer", "linear_programming"):
        return optimize_lp(project, objective=objective, **options)
    raise se.ScenarioError(
        f"未知的优化器 {optimizer!r}；可选：{'、'.join(OPTIMIZER_NAMES)}（OPTIMIZATION.md §2.1）"
    )


__all__ = [
    "OPTIMIZER_NAMES",
    "greedy_optimizer",
    "lp_optimizer",
    "optimize",
    "optimize_greedy",
    "optimize_lp",
    "optimize_rule_based",
    "rule_based",
]
