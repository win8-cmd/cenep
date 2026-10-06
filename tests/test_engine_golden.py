"""黄金测试案例（规范 §117、§118、§153、§154）。

三个固定案例：``golden_pv``（1000 kWp）、``golden_storage``（500 kW/1000 kWh）、
``golden_pv_storage``（1000 kWp + 500 kW/1000 kWh）。

关键期望值均为**手工独立推算**（见 TEST_PLAN.md 推导过程），不是从程序输出反抄，
以便在公式被误改时立刻失败。
"""

from __future__ import annotations

import math

import pytest
from conftest import GOLDEN_LCOE_PV, GOLDEN_LCOE_PV_STORAGE, GOLDEN_LCOS_STORAGE

from cenep.calculation.engine import calculation_engine

SQRT_088 = math.sqrt(0.88)
STORAGE_DISCHARGE_1 = 1000.0 * 0.9 * 330.0 * SQRT_088          # 278 610.696142...
STORAGE_CHARGE_1 = STORAGE_DISCHARGE_1 / SQRT_088 / SQRT_088    # 316 603.063798...


class TestGoldenPv:
    """案例 1：工商业光伏 1000 kWp。"""

    def test_capacity_and_generation(self, golden_pv):
        r = calculation_engine.calculate(golden_pv)
        assert r.pv_capacity_kwp == pytest.approx(1000.0)
        assert r.first_year_generation == pytest.approx(1_100_000.0)
        assert r.analysis_period == 25
        assert len(r.annual_results) == 25
        assert len(r.project_cashflows) == 26  # Year 0 + 25 年

    def test_energy_allocation_year1(self, golden_pv):
        """自用 = min(1.1e6 × 0.8, 负荷 1.2e6) = 880 000；余电 = 220 000。"""
        row = calculation_engine.calculate(golden_pv).annual_results[0]
        assert row.pv_self_use_kwh == pytest.approx(880_000.0)
        assert row.pv_export_kwh == pytest.approx(220_000.0)
        assert row.pv_to_storage_kwh == 0.0
        assert row.pv_generation_kwh == pytest.approx(
            row.pv_self_use_kwh + row.pv_to_storage_kwh + row.pv_export_kwh + row.pv_loss_kwh
        )

    def test_capex_opex_depreciation(self, golden_pv):
        r = calculation_engine.calculate(golden_pv)
        assert r.total_capex == pytest.approx(3_000_000.0)
        assert r.first_year_opex == pytest.approx(90_000.0)   # 60 000 + 0 + 15 000 + 10 000 + 5 000
        assert r.annual_results[0].depreciation == pytest.approx(142_500.0)  # 3e6 × 0.95 / 20

    def test_revenue_year1(self, golden_pv):
        """自用 880 000 × 0.70 = 616 000；上网 220 000 × 0.35 = 77 000。"""
        row = calculation_engine.calculate(golden_pv).annual_results[0]
        assert row.pv_self_use_revenue == pytest.approx(616_000.0)
        assert row.pv_export_revenue == pytest.approx(77_000.0)
        assert row.total_revenue == pytest.approx(693_000.0)

    def test_profit_and_cashflow_year1(self, golden_pv):
        row = calculation_engine.calculate(golden_pv).annual_results[0]
        assert row.ebitda == pytest.approx(603_000.0)     # 693 000 - 90 000
        assert row.ebit == pytest.approx(460_500.0)
        assert row.interest == pytest.approx(57_000.0)    # 平均余额 1 425 000 × 4%
        assert row.ebt == pytest.approx(403_500.0)
        assert row.income_tax == pytest.approx(100_875.0)
        assert row.project_cashflow == pytest.approx(502_125.0)
        assert row.equity_cashflow == pytest.approx(295_125.0)

    def test_financing_amounts(self, golden_pv):
        r = calculation_engine.calculate(golden_pv)
        assert r.loan_amount == pytest.approx(1_500_000.0)
        assert r.equity_amount == pytest.approx(1_500_000.0)
        assert r.project_cashflows[0] == pytest.approx(-3_000_000.0)
        assert r.equity_cashflows[0] == pytest.approx(-1_500_000.0)

    def test_degradation_shows_in_year2(self, golden_pv):
        rows = calculation_engine.calculate(golden_pv).annual_results
        assert rows[1].pv_generation_kwh == pytest.approx(1_100_000.0 * 0.995)
        assert rows[1].pv_self_use_kwh == pytest.approx(1_100_000.0 * 0.995 * 0.8)

    def test_lcoe_computed_lcos_not_applicable(self, golden_pv):
        r = calculation_engine.calculate(golden_pv)
        assert r.lcoe is not None and r.lcoe > 0
        assert r.lcos is None
        assert r.min_dscr is not None

    def test_lcoe_cost_covers_all_pv_opex(self, golden_pv):
        """回归：LCOE 成本口径 = 光伏投资 + 全部光伏相关年运营成本，不得漏项。

        早期版本只把 ``pv_opex`` 与屋顶租金计入 LCOE 成本，漏掉**保险费、管理费、其他费用**，
        而现金流中的"运维成本"（``AnnualResult.opex``）是包含它们的，导致同一份报表里
        两个"运维成本"含义不一致。本案例首年 OPEX 90,000 元中当时只有 60,000 元进入 LCOE。

        这里用独立复算校验：LCOE 的分子必须等于 投资 + Σ(全部年运营成本/(1+r)^n)。
        """
        r = calculation_engine.calculate(golden_pv)
        rate = golden_pv.discount_rate
        num = r.total_capex + sum(
            row.opex / (1.0 + rate) ** row.year for row in r.annual_results
        )
        den = sum(
            row.pv_generation_kwh / (1.0 + rate) ** row.year for row in r.annual_results
        )
        assert r.lcoe == pytest.approx(num / den, rel=1e-12)
        assert r.lcoe == pytest.approx(GOLDEN_LCOE_PV, rel=1e-9)

    def test_lcoe_excludes_storage_opex(self, golden_pv_storage):
        """LCOE 只承担光伏相关成本；储能运维费归 LCOS，两者不重复也不遗漏。"""
        r = calculation_engine.calculate(golden_pv_storage)
        rate = golden_pv_storage.discount_rate
        # 若把"全部 OPEX"都塞进 LCOE，会得到明显偏大的值 → 证明 LCOE 未含储能运维费
        num_all = r.total_capex + sum(
            row.opex / (1.0 + rate) ** row.year for row in r.annual_results
        )
        den = sum(
            row.pv_generation_kwh / (1.0 + rate) ** row.year for row in r.annual_results
        )
        assert r.lcoe < num_all / den
        assert r.lcoe == pytest.approx(GOLDEN_LCOE_PV_STORAGE, rel=1e-9)
        assert r.lcos == pytest.approx(GOLDEN_LCOS_STORAGE, rel=1e-9)


class TestGoldenStorage:
    """案例 2：工商业储能 500 kW / 1000 kWh（无光伏）。"""

    def test_no_pv_generation(self, golden_storage):
        r = calculation_engine.calculate(golden_storage)
        assert r.pv_capacity_kwp == 0.0
        assert r.first_year_generation == 0.0
        assert r.lcoe is None

    def test_storage_energy_year1(self, golden_storage):
        """§41、§42、§45。"""
        row = calculation_engine.calculate(golden_storage).annual_results[0]
        assert row.storage_available_kwh == pytest.approx(1000.0)
        assert row.storage_discharge_kwh == pytest.approx(STORAGE_DISCHARGE_1, rel=1e-9)
        assert row.storage_charge_kwh == pytest.approx(STORAGE_CHARGE_1, rel=1e-9)

    def test_duration(self, golden_storage):
        r = calculation_engine.calculate(golden_storage)
        assert r.storage_duration_hours == pytest.approx(2.0)

    def test_arbitrage_revenue(self, golden_storage):
        """§43：278 610.70 × 0.70 - 316 603.06 × 0.40 = 68 386.26。"""
        row = calculation_engine.calculate(golden_storage).annual_results[0]
        assert row.storage_arbitrage_revenue == pytest.approx(68_386.26178, rel=1e-9)
        assert row.total_revenue == pytest.approx(68_386.26178, rel=1e-9)

    def test_all_charge_comes_from_grid(self, golden_storage):
        row = calculation_engine.calculate(golden_storage).annual_results[0]
        assert row.pv_to_storage_kwh == 0.0
        assert row.storage_grid_charge_kwh == pytest.approx(STORAGE_CHARGE_1, rel=1e-9)

    def test_loss_year_tax_is_zero(self, golden_storage):
        """EBT 为负 → 应纳税所得额为 0（§61）。"""
        row = calculation_engine.calculate(golden_storage).annual_results[0]
        assert row.ebitda == pytest.approx(28_386.26178, rel=1e-9)
        assert row.ebt < 0
        assert row.taxable_income == 0.0
        assert row.income_tax == 0.0

    def test_capex_and_capex_breakdown(self, golden_storage):
        r = calculation_engine.calculate(golden_storage)
        assert r.total_capex == pytest.approx(1_000_000.0)
        assert r.capex_breakdown["储能投资"] == pytest.approx(1_000_000.0)
        assert r.capex_breakdown["光伏投资"] == 0.0

    def test_lcos_computed(self, golden_storage):
        r = calculation_engine.calculate(golden_storage)
        assert r.lcos is not None and r.lcos > 0


class TestGoldenPvStorage:
    """案例 3：工商业光储。"""

    def test_allocation_priority(self, golden_pv_storage):
        """§23、§46：光伏余电优先进储能，储能填满后剩余才上网。"""
        row = calculation_engine.calculate(golden_pv_storage).annual_results[0]
        assert row.pv_self_use_kwh == pytest.approx(880_000.0)
        assert row.pv_to_storage_kwh == pytest.approx(220_000.0)
        assert row.pv_export_kwh == pytest.approx(0.0)

    def test_grid_topup_charge(self, golden_pv_storage):
        row = calculation_engine.calculate(golden_pv_storage).annual_results[0]
        assert row.storage_grid_charge_kwh == pytest.approx(STORAGE_CHARGE_1 - 220_000.0, rel=1e-9)

    def test_no_double_counting(self, golden_pv_storage):
        """§47：转入储能的电量不计入光伏自用收益。"""
        row = calculation_engine.calculate(golden_pv_storage).annual_results[0]
        assert row.pv_self_use_revenue == pytest.approx(616_000.0)  # 只按 880 000 kWh 计
        assert row.pv_export_revenue == 0.0

    def test_revenue_and_cashflow_year1(self, golden_pv_storage):
        row = calculation_engine.calculate(golden_pv_storage).annual_results[0]
        assert row.total_revenue == pytest.approx(616_000.0 + 68_386.26178, rel=1e-9)
        assert row.ebitda == pytest.approx(569_386.26178, rel=1e-9)
        assert row.interest == pytest.approx(76_000.0)
        assert row.income_tax == pytest.approx(75_846.565445, rel=1e-9)
        assert row.project_cashflow == pytest.approx(493_539.696335, rel=1e-9)
        assert row.equity_cashflow == pytest.approx(217_539.696335, rel=1e-9)

    def test_total_capex(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        assert r.total_capex == pytest.approx(4_000_000.0)

    def test_both_lcoe_and_lcos(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        assert r.lcoe is not None
        assert r.lcos is not None


class TestDeterminismAndStructure:
    def test_same_input_100_times_identical(self, golden_pv_storage):
        """§118：同一输入重复计算 100 次，结果必须完全一致。"""
        results = []
        for _ in range(100):
            r = calculation_engine.calculate(golden_pv_storage)
            results.append(
                (
                    r.total_capex,
                    r.project_irr,
                    r.equity_irr,
                    r.project_npv,
                    r.static_payback,
                    r.discounted_payback,
                    r.lcoe,
                    r.lcos,
                    r.min_dscr,
                    tuple(x.project_cashflow for x in r.annual_results),
                )
            )
        assert len(set(results)) == 1

    def test_key_indicators_present_and_ordered(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        assert r.project_irr is not None
        assert r.equity_irr is not None
        assert 0 < r.static_payback < 25
        assert r.discounted_payback >= r.static_payback
        assert r.roi is not None
        assert r.min_dscr is not None

    def test_caliber_notes_present(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        assert len(r.notes) >= 15
        assert any("Year 0" in n for n in r.notes)
        assert any("LCOE" in n for n in r.notes)

    def test_parameter_sources_registered(self, golden_pv_storage):
        """§83、§91：所有重要参数必须记录来源。"""
        r = calculation_engine.calculate(golden_pv_storage)
        assert r.parameter_sources
        assert r.parameter_sources["pv.pv_capacity_kwp"]["source_type"] == "USER_INPUT"
        assert "policy.version" not in r.parameter_sources  # 未挂政策文件时不应出现

    def test_hubei_policy_is_recorded(self, golden_pv_storage, hubei_policy):
        """§36、§90：挂了政策就必须能在报告中显示政策版本。"""
        project = golden_pv_storage.model_copy(deep=True)
        project.policy = hubei_policy
        r = calculation_engine.calculate(project)
        assert any("政策" in n for n in r.notes)
        assert r.parameter_sources["policy.version"]["source_type"] == "POLICY"

    def test_energy_balance_across_all_years(self, golden_pv_storage):
        """§113：全部 25 年电量守恒。"""
        r = calculation_engine.calculate(golden_pv_storage)
        for row in r.annual_results:
            lhs = row.pv_generation_kwh
            rhs = row.pv_self_use_kwh + row.pv_to_storage_kwh + row.pv_export_kwh + row.pv_loss_kwh
            assert abs(lhs - rhs) <= 1e-6

    def test_cumulative_matches_running_sum(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        assert r.cumulative_cashflow[-1] == pytest.approx(sum(r.project_cashflows))
        assert r.annual_results[-1].cumulative_project_cashflow == pytest.approx(sum(r.project_cashflows))
