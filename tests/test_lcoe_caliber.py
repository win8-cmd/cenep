"""LCOE/LCOS 口径开关与光伏设备更换（规范 §25 延伸、§75）。

对应两个由公开招标案例校核发现的缺口：

* **缺口 2**：LCOE 不认增值税进项抵扣与残值抵减，与行业/招标通行口径不同
  → 新增 ``TaxConfig.lcoe_vat_deductible_ratio`` 与 ``TaxConfig.lcoe_residual_credit``（默认关闭）
* **缺口 3**：无法表达光伏设备更换（第 N 年一次性支出，如逆变器第 12 年更换）
  → 新增 ``PVConfig.replacement_year`` 与 ``PVConfig.replacement_cost_per_kwp``

标尺案例：常州市武进区分布式光伏 EPC 招标文件给定的 LCOE 公式

    LCOE = [ I₀ − It − VR/(1+i)^N + Σ Mₙ/(1+i)ⁿ ] / Σ Yₙ/(1+i)ⁿ
"""

from __future__ import annotations

import pytest
from conftest import GOLDEN_PV_CAPACITY_KWP

from cenep.calculation.engine import calculation_engine
from cenep.calculation.tax import residual_value

REPLACEMENT_YEAR = 12
REPLACEMENT_PER_KWP = 190.0


def _discounted_energy(project, result) -> float:
    rate = project.discount_rate
    return sum(
        row.pv_generation_kwh / (1.0 + rate) ** row.year for row in result.annual_results
    )


class TestLcoeInvestmentBase:
    """LCOE 的投资基数必须是光伏应承担的全部投资。"""

    def test_shared_capex_counted(self, golden_pv):
        """回归：并网/开发等共享投资必须计入 LCOE，不能只取 pv_capex。"""
        proj = golden_pv.model_copy(deep=True)
        proj.investment.grid_connection_cost = 100_000.0
        r = calculation_engine.calculate(proj)
        rate = proj.discount_rate
        num = r.total_capex + sum(
            row.opex / (1.0 + rate) ** row.year for row in r.annual_results
        )
        assert r.lcoe == pytest.approx(num / _discounted_energy(proj, r), rel=1e-12)


class TestLcoeVatDeduction:
    """增值税进项抵扣开关（默认关闭）。"""

    def test_default_off(self, golden_pv):
        proj = golden_pv.model_copy(deep=True)
        assert proj.tax.lcoe_vat_deductible_ratio == 0.0
        base = calculation_engine.calculate(proj)
        assert base.lcoe == pytest.approx(0.35131245094782904, rel=1e-9)

    def test_deduction_lowers_lcoe_exactly(self, golden_pv):
        proj = golden_pv.model_copy(deep=True)
        proj.tax.lcoe_vat_deductible_ratio = 0.13
        base = calculation_engine.calculate(golden_pv)
        r = calculation_engine.calculate(proj)
        expected_drop = r.total_capex * 0.13 / _discounted_energy(proj, r)
        assert base.lcoe - r.lcoe == pytest.approx(expected_drop, rel=1e-12)


class TestLcoeResidualCredit:
    """残值现值抵减开关（默认关闭）。"""

    def test_default_off(self, golden_pv):
        proj = golden_pv.model_copy(deep=True)
        assert proj.tax.lcoe_residual_credit is False

    def test_credit_lowers_lcoe_exactly(self, golden_pv):
        proj = golden_pv.model_copy(deep=True)
        proj.tax.lcoe_residual_credit = True
        base = calculation_engine.calculate(golden_pv)
        r = calculation_engine.calculate(proj)
        residual = residual_value(
            r.total_capex * proj.tax.depreciable_capex_ratio, proj.tax.residual_value_ratio
        )
        expected_drop = residual / (1.0 + proj.discount_rate) ** proj.analysis_period
        expected_drop /= _discounted_energy(proj, r)
        assert base.lcoe - r.lcoe == pytest.approx(expected_drop, rel=1e-9)


class TestPvReplacement:
    """光伏设备更换（逆变器等）：第 N 年一次性支出。"""

    def _project(self, golden_pv):
        proj = golden_pv.model_copy(deep=True)
        proj.pv.replacement_year = REPLACEMENT_YEAR
        proj.pv.replacement_cost_per_kwp = REPLACEMENT_PER_KWP
        return proj

    def test_only_in_replacement_year(self, golden_pv):
        proj = self._project(golden_pv)
        r = calculation_engine.calculate(proj)
        cost = REPLACEMENT_PER_KWP * GOLDEN_PV_CAPACITY_KWP
        row = next(x for x in r.annual_results if x.year == REPLACEMENT_YEAR)
        assert row.replacement_capex == pytest.approx(cost)
        assert all(
            x.replacement_capex == 0.0
            for x in r.annual_results
            if x.year != REPLACEMENT_YEAR
        )

    def test_hits_project_cashflow(self, golden_pv):
        """更换支出必须进入项目现金流（且不影响税费，因为不费用化）。"""
        proj = self._project(golden_pv)
        r = calculation_engine.calculate(proj)
        base = calculation_engine.calculate(golden_pv)
        cost = REPLACEMENT_PER_KWP * GOLDEN_PV_CAPACITY_KWP
        row = next(x for x in r.annual_results if x.year == REPLACEMENT_YEAR)
        base_row = next(x for x in base.annual_results if x.year == REPLACEMENT_YEAR)
        assert base_row.project_cashflow - row.project_cashflow == pytest.approx(cost)
        assert row.cash_tax == pytest.approx(base_row.cash_tax)

    def test_increases_lcoe(self, golden_pv):
        proj = self._project(golden_pv)
        r = calculation_engine.calculate(proj)
        base = calculation_engine.calculate(golden_pv)
        cost = REPLACEMENT_PER_KWP * GOLDEN_PV_CAPACITY_KWP
        expected_rise = cost / (1.0 + proj.discount_rate) ** REPLACEMENT_YEAR
        expected_rise /= _discounted_energy(proj, r)
        assert r.lcoe - base.lcoe == pytest.approx(expected_rise, rel=1e-12)

    def test_disabled_by_default(self, golden_pv):
        assert golden_pv.pv.replacement_year is None
        assert golden_pv.pv.replacement_cost_per_kwp == 0.0


class TestTenderCaliberReproduction:
    """端到端：按招标文件公式口径复算，引擎结果必须与手工公式完全一致。"""

    def test_matches_manual_formula(self, golden_pv):
        proj = golden_pv.model_copy(deep=True)
        proj.tax.lcoe_vat_deductible_ratio = 0.13
        proj.tax.lcoe_residual_credit = True
        proj.pv.replacement_year = REPLACEMENT_YEAR
        proj.pv.replacement_cost_per_kwp = REPLACEMENT_PER_KWP
        r = calculation_engine.calculate(proj)

        rate = proj.discount_rate
        n = proj.analysis_period
        capex = r.total_capex
        vat = capex * proj.tax.lcoe_vat_deductible_ratio
        residual = residual_value(
            capex * proj.tax.depreciable_capex_ratio, proj.tax.residual_value_ratio
        )
        numerator = capex - vat - residual / (1.0 + rate) ** n
        denominator = 0.0
        for row in r.annual_results:
            numerator += (row.opex + row.replacement_capex) / (1.0 + rate) ** row.year
            denominator += row.pv_generation_kwh / (1.0 + rate) ** row.year

        assert r.lcoe == pytest.approx(numerator / denominator, rel=1e-12)
        # 该口径下的 LCOE 必须低于"不含任何抵减"的原始口径
        base = calculation_engine.calculate(golden_pv)
        assert r.lcoe < base.lcoe
