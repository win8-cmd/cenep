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
            result = self.engine.calculate(
                project,
                include_scenario=include_scenario,
                include_sensitivity=include_sensitivity,
            )
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
