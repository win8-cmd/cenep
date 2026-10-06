"""情景分析（规范 §92–§94）。

**规范 §93：所有情景必须从 BASE 复制**，不允许"保守 → 乐观"链式推导。
本模块只负责"在基准项目上施加一套显式乘数"，返回新的 :class:`Project`；计算公式仍由引擎执行。
"""

from __future__ import annotations

from ..domain.enums import ScenarioType
from ..domain.models import Project, ScenarioDelta
from .errors import ScenarioError


def _clamp_ratio(value: float) -> float:
    return max(0.0, min(1.0, value))


def apply_delta(project: Project, delta: ScenarioDelta) -> Project:
    """把情景乘数施加到**基准项目**上（规范 §93、§94）。

    施加范围（全部显式，无隐藏逻辑）：

    * ``capex_multiplier``：光伏/储能单位投资、DETAILED 各项投资、预备费
    * ``storage_capex_multiplier``：在 capex 乘数基础上**再乘**储能相关投资
    * ``electricity_price_multiplier``：固定/峰/平/谷/市场/自定义/覆盖价、上网电价、绿电与环境价值
    * ``generation_multiplier``：年等效利用小时
    * ``opex_multiplier``：各项运维费用数值
    * ``self_consumption_ratio_multiplier``：自用比例（结果裁剪到 0~1）
    * ``storage_cycles_multiplier``：年循环次数
    * ``interest_rate_multiplier``：贷款利率
    """
    if project is None:
        raise ScenarioError("情景分析需要基准项目", field="project")

    p = project.model_copy(deep=True)

    # ---- CAPEX ----
    inv = p.investment
    inv.pv_capex_per_kw *= delta.capex_multiplier
    inv.pv_capex *= delta.capex_multiplier
    inv.grid_connection_cost *= delta.capex_multiplier
    inv.roof_cost *= delta.capex_multiplier
    inv.development_cost *= delta.capex_multiplier
    inv.engineering_cost *= delta.capex_multiplier
    inv.construction_cost *= delta.capex_multiplier
    inv.other_capex *= delta.capex_multiplier
    inv.contingency *= delta.capex_multiplier

    storage_capex_factor = delta.capex_multiplier * delta.storage_capex_multiplier
    inv.storage_capex_per_kwh *= storage_capex_factor
    inv.storage_capex *= storage_capex_factor
    p.storage.replacement_capex *= storage_capex_factor

    # ---- 电价 ----
    price_factor = delta.electricity_price_multiplier
    t = p.tariff
    t.average_price *= price_factor
    t.peak_price *= price_factor
    t.flat_price *= price_factor
    t.valley_price *= price_factor
    t.market_price *= price_factor
    t.export_price *= price_factor
    t.green_energy_price *= price_factor
    t.green_environmental_value *= price_factor
    if t.custom_avoided_price is not None:
        t.custom_avoided_price *= price_factor
    if t.custom_charge_price is not None:
        t.custom_charge_price *= price_factor
    if t.avoided_price_override is not None:
        t.avoided_price_override *= price_factor
    if t.charge_price_override is not None:
        t.charge_price_override *= price_factor

    s = p.storage
    if s.discharge_avoided_price is not None:
        s.discharge_avoided_price *= price_factor
    if s.charge_price is not None:
        s.charge_price *= price_factor

    # ---- 发电量 ----
    p.pv.equivalent_hours *= delta.generation_multiplier

    # ---- 运维成本 ----
    o = p.opex
    o.pv_opex *= delta.opex_multiplier
    o.storage_opex *= delta.opex_multiplier
    o.insurance *= delta.opex_multiplier
    o.management_cost *= delta.opex_multiplier
    o.other_opex *= delta.opex_multiplier
    o.rent_per_m2 *= delta.opex_multiplier
    o.rent_per_kw *= delta.opex_multiplier
    o.annual_fixed_rent *= delta.opex_multiplier

    # ---- 自用比例 / 循环次数 / 利率 ----
    p.pv.self_consumption_ratio = _clamp_ratio(
        p.pv.self_consumption_ratio * delta.self_consumption_ratio_multiplier
    )
    p.storage.annual_cycles *= delta.storage_cycles_multiplier
    if p.financing.enabled:
        p.financing.interest_rate *= delta.interest_rate_multiplier

    return p


def delta_for(project: Project, scenario: ScenarioType) -> ScenarioDelta:
    """取情景对应的乘数集合（规范 §92）。BASE 返回全 1。"""
    if scenario == ScenarioType.BASE:
        return ScenarioDelta()
    if scenario == ScenarioType.CONSERVATIVE:
        return project.scenario.conservative
    if scenario == ScenarioType.OPTIMISTIC:
        return project.scenario.optimistic
    raise ScenarioError(f"不支持的情景类型：{scenario}", field="scenario")
