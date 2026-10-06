"""规则型优化器（V2 §45、§46；OPTIMIZATION.md §4）。

**定位**：不做搜索，直接依据工程规则 R1–R7 给出推荐容量（秒级返回），
再在推荐点附近做**小规模网格扫描**，逐候选调用
:func:`~cenep.calculation.scenario_engine.evaluate_scenario` 得到指标，
按目标函数选出最优方案。

规则清单（OPTIMIZATION.md §4.1）
-------------------------------

======  ============================  ==========================================
编号    规则                          公式
======  ============================  ==========================================
R1      屋顶可装上限                  ``PV_max = 可利用屋顶面积 ÷ 单位容量占用面积``
R2      负荷匹配                      ``PV_rec = 年用电量 × 目标自用率 ÷ 年等效利用小时``
R3      变压器容量约束                ``PV_rec ≤ 变压器容量 × 允许渗透率``（未配置时跳过）
R4      逆变器容配比                  ``AC = PV_dc ÷ 容配比``（默认 1.1，仅供参考）
R5      储能容量（负荷侧）            ``E_rec = 典型日峰段用电量 × 目标削峰比例``
R6      储能功率                      ``P_rec = E_rec ÷ 目标时长``（默认 2 小时）
R7      储能时长下限                  ``P_rec ≥ E_rec ÷ 4 小时``
======  ============================  ==========================================

最终建议：``PV_rec = min(R1, R2, R3)``、``E_rec = R5``、``P_rec = max(R6, R7)``。
``rule_trace`` 给出每条规则的输入值、公式与结果（V2 §48 的可解释性要求）。

所有财务口径复用唯一计算引擎，本模块只生成候选与比较（原则 O2）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..calculation import load_profile as load_mod
from ..calculation import scenario_engine as se
from ..calculation import tariff_series as tariff_mod
from ..calculation.timeseries_engine import TimeAxis
from ..domain.enums import OptimizationObjective, TariffPeriod
from ..domain.models import Project
from ..domain.timeseries_results import OptimizationResult
from ..infrastructure.logging_setup import get_logger

logger = get_logger()

#: 默认目标时长（小时），OPTIMIZATION.md §4.1 R6
DEFAULT_TARGET_DURATION_H = 2.0
#: 默认目标削峰比例，OPTIMIZATION.md §4.1 R5
DEFAULT_PEAK_SHAVING_RATIO = 0.3
#: 逆变器容配比默认值，OPTIMIZATION.md §4.1 R4
DEFAULT_DC_AC_RATIO = 1.1


@dataclass(frozen=True)
class RuleTrace:
    """一条规则的输入、公式与结果（V2 §48 的可解释性要求）。"""

    rule: str
    formula: str
    value: float
    unit: str
    inputs: dict[str, float] = field(default_factory=dict)
    applicable: bool = True
    note: str = ""

    def describe(self) -> str:
        if not self.applicable:
            return f"{self.rule} {self.formula}：不适用（{self.note}）"
        inputs = "、".join(f"{k}={v:g}" for k, v in self.inputs.items())
        return (
            f"{self.rule} {self.formula}：{self.value:,.2f} {self.unit}"
            + (f"（输入：{inputs}）" if inputs else "")
        )


@dataclass(frozen=True)
class RuleRecommendation:
    """规则型容量建议（OPTIMIZATION.md §4.2）。"""

    pv_capacity_kwp: float
    storage_energy_kwh: float
    storage_power_kw: float
    rule_trace: list[RuleTrace]
    binding_rule: str

    def describe(self) -> list[str]:
        return [trace.describe() for trace in self.rule_trace]


def _days_in_axis(axis: TimeAxis) -> float:
    delta = float(getattr(axis, "delta_hours", 1.0)) or 1.0
    return max(axis.point_count * delta / 24.0, 1.0)


def recommend(
    project: Project,
    *,
    axis: TimeAxis | None = None,
    target_self_consumption: float | None = None,
    peak_shaving_ratio: float = DEFAULT_PEAK_SHAVING_RATIO,
    target_duration_h: float = DEFAULT_TARGET_DURATION_H,
    dc_ac_ratio: float = DEFAULT_DC_AC_RATIO,
) -> RuleRecommendation:
    """按 R1–R7 给出容量建议与完整规则轨迹（OPTIMIZATION.md §4）。

    :param target_self_consumption: R2 的目标自用率；``None`` 时取项目配置的自用比例。
    :param peak_shaving_ratio: R5 的目标削峰比例（相对典型日峰段用电量）。
    :param target_duration_h: R6 的目标储能时长（小时）。
    """
    resolved_axis = axis if axis is not None else se.build_axis(project)
    trace: list[RuleTrace] = []

    # ---- R1 屋顶可装上限 ----
    roof_limit = se.roof_limit_kwp(project)
    if roof_limit is not None:
        trace.append(
            RuleTrace(
                rule="R1",
                formula="屋顶可装上限 = 可利用屋顶面积 ÷ 单位容量占用面积",
                value=roof_limit,
                unit="kWp",
                inputs={
                    "可利用屋顶面积(m²)": float(project.pv.usable_roof_area_m2),
                    "单位容量占用面积(m²/kWp)": float(project.pv.area_per_kwp),
                },
            )
        )
    else:
        trace.append(
            RuleTrace(
                rule="R1",
                formula="屋顶可装上限",
                value=0.0,
                unit="kWp",
                applicable=False,
                note="未配置可利用屋顶面积或单位容量占用面积",
            )
        )

    # ---- R2 负荷匹配 ----
    share = (
        float(target_self_consumption)
        if target_self_consumption is not None
        else float(project.pv.self_consumption_ratio)
    )
    equivalent_hours = float(project.pv.equivalent_hours)
    annual_load = float(project.load.annual_load_kwh)
    load_match = annual_load * share / equivalent_hours if equivalent_hours > 0 else 0.0
    trace.append(
        RuleTrace(
            rule="R2",
            formula="负荷匹配 = 年用电量 × 目标自用率 ÷ 年等效利用小时",
            value=load_match,
            unit="kWp",
            inputs={
                "年用电量(kWh)": annual_load,
                "目标自用率": share,
                "年等效利用小时(h)": equivalent_hours,
            },
        )
    )

    # ---- R3 变压器容量约束（V1 未建模变压器容量，标记为不适用）----
    trace.append(
        RuleTrace(
            rule="R3",
            formula="变压器容量约束 = 变压器容量 × 允许渗透率",
            value=0.0,
            unit="kWp",
            applicable=False,
            note="项目参数未包含变压器容量，跳过该约束",
        )
    )

    if roof_limit is not None:
        pv_recommended = min(roof_limit, load_match)
        binding_rule = "R1" if roof_limit < load_match else "R2"
    else:
        pv_recommended = load_match
        binding_rule = "R2"
    if pv_recommended <= 0.0:
        # 规则无法给出正容量时退回项目现有容量（避免生成非法候选）
        pv_recommended = se.resolved_pv_capacity(project)

    # ---- R4 逆变器容配比（信息项）----
    ratio = float(dc_ac_ratio) if float(dc_ac_ratio) > 0 else DEFAULT_DC_AC_RATIO
    trace.append(
        RuleTrace(
            rule="R4",
            formula="逆变器交流侧容量 = 直流容量 ÷ 容配比（信息项，不参与取小）",
            value=pv_recommended / ratio,
            unit="kW",
            inputs={"容配比": ratio},
        )
    )

    # ---- R5 储能容量（典型日峰段用电量 × 目标削峰比例）----
    load_series = load_mod.resolve_load(project.timeseries.load, resolved_axis, 1)
    tariff = tariff_mod.resolve_tariff(project.timeseries.tariff, resolved_axis, 1)
    codes = np.asarray(tariff.period_code).astype(str)
    peak_mask = np.isin(
        codes, [TariffPeriod.PEAK.value, TariffPeriod.SHARP_PEAK.value]
    )
    peak_energy = float(np.sum(load_series[peak_mask])) if peak_mask.any() else 0.0
    days = _days_in_axis(resolved_axis)
    daily_peak = peak_energy / days
    shaving = max(0.0, float(peak_shaving_ratio))
    storage_energy = daily_peak * shaving
    trace.append(
        RuleTrace(
            rule="R5",
            formula="储能容量 = 典型日峰段用电量 × 目标削峰比例",
            value=storage_energy,
            unit="kWh",
            inputs={
                "全年峰段用电量(kWh)": peak_energy,
                "天数": days,
                "典型日峰段用电量(kWh)": daily_peak,
                "目标削峰比例": shaving,
            },
        )
    )

    # ---- R6 / R7 储能功率与时长下限 ----
    duration = float(target_duration_h) if float(target_duration_h) > 0 else DEFAULT_TARGET_DURATION_H
    power_by_duration = storage_energy / duration
    power_floor = storage_energy / 4.0  # 时长不超过 4 小时
    storage_power = max(power_by_duration, power_floor)
    trace.append(
        RuleTrace(
            rule="R6",
            formula="储能功率 = 储能容量 ÷ 目标时长",
            value=power_by_duration,
            unit="kW",
            inputs={"目标时长(h)": duration},
        )
    )
    trace.append(
        RuleTrace(
            rule="R7",
            formula="储能功率下限 = 储能容量 ÷ 4 小时",
            value=power_floor,
            unit="kW",
        )
    )
    if storage_energy <= 0.0:
        # 无峰段负荷或无削峰需求：退回项目现有储能配置
        storage_energy = float(project.storage.storage_energy_kwh)
        storage_power = float(project.storage.storage_power_kw)

    logger.debug(
        "规则型建议：PV=%.1f kWp（约束规则 %s）、E=%.1f kWh、P=%.1f kW",
        pv_recommended,
        binding_rule,
        storage_energy,
        storage_power,
    )
    return RuleRecommendation(
        pv_capacity_kwp=pv_recommended,
        storage_energy_kwh=storage_energy,
        storage_power_kw=storage_power,
        rule_trace=trace,
        binding_rule=binding_rule,
    )


def _dedupe(values: list[float], *, positive_only: bool = False) -> list[float]:
    out: list[float] = []
    for value in values:
        value = float(value)
        if positive_only and value <= 0.0:
            continue
        if not any(abs(value - existing) < 1e-9 for existing in out):
            out.append(value)
    return out


def default_scan_values(
    project: Project,
    recommendation: RuleRecommendation,
    *,
    span: float = 0.2,
) -> tuple[list[float], list[float], list[float]]:
    """规则建议附近的**小规模**候选值（光伏 ±``span``、储能 0 与建议值）。

    候选规模控制在个位数，符合 OPTIMIZATION.md §2.1「规则型：启发式、秒级返回」。
    """
    pv = max(recommendation.pv_capacity_kwp, 0.0)
    roof_limit = se.roof_limit_kwp(project)
    pv_values = _dedupe(
        [
            pv,
            pv * (1.0 - span),
            pv * (1.0 + span),
        ],
        positive_only=True,
    )
    if roof_limit is not None:
        pv_values = [min(v, roof_limit) for v in pv_values]
    pv_values = _dedupe(pv_values, positive_only=True)
    energy_values = _dedupe([0.0, recommendation.storage_energy_kwh])
    power_values = _dedupe(
        [p for p in [recommendation.storage_power_kw] if p > 0.0]
    )
    if not power_values:
        power_values = [0.0]
    return pv_values, energy_values, power_values


def optimize(
    project: Project,
    *,
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV,
    pv_values: list[float] | None = None,
    storage_energy_values: list[float] | None = None,
    storage_power_values: list[float] | None = None,
    axis: TimeAxis | None = None,
    budget: float | None = None,
    verify_top_n: int = 1,
    target_self_consumption: float | None = None,
    peak_shaving_ratio: float = DEFAULT_PEAK_SHAVING_RATIO,
    target_duration_h: float = DEFAULT_TARGET_DURATION_H,
) -> OptimizationResult:
    """规则型寻优：R1–R7 建议 + 小规模网格扫描（V2 §45、§46）。

    :param pv_values: 光伏容量候选（kWp）；``None`` 时取规则建议值 ±20%。
    :param storage_energy_values: 储能容量候选（kWh）；``None`` 时取 ``{0, 建议值}``。
    :param storage_power_values: 储能功率候选（kW）；``None`` 时取规则建议功率。
    :param verify_top_n: 复核阶段跑精确全周期仿真的候选个数。
    """
    resolved_axis = axis if axis is not None else se.build_axis(project)
    recommendation = recommend(
        project,
        axis=resolved_axis,
        target_self_consumption=target_self_consumption,
        peak_shaving_ratio=peak_shaving_ratio,
        target_duration_h=target_duration_h,
    )
    default_pv, default_energy, default_power = default_scan_values(project, recommendation)
    pv_grid = _dedupe(list(pv_values) if pv_values is not None else default_pv, positive_only=True)
    energy_grid = _dedupe(
        list(storage_energy_values) if storage_energy_values is not None else default_energy
    )
    power_grid = _dedupe(
        list(storage_power_values) if storage_power_values is not None else default_power
    )
    if not pv_grid or not energy_grid or not power_grid:
        raise se.ScenarioError("规则型寻优的候选值不能为空（V2 §45）")

    specs: list[se.CandidateSpec] = []
    for pv in pv_grid:
        for energy in energy_grid:
            for power in power_grid:
                # PR1：储能容量为 0 时功率必须为 0（否则该组合结构性无效）
                effective_power = power if energy > 0.0 else 0.0
                code = se.scenario_code(pv, energy, effective_power)
                specs.append(
                    se.CandidateSpec(
                        run_id=f"RULE-{code}",
                        variables={
                            "pv.pv_capacity_kwp": pv,
                            "storage.storage_energy_kwh": energy,
                            "storage.storage_power_kw": effective_power,
                        },
                        changes={
                            "pv.pv_capacity_kwp": pv,
                            "storage.storage_energy_kwh": energy,
                            "storage.storage_power_kw": effective_power,
                        },
                        label=f"规则型候选 {code}",
                    )
                )

    explanation = [
        "规则型优化（OPTIMIZATION.md §4）：先按 R1–R7 给出容量建议，再在建议点附近做"
        f"小规模网格扫描（光伏 {len(pv_grid)} 档 × 储能容量 {len(energy_grid)} 档 × "
        f"储能功率 {len(power_grid)} 档，共 {len(specs)} 个候选）。",
        "规则建议：光伏 "
        f"{recommendation.pv_capacity_kwp:,.2f} kWp（起决定作用的规则 {recommendation.binding_rule}）、"
        f"储能 {recommendation.storage_energy_kwh:,.2f} kWh / "
        f"{recommendation.storage_power_kw:,.2f} kW。",
    ]
    explanation.extend(f"规则轨迹 {line}" for line in recommendation.describe())
    explanation.append(
        "规则型候选由工程规则给定，属于**启发式**方案；若需网格内严格最优，"
        "请使用贪心经济优化（greedy_optimizer），连续最优请使用线性规划（lp_optimizer）。"
    )
    result = se.evaluate_candidates(
        project,
        _prune_duplicate_specs(specs),
        objective=objective,
        axis=resolved_axis,
        budget=budget,
        verify_top_n=verify_top_n,
        scan_variables=[
            "pv.pv_capacity_kwp（光伏容量）",
            "storage.storage_energy_kwh（储能容量）",
            "storage.storage_power_kw（储能功率）",
        ],
        extra_explanation=explanation,
    )
    result.explanation.insert(
        0,
        f"规则型寻优（V2 §45、§46）：目标「{objective.label}」，"
        f"规则建议 {recommendation.pv_capacity_kwp:,.1f} kWp / "
        f"{recommendation.storage_energy_kwh:,.1f} kWh / "
        f"{recommendation.storage_power_kw:,.1f} kW。",
    )
    return result


def _prune_duplicate_specs(specs: list[se.CandidateSpec]) -> list[se.CandidateSpec]:
    """去掉取值完全相同的候选（OPTIMIZATION.md §5.2 G2）。"""
    seen: set[tuple[tuple[str, float], ...]] = set()
    out: list[se.CandidateSpec] = []
    for spec in specs:
        key = spec.value_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


__all__ = [
    "DEFAULT_DC_AC_RATIO",
    "DEFAULT_PEAK_SHAVING_RATIO",
    "DEFAULT_TARGET_DURATION_H",
    "RuleRecommendation",
    "RuleTrace",
    "default_scan_values",
    "optimize",
    "recommend",
]
