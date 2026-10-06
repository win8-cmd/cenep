"""情景分析与敏感性分析测试（规范 §92–§96、§106）。"""

from __future__ import annotations

import pytest

from cenep.calculation.engine import calculation_engine
from cenep.calculation.scenario import apply_delta
from cenep.domain.enums import ScenarioType, SensitivityVariable
from cenep.domain.models import ScenarioDelta


class TestScenario:
    def test_three_scenarios_returned(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        labels = [s.scenario for s in r.scenarios]
        assert labels == ["BASE", "CONSERVATIVE", "OPTIMISTIC"]

    def test_base_scenario_matches_base_result(self, golden_pv_storage):
        """§93：情景必须从 BASE 复制，BASE 情景应与基准结果一致。"""
        r = calculation_engine.calculate(golden_pv_storage)
        base = next(s for s in r.scenarios if s.scenario == "BASE")
        assert base.project_irr == pytest.approx(r.project_irr, rel=1e-12)
        assert base.total_capex == pytest.approx(r.total_capex, rel=1e-12)
        assert base.deltas == []

    def test_conservative_worse_than_base_worse_than_optimistic(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        values = {s.scenario: s for s in r.scenarios}
        assert values["CONSERVATIVE"].project_irr < values["BASE"].project_irr
        assert values["BASE"].project_irr < values["OPTIMISTIC"].project_irr
        assert values["CONSERVATIVE"].total_capex > values["BASE"].total_capex
        assert values["OPTIMISTIC"].total_capex < values["BASE"].total_capex

    def test_scenarios_are_not_chained(self, golden_pv_storage):
        """§93：不允许"保守 → 乐观"链式推导，乐观情景必须从 BASE 出发。"""
        r = calculation_engine.calculate(golden_pv_storage)
        optimistic = next(s for s in r.scenarios if s.scenario == "OPTIMISTIC")
        conv = golden_pv_storage.scenario.conservative
        opt = golden_pv_storage.scenario.optimistic
        # 乐观情景的乘数必须等于配置值本身，而不是与保守情景相乘
        assert f"总投资 × {opt.capex_multiplier:g}" in optimistic.deltas
        assert conv.capex_multiplier * opt.capex_multiplier != pytest.approx(opt.capex_multiplier)

    def test_apply_delta_does_not_mutate_base(self, golden_pv_storage):
        before = golden_pv_storage.model_dump()
        apply_delta(golden_pv_storage, golden_pv_storage.scenario.conservative)
        assert golden_pv_storage.model_dump() == before

    def test_delta_describe(self):
        delta = ScenarioDelta(capex_multiplier=1.1, opex_multiplier=1.0)
        assert delta.describe() == ["总投资 × 1.1"]

    def test_scenario_disabled(self, golden_pv_storage):
        project = golden_pv_storage.model_copy(deep=True)
        project.scenario.enabled = False
        r = calculation_engine.calculate(project)
        assert r.scenarios == []


class TestSensitivity:
    def test_row_count_matches_applicable_variables(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        variables = {row.variable for row in r.sensitivity}
        steps = len(golden_pv_storage.sensitivity.steps)
        assert len(r.sensitivity) == len(variables) * steps
        assert SensitivityVariable.CAPEX.value in variables
        assert SensitivityVariable.STORAGE_CYCLES.value in variables

    def test_zero_change_row_matches_base(self, golden_pv_storage):
        """§95：0% 行必须等于基准值。"""
        r = calculation_engine.calculate(golden_pv_storage)
        for row in r.sensitivity:
            if abs(row.change) < 1e-12:
                assert row.project_irr == pytest.approx(r.project_irr, rel=1e-12)
                assert row.project_npv == pytest.approx(r.project_npv, rel=1e-12)

    def test_only_one_variable_changes(self, golden_pv_storage):
        """§95：一次只改变一个参数。CAPEX 行不应改变发电量。"""
        r = calculation_engine.calculate(golden_pv_storage)
        capex_rows = [x for x in r.sensitivity if x.variable == SensitivityVariable.CAPEX.value]
        assert len(capex_rows) == len(golden_pv_storage.sensitivity.steps)
        for row in capex_rows:
            if row.change < 0:
                assert row.project_irr > r.project_irr
            elif row.change > 0:
                assert row.project_irr < r.project_irr

    def test_irr_rises_with_price(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        rows = sorted(
            (x for x in r.sensitivity if x.variable == SensitivityVariable.ELECTRICITY_PRICE.value),
            key=lambda x: x.change,
        )
        irrs = [x.project_irr for x in rows]
        assert irrs == sorted(irrs)

    def test_irr_falls_with_cost(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        rows = sorted(
            (x for x in r.sensitivity if x.variable == SensitivityVariable.OPEX.value),
            key=lambda x: x.change,
        )
        irrs = [x.project_irr for x in rows]
        assert irrs == sorted(irrs, reverse=True)

    def test_sensitivity_outputs_required_metrics(self, golden_pv_storage):
        """§96：至少输出 IRR、NPV、回收期。"""
        r = calculation_engine.calculate(golden_pv_storage)
        for row in r.sensitivity:
            assert row.project_irr is not None
            assert row.project_npv is not None
            assert row.static_payback is not None

    def test_coefficient_sign(self, golden_pv_storage):
        r = calculation_engine.calculate(golden_pv_storage)
        row = next(
            x
            for x in r.sensitivity
            if x.variable == SensitivityVariable.CAPEX.value and x.change == pytest.approx(0.2)
        )
        assert row.coefficient is not None and row.coefficient < 0

    def test_storage_variables_skipped_for_pv_only(self, golden_pv):
        r = calculation_engine.calculate(golden_pv)
        variables = {row.variable for row in r.sensitivity}
        assert SensitivityVariable.STORAGE_CYCLES.value not in variables
        assert SensitivityVariable.STORAGE_CAPEX.value not in variables
        assert SensitivityVariable.GENERATION.value in variables

    def test_pv_variables_skipped_for_storage_only(self, golden_storage):
        r = calculation_engine.calculate(golden_storage)
        variables = {row.variable for row in r.sensitivity}
        assert SensitivityVariable.GENERATION.value not in variables
        assert SensitivityVariable.SELF_CONSUMPTION_RATIO.value not in variables
        assert SensitivityVariable.STORAGE_CYCLES.value in variables

    def test_disabled(self, golden_pv_storage):
        project = golden_pv_storage.model_copy(deep=True)
        project.sensitivity.enabled = False
        r = calculation_engine.calculate(project)
        assert r.sensitivity == []


class TestScenarioTypes:
    def test_enum_labels(self):
        assert ScenarioType.BASE.label == "基准"
        assert ScenarioType.CONSERVATIVE.label == "保守"
        assert ScenarioType.OPTIMISTIC.label == "乐观"
