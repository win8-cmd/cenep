"""融资模块（规范 §62–§65、§146）。

* 贷款额（§63）：``LoanAmount = TotalCAPEX × DebtRatio``
* 资本金（§63）：``EquityAmount = TotalCAPEX - LoanAmount``
* 贷款余额（§64）：``EndingDebt = BeginningDebt + Drawdown - PrincipalRepayment ≥ 0``
* 利息（§65）：``Interest = AverageDebtBalance × InterestRate``
  —— **禁止**用"原始贷款 × 利率"作为所有年份的利息。

V1 口径（写入报告）
------------------
1. 提款：Year 0 一次性全额提款（``LoanAmount``）。
2. ``loan_term`` = 贷款期限（含宽限期，单位：年）；实际还本年数 = ``loan_term - grace_period``。
3. 宽限期内只付息不还本。
4. 等额本金：每年还本额相同，末年轧平余额。
5. 等额本息：按年金公式计算年还款额，用**期初余额**利息分解出还本额，末年轧平余额；
   而**利息入账口径仍按 §65 的期初/期末平均余额**计算，两者差异在报告口径说明中披露。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError

EQUAL_PRINCIPAL = "EQUAL_PRINCIPAL"
EQUAL_INSTALLMENT = "EQUAL_INSTALLMENT"


@dataclass(frozen=True)
class LoanYear:
    """某一年度的贷款情况（单位：元）。"""

    year: int
    debt_begin: float
    drawdown: float
    principal_repayment: float
    interest: float
    debt_end: float

    @property
    def debt_service(self) -> float:
        """还本付息合计（规范 §79 ``DebtService``）。"""
        return self.principal_repayment + self.interest


def loan_amount_of(total_capex: float, debt_ratio: float) -> float:
    """贷款金额（规范 §63）。"""
    return float(total_capex) * float(debt_ratio)


def equity_amount_of(total_capex: float, loan_amount: float) -> float:
    """资本金金额（规范 §63）：``TotalCAPEX - LoanAmount``。"""
    return float(total_capex) - float(loan_amount)


def principal_schedule(
    loan_amount: float,
    loan_term: int,
    grace_period: int,
    repayment_method: str,
    interest_rate: float,
) -> list[float]:
    """还本计划（下标 0 对应第 1 年）。

    返回长度 ``loan_term`` 的列表；宽限期内为 0。
    """
    term = int(loan_term)
    grace = int(grace_period)
    if term <= 0:
        raise ValidationError("贷款期限必须大于 0", field="loan_term")
    if grace < 0:
        raise ValidationError("宽限期不能为负数", field="grace_period")
    if grace >= term:
        raise ValidationError("宽限期必须小于贷款期限，否则无法还本", field="grace_period")

    n_repay = term - grace
    method = str(repayment_method).upper()
    schedule = [0.0] * term

    if method == EQUAL_PRINCIPAL:
        per_year = float(loan_amount) / n_repay
        for i in range(grace, term):
            schedule[i] = per_year
        schedule[term - 1] = float(loan_amount) - sum(schedule[: term - 1])
    elif method == EQUAL_INSTALLMENT:
        r = float(interest_rate)
        if r <= 0:
            per_year = float(loan_amount) / n_repay
            for i in range(grace, term):
                schedule[i] = per_year
        else:
            factor = (1.0 + r) ** n_repay
            annuity = float(loan_amount) * r * factor / (factor - 1.0)
            balance = float(loan_amount)
            for i in range(grace, term):
                interest = balance * r
                principal = min(annuity - interest, balance)
                if i == term - 1:
                    principal = balance
                schedule[i] = principal
                balance -= principal
    else:
        raise ValidationError(
            f"不支持的还款方式：{repayment_method}（只能是等额本金或等额本息）",
            field="repayment_method",
        )
    return schedule


def build_loan_schedule(
    loan_amount: float,
    interest_rate: float,
    loan_term: int,
    grace_period: int,
    repayment_method: str,
    analysis_period: int,
) -> list[LoanYear]:
    """生成完整计算期（第 1..analysis_period 年）的贷款计划。

    第 0 年提款不在此列表中体现，由现金流模块按规范 §69 处理。
    """
    schedule = principal_schedule(loan_amount, loan_term, grace_period, repayment_method, interest_rate)
    rate = float(interest_rate)
    years: list[LoanYear] = []
    balance = float(loan_amount)
    for year in range(1, int(analysis_period) + 1):
        principal = schedule[year - 1] if year <= len(schedule) else 0.0
        principal = min(principal, balance)
        begin = balance
        end = begin - principal
        if end < -1e-9:  # 规范 §64：EndingDebt >= 0
            raise ValidationError("贷款余额出现负数，请检查贷款期限与还款方式", field="loan_term")
        end = max(end, 0.0)
        average_balance = (begin + end) / 2.0
        years.append(
            LoanYear(
                year=year,
                debt_begin=begin,
                drawdown=0.0,
                principal_repayment=principal,
                interest=average_balance * rate,
                debt_end=end,
            )
        )
        balance = end
    return years
