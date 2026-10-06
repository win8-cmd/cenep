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
from typing import Any

from ..calculation.engine import CalculationEngine, calculation_engine
from ..calculation.errors import CalculationError
from ..domain.enums import DispatchStrategy, ProjectType
from ..domain.models import Project
from ..domain.results import CalculationResult
from ..domain.timeseries_results import ScenarioResult
from ..infrastructure.logging_setup import get_logger
from .project_service import ProjectService

logger = get_logger()

#: 归一化说明的固定措辞（用户提示、报表与测试据此检索，不得随意改动）
ALIGNMENT_NOTE_KEYWORD = "项目类型已按容量自动判定"

#: 「当前方案」在方案比较中的固定编号与标签（V2 §105 口径一致性核对用，不得随意改动）
CURRENT_SCENARIO_NAME = "CURRENT"
CURRENT_SCENARIO_LABEL = "当前方案（项目配置，精确口径）"

#: 方案比较未执行时的中文说明关键词（用户提示与测试据此检索，不得随意改动）
SCENARIO_SKIPPED_KEYWORD = "方案比较未执行"
#: 方案寻优未执行时的中文说明关键词（用户提示与测试据此检索，不得随意改动）
OPTIMIZATION_SKIPPED_KEYWORD = "方案寻优未执行"


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

    归一化的依据是**用户实际配置的容量**，取"逐时仿真实际使用的容量"口径：

    * 光伏容量：``scenario_engine.effective_pv_capacity``
      （``timeseries.pv.capacity_kwp`` → ``pv.pv_capacity_kwp`` → 0）；
    * 储能容量：``storage.storage_energy_kwh``（时序与年度模型都用它）。

    说明：光伏口径**不含** V1 §19 的"按屋顶面积换算"。若只填了屋顶面积，
    两侧口径会撕裂，此时按逐时仿真口径判定并给出中文告警（``warning``），
    而不是静默按任一侧出结果。

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

    warnings: list[str] = []
    if not has_storage:
        storage_warning = _storage_missing_warning(clone, aligned_type)
        if storage_warning:
            warnings.append(storage_warning)
    pv_warning = _pv_capacity_gap_warning(clone, pv_capacity, aligned_type)
    if pv_warning:
        warnings.append(pv_warning)
    warning = "\n".join(warnings) if warnings else None

    if note is not None:
        logger.info(
            "项目类型已按容量归一化：%s → %s（光伏 %g kWp、储能 %g kWh，V2 §105）",
            original_type.value,
            aligned_type.value,
            pv_capacity,
            storage_energy,
        )
    for item in warnings:
        logger.warning("口径告警：%s", item)

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


def _pv_capacity_gap_warning(
    project: Project, pv_capacity_kwp: float, aligned_type: ProjectType
) -> str | None:
    """「V1 §19 能解析出光伏、时序口径却解析为 0」时的中文告警（V2 §105）。

    逐时时序仿真的光伏容量口径为 ``timeseries.pv.capacity_kwp → pv.pv_capacity_kwp → 0``
    （与 ``scenario_engine.effective_pv_capacity`` 一致），**不含**按屋顶面积换算；
    而年度模型（V1 §19）在未直接输入容量时会按屋顶面积换算。若用户只填了屋顶面积，
    两侧口径就会撕裂。归一口径按时序侧判定（这是"实际参与仿真的容量"），但必须
    明确提示用户补填显式容量，**不得静默**按任一侧出结果。

    :returns: 中文告警；两侧口径一致（或都不含光伏）时为 ``None``。
    """
    from ..calculation.scenario_engine import resolved_pv_capacity

    if pv_capacity_kwp > 0.0:
        return None
    by_area = resolved_pv_capacity(project)
    if by_area <= 0.0:
        return None
    return (
        f"光伏容量口径不一致：按 V1 §19 的屋顶面积口径可解析出 {by_area:g} kWp"
        f"（可利用屋顶面积 {float(project.pv.usable_roof_area_m2):g} m² ÷ "
        f"单位容量占用面积 {float(project.pv.area_per_kwp):g} m²/kWp），"
        "但逐时时序仿真的光伏容量口径（timeseries.pv.capacity_kwp → "
        "pv.pv_capacity_kwp → 0）解析为 0，逐时结果不含光伏电量与收益。"
        f"本次按「{aligned_type.label}」（{aligned_type.value}）计算；"
        "如项目确有光伏，请显式填写 pv.pv_capacity_kwp 或 "
        "timeseries.pv.capacity_kwp 后重新计算（V2 §105 结果一致性）。"
    )


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
                # V2 §43–§48：方案比较（默认执行）与方案寻优（需项目显式开启）
                # 一并写入唯一结果对象，供 GUI「方案比较」图与 Excel
                # 「方案比较」「方案寻优」两张表消费（V2 §62、§105、§109）。
                try:
                    self._attach_advanced_analysis(result, calculation_project, simulation)
                except Exception:  # noqa: BLE001 - 附加分析不得拖垮主计算（V2 §86）
                    logger.warning(
                        "方案比较与寻优装配失败，已跳过（V2 §43–§48）", exc_info=True
                    )
                    result.notes.append(
                        f"{SCENARIO_SKIPPED_KEYWORD}：装配过程出现未预期错误，已跳过；"
                        "主计算结果不受影响（V2 §43–§48、§86）。"
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

    # ------------------------------------------------------------------ #
    # V2 方案比较与寻优装配（V2 §43–§48、§62、§105）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _attach_advanced_analysis(
        result: CalculationResult, project: Project, simulation: object
    ) -> None:
        """把方案比较与寻优结果写入唯一结果对象（V2 §43–§48、§62）。

        默认行为由**项目配置**驱动（全部走正规字段，不使用 ``object.__setattr__``）：

        * **方案比较**：项目启用时序仿真就执行（默认开启）。只评估
          :func:`~cenep.calculation.scenario_engine.standard_variants` 的三种标准组合
          （仅光伏 / 仅储能 / 光伏+储能），按 V2 §86 的**排名阶段**（首年 + 线性外推）
          评估 —— 实测约 0.12 秒/组合，而完整运营期精确仿真是 2~3 秒/组合；
          再前置一行「当前方案」，它直接取自本次唯一计算结果，**零额外计算**，
          且 IRR / NPV / LCOE / LCOS 与 ``result`` 上的同名字段逐位一致（V2 §105）。
          这样 GUI 的「方案比较」图（:class:`~cenep.ui.charts.ScenarioBarChart`）
          与 Excel 的「方案比较」表不再显示"未执行"。
        * **方案寻优**：默认**不执行**（开销大：逐候选时序仿真 + 最优候选完整运营期
          精确复核，实测约 3 秒），仅当项目显式开启
          ``project.timeseries.optimization_enabled`` 时才执行。

        每一步都自带 ``try / except``：任何失败只记录 ``logger.warning`` 并向
        ``result.notes`` 追加中文说明后跳过，绝不抛出（V2 §86：附加分析不得拖垮主计算）。
        """
        CalculationService._run_scenario_comparison(result, project, simulation)
        CalculationService._run_optimization_if_enabled(result, project)

    @staticmethod
    def _run_scenario_comparison(
        result: CalculationResult, project: Project, simulation: object
    ) -> None:
        """执行方案比较并写入 ``result.scenario_results``（V2 §43、§44、§105）。"""
        # V2 模块延迟导入：未启用时序的项目（V1）不加载 V2 代码（V2 §1.1）。
        try:
            from ..calculation import scenario_engine as se
        except Exception as exc:  # noqa: BLE001 - V2 模块不可用时主计算仍须返回
            logger.warning("V2 方案模块加载失败，方案比较已跳过：%s", exc)
            result.notes.append(
                f"{SCENARIO_SKIPPED_KEYWORD}：V2 方案模块"
                f"（cenep.calculation.scenario_engine）加载失败（{exc}），已跳过方案比较"
                "与方案寻优；主计算结果不受影响（V2 §43–§48）。"
            )
            return

        skipped: list[str] = []
        scenarios: list[ScenarioResult] = []
        try:
            # §43 三种标准组合：仅光伏 / 仅储能 / 光伏 + 储能。
            # 两侧容量同时为 0 的组合就是 §42 基准方案（没有可投资对象，V1 引擎会
            # 拒绝计算），因此不进入"可投资方案"比较，只作为说明记录下来。
            variants: list[tuple[str, dict[str, Any]]] = []
            for label, changes in se.standard_variants(project):
                variant = se.apply_variant(project, changes)
                if (
                    se.effective_pv_capacity(variant) <= 0.0
                    and float(variant.storage.storage_energy_kwh) <= 0.0
                ):
                    skipped.append(label)
                    continue
                variants.append((label, changes))
            # full_period=False = V2 §86 排名阶段（首年仿真 + 线性外推），仅用于排序；
            # 「当前方案」一行才是精确口径（见下），避免每次计算都跑 3 遍完整运营期仿真。
            scenarios = se.compare_scenarios(project, variants, full_period=False)
        except Exception as exc:  # noqa: BLE001 - 方案比较失败不得影响主计算
            logger.warning(
                "方案比较执行失败，已跳过（V2 §43、§44；主计算结果不受影响）：%s", exc
            )
            result.notes.append(
                f"{SCENARIO_SKIPPED_KEYWORD}：执行过程中出现错误（{exc}），"
                "已跳过方案比较并继续输出主计算结果（V2 §43–§48、§86）。"
            )
            return

        # 「当前方案」直接取自本次唯一计算结果（不重新计算）：project_irr / project_npv /
        # lcoe / lcos 等与 result 上的同名字段逐位一致，满足 V2 §105 结果一致性。
        first = simulation.first_year  # type: ignore[attr-defined]
        current = se.build_scenario_result(
            project,
            result,
            first.metrics,
            label=CURRENT_SCENARIO_LABEL,
            name=CURRENT_SCENARIO_NAME,
        )
        result.scenario_results = [current, *scenarios]
        compared = "、".join(item.label for item in scenarios) or "（无）"
        result.notes.append(
            f"方案比较已执行（V2 §43、§44）：共 {len(result.scenario_results)} 个方案 —— "
            f"「{CURRENT_SCENARIO_LABEL}」即本次项目配置（口径与主结果逐位一致，V2 §105），"
            f"另含 {len(scenarios)} 个标准组合（{compared}）。"
            + (
                f" 未参与比较：{'、'.join(skipped)}"
                "（光伏与储能容量同时为 0，属 §42 基准方案、没有可投资对象）。"
                if skipped
                else ""
            )
        )
        result.notes.append(
            "方案比较口径说明（V2 §86、§105）：标准组合的 IRR / NPV / 回收期 / LCOE / LCOS "
            "为**排名近似值**——每个组合只跑首年逐时仿真，再用线性外推"
            "（负荷类 (1+g_load)^(n-1)、光伏类 (1-d_pv)^(n-1)、储能类 (1-d_es)^(n-1)、"
            "电价类 (1+g_tariff)^(n-1)）构造全周期收益，只用于横向排序，不得作为最终结论；"
            "方案寻优（若开启）同样按「排名用首年 + 线性外推的近似值、最优候选做完整运营期"
            "**精确复核**」的两阶段口径输出（V2 §48 禁止黑盒、§86 两阶段评估）；"
            f"「{CURRENT_SCENARIO_LABEL}」一行直接取自本次唯一计算结果，其 "
            "project_irr / project_npv / lcoe / lcos 与主结果逐位一致（精确口径，非近似）。"
        )

    @staticmethod
    def _run_optimization_if_enabled(result: CalculationResult, project: Project) -> None:
        """按项目配置决定是否执行方案寻优并写入结果（V2 §45–§48）。

        开关为 :attr:`cenep.domain.timeseries.TimeSeriesConfig.optimization_enabled`
        （默认 ``False``，**不**擅自开启）：寻优要逐候选跑时序仿真并对最优候选做完整
        运营期精确复核，开销远大于常规计算，必须由用户在
        「参数 → 时序仿真总开关」中显式打开（V2 §86）。
        """
        if not bool(project.timeseries.optimization_enabled):
            result.notes.append(
                f"{OPTIMIZATION_SKIPPED_KEYWORD}：项目未开启方案寻优。开启方式：勾选"
                "「参数 → 时序仿真总开关 → 启用方案寻优」（配置项 "
                "timeseries.optimization_enabled = true）后重新计算。默认关闭的原因："
                "寻优要逐个候选跑时序仿真、并对最优候选做完整运营期的精确复核，"
                "耗时远高于常规计算（V2 §45–§48、§86）。"
            )
            return

        try:
            # 延迟导入：V1（未启用时序）路径不加载优化器（V2 §1.1）。
            from ..optimization import optimize

            optimization = optimize(project, optimizer="rule_based")
        except Exception as exc:  # noqa: BLE001 - 寻优失败不得影响主计算
            logger.warning("方案寻优执行失败，已跳过（V2 §45–§48）：%s", exc)
            result.notes.append(
                f"{OPTIMIZATION_SKIPPED_KEYWORD}：执行过程中出现错误（{exc}），"
                "已跳过方案寻优并继续输出主计算结果（V2 §45–§48）。"
            )
            return

        result.optimization_results = optimization
        result.notes.append(
            "方案寻优已执行（V2 §45–§48、§86）：优化器「规则型」（在规则建议点附近做"
            f"小规模网格扫描），目标为「{optimization.objective.label}」，共 "
            f"{len(optimization.candidates)} 个候选，最优方案 "
            f"{optimization.best_run_id}（耗时 {optimization.elapsed_seconds:.2f} 秒）。"
            "排名用的是首年 + 线性外推的**近似**值，最优候选已做**精确复核**"
            "（完整运营期时序仿真），逐条依据见 optimization_results.explanation"
            "（V2 §48 禁止黑盒，两阶段评估见 V2 §86）。"
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
