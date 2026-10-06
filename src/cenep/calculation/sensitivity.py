"""敏感性分析（规范 §95、§96）。

**一次只改变一个参数**，其他参数保持基准值（规范 §95）。
本模块只负责"按变量施加变化"，重算仍由引擎完成。
"""

from __future__ import annotations

from ..domain.enums import SensitivityVariable
from ..domain.models import Project


def _scale_capex(project: Project, factor: float) -> None:
    inv = project.investment
    inv.pv_capex_per_kw *= factor
    inv.pv_capex *= factor
    inv.storage_capex_per_kwh *= factor
    inv.storage_capex *= factor
    inv.grid_connection_cost *= factor
    inv.roof_cost *= factor
    inv.development_cost *= factor
    inv.engineering_cost *= factor
    inv.construction_cost *= factor
    inv.other_capex *= factor
    inv.contingency *= factor
    project.storage.replacement_capex *= factor


def _scale_prices(project: Project, factor: float) -> None:
    t = project.tariff
    for attr in (
        "average_price",
        "peak_price",
        "flat_price",
        "valley_price",
        "market_price",
        "export_price",
        "green_energy_price",
        "green_environmental_value",
    ):
        setattr(t, attr, getattr(t, attr) * factor)
    for attr in ("custom_avoided_price", "custom_charge_price", "avoided_price_override", "charge_price_override"):
        value = getattr(t, attr)
        if value is not None:
            setattr(t, attr, value * factor)
    s = project.storage
    if s.discharge_avoided_price is not None:
        s.discharge_avoided_price *= factor
    if s.charge_price is not None:
        s.charge_price *= factor


def _scale_opex(project: Project, factor: float) -> None:
    o = project.opex
    for attr in ("pv_opex", "storage_opex", "insurance", "management_cost", "other_opex", "rent_per_m2", "rent_per_kw", "annual_fixed_rent"):
        setattr(o, attr, getattr(o, attr) * factor)


def _scale_storage_capex(project: Project, factor: float) -> None:
    project.investment.storage_capex_per_kwh *= factor
    project.investment.storage_capex *= factor
    project.storage.replacement_capex *= factor


def applicable_variables(project: Project) -> list[SensitivityVariable]:
    """按项目类型过滤有意义的敏感性变量。

    无光伏的项目不做发电量/自用比例敏感性；无储能的项目不做储能相关敏感性。
    """
    ptype = project.basic_info.project_type
    out: list[SensitivityVariable] = [
        SensitivityVariable.CAPEX,
        SensitivityVariable.ELECTRICITY_PRICE,
        SensitivityVariable.OPEX,
    ]
    if ptype.has_pv:
        out.insert(2, SensitivityVariable.GENERATION)
        out.append(SensitivityVariable.SELF_CONSUMPTION_RATIO)
    if ptype.has_storage:
        out.append(SensitivityVariable.STORAGE_CYCLES)
        out.append(SensitivityVariable.STORAGE_CAPEX)
    if project.financing.enabled and project.financing.debt_ratio > 0:
        out.append(SensitivityVariable.INTEREST_RATE)
    return out


def apply_variable(project: Project, variable: SensitivityVariable, change: float) -> Project:
    """在基准项目上施加单变量变化（规范 §95）。``change = 0.1`` 表示 +10%。"""
    p = project.model_copy(deep=True)
    factor = 1.0 + float(change)

    if variable == SensitivityVariable.CAPEX:
        _scale_capex(p, factor)
    elif variable == SensitivityVariable.ELECTRICITY_PRICE:
        _scale_prices(p, factor)
    elif variable == SensitivityVariable.GENERATION:
        p.pv.equivalent_hours *= factor
    elif variable == SensitivityVariable.OPEX:
        _scale_opex(p, factor)
    elif variable == SensitivityVariable.SELF_CONSUMPTION_RATIO:
        p.pv.self_consumption_ratio = max(0.0, min(1.0, p.pv.self_consumption_ratio * factor))
    elif variable == SensitivityVariable.STORAGE_CYCLES:
        p.storage.annual_cycles *= factor
    elif variable == SensitivityVariable.STORAGE_CAPEX:
        _scale_storage_capex(p, factor)
    elif variable == SensitivityVariable.INTEREST_RATE:
        p.financing.interest_rate *= factor
    else:  # pragma: no cover - 枚举已覆盖
        raise ValueError(f"不支持的敏感性变量：{variable}")
    return p
