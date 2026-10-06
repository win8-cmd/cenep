"""投资、运维、折旧、税务、融资模块单元测试（规范 §49–§58、§60–§65）。"""

from __future__ import annotations

import pytest

from cenep.calculation import financing as fin
from cenep.calculation import investment as inv
from cenep.calculation import opex as opx
from cenep.calculation import tax as taxmod
from cenep.calculation.errors import ValidationError


class TestCapex:
    def test_unit_price_mode(self):
        """§50、§51：容量 × 单价。"""
        capex = inv.compute_capex(
            mode="UNIT_PRICE",
            pv_capacity_kwp=1000.0,
            storage_energy_kwh=1000.0,
            pv_capex_per_kw=3000.0,
            storage_capex_per_kwh=1000.0,
        )
        assert capex.pv_capex == pytest.approx(3_000_000.0)
        assert capex.storage_capex == pytest.approx(1_000_000.0)
        assert capex.total == pytest.approx(4_000_000.0)

    def test_detailed_mode_uses_given_amounts(self):
        """§52：DETAILED 模式下容量不参与计算。"""
        capex = inv.compute_capex(
            mode="DETAILED",
            pv_capacity_kwp=1000.0,
            storage_energy_kwh=1000.0,
            pv_capex_per_kw=9999.0,
            storage_capex_per_kwh=9999.0,
            pv_capex=2_500_000.0,
            storage_capex=900_000.0,
        )
        assert capex.pv_capex == pytest.approx(2_500_000.0)
        assert capex.storage_capex == pytest.approx(900_000.0)

    def test_total_includes_all_nine_components(self):
        """§49：总投资为九项之和。"""
        capex = inv.compute_capex(
            mode="UNIT_PRICE",
            pv_capacity_kwp=1000.0,
            storage_energy_kwh=1000.0,
            pv_capex_per_kw=3000.0,
            storage_capex_per_kwh=1000.0,
            grid_connection_cost=1.0,
            roof_cost=2.0,
            development_cost=3.0,
            engineering_cost=4.0,
            construction_cost=5.0,
            other_capex=6.0,
            contingency=7.0,
        )
        assert capex.total == pytest.approx(3_000_000.0 + 1_000_000.0 + 1 + 2 + 3 + 4 + 5 + 6 + 7)

    def test_invalid_mode(self):
        with pytest.raises(ValidationError) as exc:
            inv.compute_capex(mode="MAGIC")
        assert "投资模式" in str(exc.value)

    def test_unit_investment(self):
        """§102：结果需给出单位投资。"""
        values = inv.unit_investment(4_000_000.0, 1000.0, 1000.0)
        assert values["yuan_per_w"] == pytest.approx(4.0)
        assert values["yuan_per_wh"] == pytest.approx(4.0)


class TestOpex:
    def test_fixed_mode(self):
        """§54：AnnualOpex = FixedOpex。"""
        assert opx.resolve_opex_item(50_000.0, "FIXED") == pytest.approx(50_000.0)

    def test_ratio_mode(self):
        """§54：AnnualOpex = InitialCAPEX × OpexRate。"""
        assert opx.resolve_opex_item(0.02, "RATIO_OF_CAPEX", 3_000_000.0) == pytest.approx(60_000.0)

    def test_growth(self):
        """§55：Opex_n = Opex_1 × (1+g)^(n-1)。"""
        assert opx.opex_for_year(115_000.0, 0.03, 3) == pytest.approx(115_000.0 * 1.03**2)

    def test_roof_rent_area_mode(self):
        assert opx.roof_rent("AREA", roof_area_m2=8000.0, rent_per_m2=5.0) == pytest.approx(40_000.0)

    def test_roof_rent_capacity_mode(self):
        assert opx.roof_rent("CAPACITY", pv_capacity_kwp=1000.0, rent_per_kw=30.0) == pytest.approx(30_000.0)

    def test_roof_rent_fixed_mode(self):
        assert opx.roof_rent("FIXED", annual_fixed_rent=25_000.0) == pytest.approx(25_000.0)

    def test_bad_mode(self):
        with pytest.raises(ValidationError):
            opx.roof_rent("SOMETHING")


class TestTax:
    def test_depreciable_base_and_annual(self):
        """§58：基数 = CAPEX × (1-残值率)，年折旧 = 基数 / 年限。"""
        base = taxmod.depreciable_base(4_000_000.0, 0.05)
        assert base == pytest.approx(3_800_000.0)
        assert taxmod.annual_depreciation(4_000_000.0, 0.05, 20) == pytest.approx(190_000.0)

    def test_depreciation_stops_after_years(self):
        assert taxmod.depreciation_for_year(4_000_000.0, 0.05, 20, 20) == pytest.approx(190_000.0)
        assert taxmod.depreciation_for_year(4_000_000.0, 0.05, 20, 21) == 0.0

    def test_residual_value(self):
        assert taxmod.residual_value(4_000_000.0, 0.05) == pytest.approx(200_000.0)

    def test_revenue_net_exclusive(self):
        assert taxmod.revenue_net(1_130_000.0, 0.13, False) == pytest.approx(1_130_000.0)

    def test_revenue_net_inclusive(self):
        assert taxmod.revenue_net(1_130_000.0, 0.13, True) == pytest.approx(1_000_000.0)

    def test_profit_chain(self):
        """§60：EBITDA → EBIT → EBT。"""
        ebitda = taxmod.ebitda_of(684_386.26178, 115_000.0)
        assert ebitda == pytest.approx(569_386.26178)
        ebit = taxmod.ebit_of(ebitda, 190_000.0)
        assert ebit == pytest.approx(379_386.26178)
        ebt = taxmod.ebt_of(ebit, 76_000.0)
        assert ebt == pytest.approx(303_386.26178)

    def test_taxable_income_never_negative(self):
        """§61：TaxableIncome = max(EBT, 0)。"""
        assert taxmod.taxable_income_of(-50_000.0) == 0.0
        assert taxmod.taxable_income_of(100_000.0) == pytest.approx(100_000.0)

    def test_income_tax(self):
        """§61：所得税 = 应纳税所得额 × 税率。"""
        assert taxmod.income_tax_of(303_386.26178, 0.25) == pytest.approx(75_846.565445)

    def test_tax_year_result_matches_manual(self):
        row = taxmod.tax_year_result(
            year=1,
            revenue_net_amount=684_386.26178,
            opex=115_000.0,
            depreciation=190_000.0,
            interest=76_000.0,
            income_tax_rate=0.25,
            surcharge_rate=0.0,
            other_tax_rate=0.0,
        )
        assert row.ebitda == pytest.approx(569_386.26178)
        assert row.income_tax == pytest.approx(75_846.565445)
        assert row.cash_tax == pytest.approx(75_846.565445)

    def test_surcharge_and_other_tax_on_revenue(self):
        row = taxmod.tax_year_result(1, 1_000_000.0, 0.0, 0.0, 0.0, 0.25, 0.01, 0.002)
        assert row.surcharge == pytest.approx(10_000.0)
        assert row.other_tax == pytest.approx(2_000.0)
        assert row.cash_tax == pytest.approx(250_000.0 + 10_000.0 + 2_000.0)


class TestFinancing:
    def test_amounts(self):
        """§63：贷款 = 总投资 × 贷款比例；资本金 = 总投资 - 贷款。"""
        loan = fin.loan_amount_of(4_000_000.0, 0.5)
        assert loan == pytest.approx(2_000_000.0)
        assert fin.equity_amount_of(4_000_000.0, loan) == pytest.approx(2_000_000.0)

    def test_equal_principal_sums_to_loan(self):
        schedule = fin.principal_schedule(2_000_000.0, 10, 0, "EQUAL_PRINCIPAL", 0.04)
        assert len(schedule) == 10
        assert sum(schedule) == pytest.approx(2_000_000.0)
        assert schedule[0] == pytest.approx(200_000.0)

    def test_grace_period_has_no_principal(self):
        schedule = fin.principal_schedule(2_000_000.0, 10, 2, "EQUAL_PRINCIPAL", 0.04)
        assert schedule[0] == 0.0
        assert schedule[1] == 0.0
        assert sum(schedule) == pytest.approx(2_000_000.0)

    def test_equal_installment_sums_to_loan(self):
        schedule = fin.principal_schedule(2_000_000.0, 10, 0, "EQUAL_INSTALLMENT", 0.04)
        assert sum(schedule) == pytest.approx(2_000_000.0, rel=1e-9)
        assert schedule[0] < schedule[-1]  # 等额本息：本金逐年递增

    def test_zero_rate_equal_installment(self):
        schedule = fin.principal_schedule(1_000_000.0, 10, 0, "EQUAL_INSTALLMENT", 0.0)
        assert sum(schedule) == pytest.approx(1_000_000.0)
        assert all(p == pytest.approx(100_000.0) for p in schedule)

    def test_interest_uses_average_balance(self):
        """§65：Interest = AverageDebtBalance × InterestRate（不得用原始贷款×利率）。"""
        rows = fin.build_loan_schedule(2_000_000.0, 0.04, 10, 0, "EQUAL_PRINCIPAL", 10)
        first = rows[0]
        assert first.debt_begin == pytest.approx(2_000_000.0)
        assert first.principal_repayment == pytest.approx(200_000.0)
        assert first.debt_end == pytest.approx(1_800_000.0)
        assert first.interest == pytest.approx(1_900_000.0 * 0.04)
        assert first.interest != pytest.approx(2_000_000.0 * 0.04)

    def test_balance_never_negative_and_closes(self):
        """§64：EndingDebt >= 0，且期末余额为 0。"""
        rows = fin.build_loan_schedule(2_000_000.0, 0.04, 10, 0, "EQUAL_PRINCIPAL", 25)
        assert all(r.debt_end >= 0 for r in rows)
        assert rows[-1].debt_end == pytest.approx(0.0)
        assert sum(r.principal_repayment for r in rows) == pytest.approx(2_000_000.0)

    def test_debt_service(self):
        rows = fin.build_loan_schedule(2_000_000.0, 0.04, 10, 0, "EQUAL_PRINCIPAL", 10)
        assert rows[0].debt_service == pytest.approx(200_000.0 + 76_000.0)

    def test_grace_must_be_smaller_than_term(self):
        with pytest.raises(ValidationError):
            fin.principal_schedule(1_000_000.0, 10, 10, "EQUAL_PRINCIPAL", 0.04)

    def test_bad_repayment_method(self):
        with pytest.raises(ValidationError):
            fin.principal_schedule(1_000_000.0, 10, 0, "SOMETHING", 0.04)
