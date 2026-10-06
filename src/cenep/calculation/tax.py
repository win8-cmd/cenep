"""折旧与税务模块（规范 §57–§61、§146）。

V1 采用**简化财务模型**（规范 §59）。本模块的所有简化口径都写在下面，
报告中必须原样披露，不得把简化模型描述为正式税务计算。

折旧（§57、§58）
----------------
* 方法：直线法 ``STRAIGHT_LINE``
* ``DepreciableBase = DepreciableCAPEX × (1 - ResidualValueRatio)``
* ``AnnualDepreciation = DepreciableBase / DepreciationYears``

税务简化口径（V1 明确声明）
--------------------------
1. ``Revenue`` 默认按**不含税**口径参与利润表；若用户勾选
   ``revenue_is_vat_inclusive``，则先按 ``RevenueNet = Revenue / (1 + vat_rate)`` 换算。
2. 增值税（销项/进项抵扣/留抵退税）**不在 V1 现金流中建模**，``vat_rate`` 仅用于
   含税↔不含税换算与报表展示。**TODO(V2)**：进项抵扣与留抵退税建模。
3. 附加税费（``surcharge_rate``）与 ``other_tax_rate`` 以**不含税收入**为基数简化计算，
   计入 ``CashTax``。
4. 所得税：``TaxableIncome = max(EBT, 0)``，``IncomeTax = TaxableIncome × rate``（§61）。
   **V1 不建模亏损跨年弥补**。**TODO(V2)**：亏损结转 5 年。
5. 三免三减半、西部大开发等税收优惠**不内置**，通过 ``income_tax_rate`` 直接体现，
   并在参数来源中标注为"政策/假设"。
"""

from __future__ import annotations

from dataclasses import dataclass

STRAIGHT_LINE = "STRAIGHT_LINE"


@dataclass(frozen=True)
class TaxYear:
    """某一年度的折旧与税务结果（单位：元）。"""

    year: int
    depreciation: float
    ebitda: float
    ebit: float
    ebt: float
    taxable_income: float
    income_tax: float
    surcharge: float
    other_tax: float

    @property
    def cash_tax(self) -> float:
        """现金税费合计 = 所得税 + 附加税费 + 其他税费。"""
        return self.income_tax + self.surcharge + self.other_tax


def depreciable_base(depreciable_capex: float, residual_value_ratio: float) -> float:
    """应折旧基数（规范 §58）：``DepreciableCAPEX × (1 - ResidualValueRatio)``。"""
    return float(depreciable_capex) * (1.0 - float(residual_value_ratio))


def annual_depreciation(
    depreciable_capex: float,
    residual_value_ratio: float,
    depreciation_years: int,
) -> float:
    """年折旧额（规范 §58）：``DepreciableBase / DepreciationYears``。"""
    if depreciation_years <= 0:
        raise ValueError("折旧年限必须大于 0")
    return depreciable_base(depreciable_capex, residual_value_ratio) / float(depreciation_years)


def depreciation_for_year(
    depreciable_capex: float,
    residual_value_ratio: float,
    depreciation_years: int,
    year: int,
    method: str = STRAIGHT_LINE,
) -> float:
    """第 ``year`` 年折旧额；超过折旧年限后为 0。"""
    if str(method).upper() != STRAIGHT_LINE:
        raise ValueError(f"V1 只支持直线法折旧，收到：{method}")
    if year < 1:
        raise ValueError("year 必须从 1 开始（Year 0 为建设期）")
    if year > int(depreciation_years):
        return 0.0
    return annual_depreciation(depreciable_capex, residual_value_ratio, depreciation_years)


def residual_value(depreciable_capex: float, residual_value_ratio: float) -> float:
    """寿命期末固定资产余值（规范 §67 中的 ``ResidualValue``）。"""
    return float(depreciable_capex) * float(residual_value_ratio)


def revenue_net(revenue: float, vat_rate: float, revenue_is_vat_inclusive: bool) -> float:
    """不含税收入口径换算（V1 简化口径，见模块说明第 1 条）。"""
    if revenue_is_vat_inclusive:
        return float(revenue) / (1.0 + float(vat_rate))
    return float(revenue)


def ebitda_of(revenue: float, opex: float) -> float:
    """``EBITDA = Revenue - Opex``（规范 §60）。"""
    return float(revenue) - float(opex)


def ebit_of(ebitda: float, depreciation: float) -> float:
    """``EBIT = EBITDA - Depreciation``（规范 §60）。"""
    return float(ebitda) - float(depreciation)


def ebt_of(ebit: float, interest: float) -> float:
    """``EBT = EBIT - Interest``（规范 §60）。"""
    return float(ebit) - float(interest)


def taxable_income_of(ebt: float) -> float:
    """``TaxableIncome = max(EBT, 0)``（规范 §61）。"""
    return max(float(ebt), 0.0)


def income_tax_of(taxable_income: float, income_tax_rate: float) -> float:
    """``IncomeTax = TaxableIncome × IncomeTaxRate``（规范 §61）。"""
    return float(taxable_income) * float(income_tax_rate)


def surcharge_of(revenue_net_amount: float, surcharge_rate: float) -> float:
    """附加税费（V1 简化：以不含税收入为基数）。"""
    return float(revenue_net_amount) * float(surcharge_rate)


def other_tax_of(revenue_net_amount: float, other_tax_rate: float) -> float:
    """其他税费（V1 简化：以不含税收入为基数）。"""
    return float(revenue_net_amount) * float(other_tax_rate)


def tax_year_result(
    year: int,
    revenue_net_amount: float,
    opex: float,
    depreciation: float,
    interest: float,
    income_tax_rate: float,
    surcharge_rate: float,
    other_tax_rate: float,
) -> TaxYear:
    """按规范 §60、§61 串起一年的利润与税。"""
    ebitda = ebitda_of(revenue_net_amount, opex)
    ebit = ebit_of(ebitda, depreciation)
    ebt = ebt_of(ebit, interest)
    taxable = taxable_income_of(ebt)
    return TaxYear(
        year=year,
        depreciation=float(depreciation),
        ebitda=ebitda,
        ebit=ebit,
        ebt=ebt,
        taxable_income=taxable,
        income_tax=income_tax_of(taxable, income_tax_rate),
        surcharge=surcharge_of(revenue_net_amount, surcharge_rate),
        other_tax=other_tax_of(revenue_net_amount, other_tax_rate),
    )
