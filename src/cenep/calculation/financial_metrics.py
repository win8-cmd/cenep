"""财务指标模块（规范 §70–§79、§146）。

包含：IRR、NPV、静态回收期、动态回收期、LCOE、LCOS、ROI、DSCR。

实现约定
--------
* 现金流序列下标 ``t = 0..N``，``t = 0`` 为建设期（规范 §15）。
* NPV（§71）：``Σ CF_t/(1+r)^t``，``t = 0`` 不额外折现（即除以 ``(1+r)^0 = 1``）。
* IRR（§70）：用**纯 Python 二分法**求解，固定迭代次数，保证结果完全确定（规范 §118
  要求同一输入重复计算 100 次结果一致，不使用随机初值）。
* 若现金流不存在"至少一个正值和一个负值"（§114），IRR 返回 ``None``，展示层显示"无法计算"。
* 回收期（§73、§74）：``Payback = (n-1) + |Cum_(n-1)| / CF_n``，始终无法回收时返回 ``None``，
  展示层显示"未回收"。
"""

from __future__ import annotations

from collections.abc import Sequence

from .cashflow import cumulative

# 二分法搜索区间与迭代次数（确定性）
_IRR_LOW = -0.9999
_IRR_HIGH = 10.0
_IRR_ITERATIONS = 300


def npv(rate: float, cashflows: Sequence[float]) -> float:
    """净现值（规范 §71）。"""
    r = float(rate)
    return sum(float(cf) / (1.0 + r) ** t for t, cf in enumerate(cashflows))


def has_sign_change(cashflows: Sequence[float]) -> bool:
    """IRR 可解性检查（规范 §114）：至少存在一个正值和一个负值。"""
    has_pos = any(cf > 0 for cf in cashflows)
    has_neg = any(cf < 0 for cf in cashflows)
    return has_pos and has_neg


def irr(cashflows: Sequence[float], tolerance: float = 1e-12) -> float | None:
    """内部收益率（规范 §70）。无法计算时返回 ``None``。

    使用二分法：先扫描确定符号变化区间，再固定次数二分。
    """
    flows = [float(cf) for cf in cashflows]
    if not has_sign_change(flows):
        return None

    low, high = _IRR_LOW, _IRR_HIGH
    f_low, f_high = npv(low, flows), npv(high, flows)
    if f_low * f_high > 0:
        # 区间内无符号变化：退化为扫描网格寻找更窄区间
        grid = [_IRR_LOW + (_IRR_HIGH - _IRR_LOW) * i / 2000 for i in range(2001)]
        prev_r, prev_v = grid[0], npv(grid[0], flows)
        found = False
        for r in grid[1:]:
            v = npv(r, flows)
            if prev_v * v <= 0:
                low, high, f_low, f_high = prev_r, r, prev_v, v
                found = True
                break
            prev_r, prev_v = r, v
        if not found:
            return None

    for _ in range(_IRR_ITERATIONS):
        mid = (low + high) / 2.0
        f_mid = npv(mid, flows)
        if abs(f_mid) < tolerance or (high - low) < 1e-14:
            return mid
        if f_low * f_mid <= 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2.0


def payback_period(cashflows: Sequence[float]) -> float | None:
    """静态投资回收期（规范 §73），单位：年（自 Year 0 起算）。

    始终无法回收时返回 ``None``（展示为"未回收"）。
    """
    flows = [float(cf) for cf in cashflows]
    cum = cumulative(flows)
    for n in range(1, len(cum)):
        if cum[n - 1] < 0 <= cum[n]:
            cf_n = flows[n]
            if cf_n <= 0:
                continue
            return (n - 1) + abs(cum[n - 1]) / cf_n
    return None


def discounted_cashflows(rate: float, cashflows: Sequence[float]) -> list[float]:
    """折现现金流序列（规范 §74）。"""
    r = float(rate)
    return [float(cf) / (1.0 + r) ** t for t, cf in enumerate(cashflows)]


def discounted_payback_period(rate: float, cashflows: Sequence[float]) -> float | None:
    """动态投资回收期（规范 §74）：对折现现金流使用与静态相同的插值方法。"""
    return payback_period(discounted_cashflows(rate, cashflows))


def lcoe(
    rate: float,
    costs: Sequence[float],
    energies: Sequence[float],
) -> float | None:
    """平准化度电成本（规范 §75）。

    ``LCOE = Σ(Cost_t/(1+r)^t) / Σ(Energy_t/(1+r)^t)``；能量折现值 ≤ 0 时返回 ``None``。
    """
    r = float(rate)
    pv_cost = sum(float(c) / (1.0 + r) ** t for t, c in enumerate(costs))
    pv_energy = sum(float(e) / (1.0 + r) ** t for t, e in enumerate(energies))
    if pv_energy <= 0:
        return None
    return pv_cost / pv_energy


def lcos(
    rate: float,
    storage_costs: Sequence[float],
    discharge_energies: Sequence[float],
) -> float | None:
    """储能平准化度电成本（规范 §77）。"""
    return lcoe(rate, storage_costs, discharge_energies)


def roi(lifecycle_net_profit: float, initial_capex: float) -> float | None:
    """总投资收益率口径 ROI（规范 §78）：``生命周期累计净收益 / 初始总投资``。

    报告必须注明口径："生命周期累计净收益 / 初始总投资"。
    """
    if initial_capex <= 0:
        return None
    return float(lifecycle_net_profit) / float(initial_capex)


def dscr_series(
    cfads: Sequence[float],
    debt_service: Sequence[float],
) -> list[float | None]:
    """逐年偿债备付率（规范 §79）。``DebtService <= 0`` 的年份返回 ``None``。"""
    out: list[float | None] = []
    for c, d in zip(cfads, debt_service, strict=True):
        out.append((float(c) / float(d)) if float(d) > 0 else None)
    return out


def minimum_dscr(cfads: Sequence[float], debt_service: Sequence[float]) -> float | None:
    """最低偿债备付率（规范 §79）。无可计算年份时返回 ``None``。"""
    values = [v for v in dscr_series(cfads, debt_service) if v is not None]
    return min(values) if values else None


def cfads_of(ebitda: float, cash_tax: float, maintenance_capex: float) -> float:
    """可用于偿债的现金流（规范 §79）：``EBITDA - CashTax - MaintenanceCAPEX``。"""
    return float(ebitda) - float(cash_tax) - float(maintenance_capex)
