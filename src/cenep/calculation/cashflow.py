"""现金流模块（规范 §66–§69、§146）。

规范 §66 要求**同时**建立两套现金流：

项目现金流（§67，融资前，用于项目IRR）
    ``ProjectCF = EBITDA - CashTax - CAPEX - ReplacementCAPEX + ResidualValue``

资本金现金流（§68）
    ``EquityCF = EBITDA - CashTax - Interest - PrincipalRepayment - EquityCAPEX
                 - ReplacementCAPEX + DebtDrawdown + ResidualValue``

Year 0（§69）
    ``ProjectCF_0 = -TotalCAPEX``；``EquityCF_0 = -EquityAmount``
"""

from __future__ import annotations


def project_cashflow(
    ebitda: float,
    cash_tax: float,
    capex: float,
    replacement_capex: float = 0.0,
    residual_value: float = 0.0,
) -> float:
    """年度项目现金流（规范 §67）。"""
    return float(ebitda) - float(cash_tax) - float(capex) - float(replacement_capex) + float(
        residual_value
    )


def equity_cashflow(
    ebitda: float,
    cash_tax: float,
    interest: float,
    principal_repayment: float,
    equity_capex: float,
    replacement_capex: float = 0.0,
    debt_drawdown: float = 0.0,
    residual_value: float = 0.0,
) -> float:
    """年度资本金现金流（规范 §68）。"""
    return (
        float(ebitda)
        - float(cash_tax)
        - float(interest)
        - float(principal_repayment)
        - float(equity_capex)
        - float(replacement_capex)
        + float(debt_drawdown)
        + float(residual_value)
    )


def year0_project_cashflow(total_capex: float) -> float:
    """Year 0 项目现金流（规范 §69）：``-TotalCAPEX``。"""
    return -float(total_capex)


def year0_equity_cashflow(equity_amount: float) -> float:
    """Year 0 资本金现金流（规范 §69）：``-EquityAmount``。"""
    return -float(equity_amount)


def cumulative(values: list[float]) -> list[float]:
    """累计序列（含起点），用于累计现金流与回收期。"""
    out: list[float] = []
    total = 0.0
    for v in values:
        total += float(v)
        out.append(total)
    return out
