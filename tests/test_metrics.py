"""财务指标模块单元测试（规范 §70–§79、§114、§116）。"""

from __future__ import annotations

import pytest

from cenep.calculation import financial_metrics as fm

GOLDEN_FLOWS = [-3_000_000.0] + [500_000.0] * 25


class TestNpv:
    def test_npv_at_zero_rate_is_sum(self):
        """§71：r=0 时 NPV 等于现金流求和。"""
        assert fm.npv(0.0, [-100.0, 50.0, 80.0]) == pytest.approx(30.0)

    def test_npv_year_zero_not_discounted(self):
        """§71：Year 0 不额外折现。"""
        assert fm.npv(0.10, [-100.0]) == pytest.approx(-100.0)

    def test_npv_manual(self):
        value = fm.npv(0.08, [-1000.0, 500.0, 500.0])
        assert value == pytest.approx(-1000.0 + 500.0 / 1.08 + 500.0 / 1.08**2)


class TestIrr:
    def test_known_irr(self):
        """三笔 500 收回 1000，IRR ≈ 23.375%。"""
        value = fm.irr([-1000.0, 500.0, 500.0, 500.0])
        assert value == pytest.approx(0.233751, abs=1e-4)

    def test_irr_makes_npv_zero(self):
        value = fm.irr(GOLDEN_FLOWS)
        assert value is not None
        assert fm.npv(value, GOLDEN_FLOWS) == pytest.approx(0.0, abs=1e-4)

    def test_no_sign_change_returns_none(self):
        """§114：IRR 输入现金流至少存在一个正值和一个负值。"""
        assert fm.irr([100.0, 200.0, 300.0]) is None
        assert fm.irr([-100.0, -200.0]) is None
        assert fm.has_sign_change([100.0, -50.0]) is True

    def test_deterministic(self):
        """§118：同一输入重复计算 100 次结果一致。"""
        values = {fm.irr(GOLDEN_FLOWS) for _ in range(100)}
        assert len(values) == 1


class TestPayback:
    def test_exact_year(self):
        """§73：Payback = (n-1) + |Cum_(n-1)| / CF_n。"""
        assert fm.payback_period([-100.0, 50.0, 50.0, 50.0]) == pytest.approx(2.0)

    def test_interpolated(self):
        assert fm.payback_period([-100.0, 40.0, 80.0]) == pytest.approx(1 + 60.0 / 80.0)

    def test_never_recovered_returns_none(self):
        assert fm.payback_period([-100.0, 10.0, 10.0]) is None

    def test_discounted_payback(self):
        """§74：对折现现金流使用相同插值方法。"""
        flows = [-100.0, 60.0, 60.0]
        discounted = fm.discounted_cashflows(0.10, flows)
        cum = [-100.0, -100.0 + discounted[1]]
        expected = 1 + abs(cum[1]) / discounted[2]
        assert fm.discounted_payback_period(0.10, flows) == pytest.approx(expected)
        assert fm.discounted_payback_period(0.10, flows) > fm.payback_period(flows)


class TestLcoeLcos:
    def test_lcoe_at_zero_rate(self):
        """§75：r=0 时 LCOE = 总成本 / 总电量。"""
        assert fm.lcoe(0.0, [1000.0, 100.0, 100.0], [0.0, 500.0, 500.0]) == pytest.approx(1.2)

    def test_lcoe_manual_discounting(self):
        value = fm.lcoe(0.08, [1000.0, 100.0], [0.0, 500.0])
        assert value == pytest.approx((1000.0 + 100.0 / 1.08) / (500.0 / 1.08))

    def test_lcoe_no_energy_returns_none(self):
        assert fm.lcoe(0.08, [1000.0], [0.0]) is None

    def test_lcos_uses_discharge_energy(self):
        """§77：LCOS = 储能成本现值 / 放电量现值。"""
        value = fm.lcos(0.08, [1000.0, 50.0], [0.0, 300.0])
        assert value == pytest.approx((1000.0 + 50.0 / 1.08) / (300.0 / 1.08))


class TestRoiDscr:
    def test_roi(self):
        """§78：ROI = 生命周期累计净收益 / 初始总投资。"""
        assert fm.roi(400_000.0, 1_000_000.0) == pytest.approx(0.4)

    def test_roi_zero_capex_returns_none(self):
        assert fm.roi(100.0, 0.0) is None

    def test_dscr_series_and_min(self):
        """§79：DSCR = CFADS / DebtService，输出最低值。"""
        cfads = [100.0, 200.0, 300.0]
        service = [100.0, 100.0, 100.0]
        assert fm.dscr_series(cfads, service) == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(3.0)]
        assert fm.minimum_dscr(cfads, service) == pytest.approx(1.0)

    def test_dscr_none_when_no_debt_service(self):
        assert fm.dscr_series([100.0], [0.0]) == [None]
        assert fm.minimum_dscr([100.0], [0.0]) is None

    def test_cfads_formula(self):
        """§79：CFADS = EBITDA - CashTax - MaintenanceCAPEX。"""
        assert fm.cfads_of(500_000.0, 80_000.0, 20_000.0) == pytest.approx(400_000.0)
