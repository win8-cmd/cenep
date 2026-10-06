"""计算服务：唯一计算入口的应用层封装（规范 §8、§134、§147–§150；V2 §61、§105）。

职责边界
--------
* **不做任何计算**：校验、公式、指标全部由 :mod:`cenep.calculation` 完成；
* 负责：计算前自动保存、日志、异常翻译、耗时统计；
* 负责：启用 V2 时序仿真时的**口径归一化**——把项目类型调整到与用户实际配置的
  容量一致（V2 §105 结果一致性），使年度模型与逐时仿真使用同一套储能/光伏口径。
  归一化只在 ``timeseries.enabled`` 为真时发生，且在**副本**上进行，绝不修改调用方对象；
  V1（未启用时序）路径**不做**任何归一化，保持 §1.1 兼容性承诺；
* GUI / Excel / PDF 都必须经由本服务拿到 :class:`CalculationResult`。
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from pathlib import Path

from ..calculation.engine import CalculationEngine, calculation_engine
from ..calculation.errors import CalculationError
from ..domain.enums import DispatchStrategy, ProjectType
from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.logging_setup import get_logger
from .project_service import ProjectService

logger = get_logger()

#: 归一化说明的固定措辞（用户提示、报表与测试据此检索，不得随意改动）
ALIGNMENT_NOTE_KEYWORD = "项目类型已按容量自动判定"


@dataclass(frozen=True)
class CalculationOutcome:
    """一次计算的完整回执。"""

    result: CalculationResult
    elapsed_seconds: float
    autosaved_to: Path | None


@dataclass(frozen=True)
class ProjectTypeAlignment:
    """V2 时序项目的**类型归一化**结果（V2 §105 结果一致性）。

    ``project`` 是归一化后的**副本**：调用方传入的项目对象（含其
    ``basic_info.project_type``）始终不被修改。
    """

    #: 归一化后的项目副本（年度模型与时序仿真都以它为准）
    project: Project
    #: 调用方配置的项目类型
    original_type: ProjectType
    #: 按实际容量判定的项目类型
    aligned_type: ProjectType
    #: 时序仿真实际使用的光伏容量 kWp（口径见 ``scenario_engine.effective_pv_capacity``）
    pv_capacity_kwp: float
    #: 储能容量 kWh
    storage_energy_kwh: float
    #: 类型确实被调整时的中文说明；未调整时为 ``None``
    note: str | None
    #: 「储能容量为 0 但配置要求储能」时的中文告警；无此情形时为 ``None``
    warning: str | None

    @property
    def changed(self) -> bool:
        """类型是否被归一化调整过。"""
        return self.original_type is not self.aligned_type

    @property
    def has_pv(self) -> bool:
        """时序仿真是否使用光伏（容量 > 0）。"""
        return self.pv_capacity_kwp > 0.0

    @property
    def has_storage(self) -> bool:
        """时序仿真是否使用储能（容量 > 0）。"""
        return self.storage_energy_kwh > 0.0


def align_project_type(project: Project) -> ProjectTypeAlignment:
    """按**用户实际配置的容量**归一化项目类型，返回副本与中文说明（V2 §105）。

    背景（V2 并行会话在报表开发中发现的真实缺陷）
    ---------------------------------------------
    典型 V2 项目只配置 ``project.storage.storage_energy_kwh = 1000`` 与
    ``storage_power_kw = 500``，却没有设置 ``basic_info.project_type``（默认
    ``COMMERCIAL_PV``）。此时：

    * 逐时仿真 ``economic_v2.simulate_project`` **确实**用了 1000 kWh 储能；
    * 年度模型按类型的 ``has_storage == False`` 计算，储能**不计造价、不计循环**；
    * 服务又把时序的储能套利收益通过 ``year_override`` 注入年度模型。

    结果是"算了储能收益、没算储能造价"，IRR / NPV 被系统性高估，同一份
    :class:`CalculationResult` 内部也自相矛盾（年度模型说无储能、时序说有）。

    归一化规则
    ----------
    直接复用 :func:`cenep.calculation.scenario_engine.normalize_project_type`
    （Phase 8 已实现的同一逻辑，本模块不重复实现）：

    * 光伏 > 0 且 储能 > 0 → ``PV_STORAGE``
    * 光伏 > 0 且 储能 = 0 → ``COMMERCIAL_PV``
    * 光伏 = 0 且 储能 > 0 → ``COMMERCIAL_STORAGE``
    * 两者都为 0 → 保留原类型（无对应类型，属 §42 基准方案）

    调用约束
    --------
    只允许在 ``project.timeseries.enabled`` 为真时调用；V1 路径必须原样交给
    V1 校验器报错（V2 §1.1 兼容性红线）。``project`` 不会被修改，改的是内部副本。

    :raises CalculationError: 归一化后年度模型仍判定"无储能/无光伏"，与逐时仿真
        实际使用的容量口径矛盾——这是内部不一致，必须显式失败，**不得**静默继续
        （静默继续正是 IRR 被高估的成因）。
    """
    # V2 模块延迟导入：未启用时序的项目（V1）不加载 V2 代码（V2 §1.1）。
    from ..calculation.scenario_engine import effective_pv_capacity, normalize_project_type

    clone = copy.deepcopy(project)
    original_type = clone.basic_info.project_type
    pv_capacity = effective_pv_capacity(clone)
    storage_energy = float(clone.storage.storage_energy_kwh)
    has_pv = pv_capacity > 0.0
    has_storage = storage_energy > 0.0

    aligned_type = normalize_project_type(clone)

    # ---- 反向保护 ①：时序用储能、年度模型却不含储能 → 内部不一致，必须报错 ---- #
    if has_storage and not aligned_type.has_storage:
        raise CalculationError(
            "项目类型与储能容量口径不一致（内部一致性错误）：时序仿真按储能容量 "
            f"{storage_energy:g} kWh 计算，但年度模型的项目类型为「{aligned_type.label}」"
            f"（{aligned_type.value}），该类型不含储能。继续计算会出现"
            "「计入储能收益却不计储能造价」，使 IRR / NPV 被高估，因此拒绝计算。"
            "请检查项目类型归一化逻辑（V2 §105 结果一致性）。",
            field="basic_info.project_type",
        )
    # ---- 反向保护 ②：同理，时序用光伏而年度模型不含光伏 ---- #
    if has_pv and not aligned_type.has_pv:
        raise CalculationError(
            "项目类型与光伏容量口径不一致（内部一致性错误）：时序仿真按光伏容量 "
            f"{pv_capacity:g} kWp 计算，但年度模型的项目类型为「{aligned_type.label}」"
            f"（{aligned_type.value}），该类型不含光伏。继续计算会出现"
            "「计入光伏收益却不计光伏造价」，使 IRR / NPV 被高估，因此拒绝计算"
            "（V2 §105 结果一致性）。",
            field="basic_info.project_type",
        )

    note = (
        _alignment_note(original_type, aligned_type, pv_capacity, storage_energy)
        if aligned_type is not original_type
        else None
    )
    warning = None if has_storage else _storage_missing_warning(clone, aligned_type)

    if note is not None:
        logger.info(
            "项目类型已按容量归一化：%s → %s（光伏 %g kWp、储能 %g kWh，V2 §105）",
            original_type.value,
            aligned_type.value,
            pv_capacity,
            storage_energy,
        )
    if warning is not None:
        logger.warning("储能容量为 0 但配置要求储能：%s", warning)

    return ProjectTypeAlignment(
        project=clone,
        original_type=original_type,
        aligned_type=aligned_type,
        pv_capacity_kwp=pv_capacity,
        storage_energy_kwh=storage_energy,
        note=note,
        warning=warning,
    )


def _alignment_note(
    original_type: ProjectType,
    aligned_type: ProjectType,
    pv_capacity_kwp: float,
    storage_energy_kwh: float,
) -> str:
    """归一化说明（中文，写入 ``CalculationResult.notes``，V2 §105）。"""
    basis: list[str] = []
    if pv_capacity_kwp > 0.0:
        basis.append(f"光伏容量 {pv_capacity_kwp:g} kWp > 0")
    if storage_energy_kwh > 0.0:
        basis.append(f"储能容量 {storage_energy_kwh:g} kWh > 0")
    if not basis:
        basis.append("光伏容量与储能容量均为 0")
    return (
        f"{ALIGNMENT_NOTE_KEYWORD}为「{aligned_type.label}」（{aligned_type.value}）："
        f"依据为{'、'.join(basis)}；用户配置的类型为"
        f"「{original_type.label}」（{original_type.value}）。"
        "年度模型与逐时时序仿真已统一到同一套光伏/储能口径，"
        "不会出现「计入储能收益却不计储能造价」的高估（V2 §105 结果一致性）。"
    )


def _storage_missing_warning(project: Project, aligned_type: ProjectType) -> str | None:
    """「时序启用、储能容量为 0」但配置明显要求储能时的中文告警（V2 §105）。

    该情形不在 §105 的"不一致"之列（两边都是无储能，口径一致），但用户很可能是
    **漏填了储能容量**，因此给出明确告警而不是静默按无储能处理。
    返回 ``None`` 表示配置中没有任何要求储能的证据。
    """
    reason = _storage_expectation_reason(project)
    if not reason:
        return None
    return (
        f"储能容量为 0，本次计算按「{aligned_type.label}」（{aligned_type.value}）处理："
        "年度模型与逐时时序仿真均不计储能（无充放电、无储能造价、无套利收益）；"
        f"但配置中仍存在储能相关设置——{reason}。"
        "请确认是否漏填 storage.storage_energy_kwh，或清除上述储能配置后重新计算"
        "（V2 §105）。"
    )


def _storage_expectation_reason(project: Project) -> str:
    """找出"储能容量为 0、但配置仍要求储能"的证据（中文；空串 = 无证据）。"""
    storage = project.storage
    reasons: list[str] = []
    if float(storage.storage_power_kw) > 0.0:
        reasons.append(
            f"储能功率 {float(storage.storage_power_kw):g} kW > 0（容量为 0 时功率无意义）"
        )
    if any(
        float(value) > 0.0
        for value in (
            storage.annual_capacity_revenue,
            storage.annual_ancillary_revenue,
            storage.annual_other_revenue,
        )
    ):
        reasons.append("已填写储能容量/辅助服务/其他收益")
    if storage.replacement_year is not None or float(storage.replacement_capex) > 0.0:
        reasons.append("已填写储能电芯更换年份或更换投资")
    strategy = project.timeseries.dispatch.strategy
    if strategy in (DispatchStrategy.PEAK_VALLEY, DispatchStrategy.ECONOMIC_OPTIMIZATION):
        reasons.append(f"储能调度策略为「{strategy.label}」（以充放电为前提）")
    return "；".join(reasons)


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
            alignment: ProjectTypeAlignment | None = None
            # V2 §105：启用时序时必须先按实际配置的容量归一化项目类型，
            # 让年度模型与时序仿真使用同一个储能口径（否则"算了储能收益、
            # 没算储能造价"，IRR / NPV 被高估）。归一化在副本上进行，
            # 调用方传入的 project 对象不被修改。
            # V1（未启用时序）**不做**任何归一化，原样交给 V1 校验器报错（§1.1）。
            calculation_project = project
            if project.timeseries.enabled:
                alignment = align_project_type(project)
                calculation_project = alignment.project

                from ..calculation import economic_v2

                simulation = economic_v2.simulate_project(calculation_project)
                year_override = simulation.overrides()
                logger.info(
                    "时序仿真完成：%d 年，耗时 %.3f 秒，最大守恒误差 %.3e kWh",
                    len(simulation.years),
                    simulation.elapsed_seconds,
                    simulation.max_balance_error(),
                )

            result = self.engine.calculate(
                calculation_project,
                include_scenario=include_scenario,
                include_sensitivity=include_sensitivity,
                year_override=year_override,
            )
            if alignment is not None:
                # 口径归一化必须对用户可见（§161 可追溯；V2 §105 一致性）
                if alignment.note:
                    result.notes.append(alignment.note)
                if alignment.warning:
                    result.notes.append(alignment.warning)
            if simulation is not None:
                self._attach_timeseries(result, calculation_project, simulation)
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
