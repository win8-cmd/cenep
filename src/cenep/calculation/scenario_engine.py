"""方案比较、容量扫描与寻优入口（V2 §42–§48、§15、§86）。

本模块回答两类问题（V2 §44）：

* **方案扫描**（§43、§45）：在给定的候选集合上逐个评估，找出最优容量配置；
* **参数扫描**（§46）：在**固定容量方案**上逐一改变单个参数，回答"结论稳不稳"。

三个优化器（规则型 / 贪心经济 / 线性规划）位于 :mod:`cenep.optimization`，
它们**只负责生成候选与比较**，所有财务口径一律复用 V1 的唯一计算引擎
（``calculation_engine.calculate``），不引入第二套公式（OPTIMIZATION.md §1.2 原则 O2）。

两阶段评估（V2 §86，性能红线）
------------------------------
单个候选的**完整 25 年**时序仿真约 2–5 秒（逐年调度 + 储能衰减），
若 100 个候选各跑 25 年将远超 §86 的「100 方案 < 30 秒」。因此本模块的扫描
一律采用**两阶段**：

1. **排名阶段**（近似）：每个候选只跑**首年** ``simulate_year(project, axis, 1)``
   （约 0.08 秒），得到首年收益分解；再用**文档化的线性外推**构造 25 年
   :class:`~cenep.calculation.economic_v2.YearOverride` 表，送进唯一引擎得到近似的
   IRR / NPV / 回收期 / LCOE / LCOS，**仅用于排序**。外推规则见
   :func:`build_ranking_overrides`（负荷类 ``(1+g_load)^(n-1)``、光伏类
   ``(1-d_pv)^(n-1)``、储能类 ``(1-d_es)^(n-1)``、电价类 ``(1+g_tariff)^(n-1)``）。
2. **复核阶段**（精确）：对**最优候选**（以及可选的前 N 名）跑完整 25 年
   ``simulate_project``，用精确结果覆盖 ``OptimizationResult.best_scenario``。

哪些数字是近似、哪些是精确，逐条写在 ``OptimizationResult.explanation`` 中，
并逐候选写在 :class:`~cenep.domain.timeseries_results.OptimizationCandidate` 的
``note`` 里（「排名用（线性外推）」/「精确复核」）。

禁止黑盒（V2 §48）
------------------
扫描结果必须完整给出：输入参数（``scan_variables``）、约束条件（``constraints``）、
全部候选（``candidates``）、最优方案（``best_run_id`` / ``best_variables`` / ``best_scenario``）
以及中文的 ``explanation``（含目标函数值、与次优的差距、关键指标与近似说明）。

零光伏候选与基准方案（V2 §42、§78）
-----------------------------------
光伏容量为 0 是**合法**输入（V2 §78：项目退化为纯电网负荷项目，出力恒为 0）。
``simulate_candidate_year`` / ``simulate_candidate_project`` 优先调用
``economic_v2`` 的实现；若底层的 ``simulate_year`` 仍拒绝零容量（旧修订），
则退回到 :func:`_simulate_year_without_pv` —— 它用**同一套编排**（同样的
``dispatch`` / ``compute_baseline`` / ``compute_metrics`` / ``check_saving_identity``），
只把光伏曲线换成全零数组，不复制任何公式。

基准方案（无光伏、无储能）没有可投资对象，其财务指标按 §42 取退化值
（IRR / 回收期 / LCOE / LCOS 为 ``None``，NPV 为 0），不进入 V1 引擎
（V1 输入校验要求光伏或储能容量必须大于 0）。
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from ..domain.enums import OptimizationObjective, ProjectType, ScanVariable
from ..domain.models import Project
from ..domain.timeseries_results import (
    OptimizationCandidate,
    OptimizationResult,
    ScenarioResult,
    TimeSeriesMetrics,
)
from ..infrastructure.logging_setup import get_logger
from . import energy_balance as balance_mod
from . import load_profile as load_mod
from . import pv as pv_mod
from . import tariff_series as tariff_mod
from .dispatch_engine import dispatch
from .economic_v2 import (
    SAVING_IDENTITY_TOLERANCE,
    ProjectSimulation,
    YearOverride,
    YearSimulation,
    build_axis,
    check_saving_identity,
    compute_baseline,
    compute_metrics,
    simulate_project,
    simulate_year,
)
from .engine import calculation_engine
from .errors import ValidationError as CalculationValidationError
from .timeseries_engine import TimeAxis

logger: logging.Logger = get_logger()

# --------------------------------------------------------------------------- #
# 规范给定的候选值（V2 §45，候选值由规范给定，不得自行改动）
# --------------------------------------------------------------------------- #
#: 光伏容量候选值（kWp）——V2 §45 规范给定
PV_CAPACITY_VALUES: tuple[float, ...] = (500.0, 750.0, 1000.0, 1250.0, 1500.0, 2000.0)
#: 储能容量候选值（kWh）——V2 §45 规范给定
STORAGE_ENERGY_VALUES: tuple[float, ...] = (0.0, 500.0, 1000.0, 1500.0, 2000.0)
#: 储能功率候选值（kW）——V2 §45 规范给定
STORAGE_POWER_VALUES: tuple[float, ...] = (250.0, 500.0, 750.0, 1000.0)
#: 相对变化型扫描维度的默认取值（±20%，步长 10%）——V2 §46
RELATIVE_SCAN_VALUES: tuple[float, ...] = (-0.2, -0.1, 0.0, 0.1, 0.2)
#: 峰谷价差情景的默认 k 系数（OPTIMIZATION.md §8 第 6 行）
SPREAD_SCAN_VALUES: tuple[float, ...] = (0.7, 0.85, 1.0, 1.15, 1.3)

#: 各扫描维度的默认候选值（V2 §46 的 8 个维度）
DEFAULT_SCAN_VALUES: dict[ScanVariable, tuple[float, ...]] = {
    ScanVariable.PV_CAPACITY: PV_CAPACITY_VALUES,
    ScanVariable.STORAGE_CAPACITY: STORAGE_ENERGY_VALUES,
    ScanVariable.STORAGE_POWER: STORAGE_POWER_VALUES,
    ScanVariable.STORAGE_PRICE: RELATIVE_SCAN_VALUES,
    ScanVariable.TARIFF: RELATIVE_SCAN_VALUES,
    ScanVariable.PEAK_VALLEY_SPREAD: SPREAD_SCAN_VALUES,
    ScanVariable.LOAD: RELATIVE_SCAN_VALUES,
    ScanVariable.CAPEX: RELATIVE_SCAN_VALUES,
}

#: 参数路径 → 中文名（用于 ``scan_variables`` 的可读输出，V2 §48 ①）
PATH_LABELS: dict[str, str] = {
    "pv.pv_capacity_kwp": "光伏容量",
    "storage.storage_energy_kwh": "储能容量",
    "storage.storage_power_kw": "储能功率",
    "investment.storage_capex_per_kwh": "储能单价",
    "investment.pv_capex_per_kw": "光伏单价",
    "tariff.peak_price": "峰段电价",
    "tariff.flat_price": "平段电价",
    "tariff.valley_price": "谷段电价",
    "tariff.export_price": "上网电价",
    "load.annual_load_kwh": "年用电量",
    "timeseries.load.annual_energy_kwh": "年用电量（时序）",
}

#: 目标函数为「越大越好」的取值（其余为「越小越好」，V2 §47）
MAXIMIZATION_OBJECTIVES: frozenset[OptimizationObjective] = frozenset(
    {OptimizationObjective.MAX_NPV, OptimizationObjective.MAX_IRR}
)

#: 候选备注：排名阶段（近似）
RANK_NOTE = "排名用（线性外推）"
#: 候选备注：复核阶段（精确）
VERIFY_NOTE = "精确复核"

#: 目标指标不可计算时的取值（有序、可比较、排序时落在最劣一端）
_UNAVAILABLE_MAX = float("-inf")
_UNAVAILABLE_MIN = float("inf")


class ScenarioError(ValueError):
    """方案扫描 / 寻优过程的错误（中文提示，OPTIMIZATION.md 原则 O4）。"""


class ScenarioPathError(ScenarioError):
    """方案参数路径错误（非法路径或类型不符，OPTIMIZATION.md §11 兼容性红线）。"""


# --------------------------------------------------------------------------- #
# 1. 参数路径读写与深拷贝（候选方案的构造方式）
# --------------------------------------------------------------------------- #
def _check_field(model: object, name: str, path: str) -> None:
    """确认 ``name`` 是 Pydantic 模型的字段（防止误改方法或私有属性）。"""
    fields = getattr(type(model), "model_fields", None)
    if fields is not None and name not in fields:
        raise ScenarioPathError(
            f"参数路径不存在：{path}（“{name}”不是 {type(model).__name__} 的参数字段）"
        )


def get_by_path(model: object, path: str) -> Any:
    """按点号路径读取项目参数，例如 ``"pv.pv_capacity_kwp"``。

    :raises ScenarioPathError: 路径为空、层级不存在或不是模型字段（中文提示）。
    """
    parts = _split_path(path)
    node: Any = model
    for name in parts:
        _check_field(node, name, path)
        node = getattr(node, name)
    return node


def set_by_path(model: object, path: str, value: Any) -> None:
    """按点号路径写入项目参数（**就地修改**，调用方应先深拷贝）。

    类型不符的值会被拒绝（中文提示）；``None`` 允许写入，用于表达"取消该容量"
    （如 ``pv.pv_capacity_kwp = None`` 表示不装光伏），是否可空由领域模型自身校验。

    :raises ScenarioPathError: 路径非法、值类型不符或领域模型校验失败。
    """
    parts = _split_path(path)
    node: Any = model
    for name in parts[:-1]:
        _check_field(node, name, path)
        node = getattr(node, name)
    leaf = parts[-1]
    _check_field(node, leaf, path)

    current = getattr(node, leaf)
    if value is not None and _is_numeric(current) and not _is_numeric(value):
        raise ScenarioPathError(
            f"参数 {path} 需要数值，收到 {type(value).__name__}：{value!r}"
        )
    if isinstance(value, bool) and _is_numeric(current) and not isinstance(current, bool):
        raise ScenarioPathError(f"参数 {path} 需要数值，收到布尔值：{value!r}")
    try:
        setattr(node, leaf, value)
    except CalculationValidationError as exc:
        raise ScenarioPathError(f"参数 {path} 赋值未通过领域模型校验：{exc}") from exc
    except ValueError as exc:  # Pydantic 校验失败（中文包装）
        raise ScenarioPathError(f"参数 {path} 赋值未通过领域模型校验：{exc}") from exc


def _split_path(path: str) -> list[str]:
    if not isinstance(path, str) or not path.strip():
        raise ScenarioPathError(f"参数路径不能为空：{path!r}")
    parts = [p.strip() for p in path.split(".")]
    if any(not p for p in parts) or len(parts) < 2:
        raise ScenarioPathError(
            f"参数路径必须形如“一级字段.二级字段”（如 pv.pv_capacity_kwp），收到：{path!r}"
        )
    return parts


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def apply_variant(project: Project, changes: Mapping[str, Any], *, normalize_type: bool = True) -> Project:
    """深拷贝项目后按点号路径写入覆盖值，返回**新项目**（V2 §43、§45）。

    :param changes: ``{"pv.pv_capacity_kwp": 1500, "storage.storage_energy_kwh": 2000}``
    :param normalize_type: 覆盖后若容量组合与项目类型不匹配，自动切换项目类型。
        V1 输入校验要求「光伏项目容量必须 > 0、储能项目容量必须 > 0」，
        而 §45 的候选值包含 0（如储能容量 0 = 仅光伏），不切换类型将无法计算。
        两侧容量同时为 0 时保留原类型（该组合没有对应的项目类型，属 §42 基准方案，
        不进入 V1 引擎）。

    覆盖 ``pv.pv_capacity_kwp`` 时会同步时序侧的显式容量 ``timeseries.pv.capacity_kwp``
    （若原项目设置了该值），避免出现"引擎按新容量、时序仿真按旧容量"的口径撕裂
    （V2 §105 结果一致性）。

    :raises ScenarioPathError: 路径非法或值类型不符（中文提示）。
    """
    variant = copy.deepcopy(project)
    for path, value in changes.items():
        set_by_path(variant, path, value)
    if "pv.pv_capacity_kwp" in changes and "timeseries.pv.capacity_kwp" not in changes:
        if variant.timeseries.pv.capacity_kwp is not None:
            variant.timeseries.pv.capacity_kwp = variant.pv.pv_capacity_kwp
    if normalize_type:
        normalize_project_type(variant)
    return variant


def normalize_project_type(project: Project) -> ProjectType:
    """按实际容量把项目类型调整到匹配值，返回调整后的类型。

    规则（V1 只支持三种工商业项目）：

    * 光伏 > 0 且 储能 > 0 → ``PV_STORAGE``
    * 光伏 > 0 且 储能 = 0 → ``COMMERCIAL_PV``
    * 光伏 = 0 且 储能 > 0 → ``COMMERCIAL_STORAGE``
    * 两者都为 0 → **保留原类型**（无对应类型，属 §42 基准方案）
    """
    has_pv = effective_pv_capacity(project) > 0.0
    has_storage = float(project.storage.storage_energy_kwh) > 0.0
    if has_pv and has_storage:
        target = ProjectType.PV_STORAGE
    elif has_pv:
        target = ProjectType.COMMERCIAL_PV
    elif has_storage:
        target = ProjectType.COMMERCIAL_STORAGE
    else:
        target = project.basic_info.project_type
    if project.basic_info.project_type is not target:
        logger.debug(
            "候选方案项目类型由 %s 调整为 %s（容量组合要求，V1 输入校验）",
            project.basic_info.project_type.value,
            target.value,
        )
        project.basic_info.project_type = target
    return target


def effective_pv_capacity(project: Project) -> float:
    """时序仿真**实际使用**的光伏容量（与 ``economic_v2.simulate_year`` 口径一致）。

    口径为 ``timeseries.pv.capacity_kwp or pv.pv_capacity_kwp or 0``：
    时序侧显式容量优先，其次 V1 的装机容量输入，都没有时为 0（即不装光伏）。
    """
    return float(
        project.timeseries.pv.capacity_kwp or project.pv.pv_capacity_kwp or 0.0
    )


def resolved_pv_capacity(project: Project) -> float:
    """按 V1 §19 解析光伏容量（直接输入优先，否则按屋顶面积换算）。

    用于**生成候选值**（如规则型建议容量、标准组合的 PV Only 容量），
    与 :func:`effective_pv_capacity` 的区别在于本函数会把屋顶面积换算成容量。
    """
    capacity, _ = pv_mod.resolve_pv_capacity(
        project.pv.pv_capacity_kwp,
        project.pv.usable_roof_area_m2,
        project.pv.area_per_kwp,
    )
    return float(capacity)


def roof_limit_kwp(project: Project) -> float | None:
    """屋顶可装上限 ``可利用屋顶面积 ÷ 单位容量占用面积``（OPTIMIZATION.md §4.1 R1）。

    无可用屋顶面积信息时返回 ``None``（不施加 PR3 剪枝）。
    """
    area = float(project.pv.usable_roof_area_m2)
    per_kwp = float(project.pv.area_per_kwp)
    if area <= 0.0 or per_kwp <= 0.0:
        return None
    return area / per_kwp


# --------------------------------------------------------------------------- #
# 2. 仿真编排（含零光伏候选与两阶段外推）
# --------------------------------------------------------------------------- #
def _simulate_year_without_pv(project: Project, axis: TimeAxis, year_index: int = 1) -> YearSimulation:
    """零光伏候选的单年仿真（V2 §42）。

    与 :func:`~cenep.calculation.economic_v2.simulate_year` 的编排**完全一致**，
    唯一差别是光伏曲线直接取全零数组 —— 因为 ``resolve_pv_series`` 拒绝容量 ≤ 0，
    而「仅储能 / 基准方案」必须表达"没有光伏"。所有公式仍来自 ``economic_v2``。
    """
    ts = project.timeseries
    load = load_mod.resolve_load(ts.load, axis, year_index)
    pv = np.zeros(axis.point_count, dtype=float)
    tariff = tariff_mod.resolve_tariff(ts.tariff, axis, year_index)

    outcome = dispatch(
        load=load,
        pv=pv,
        tariff=tariff,
        axis=axis,
        config=ts.dispatch,
        storage_capacity_kwh=float(project.storage.storage_energy_kwh),
        storage_power_kw=float(project.storage.storage_power_kw),
        storage_degradation_rate=float(project.storage.annual_degradation_rate),
        replacement_year=project.storage.replacement_year,
        year_index=year_index,
    )
    balance = balance_mod.check_balance(outcome, tolerance=float(ts.balance_tolerance))
    baseline = compute_baseline(load, tariff, axis)
    metrics = compute_metrics(
        outcome,
        axis,
        baseline,
        configured_cycles=float(project.storage.annual_cycles),
        demand_charge=float(tariff.demand_charge) if tariff.demand_charge_enabled else 0.0,
        capacity_revenue=float(project.storage.annual_capacity_revenue),
        ancillary_revenue=float(project.storage.annual_ancillary_revenue),
        other_revenue=float(project.storage.annual_other_revenue),
    )
    deviation = check_saving_identity(metrics)
    if deviation > max(SAVING_IDENTITY_TOLERANCE, 1e-9) * max(
        1.0, abs(metrics.electricity_cost_saving)
    ):
        raise ScenarioError(
            f"第 {year_index} 年电费节省分解不闭合（偏差 {deviation:.6f} 元）："
            "存在重复计算或漏计（V2 §19 精神）"
        )
    return YearSimulation(
        year_index=year_index,
        axis=axis,
        load=load,
        pv=pv,
        tariff=tariff,
        outcome=outcome,
        balance=balance,
        metrics=metrics,
        baseline=baseline,
    )


def simulate_candidate_year(project: Project, axis: TimeAxis, year_index: int = 1) -> YearSimulation:
    """候选方案的**单年**仿真。

    优先走 :func:`~cenep.calculation.economic_v2.simulate_year`（它已按 V2 §78
    支持光伏容量为 0）；若该实现仍拒绝零容量（旧修订），则退回到
    :func:`_simulate_year_without_pv` 的等价零光伏编排。
    """
    try:
        return simulate_year(project, axis, year_index)
    except CalculationValidationError:
        if effective_pv_capacity(project) > 0.0:
            raise
        logger.debug("simulate_year 拒绝零光伏容量，改用等价零光伏编排（V2 §42）")
        return _simulate_year_without_pv(project, axis, year_index)


def simulate_candidate_project(project: Project) -> ProjectSimulation:
    """候选方案的**完整运营期**仿真（V2 §32）。

    有光伏时直接复用 :func:`simulate_project`；无光伏且底层实现拒绝零容量时，
    逐年调用零光伏编排，结果同样封装成 :class:`ProjectSimulation`，
    以便复用其 ``overrides()`` 映射。
    """
    try:
        return simulate_project(project)
    except CalculationValidationError:
        if effective_pv_capacity(project) > 0.0:
            raise

    start = time.perf_counter()
    axis = build_axis(project)
    period = int(project.analysis_period)
    years = [_simulate_year_without_pv(project, axis, year) for year in range(1, period + 1)]
    return ProjectSimulation(
        axis=axis,
        years=years,
        reused_first_year=0,
        elapsed_seconds=time.perf_counter() - start,
    )


# 线性外推：年份 n 相对首年的缩放因子按字段分组（V2 §86 排名阶段，文档化近似）
#   load    → (1 + 负荷年增长率)^(n-1)
#   pv      → (1 − 光伏年衰减率)^(n-1)
#   storage → (1 − 储能年衰减率)^(n-1)
#   tariff  → (1 + 电价年增长率)^(n-1)
_EXTRAPOLATION_FACTORS: dict[str, tuple[str, ...]] = {
    # ---- 电量 ----
    "load": ("load",),
    "pv_generation": ("pv",),
    "pv_self_use": ("pv",),
    "pv_export": ("pv",),
    "pv_to_storage": ("pv",),
    "grid_import": ("load",),
    "grid_export": ("pv",),
    "storage_charge": ("storage",),
    "storage_discharge": ("storage",),
    "grid_charge": ("storage",),
    "pv_self_use_revenue": ("pv", "tariff"),
    "pv_export_revenue": ("pv", "tariff"),
    "storage_arbitrage_revenue": ("storage", "tariff"),
    # 容量 / 辅助服务 / 其他收益按 V1 口径为固定值，不随年份增长
    "storage_capacity_revenue": (),
    "storage_ancillary_revenue": (),
    "storage_other_revenue": (),
    "demand_saving": ("load", "tariff"),
}


def _extrapolation_factors(project: Project) -> dict[str, float]:
    """线性外推的各组年因子（V2 §86 排名阶段）。"""
    ts = project.timeseries
    return {
        "load": max(0.0, 1.0 + float(ts.load.annual_growth_rate)),
        "pv": max(0.0, 1.0 - float(project.pv.annual_degradation_rate)),
        "storage": max(0.0, 1.0 - float(project.storage.annual_degradation_rate)),
        "tariff": max(0.0, 1.0 + float(ts.tariff.annual_growth_rate)),
    }


def build_ranking_overrides(
    project: Project, first_year: YearSimulation, axis: TimeAxis
) -> dict[int, YearOverride]:
    """由**首年**仿真结果线性外推整个运营期的 ``YearOverride`` 表（V2 §86 排名阶段）。

    外推规则（全部为文档化的线性近似，仅用于**排序**）：

    ============  ==========================================  ==================================
    分组          缩放因子                                    覆盖字段
    ============  ==========================================  ==================================
    负荷类        ``(1 + g_load)^(n-1)``                       负荷、购电量、需量节省
    光伏类        ``(1 − d_pv)^(n-1)``                         发电量、自用电量、上网电量与收益
    储能类        ``(1 − d_storage)^(n-1)``                    充放电量与套利收益
    电价类        ``(1 + g_tariff)^(n-1)``                     各类收益（与能量因子相乘）
    ============  ==========================================  ==================================

    容量 / 辅助服务 / 其他收益按 V1 口径为固定值（不随年份增长），保持首年数值。
    第 1 年直接取首年精确值，不做任何缩放。
    """
    base = ProjectSimulation(axis=axis, years=[first_year]).overrides()[first_year.year_index]
    factors = _extrapolation_factors(project)
    period = int(project.analysis_period)

    overrides: dict[int, YearOverride] = {first_year.year_index: base}
    for year in range(2, period + 1):
        values: dict[str, float] = {}
        for name, groups in _EXTRAPOLATION_FACTORS.items():
            factor = 1.0
            for group in groups:
                factor *= factors[group] ** (year - 1)
            values[name] = getattr(base, name) * factor
        # 一致性修正（V2 §19、§113）：储能年充电量必须 ≥ 光伏转入储能的电量。
        # 光伏类与储能类的衰减率不同，各自缩放后可能出现"转入大于充电"，
        # 因此按储能年充电量的下限补齐（多出的部分视为电网充电，同步修正）。
        storage_charge = values["storage_charge"]
        pv_to_storage = values["pv_to_storage"]
        if pv_to_storage > storage_charge:
            values["storage_charge"] = pv_to_storage
            storage_charge = pv_to_storage
        values["grid_charge"] = max(storage_charge - pv_to_storage, 0.0)
        overrides[year] = replace(base, year_index=year, **values)
    return overrides


# --------------------------------------------------------------------------- #
# 3. 目标函数与排序（V2 §47、OPTIMIZATION.md §3.3）
# --------------------------------------------------------------------------- #
def is_maximization(objective: OptimizationObjective) -> bool:
    """该目标是否为"越大越好"（V2 §47）。"""
    return objective in MAXIMIZATION_OBJECTIVES


def annual_operating_cost(scenario: ScenarioResult) -> float:
    """年运行成本目标函数（OPTIMIZATION.md §3.1）：

    ``购电成本 − 上网收入 + 储能运行成本``；其中储能度电运行成本 ``c_op``
    未在本期建模（默认 0），需量电费计入购电成本。
    """
    metrics = scenario.metrics
    return float(
        metrics.actual_electricity_cost
        + metrics.actual_demand_cost
        - metrics.pv_export_revenue
    )


def objective_value(objective: OptimizationObjective, scenario: ScenarioResult) -> float:
    """按目标取候选方案的目标函数值（V2 §47）。

    指标无法计算时（如无光伏时的 LCOE、未回收时的回收期、现金流无正负号变化时的 IRR）
    返回该目标方向上的**最劣值**（MAX 目标为 ``-inf``、MIN 目标为 ``+inf``），
    保证排序稳定且不会被误当成最优；此时候选的 ``note`` 会说明原因。
    """
    if objective is OptimizationObjective.MAX_NPV:
        return float(scenario.project_npv)
    if objective is OptimizationObjective.MAX_IRR:
        return float(scenario.project_irr) if scenario.project_irr is not None else _UNAVAILABLE_MAX
    if objective is OptimizationObjective.MIN_PAYBACK:
        return (
            float(scenario.static_payback)
            if scenario.static_payback is not None
            else _UNAVAILABLE_MIN
        )
    if objective is OptimizationObjective.MIN_LCOE:
        return float(scenario.lcoe) if scenario.lcoe is not None else _UNAVAILABLE_MIN
    if objective is OptimizationObjective.MIN_LCOS:
        return float(scenario.lcos) if scenario.lcos is not None else _UNAVAILABLE_MIN
    if objective is OptimizationObjective.MIN_ANNUAL_COST:
        return annual_operating_cost(scenario)
    raise ScenarioError(f"不支持的寻优目标：{objective!r}（V2 §47 共 6 种）")


def objective_metric_name(objective: OptimizationObjective) -> str:
    """目标函数对应的中文指标名（用于 ``explanation``）。"""
    return {
        OptimizationObjective.MAX_NPV: "项目 NPV（元）",
        OptimizationObjective.MAX_IRR: "项目 IRR",
        OptimizationObjective.MIN_PAYBACK: "静态回收期（年）",
        OptimizationObjective.MIN_LCOE: "LCOE（元/kWh）",
        OptimizationObjective.MIN_LCOS: "LCOS（元/kWh）",
        OptimizationObjective.MIN_ANNUAL_COST: "年运行成本（元）",
    }[objective]


def rank_key(
    objective: OptimizationObjective, index: int, scenario: ScenarioResult
) -> tuple[float, float, float, float, int]:
    """排序键（升序即"从优到劣"），含 OPTIMIZATION.md §3.3 的并列打破规则。

    并列时依次比较：① 总投资较小者 ② 储能容量较小者 ③ 光伏容量较小者 ④ 候选编号较小者。
    """
    value = objective_value(objective, scenario)
    direction = -1.0 if is_maximization(objective) else 1.0
    return (
        direction * value,
        float(scenario.total_capex),
        float(scenario.storage_energy_kwh),
        float(scenario.pv_capacity_kwp),
        index,
    )


# --------------------------------------------------------------------------- #
# 4. 候选方案与结果装配
# --------------------------------------------------------------------------- #
def build_scenario_result(
    project: Project,
    calculation: Any,
    metrics: TimeSeriesMetrics,
    *,
    label: str = "",
    name: str = "",
    is_baseline: bool = False,
    capacities: tuple[float, float, float] | None = None,
) -> ScenarioResult:
    """把唯一计算引擎的结果与首年时序指标装配成 :class:`ScenarioResult`（V2 §43、§44）。

    :param calculation: ``calculation_engine.calculate`` 的返回值；基准方案传 ``None``。
    :param capacities: ``(光伏 kWp, 储能功率 kW, 储能容量 kWh)``；为 ``None`` 时取引擎解析值。
    """
    if capacities is None:
        if calculation is not None:
            capacities = (
                float(calculation.pv_capacity_kwp),
                float(calculation.storage_power_kw),
                float(calculation.storage_energy_kwh),
            )
        else:
            capacities = (
                effective_pv_capacity(project),
                float(project.storage.storage_power_kw),
                float(project.storage.storage_energy_kwh),
            )
    pv_capacity, storage_power, storage_energy = capacities

    if calculation is None:
        # §42 基准方案：没有可投资对象，财务指标为退化值（不进入 V1 引擎）
        return ScenarioResult(
            name=name or "BASELINE",
            label=label or "基准方案（无光伏、无储能）",
            is_baseline=True,
            pv_capacity_kwp=pv_capacity,
            storage_power_kw=storage_power,
            storage_energy_kwh=storage_energy,
            total_capex=0.0,
            project_irr=None,
            equity_irr=None,
            project_npv=0.0,
            static_payback=None,
            discounted_payback=None,
            lcoe=None,
            lcos=None,
            annual_saving=float(
                metrics.electricity_cost_saving + metrics.demand_cost_saving
            ),
            self_consumption_rate=float(metrics.self_consumption_rate),
            self_sufficiency_rate=float(metrics.self_sufficiency_rate),
            equivalent_cycles=float(metrics.equivalent_cycles),
            demand_saving=float(metrics.demand_cost_saving),
            metrics=metrics,
        )

    return ScenarioResult(
        name=name or scenario_code(pv_capacity, storage_energy, storage_power),
        label=label or scenario_code(pv_capacity, storage_energy, storage_power),
        is_baseline=is_baseline,
        pv_capacity_kwp=pv_capacity,
        storage_power_kw=storage_power,
        storage_energy_kwh=storage_energy,
        total_capex=float(calculation.total_capex),
        project_irr=calculation.project_irr,
        equity_irr=calculation.equity_irr,
        project_npv=float(calculation.project_npv),
        static_payback=calculation.static_payback,
        discounted_payback=calculation.discounted_payback,
        lcoe=calculation.lcoe,
        lcos=calculation.lcos,
        annual_saving=float(metrics.electricity_cost_saving + metrics.demand_cost_saving),
        self_consumption_rate=float(metrics.self_consumption_rate),
        self_sufficiency_rate=float(metrics.self_sufficiency_rate),
        equivalent_cycles=float(metrics.equivalent_cycles),
        demand_saving=float(metrics.demand_cost_saving),
        metrics=metrics,
    )


def scenario_code(pv_capacity_kwp: float, storage_energy_kwh: float, storage_power_kw: float) -> str:
    """候选方案编号（V2 §7.3：编号编码容量维度取值，便于人工核对）。"""
    return f"PV{pv_capacity_kwp:.0f}_E{storage_energy_kwh:.0f}_P{storage_power_kw:.0f}"


def _run_engine(project: Project, overrides: dict[int, YearOverride]) -> Any:
    """调用唯一计算引擎（V2 §61：不存在第二套财务实现）。"""
    return calculation_engine.calculate(
        project,
        year_override=overrides,
        include_scenario=False,
        include_sensitivity=False,
    )


def _wrap_engine_error(project: Project, error: Exception) -> ScenarioError:
    return ScenarioError(
        "候选方案无法完成经济计算："
        f"{error}（请检查 timeseries 的负荷/光伏/电价曲线与容量设置，V2 §45）"
    )


def evaluate_scenario(
    project: Project,
    axis: TimeAxis | None = None,
    *,
    label: str = "",
    full_period: bool = True,
    name: str = "",
) -> ScenarioResult:
    """评估单个方案，返回 :class:`ScenarioResult`（V2 §43、§44）。

    :param full_period: ``True`` 为**精确复核**（完整运营期 25 年时序仿真）；
        ``False`` 为**排名阶段**（仅首年仿真 + 线性外推，见 :func:`build_ranking_overrides`，
        结果只能用于排序，不得作为最终结论）。
    :param axis: 可复用的时间轴；``None`` 时按候选方案自行构造（构造耗时约 0.1 秒）。
    """
    resolved_axis = axis if axis is not None else build_axis(project)
    try:
        if full_period:
            simulation = simulate_candidate_project(project)
            first_year = simulation.first_year
            overrides = simulation.overrides()
        else:
            first_year = simulate_candidate_year(project, resolved_axis, 1)
            overrides = build_ranking_overrides(project, first_year, resolved_axis)
        calculation = _run_engine(project, overrides)
    except ScenarioError:
        raise
    except (CalculationValidationError, ValueError) as exc:
        raise _wrap_engine_error(project, exc) from exc

    return build_scenario_result(
        project,
        calculation,
        first_year.metrics,
        label=label,
        name=name,
    )


def build_baseline_scenario(project: Project, axis: TimeAxis | None = None) -> ScenarioResult:
    """构造 §42 基准方案：**无光伏、无储能**，全部负荷由电网供应。

    基准方案没有投资对象，因此 ``total_capex = 0``、``project_npv = 0``，
    IRR / 回收期 / LCOE / LCOS 为 ``None``（展示层显示"无法计算"），
    ``annual_saving`` 恒为 0 —— 它只作为电费与需量的对照基准。
    """
    resolved_axis = axis if axis is not None else build_axis(project)
    changes: dict[str, Any] = {
        "pv.pv_capacity_kwp": 0.0,
        "timeseries.pv.capacity_kwp": None,
        "storage.storage_energy_kwh": 0.0,
        "storage.storage_power_kw": 0.0,
    }
    baseline_project = apply_variant(project, changes, normalize_type=False)
    try:
        simulation = _simulate_year_without_pv(baseline_project, resolved_axis, 1)
    except (CalculationValidationError, ValueError) as exc:
        raise _wrap_engine_error(project, exc) from exc
    return build_scenario_result(
        baseline_project,
        None,
        simulation.metrics,
        label="基准方案（无光伏、无储能）",
        name="BASELINE",
        is_baseline=True,
    )


def standard_variants(
    project: Project,
    *,
    pv_capacity_kwp: float | None = None,
    storage_energy_kwh: float | None = None,
    storage_power_kw: float | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """§43 要求的三种标准组合：**PV Only / Storage Only / PV + Storage**。

    默认容量取项目自身取值（光伏按 V1 §19 解析，储能取配置值），可用参数显式覆盖。
    返回 ``[(中文标签, 覆盖字典)]``，可直接交给 :func:`compare_scenarios`。
    """
    pv = (
        float(pv_capacity_kwp)
        if pv_capacity_kwp is not None
        else resolved_pv_capacity(project)
    )
    energy = (
        float(storage_energy_kwh)
        if storage_energy_kwh is not None
        else float(project.storage.storage_energy_kwh)
    )
    power = (
        float(storage_power_kw)
        if storage_power_kw is not None
        else float(project.storage.storage_power_kw)
    )
    pv_value: Any = pv if pv > 0.0 else 0.0
    return [
        (
            "仅光伏（PV Only）",
            {
                "pv.pv_capacity_kwp": pv_value,
                "storage.storage_energy_kwh": 0.0,
                "storage.storage_power_kw": 0.0,
            },
        ),
        (
            "仅储能（Storage Only）",
            {
                "pv.pv_capacity_kwp": 0.0,
                "timeseries.pv.capacity_kwp": None,
                "storage.storage_energy_kwh": energy,
                "storage.storage_power_kw": power,
            },
        ),
        (
            "光伏+储能（PV + Storage）",
            {
                "pv.pv_capacity_kwp": pv_value,
                "storage.storage_energy_kwh": energy,
                "storage.storage_power_kw": power,
            },
        ),
    ]


def compare_scenarios(
    project: Project,
    variants: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    full_period: bool = True,
) -> list[ScenarioResult]:
    """批量评估多个方案变体并返回结果列表（V2 §43）。

    :param variants: ``[(标签, 覆盖字典)]``，覆盖字典为**点号路径 → 值**。
    :param full_period: 同 :func:`evaluate_scenario`（``False`` = 排名用近似）。
    """
    axis = build_axis(project)
    results: list[ScenarioResult] = []
    for label, changes in variants:
        variant = apply_variant(project, changes)
        results.append(
            evaluate_scenario(variant, axis, label=label, full_period=full_period)
        )
    return results


# --------------------------------------------------------------------------- #
# 5. 候选评估器（两阶段：排名 + 复核）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CandidateSpec:
    """一个待评估的候选方案（V2 §45、§48 ②）。"""

    run_id: str
    variables: dict[str, float] = field(default_factory=dict)
    changes: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    note: str = ""

    def value_key(self) -> tuple[tuple[str, float], ...]:
        """取值的规范化键（用于候选去重，OPTIMIZATION.md §5.2 G2）。"""
        return tuple(sorted((k, float(v)) for k, v in self.variables.items()))


def default_constraints(project: Project, budget: float | None = None) -> list[str]:
    """生效的约束清单（中文，V2 §13、§14、§48 ③；OPTIMIZATION.md §3.2、§7.1）。"""
    dispatch_cfg = project.timeseries.dispatch
    limit = roof_limit_kwp(project)
    out = [
        f"C1 SOC 上下限：全部小时的 SOC 必须落在 [{dispatch_cfg.soc_min:.0%}, "
        f"{dispatch_cfg.soc_max:.0%}] 内（V2 §13）",
        f"C4 能量平衡：逐时残差 ≤ {float(project.timeseries.balance_tolerance):g} kWh，"
        "违反者判为不可行（V2 §95）",
        "C5 容量非负：光伏容量、储能容量、储能功率均不得为负（V2 §13）",
        "C7 策略一致性：同一候选只跑一种调度策略 "
        f"{dispatch_cfg.strategy.value}（V2 §13）",
        "PR1 储能容量为 0 时储能功率必须为 0（避免出现「有功率无容量」的无效组合）",
        "PR2 储能时长 = 储能容量 ÷ 储能功率，必须落在 [0.5, 4] 小时",
    ]
    if limit is not None:
        out.append(
            f"C6/PR3 光伏容量上限 {limit:.1f} kWp"
            f"（可利用屋顶面积 {float(project.pv.usable_roof_area_m2):.1f} m² ÷ "
            f"单位容量占用面积 {float(project.pv.area_per_kwp):.2f} m²/kWp），超限候选剪枝"
        )
    if budget is not None:
        out.append(f"投资预算上限 {float(budget):,.2f} 元，超出者判为不可行")
    return out


def structural_issue(project: Project) -> str:
    """候选方案的**结构性**约束检查，返回中文不可行原因（空字符串 = 通过）。

    不通过者不必进入时序仿真（PR1 / PR2 / PR3 / C5），直接以 ``feasible=False`` 记录。
    """
    pv_capacity = effective_pv_capacity(project)
    energy = float(project.storage.storage_energy_kwh)
    power = float(project.storage.storage_power_kw)
    if pv_capacity < 0.0 or energy < 0.0 or power < 0.0:
        return (
            f"C5：容量不得为负（光伏 {pv_capacity:g} kWp、储能 {energy:g} kWh、"
            f"功率 {power:g} kW），已剪枝"
        )
    limit = roof_limit_kwp(project)
    if pv_capacity > 0.0 and limit is not None and pv_capacity > limit + 1e-9:
        return (
            f"PR3：光伏容量 {pv_capacity:.1f} kWp 超过屋顶可装上限 {limit:.1f} kWp，已剪枝"
        )
    if energy <= 0.0 and power > 0.0:
        return f"PR1：储能容量为 0 时储能功率必须为 0（当前 {power:g} kW），已剪枝"
    if energy > 0.0 and power <= 0.0:
        return "PR2：储能容量大于 0 但储能功率为 0，无法充放电，已剪枝"
    if energy > 0.0:
        duration = energy / power
        if not (0.5 - 1e-9 <= duration <= 4.0 + 1e-9):
            return (
                f"PR2：储能时长 {duration:.2f} 小时（= 容量 {energy:g} kWh ÷ 功率 {power:g} kW）"
                "不在 [0.5, 4] 小时区间内，已剪枝"
            )
    return ""


class CandidateEvaluator:
    """候选方案评估器：两阶段评估 + 去重缓存（V2 §86、OPTIMIZATION.md §5.2）。

    使用方式::

        evaluator = CandidateEvaluator(project, objective=...)
        for spec in specs:
            evaluator.evaluate(spec)          # 排名阶段（首年 + 线性外推）
        result = evaluator.finalize(...)      # 复核最优 + 生成 §48 五段式输出
    """

    def __init__(
        self,
        project: Project,
        *,
        objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
        axis: TimeAxis | None = None,
        budget: float | None = None,
        constraints: Sequence[str] | None = None,
    ) -> None:
        self.project = project
        self.objective = objective
        self.axis = axis if axis is not None else build_axis(project)
        self.budget = budget
        self.constraints = (
            list(constraints) if constraints is not None else default_constraints(project, budget)
        )
        self.candidates: dict[tuple[tuple[str, float], ...], OptimizationCandidate] = {}
        self.order: list[tuple[tuple[str, float], ...]] = []
        self.evaluations = 0
        self.verified: set[str] = set()
        self._changes_by_run: dict[str, dict[str, Any]] = {}
        self._started = time.perf_counter()

    # ---- 排名阶段 ---------------------------------------------------------- #
    def evaluate(self, spec: CandidateSpec) -> OptimizationCandidate:
        """评估一个候选（排名阶段）；相同取值只算一次（§5.2 G2）。"""
        key = spec.value_key()
        if key in self.candidates:
            return self.candidates[key]
        self._changes_by_run.setdefault(spec.run_id, dict(spec.changes))

        variant = apply_variant(self.project, spec.changes)
        issue = structural_issue(variant)
        if issue:
            candidate = OptimizationCandidate(
                run_id=spec.run_id,
                variables=dict(spec.variables),
                objective=self.objective,
                objective_value=(
                    _UNAVAILABLE_MAX if is_maximization(self.objective) else _UNAVAILABLE_MIN
                ),
                feasible=False,
                note=f"{issue}（未做时序仿真）",
                scenario=ScenarioResult(name=spec.run_id, label=spec.label or spec.run_id),
            )
            self._remember(key, candidate)
            return candidate

        scenario, issue = self._rank(variant, spec)
        if scenario is None:
            # 时序仿真或经济计算失败：不能拿"零值方案"参与排序（会被误判为最优）
            candidate = OptimizationCandidate(
                run_id=spec.run_id,
                variables=dict(spec.variables),
                objective=self.objective,
                objective_value=(
                    _UNAVAILABLE_MAX if is_maximization(self.objective) else _UNAVAILABLE_MIN
                ),
                feasible=False,
                note=f"{RANK_NOTE}；{issue}",
                scenario=ScenarioResult(name=spec.run_id, label=spec.label or spec.run_id),
            )
            self._remember(key, candidate)
            return candidate
        candidate = OptimizationCandidate(
            run_id=spec.run_id,
            variables=dict(spec.variables),
            objective=self.objective,
            objective_value=objective_value(self.objective, scenario),
            feasible=not issue,
            note=self._ranking_note(issue, spec.note),
            scenario=scenario,
        )
        self.evaluations += 1
        self._remember(key, candidate)
        return candidate

    def _rank(self, variant: Project, spec: CandidateSpec) -> tuple[ScenarioResult | None, str]:
        """排名阶段：首年仿真 + 线性外推 + 唯一引擎；失败时返回 ``(None, 原因)``。"""
        try:
            first_year = simulate_candidate_year(variant, self.axis, 1)
        except (CalculationValidationError, ValueError) as exc:
            return None, f"时序仿真失败：{exc}"
        tolerance = float(variant.timeseries.balance_tolerance)
        if abs(float(first_year.balance.error)) > tolerance:
            scenario = build_scenario_result(
                variant,
                None,
                first_year.metrics,
                label=spec.label or spec.run_id,
                name=spec.run_id,
                capacities=(
                    effective_pv_capacity(variant),
                    float(variant.storage.storage_power_kw),
                    float(variant.storage.storage_energy_kwh),
                ),
            )
            return (
                scenario,
                f"C4：能量平衡残差 {abs(float(first_year.balance.error)):.3e} kWh "
                f"超过容差 {tolerance:g} kWh",
            )

        overrides = build_ranking_overrides(variant, first_year, self.axis)
        try:
            calculation = _run_engine(variant, overrides)
        except (CalculationValidationError, ValueError) as exc:
            return None, f"经济计算失败：{exc}"
        scenario = build_scenario_result(
            variant, calculation, first_year.metrics, label=spec.label or spec.run_id, name=spec.run_id
        )
        issue = ""
        if self.budget is not None and scenario.total_capex > float(self.budget) + 1e-9:
            issue = (
                f"投资预算约束：总投资 {scenario.total_capex:,.2f} 元超过上限 "
                f"{float(self.budget):,.2f} 元"
            )
        return scenario, issue

    @staticmethod
    def _ranking_note(issue: str, extra: str) -> str:
        parts = [RANK_NOTE]
        if issue:
            parts.append(issue)
        if extra:
            parts.append(extra)
        return "；".join(parts)

    # ---- 复核阶段 ---------------------------------------------------------- #
    def verify(self, candidate: OptimizationCandidate) -> OptimizationCandidate:
        """对候选跑完整运营期时序仿真，用精确结果覆盖（V2 §86 复核阶段）。"""
        changes = self._changes_by_run.get(candidate.run_id, dict(candidate.variables))
        variant = apply_variant(self.project, changes)
        try:
            scenario = evaluate_scenario(
                variant,
                None,
                label=candidate.scenario.label or candidate.run_id,
                full_period=True,
                name=candidate.run_id,
            )
        except ScenarioError as exc:
            candidate.feasible = False
            candidate.note = f"{RANK_NOTE}；精确复核失败：{exc}"
            return candidate
        candidate.scenario = scenario
        candidate.objective_value = objective_value(self.objective, scenario)
        issue = ""
        if self.budget is not None and scenario.total_capex > float(self.budget) + 1e-9:
            issue = (
                f"投资预算约束：总投资 {scenario.total_capex:,.2f} 元超过上限 "
                f"{float(self.budget):,.2f} 元"
            )
            candidate.feasible = False
        candidate.note = "；".join(part for part in (VERIFY_NOTE, issue) if part)
        self.verified.add(candidate.run_id)
        return candidate

    # ---- 汇总 -------------------------------------------------------------- #
    def ranked(self) -> list[OptimizationCandidate]:
        """按目标函数从优到劣排序（含并列打破规则；OPTIMIZATION.md §3.3）。"""
        return [
            candidate
            for _, candidate in sorted(
                enumerate(self.ordered_candidates()),
                key=lambda pair: rank_key(self.objective, pair[0], pair[1].scenario),
            )
        ]

    def ordered_candidates(self) -> list[OptimizationCandidate]:
        """按评估顺序返回候选（确定性，OPTIMIZATION.md 原则 O5）。"""
        return [self.candidates[key] for key in self.order]

    def _remember(
        self, key: tuple[tuple[str, float], ...], candidate: OptimizationCandidate
    ) -> None:
        self.candidates[key] = candidate
        self.order.append(key)

    def finalize(
        self,
        *,
        scan_variables: Sequence[str],
        verify_top_n: int = 1,
        extra_explanation: Sequence[str] = (),
    ) -> OptimizationResult:
        """复核最优（及前 N 名）并生成 §48 五段式结果。"""
        ranked = self.ranked()
        feasible = [c for c in ranked if c.feasible]

        verified: list[OptimizationCandidate] = []
        for candidate in feasible[: max(int(verify_top_n), 0)]:
            verified.append(self.verify(candidate))
        if verified:
            ranked = self.ranked()
            feasible = [c for c in ranked if c.feasible]

        best = feasible[0] if feasible else (ranked[0] if ranked else None)
        best_scenario = best.scenario if best is not None else None
        explanation = list(extra_explanation)
        explanation.extend(self._explain(ranked, feasible, best, best_scenario))
        return OptimizationResult(
            objective=self.objective,
            scan_variables=list(scan_variables),
            constraints=list(self.constraints),
            candidates=ranked,
            best_run_id=best.run_id if best is not None else "",
            best_variables=dict(best.variables) if best is not None else {},
            best_scenario=best_scenario,
            explanation=explanation,
            elapsed_seconds=time.perf_counter() - self._started,
        )

    def _explain(
        self,
        ranked: Sequence[OptimizationCandidate],
        feasible: Sequence[OptimizationCandidate],
        best: OptimizationCandidate | None,
        best_scenario: ScenarioResult | None,
    ) -> list[str]:
        """生成中文 ``explanation``（V2 §48 ⑤；OPTIMIZATION.md §9.1 强制内容）。"""
        lines: list[str] = []
        objective = self.objective
        lines.append(
            f"本次寻优目标为「{objective.label}」（{objective.value}，"
            f"{'越大越好' if is_maximization(objective) else '越小越好'}），"
            f"共输出 {len(ranked)} 个候选方案（其中 {self.evaluations} 个完成时序仿真评估，"
            f"其余在结构性剪枝阶段被剔除），可行 {len(feasible)} 个、"
            f"不可行 {len(ranked) - len(feasible)} 个。"
        )
        if best is None or best_scenario is None:
            lines.append("没有可比较的候选方案，未能给出最优方案（请检查候选值或约束设置）。")
            return lines

        metric_name = objective_metric_name(objective)
        lines.append(
            f"最优方案为 {best.run_id}：{metric_name} = {best.objective_value:,.4f}"
            f"（总投资 {best_scenario.total_capex:,.2f} 元）"
            + (
                f"；该候选备注：{best.note}"
                if best.note
                else ""
            )
        )
        others = [c for c in feasible if c.run_id != best.run_id]
        if others:
            second = others[0]
            gap = abs(best.objective_value - second.objective_value)
            base = abs(second.objective_value)
            percent = (gap / base * 100.0) if base > 1e-12 else float("nan")
            direction = "高" if is_maximization(objective) else "低"
            percent_text = f"{percent:+.2f}%" if percent == percent else "无法计算百分比"
            lines.append(
                f"与次优方案 {second.run_id}（{metric_name} = {second.objective_value:,.4f}）相比，"
                f"最优方案的 {metric_name} {direction} {gap:,.4f}（{percent_text}）；"
                "并列时按「① 总投资较小者 ② 储能容量较小者 ③ 光伏容量较小者 ④ 候选编号较小者」"
                "的顺序打破（OPTIMIZATION.md §3.3）。"
            )
        else:
            lines.append("本次仅有 1 个可行候选方案，不存在次优方案可比。")

        lines.append(
            "最优方案关键指标："
            f"项目 NPV = {best_scenario.project_npv:,.2f} 元；"
            f"项目 IRR = {_fmt_optional(best_scenario.project_irr, '.4%')}；"
            f"资本金 IRR = {_fmt_optional(best_scenario.equity_irr, '.4%')}；"
            f"静态回收期 = {_fmt_optional(best_scenario.static_payback, '.2f')} 年；"
            f"动态回收期 = {_fmt_optional(best_scenario.discounted_payback, '.2f')} 年；"
            f"LCOE = {_fmt_optional(best_scenario.lcoe, '.4f')} 元/kWh；"
            f"LCOS = {_fmt_optional(best_scenario.lcos, '.4f')} 元/kWh；"
            f"年节省 = {best_scenario.annual_saving:,.2f} 元；"
            f"自用率 = {best_scenario.self_consumption_rate:.2%}；"
            f"自给率 = {best_scenario.self_sufficiency_rate:.2%}；"
            f"等效循环 = {best_scenario.equivalent_cycles:.2f} 次/年。"
        )

        approximate = [c.run_id for c in ranked if c.run_id not in self.verified]
        lines.append(
            "近似说明（V2 §86 两阶段评估）：排名阶段每个候选只跑**首年**时序仿真，"
            "再用线性外推（负荷类 (1+g_load)^(n-1)、光伏类 (1-d_pv)^(n-1)、"
            "储能类 (1-d_es)^(n-1)、电价类 (1+g_tariff)^(n-1)）构造全周期收益，"
            "因此这些候选的 IRR / NPV / 回收期 / LCOE / LCOS 均为**近似值**，只用于排序；"
            f"其中 {len(self.verified)} 个候选（{_join_ids(sorted(self.verified))}）已做"
            "**精确复核**（完整运营期时序仿真），最优方案 best_scenario 为精确结果。"
            + (
                f"仍为近似的候选共 {len(approximate)} 个：{_join_ids(approximate)}。"
                if approximate
                else "全部候选均已精确复核。"
            )
        )
        if any(c.feasible and c.objective_value in (_UNAVAILABLE_MAX, _UNAVAILABLE_MIN) for c in feasible):
            lines.append(
                "注意：部分可行候选的目标指标无法计算（如无光伏时的 LCOE、未回收时的回收期），"
                "已按该目标方向的最劣值参与排序，并在候选 note 中说明。"
            )
        lines.append(
            "本结果只输出指标与最优方案，不输出「项目可行/不可行」的结论"
            "（V1 §106、OPTIMIZATION.md 原则 O6）。"
        )
        return lines


def _fmt_optional(value: float | None, fmt: str) -> str:
    return "无法计算" if value is None else format(float(value), fmt)


def _join_ids(ids: Sequence[str], limit: int = 12) -> str:
    ids = list(ids)
    if len(ids) <= limit:
        return "、".join(ids)
    return "、".join(ids[:limit]) + f" 等 {len(ids)} 个"


# --------------------------------------------------------------------------- #
# 6. 扫描入口：单维 / 网格 / 参数（V2 §45、§46）
# --------------------------------------------------------------------------- #
def _path_variables(changes: Mapping[str, Any]) -> dict[str, float]:
    """覆盖字典 → 候选的 ``variables``（扫描变量 → 取值，V2 §48 ②）。"""
    return {
        path: float(value)
        for path, value in changes.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def evaluate_candidates(
    project: Project,
    specs: Sequence[CandidateSpec],
    *,
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
    axis: TimeAxis | None = None,
    budget: float | None = None,
    verify_top_n: int = 1,
    constraints: Sequence[str] | None = None,
    extra_explanation: Sequence[str] = (),
    scan_variables: Sequence[str] | None = None,
) -> OptimizationResult:
    """评估一组候选方案并生成 §48 五段式结果（供三个优化器复用）。

    :param scan_variables: 本次扫了哪些维度的可读清单（§48 ①）；``None`` 时由候选
        的覆盖路径自动汇总。
    :param verify_top_n: 复核阶段跑精确全周期仿真的候选个数（默认仅最优 1 个）。
    """
    evaluator = CandidateEvaluator(
        project, objective=objective, axis=axis, budget=budget, constraints=constraints
    )
    for spec in specs:
        evaluator.evaluate(spec)
    return evaluator.finalize(
        scan_variables=(
            list(scan_variables) if scan_variables is not None else _scan_variables_of(specs)
        ),
        verify_top_n=verify_top_n,
        extra_explanation=extra_explanation,
    )


def _scan_variables_of(specs: Sequence[CandidateSpec]) -> list[str]:
    """从候选集合汇总本次扫了哪些维度（V2 §48 ①）。"""
    paths: list[str] = []
    for spec in specs:
        for path in spec.changes:
            if path not in paths:
                paths.append(path)
    return [f"{path}（{PATH_LABELS.get(path, path)}）" for path in paths]


def scan_variant_changes(project: Project, variable: ScanVariable, value: float) -> dict[str, Any]:
    """把「扫描维度 + 取值」翻译成点号路径覆盖字典（V2 §46 的 8 个维度）。

    取值口径（与 OPTIMIZATION.md §7、§8 一致）：

    * ``PV_CAPACITY`` / ``STORAGE_CAPACITY`` / ``STORAGE_POWER``：**绝对值**（kWp / kWh / kW）；
    * ``STORAGE_PRICE`` / ``TARIFF`` / ``LOAD`` / ``CAPEX``：**相对变化率**（如 ``-0.2`` = −20%）；
    * ``PEAK_VALLEY_SPREAD``：峰谷价差系数 ``k``（``peak' = flat + (peak − flat)×k``、
      ``valley' = flat − (flat − valley)×k``，禁止直接给峰谷电价乘系数）。

    为保持"一次只改变一个参数"（V1 §95 红线），容量类扫描只改容量本身；
    唯一例外是储能容量取 0 时同步把功率置 0（PR1 的结构性要求）。
    """
    if variable is ScanVariable.PV_CAPACITY:
        changes: dict[str, Any] = {"pv.pv_capacity_kwp": max(float(value), 0.0)}
        if project.timeseries.pv.capacity_kwp is not None:
            # 时序侧设置了显式容量时同步调整，避免口径撕裂（V2 §105）
            changes["timeseries.pv.capacity_kwp"] = (
                float(value) if value > 0 else None
            )
        return changes
    if variable is ScanVariable.STORAGE_CAPACITY:
        return {
            "storage.storage_energy_kwh": float(value),
            "storage.storage_power_kw": (
                float(project.storage.storage_power_kw) if value > 0 else 0.0
            ),
        }
    if variable is ScanVariable.STORAGE_POWER:
        return {"storage.storage_power_kw": float(value)}
    if variable is ScanVariable.STORAGE_PRICE:
        base = float(project.investment.storage_capex_per_kwh)
        return {"investment.storage_capex_per_kwh": base * (1.0 + float(value))}
    if variable is ScanVariable.CAPEX:
        base = float(project.investment.pv_capex_per_kw)
        return {"investment.pv_capex_per_kw": base * (1.0 + float(value))}
    if variable is ScanVariable.LOAD:
        factor = 1.0 + float(value)
        changes: dict[str, Any] = {
            "load.annual_load_kwh": float(project.load.annual_load_kwh) * factor
        }
        ts_energy = float(project.timeseries.load.annual_energy_kwh)
        if ts_energy > 0.0:
            changes["timeseries.load.annual_energy_kwh"] = ts_energy * factor
        return changes
    if variable is ScanVariable.TARIFF:
        factor = 1.0 + float(value)
        return _scale_prices(project, factor)
    if variable is ScanVariable.PEAK_VALLEY_SPREAD:
        return _spread_prices(project, float(value))
    raise ScenarioError(f"不支持的扫描维度：{variable!r}（V2 §46 共 8 个）")


def _scale_prices(project: Project, factor: float) -> dict[str, Any]:
    """电价水平整体缩放（保持峰平谷结构，OPTIMIZATION.md §8 第 5 行）。"""
    changes: dict[str, Any] = {}
    for field_name in ("peak_price", "flat_price", "valley_price", "export_price"):
        changes[f"tariff.{field_name}"] = float(getattr(project.tariff, field_name)) * factor
        profile = project.timeseries.tariff.profile
        changes[f"timeseries.tariff.profile.{field_name}"] = (
            float(getattr(profile, field_name)) * factor
        )
    return changes


def _spread_prices(project: Project, k: float) -> dict[str, Any]:
    """峰谷价差情景（保持平段电价不变，按比例放大/收窄峰谷差）。"""
    if k < 0.0:
        raise ScenarioError(f"峰谷价差系数 k 不能为负：{k!r}（OPTIMIZATION.md §7.2）")
    changes: dict[str, Any] = {}
    for scope, peak, flat, valley in (
        (
            "tariff",
            float(project.tariff.peak_price),
            float(project.tariff.flat_price),
            float(project.tariff.valley_price),
        ),
        (
            "timeseries.tariff.profile",
            float(project.timeseries.tariff.profile.peak_price),
            float(project.timeseries.tariff.profile.flat_price),
            float(project.timeseries.tariff.profile.valley_price),
        ),
    ):
        changes[f"{scope}.peak_price"] = flat + (peak - flat) * k
        # 谷段电价按 0 下限截断（避免负电价触发领域模型校验）
        changes[f"{scope}.valley_price"] = max(0.0, flat - (flat - valley) * k)
    return changes


def _scan_specs(
    project: Project,
    variable: ScanVariable,
    values: Sequence[float],
) -> list[CandidateSpec]:
    specs: list[CandidateSpec] = []
    for value in values:
        changes = scan_variant_changes(project, variable, float(value))
        specs.append(
            CandidateSpec(
                run_id=f"{variable.value}={float(value):g}",
                variables={variable.value: float(value)},
                changes=changes,
                label=f"{variable.label} = {float(value):g}",
            )
        )
    return specs


def scan(
    project: Project,
    *,
    variable: ScanVariable,
    values: Sequence[float] | None = None,
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
    axis: TimeAxis | None = None,
    budget: float | None = None,
    verify_top_n: int = 1,
) -> OptimizationResult:
    """**单维扫描**：固定其余参数，只改一个维度（V2 §45、§46、V1 §95）。

    :param values: 候选值；``None`` 时取 :data:`DEFAULT_SCAN_VALUES`（规范给定候选值）。
    :param verify_top_n: 复核阶段跑精确 25 年仿真的候选个数（默认仅最优 1 个）。
    """
    resolved_values = list(values) if values is not None else list(DEFAULT_SCAN_VALUES[variable])
    if not resolved_values:
        raise ScenarioError(f"扫描维度 {variable.label} 的候选值不能为空（V2 §45）")
    specs = _scan_specs(project, variable, resolved_values)
    return evaluate_candidates(
        project,
        specs,
        objective=objective,
        axis=axis,
        budget=budget,
        verify_top_n=verify_top_n,
        extra_explanation=[
            f"本次为单维扫描（V1 §95：一次只改变一个参数）：维度「{variable.label}」"
            f"（{variable.value}），候选值 "
            f"{'、'.join(f'{v:g}' for v in resolved_values)}，其余参数保持基准值。"
        ]
        + [_scan_caliber_note(variable)],
    )


def _scan_caliber_note(variable: ScanVariable) -> str:
    if variable in (
        ScanVariable.STORAGE_PRICE,
        ScanVariable.TARIFF,
        ScanVariable.LOAD,
        ScanVariable.CAPEX,
    ):
        return (
            f"取值口径：维度「{variable.label}」的候选值为**相对变化率**"
            "（如 -0.2 表示 −20%，0 表示基准）。"
        )
    if variable is ScanVariable.PEAK_VALLEY_SPREAD:
        return (
            "取值口径：峰谷价差系数 k，按 peak' = 平段 + (峰段 − 平段)×k、"
            "valley' = 平段 − (平段 − 谷段)×k 施加，平段电价保持不变"
            "（禁止直接给峰谷电价乘系数，OPTIMIZATION.md §7.2）。"
        )
    return f"取值口径：维度「{variable.label}」的候选值为**绝对值**（容量单位）。"


def scan_grid(
    project: Project,
    grid: Mapping[str, Sequence[float]],
    *,
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
    max_candidates: int = 200,
    axis: TimeAxis | None = None,
    budget: float | None = None,
    verify_top_n: int = 1,
) -> OptimizationResult:
    """**网格扫描**：多个维度做笛卡尔积（V2 §45、§46）。

    :param grid: ``{"pv.pv_capacity_kwp": [...], "storage.storage_energy_kwh": [...]}``
    :param max_candidates: 候选数上限（§86 性能约束），超出即报中文错误。
    """
    paths = list(grid.keys())
    if not paths:
        raise ScenarioError("网格扫描至少需要一个维度（V2 §45）")
    for path in paths:
        if not grid[path]:
            raise ScenarioError(f"网格维度 {path} 的候选值不能为空（V2 §45）")

    total = 1
    for path in paths:
        total *= len(grid[path])
    if total > int(max_candidates):
        raise ScenarioError(
            f"候选组合数 {total} 超过上限 {max_candidates}：请减少候选值、"
            "分维度扫描，或显式提高 max_candidates（V2 §86 性能约束）"
        )

    specs: list[CandidateSpec] = []
    for combo in _cartesian(paths, grid):
        changes: dict[str, Any] = dict(combo)
        variables = _path_variables(changes)
        code = "_".join(
            f"{_short_name(path)}{float(changes[path]):g}" for path in paths
        )
        specs.append(
            CandidateSpec(
                run_id=code,
                variables=variables,
                changes=changes,
                label=code,
            )
        )
    return evaluate_candidates(
        project,
        specs,
        objective=objective,
        axis=axis,
        budget=budget,
        verify_top_n=verify_top_n,
        extra_explanation=[
            f"本次为网格扫描：维度 "
            + "、".join(
                f"{path}（{PATH_LABELS.get(path, path)}）× {len(grid[path])} 个取值"
                for path in paths
            )
            + f"，共 {total} 个候选（未超上限 {max_candidates}）。"
        ],
    )


def _short_name(path: str) -> str:
    return {
        "pv.pv_capacity_kwp": "PV",
        "storage.storage_energy_kwh": "E",
        "storage.storage_power_kw": "P",
        "investment.pv_capex_per_kw": "PV价",
        "investment.storage_capex_per_kwh": "储价",
        "load.annual_load_kwh": "负荷",
    }.get(path, path.split(".")[-1])


def _cartesian(
    paths: Sequence[str], grid: Mapping[str, Sequence[float]]
) -> Iterable[dict[str, float]]:
    """确定性笛卡尔积（按给定顺序，保证结果可复现，原则 O5）。"""
    if not paths:
        yield {}
        return
    head, rest = paths[0], paths[1:]
    for value in grid[head]:
        for tail in _cartesian(rest, grid):
            yield {head: float(value), **tail}


def parameter_scan(
    project: Project,
    scans: Mapping[ScanVariable, Sequence[float]],
    *,
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
    axis: TimeAxis | None = None,
    budget: float | None = None,
    verify_top_n: int = 1,
) -> OptimizationResult:
    """**参数扫描**（8 个维度，V2 §46）：每个维度独立变化，其余保持基准值。

    与方案扫描的区别：参数扫描回答"结论稳不稳"，因此**不做组合**，
    每个候选只改一个参数（V1 §95 红线）。
    """
    if not scans:
        raise ScenarioError("参数扫描至少需要一个维度（V2 §46）")
    resolved_axis = axis if axis is not None else build_axis(project)
    specs: list[CandidateSpec] = []
    for variable, values in scans.items():
        if not values:
            raise ScenarioError(f"参数扫描维度 {variable.label} 的取值不能为空")
        specs.extend(_scan_specs(project, variable, list(values)))
    return evaluate_candidates(
        project,
        specs,
        objective=objective,
        axis=resolved_axis,
        budget=budget,
        verify_top_n=verify_top_n,
        extra_explanation=[
            "本次为参数扫描（V2 §46 的 8 个维度，V1 §95：一次只改变一个参数）："
            + "、".join(f"{v.label}（{v.value}）× {len(scans[v])} 档" for v in scans)
            + f"，共 {len(specs)} 个候选，维度之间不做组合。"
        ],
    )


__all__ = [
    "DEFAULT_SCAN_VALUES",
    "MAXIMIZATION_OBJECTIVES",
    "PATH_LABELS",
    "PV_CAPACITY_VALUES",
    "RANK_NOTE",
    "RELATIVE_SCAN_VALUES",
    "SPREAD_SCAN_VALUES",
    "STORAGE_ENERGY_VALUES",
    "STORAGE_POWER_VALUES",
    "VERIFY_NOTE",
    "CandidateEvaluator",
    "CandidateSpec",
    "ScenarioError",
    "ScenarioPathError",
    "annual_operating_cost",
    "apply_variant",
    "build_baseline_scenario",
    "build_ranking_overrides",
    "build_scenario_result",
    "compare_scenarios",
    "default_constraints",
    "effective_pv_capacity",
    "evaluate_candidates",
    "evaluate_scenario",
    "get_by_path",
    "is_maximization",
    "normalize_project_type",
    "objective_metric_name",
    "objective_value",
    "parameter_scan",
    "rank_key",
    "resolved_pv_capacity",
    "roof_limit_kwp",
    "scan",
    "scan_grid",
    "scan_variant_changes",
    "scenario_code",
    "set_by_path",
    "simulate_candidate_project",
    "simulate_candidate_year",
    "standard_variants",
    "structural_issue",
]
