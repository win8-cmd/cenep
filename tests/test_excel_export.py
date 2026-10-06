"""Excel 导出测试（规范 §108、§109、§154）。

核心断言：**Excel 中的数值必须与 CalculationResult 完全一致**，且导出层不得引入公式重算。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from cenep.calculation.engine import calculation_engine
from cenep.reports.excel_exporter import SHEET_NAMES, ExcelExporter


@pytest.fixture
def exported(golden_pv_storage, tmp_path: Path):
    result = calculation_engine.calculate(golden_pv_storage)
    exporter = ExcelExporter()
    path = exporter.export(golden_pv_storage, result, tmp_path / "报告.xlsx")
    return golden_pv_storage, result, path


class TestWorkbookStructure:
    def test_file_created_with_dev_suffix(self, exported):
        _, _, path = exported
        assert path.exists()
        assert path.suffix == ".xlsx"

    def test_suffix_is_appended_automatically(self, golden_pv_storage, tmp_path: Path):
        result = calculation_engine.calculate(golden_pv_storage)
        path = ExcelExporter().export(golden_pv_storage, result, tmp_path / "无后缀")
        assert path.name == "无后缀.xlsx"

    def test_dotted_name_not_truncated(self, golden_pv_storage, tmp_path: Path):
        """回归：文件名里的 2061.8kWp / V1.2 这类片段不得被 with_suffix 截掉。"""
        result = calculation_engine.calculate(golden_pv_storage)
        path = ExcelExporter().export(golden_pv_storage, result, tmp_path / "全屋面2061.8kWp_经济评价")
        assert path.name == "全屋面2061.8kWp_经济评价.xlsx"
        assert path.exists()

    def test_thirteen_sheets_in_required_order(self, exported):
        """§108：必须生成 13 张工作表。"""
        _, _, path = exported
        wb = load_workbook(path)
        assert wb.sheetnames == SHEET_NAMES
        assert len(wb.sheetnames) == 13

    def test_declared_sheet_names_match_actual(self, exported):
        _, result, path = exported
        wb = load_workbook(path)
        assert ExcelExporter().export_sheets(None, result) == wb.sheetnames


class TestValuesComeFromResult:
    def test_cashflow_sheet_matches_annual_results(self, exported):
        _, result, path = exported
        ws = load_workbook(path, data_only=True)["年度现金流"]
        # 第 3 行是表头，第 4 行是 Year 0，第 5 行起是 Year 1..N
        assert ws.cell(row=4, column=1).value == 0
        assert ws.cell(row=4, column=13).value == pytest.approx(result.project_cashflows[0])
        assert ws.cell(row=4, column=14).value == pytest.approx(result.equity_cashflows[0])
        for offset, row in enumerate(result.annual_results):
            excel_row = 5 + offset
            assert ws.cell(row=excel_row, column=1).value == row.year
            assert ws.cell(row=excel_row, column=7).value == pytest.approx(row.total_revenue)
            assert ws.cell(row=excel_row, column=13).value == pytest.approx(row.project_cashflow)
            assert ws.cell(row=excel_row, column=14).value == pytest.approx(row.equity_cashflow)
            assert ws.cell(row=excel_row, column=15).value == pytest.approx(row.cumulative_project_cashflow)

    def test_metrics_sheet_matches_result(self, exported):
        _, result, path = exported
        ws = load_workbook(path, data_only=True)["财务指标"]
        text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
        assert f"{result.project_irr:.6f}" in text
        assert "LCOE" in text and "LCOS" in text
        assert "最低 DSCR" in text

    def test_total_capex_matches(self, exported):
        _, result, path = exported
        ws = load_workbook(path, data_only=True)["投资参数"]
        values = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, (int, float))]
        assert any(v == pytest.approx(result.total_capex) for v in values)

    def test_sensitivity_rows_match(self, exported):
        _, result, path = exported
        ws = load_workbook(path, data_only=True)["敏感性分析"]
        # 表头在第 3 行，数据从第 4 行开始
        data_rows = [r for r in ws.iter_rows(min_row=4) if r[0].value]
        assert len(data_rows) == len(result.sensitivity)

    def test_scenario_rows_match(self, exported):
        _, result, path = exported
        ws = load_workbook(path, data_only=True)["情景分析"]
        data_rows = [r for r in ws.iter_rows(min_row=4) if r[0].value]
        assert len(data_rows) == len(result.scenarios)

    def test_parameter_sources_rows_match(self, exported):
        _, result, path = exported
        ws = load_workbook(path, data_only=True)["参数来源"]
        data_rows = [r for r in ws.iter_rows(min_row=4) if r[0].value]
        assert len(data_rows) == len(result.parameter_sources)

    def test_no_formulas_written(self, exported):
        """§109：导出层不重新计算。本实现只写数值，因此工作簿内不应出现公式。"""
        _, _, path = exported
        wb = load_workbook(path)
        formula_cells = [
            f"{ws.title}!{cell.coordinate}"
            for ws in wb.worksheets
            for row in ws.iter_rows()
            for cell in row
            if isinstance(cell.value, str) and cell.value.startswith("=")
        ]
        assert formula_cells == []


class TestPolicySheet:
    def test_without_policy(self, exported):
        _, _, path = exported
        ws = load_workbook(path, data_only=True)["政策依据"]
        text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
        assert "未关联政策" in text
        assert "请自行核对现行政策" in text

    def test_with_hubei_policy(self, golden_pv_storage, hubei_policy, tmp_path: Path):
        project = golden_pv_storage.model_copy(deep=True)
        project.policy = hubei_policy
        result = calculation_engine.calculate(project)
        path = ExcelExporter().export(project, result, tmp_path / "p.xlsx")
        ws = load_workbook(path, data_only=True)["政策依据"]
        text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
        assert "2025-10-01" in text
        assert "本测算采用政策" in text


class TestThreeProjectTypes:
    @pytest.mark.parametrize("fixture_name", ["golden_pv", "golden_storage", "golden_pv_storage"])
    def test_all_three_types_export(self, fixture_name, request, tmp_path: Path):
        project = request.getfixturevalue(fixture_name)
        result = calculation_engine.calculate(project)
        path = ExcelExporter().export(project, result, tmp_path / f"{fixture_name}.xlsx")
        wb = load_workbook(path)
        assert wb.sheetnames == SHEET_NAMES
