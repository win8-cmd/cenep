"""投资（CAPEX）模块（规范 §48–§52、§146）。

总投资（规范 §49）：

``TotalCAPEX = PV + Storage + GridConnection + Roof + Development + Engineering
             + Construction + Other + Contingency``

两种模式（规范 §52）互斥：

* ``UNIT_PRICE``：``PV_CAPEX = PVCapacity × PV_CAPEX_PerKW``（§50）
  ``Storage_CAPEX = StorageEnergy × StorageCAPEXPerKWh``（§51）
* ``DETAILED``：各项投资直接相加（容量相关项不再按单价计算）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ValidationError


@dataclass(frozen=True)
class CapexBreakdown:
    """CAPEX 明细（单位：元）。"""

    pv_capex: float = 0.0
    storage_capex: float = 0.0
    grid_connection_cost: float = 0.0
    roof_cost: float = 0.0
    development_cost: float = 0.0
    engineering_cost: float = 0.0
    construction_cost: float = 0.0
    other_capex: float = 0.0
    contingency: float = 0.0
    mode: str = "UNIT_PRICE"
    pv_capex_per_kw: float = 0.0
    storage_capex_per_kwh: float = 0.0
    items: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        """总投资（规范 §49）。"""
        return (
            self.pv_capex
            + self.storage_capex
            + self.grid_connection_cost
            + self.roof_cost
            + self.development_cost
            + self.engineering_cost
            + self.construction_cost
            + self.other_capex
            + self.contingency
        )


def compute_capex(
    mode: str,
    pv_capacity_kwp: float = 0.0,
    storage_energy_kwh: float = 0.0,
    pv_capex_per_kw: float = 0.0,
    storage_capex_per_kwh: float = 0.0,
    pv_capex: float = 0.0,
    storage_capex: float = 0.0,
    grid_connection_cost: float = 0.0,
    roof_cost: float = 0.0,
    development_cost: float = 0.0,
    engineering_cost: float = 0.0,
    construction_cost: float = 0.0,
    other_capex: float = 0.0,
    contingency: float = 0.0,
    detailed_items: dict[str, float] | None = None,
) -> CapexBreakdown:
    """计算 CAPEX（规范 §49–§52）。

    ``mode = UNIT_PRICE``：光伏/储能投资按 ``容量 × 单价`` 计算，其余各项直接相加。
    ``mode = DETAILED``：光伏/储能投资取用户填写的金额（``pv_capex`` / ``storage_capex``）。
    """
    m = str(mode).upper()
    if m not in {"UNIT_PRICE", "DETAILED"}:
        raise ValidationError(f"不支持的投资模式：{mode}（只能是 UNIT_PRICE 或 DETAILED）", field="investment_mode")

    if m == "UNIT_PRICE":
        pv = float(pv_capacity_kwp) * float(pv_capex_per_kw)
        storage = float(storage_energy_kwh) * float(storage_capex_per_kwh)
    else:
        pv = float(pv_capex)
        storage = float(storage_capex)

    if pv < 0 or storage < 0:
        raise ValidationError("光伏/储能投资不能为负数", field="capex")

    return CapexBreakdown(
        pv_capex=pv,
        storage_capex=storage,
        grid_connection_cost=float(grid_connection_cost),
        roof_cost=float(roof_cost),
        development_cost=float(development_cost),
        engineering_cost=float(engineering_cost),
        construction_cost=float(construction_cost),
        other_capex=float(other_capex),
        contingency=float(contingency),
        mode=m,
        pv_capex_per_kw=float(pv_capex_per_kw),
        storage_capex_per_kwh=float(storage_capex_per_kwh),
        items=dict(detailed_items or {}),
    )


def unit_investment(total_capex: float, pv_capacity_kwp: float, storage_energy_kwh: float) -> dict[str, float]:
    """单位投资指标（规范 §102：投资页面需自动显示单位投资）。

    返回 元/W（光伏，按 DC 容量）与 元/Wh（储能）。无对应容量时该键为 0.0。
    """
    out = {"yuan_per_w": 0.0, "yuan_per_wh": 0.0}
    if pv_capacity_kwp > 0:
        out["yuan_per_w"] = float(total_capex) / (float(pv_capacity_kwp) * 1000.0)
    if storage_energy_kwh > 0:
        out["yuan_per_wh"] = float(total_capex) / (float(storage_energy_kwh) * 1000.0)
    return out
