"""计算服务：唯一计算入口的应用层封装（规范 §8、§134、§147–§150）。

职责边界
--------
* **不做任何计算**：校验、公式、指标全部由 :mod:`cenep.calculation` 完成；
* 负责：计算前自动保存、日志、异常翻译、耗时统计；
* GUI / Excel / PDF 都必须经由本服务拿到 :class:`CalculationResult`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..calculation.engine import CalculationEngine, calculation_engine
from ..calculation.errors import CalculationError
from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.logging_setup import get_logger
from .project_service import ProjectService

logger = get_logger()


@dataclass(frozen=True)
class CalculationOutcome:
    """一次计算的完整回执。"""

    result: CalculationResult
    elapsed_seconds: float
    autosaved_to: Path | None


class CalculationService:
    """应用层计算服务。"""

    def __init__(
        self,
        engine: CalculationEngine | None = None,
        project_service: ProjectService | None = None,
    ) -> None:
        self.engine = engine or calculation_engine
        self.project_service = project_service or ProjectService()

    def calculate(
        self,
        project: Project,
        current_path: str | Path | None = None,
        *,
        include_scenario: bool = True,
        include_sensitivity: bool = True,
    ) -> CalculationResult:
        """计算并返回结果；失败时抛出带中文说明的 :class:`CalculationError`。"""
        return self.calculate_with_outcome(
            project,
            current_path,
            include_scenario=include_scenario,
            include_sensitivity=include_sensitivity,
        ).result

    def calculate_with_outcome(
        self,
        project: Project,
        current_path: str | Path | None = None,
        *,
        include_scenario: bool = True,
        include_sensitivity: bool = True,
    ) -> CalculationOutcome:
        autosaved: Path | None = None
        if current_path is not None:
            # 规范 §134：计算前自动保存，防止崩溃丢数据
            autosaved = self.project_service.autosave(project, current_path)

        logger.info("计算开始：%s", project.basic_info.project_name)
        started = time.perf_counter()
        try:
            # V2 时序仿真（V2 §62）：仅在启用时执行，且**延迟导入**，
            # 保证未启用时序的项目不会加载 V2 模块（V2 §1.1 兼容性）。
            simulation = None
            year_override = None
            if project.timeseries.enabled:
                from ..calculation import economic_v2

                simulation = economic_v2.simulate_project(project)
                year_override = simulation.overrides()
                logger.info(
                    "时序仿真完成：%d 年，耗时 %.3f 秒，最大守恒误差 %.3e kWh",
                    len(simulation.years),
                    simulation.elapsed_seconds,
                    simulation.max_balance_error(),
                )

            result = self.engine.calculate(
                project,
                include_scenario=include_scenario,
                include_sensitivity=include_sensitivity,
                year_override=year_override,
            )
            if simulation is not None:
                self._attach_timeseries(result, project, simulation)
        except CalculationError as exc:
            logger.warning("计算失败：%s", exc)
            raise
        except Exception as exc:  # 兜底：避免 GUI 崩在未预期异常上
            logger.exception("计算出现未预期异常")
            raise CalculationError(f"计算出现未预期错误：{exc}") from exc

        elapsed = time.perf_counter() - started
        logger.info(
            "计算结束：%s，耗时 %.3f 秒，项目IRR=%s，NPV=%.2f 元",
            project.basic_info.project_name,
            elapsed,
            "无法计算" if result.project_irr is None else f"{result.project_irr:.4%}",
            result.project_npv,
        )
        return CalculationOutcome(result=result, elapsed_seconds=elapsed, autosaved_to=autosaved)

    # ------------------------------------------------------------------ #
    # V2 时序结果装配（V2 §62）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _attach_timeseries(
        result: CalculationResult, project: Project, simulation: object
    ) -> None:
        """把时序仿真结果装配到唯一结果对象上。

        只装配**展示与追溯**需要的内容：

        * ``time_series_results``：首年列式逐时结果 + 指标 + 能量平衡 + 数据质量；
        * ``dispatch_results``：首周（168 小时）的可解释调度决策——整年 8760 个
          Pydantic 对象没有必要（V2 §87），完整逐时动作与原因已在列式结果集的
          ``dispatch_actions`` / ``dispatch_reasons`` 里，可随时取用；
        * ``baseline_results``：基准方案电费与需量（V2 §42）；
        * ``energy_balance``：首年守恒校验（并记录全部年度的最大误差）。
        """
        from datetime import date

        from ..domain.timeseries_results import TimeSeriesReport

        first = simulation.first_year  # type: ignore[attr-defined]
        quality = _score_profiles(project)

        result.time_series_results = TimeSeriesReport(
            enabled=True,
            resolution=simulation.axis.resolution,  # type: ignore[attr-defined]
            base_year=simulation.axis.year,  # type: ignore[attr-defined]
            dispatch_strategy=project.timeseries.dispatch.strategy,
            hourly=economic_v2_to_result_set(first),
            metrics=first.metrics,
            balance=first.balance,
            baseline=first.baseline,
            data_quality=quality,
            as_of=date.today(),
        )
        result.baseline_results = first.baseline
        result.dispatch_results = first.decisions()
        result.energy_balance = first.balance
        result.data_quality = quality

        result.notes.extend(
            [
                "已启用 V2 时序仿真（8760 小时）：电量与收益取自逐时仿真，"
                "OPEX/折旧/税/融资/现金流与 IRR/NPV 仍由同一套年度模型计算（规范 §61）。",
                f"时序分辨率 {simulation.axis.resolution.label}，"
                f"基准年 {simulation.axis.year}（"
                f"{'闰年 8784' if simulation.axis.is_leap else '平年 8760'} 点）。",
                f"储能调度策略：{project.timeseries.dispatch.strategy.label}；"
                f"电网充电{'允许' if project.timeseries.dispatch.allow_grid_charge else '禁止'}，"
                f"储能上网{'允许' if project.timeseries.dispatch.allow_export else '禁止'}。",
                f"能量守恒：首年误差 {first.balance.error:.3e} kWh，"
                f"全部 {len(simulation.years)} 年最大误差 "  # type: ignore[attr-defined]
                f"{simulation.max_balance_error():.3e} kWh，"  # type: ignore[attr-defined]
                f"容差 {first.balance.tolerance:.1e} kWh（规范 §19）。",
                "口径说明：电费节省以现金口径为准（Σ负荷×电价 − Σ购电×电价）；"
                "分解口径中光伏给储能充电按 0 计价、电网充电按购电价计价，"
                "使「光伏自用节省 + 储能套利收益」恒等于总节省额，避免重复计算。",
                f"自用率 {first.metrics.self_consumption_rate:.2%}（自用电量 ÷ 光伏发电量）与 "
                f"自给率 {first.metrics.self_sufficiency_rate:.2%}（自用电量 ÷ 负荷电量）"
                "是两个不同指标，不得混用（规范 §22、§23）。",
            ]
        )
        if simulation.reused_first_year:  # type: ignore[attr-defined]
            result.notes.append(
                f"负荷增长率、光伏/储能衰减率与电价增长率均为 0，"
                f"{simulation.reused_first_year} 个年度与首年逐位等价，"  # type: ignore[attr-defined]
                "已复用首年逐时结果（不影响数值）。"
            )
        if quality is None:
            result.notes.append(
                "数据质量评分不适用：本项目曲线由典型日/等效小时生成，"
                "而非导入实测 8760 数据；导入实测数据后会自动给出质量评分（规范 §55）。"
            )


def economic_v2_to_result_set(sim: object):
    """薄封装，避免在模块顶端导入 V2 模块（V1 路径不加载 V2）。"""
    from ..calculation.economic_v2 import to_result_set

    return to_result_set(sim)  # type: ignore[arg-type]


def _score_profiles(project: Project):
    """对项目内嵌的 8760 曲线做质量评分；失败不影响计算主流程。"""
    ts = project.timeseries
    if ts.load.hourly is None or ts.load.hourly.is_empty:
        return None
    try:
        from ..calculation.timeseries_engine import build_time_axis
        from ..data.quality import score_quality
        from ..data.validator import validate_series

        axis = build_time_axis(ts.base_year, ts.resolution, tuple(ts.holidays))
        issues = validate_series(ts.load.hourly.points, axis, kind="load")
        return score_quality(
            ts.load.hourly.points,
            axis,
            issues=issues,
            source_type=ts.load.hourly.source_type,
        )
    except Exception:  # noqa: BLE001 - 质量评分是附加信息，不得影响计算
        logger.warning("数据质量评分失败，已跳过", exc_info=True)
        return None
