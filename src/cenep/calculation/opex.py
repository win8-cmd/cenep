"""运维费用（OPEX）模块（规范 §53–§56、§146）。

* 首年运维费（§54）：按投资比例 ``InitialCAPEX × OpexRate``，或固定金额 ``FixedOpex``
* 逐年增长（§55）：``Opex_n = Opex_1 × (1+g)^(n-1)``
* 屋顶租金（§56）：面积模式 / 容量模式 / 固定租金
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError

FIXED = "FIXED"
RATIO_OF_CAPEX = "RATIO_OF_CAPEX"

ROOF_RENT_AREA = "AREA"
ROOF_RENT_CAPACITY = "CAPACITY"
ROOF_RENT_FIXED = "FIXED"


@dataclass(frozen=True)
class OpexItem:
    """单项运维费用。``amount`` 单位：元/年。"""

    name: str
    amount: float
    mode: str = FIXED


@dataclass(frozen=True)
class OpexBreakdown:
    """首年 OPEX 明细（单位：元）。"""

    pv_opex: float
    storage_opex: float
    roof_rent: float
    insurance: float
    management_cost: float
    other_opex: float
    growth_rate: float

    @property
    def first_year_total(self) -> float:
        """首年 OPEX 合计（规范 §54）。"""
        return (
            self.pv_opex
            + self.storage_opex
            + self.roof_rent
            + self.insurance
            + self.management_cost
            + self.other_opex
        )

    def as_items(self) -> list[tuple[str, float]]:
        return [
            ("光伏运维费", self.pv_opex),
            ("储能运维费", self.storage_opex),
            ("屋顶租金", self.roof_rent),
            ("保险费", self.insurance),
            ("管理费", self.management_cost),
            ("其他费用", self.other_opex),
        ]


def resolve_opex_item(
    value: float,
    mode: str = FIXED,
    capex_base: float = 0.0,
    field: str = "opex",
) -> float:
    """解析单项 OPEX（规范 §54）。"""
    m = str(mode).upper()
    if m == FIXED:
        amount = float(value)
    elif m == RATIO_OF_CAPEX:
        amount = float(capex_base) * float(value)
    else:
        raise ValidationError(f"不支持的运维费用模式：{mode}", field=field)
    if amount < 0:
        raise ValidationError("运维费用不能为负数", field=field)
    return amount


def roof_rent(
    mode: str,
    roof_area_m2: float = 0.0,
    rent_per_m2: float = 0.0,
    pv_capacity_kwp: float = 0.0,
    rent_per_kw: float = 0.0,
    annual_fixed_rent: float = 0.0,
) -> float:
    """屋顶租金（规范 §56）。

    * ``AREA``：``RoofArea × RentPerM2``
    * ``CAPACITY``：``PVCapacity × RentPerKW``
    * ``FIXED``：``AnnualFixedRent``
    """
    m = str(mode).upper()
    if m == ROOF_RENT_AREA:
        return float(roof_area_m2) * float(rent_per_m2)
    if m == ROOF_RENT_CAPACITY:
        return float(pv_capacity_kwp) * float(rent_per_kw)
    if m == ROOF_RENT_FIXED:
        return float(annual_fixed_rent)
    raise ValidationError(f"不支持的屋顶租金模式：{mode}", field="roof_rent_mode")


def opex_for_year(first_year_opex: float, annual_opex_growth_rate: float, year: int) -> float:
    """第 ``year`` 年 OPEX（规范 §55）：``Opex_1 × (1+g)^(n-1)``。"""
    if year < 1:
        raise ValueError("year 必须从 1 开始（Year 0 为建设期）")
    return float(first_year_opex) * (1.0 + float(annual_opex_growth_rate)) ** (year - 1)
