"""GUI 测试（规范 §152、§154、§155）。

在 ``offscreen`` 平台下真实创建窗口，验证：

* 界面能加载项目、能触发计算；
* **界面上显示的指标与 CalculationResult 完全一致**（§154）；
* 界面代码没有引入任何计算逻辑（§8、§148）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面测试")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cenep.calculation.engine import calculation_engine  # noqa: E402
from cenep.domain.enums import ProjectType  # noqa: E402
from cenep.ui.app import create_window  # noqa: E402

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "cenep" / "ui"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path: Path):
    win = create_window(db_path=tmp_path / "gui.db")
    yield win
    win.close()


class TestWindowStructure:
    def test_nine_tabs(self, window):
        """§97 + V2 §5 + V2.1 §5.4：主界面包含
        项目/参数/计算/结果/时序仿真/月度账单/敏感性/报告/设置。

        **V2.1 新增账单页**（「用电与电费 → 月度账单」，§5.4）：新页面插入在
        「时序仿真」之后（同属数据输入区），既有 8 个页面的标题与相对顺序**一字未改**，
        因此本断言是"追加一项"，不是改变既有页面的语义。
        """
        titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        assert titles == [
            "项目", "参数", "计算", "结果", "时序仿真", "月度账单",
            "敏感性", "报告", "设置",
        ]

    def test_bills_tab_registered_and_bound(self, window):
        """V2.1 阶段 2：账单页必须注册进主窗口并经 ProjectService 装配账单服务（§7）。"""
        assert window.bills_page is not None
        assert window.bills_page.service is not None
        assert window.bills_page.service.project is window.project

    def test_parameter_tabs(self, window):
        titles = [window.parameters_page.tabs.tabText(i) for i in range(window.parameters_page.tabs.count())]
        assert "光伏参数" in titles
        assert "储能参数" in titles
        assert "电价参数" in titles
        assert "投资参数" in titles
        assert "运维参数" in titles
        assert "融资参数" in titles

    def test_seven_charts(self, window):
        """§107：至少 7 张图表。"""
        assert len(window.result_page.chart_views) == 7
        assert set(window.result_page.chart_views) == {
            "generation",
            "revenue",
            "opex",
            "cashflow",
            "cumulative",
            "irr_sensitivity",
            "npv_sensitivity",
        }

    def test_eleven_indicator_cards(self, window):
        """§105：结果首页直接显示 11 个核心指标。"""
        assert len(window.result_page.value_labels) == 11


class TestGuiMatchesResult:
    def test_gui_values_equal_engine_result(self, window):
        """§154：GUI 结果必须与 CalculationResult 一致。"""
        window.project_page.type_combo.setCurrentIndex(
            window.project_page.type_combo.findData(ProjectType.PV_STORAGE.value)
        )
        window.project_page.name_edit.setText("界面一致性测试")
        window.parameters_page.forms["负荷参数"].rows["load.annual_load_kwh"].set_value(1_200_000.0)
        window.parameters_page.forms["电价参数"].rows["tariff.export_price"].set_value(0.35)

        result = window.calculate_now()
        assert result is not None

        expected = calculation_engine.calculate(
            window.project, include_scenario=False, include_sensitivity=False
        )
        assert result.total_capex == pytest.approx(expected.total_capex)
        assert result.project_irr == pytest.approx(expected.project_irr)
        assert result.project_npv == pytest.approx(expected.project_npv)

        labels = window.result_page.value_labels
        assert labels["total_capex"].text() == f"{expected.total_capex:,.2f}"
        assert labels["project_irr"].text() == f"{expected.project_irr:.2%}"
        assert labels["project_npv"].text() == f"{expected.project_npv:,.2f}"
        assert labels["static_payback"].text() == f"{expected.static_payback:,.2f} 年"

    def test_annual_table_rows_equal_result(self, window):
        result = window.calculate_now()
        assert result is not None
        assert window.result_page.table.rowCount() == len(result.annual_results)
        assert window.result_page.table.item(0, 0).text() == "1"
        assert window.result_page.table.item(0, 6).text() == f"{result.annual_results[0].total_revenue:,.2f}"

    def test_sensitivity_page_rows(self, window):
        result = window.calculate_now()
        assert result is not None
        assert window.sensitivity_page.table.rowCount() == len(result.sensitivity)
        assert window.sensitivity_page.scenario_table.rowCount() == len(result.scenarios)

    def test_report_page_shows_notes(self, window):
        result = window.calculate_now()
        assert result is not None
        text = window.report_page.notes_view.toPlainText()
        assert "LCOE" in text
        assert "Year 0" in text

    def test_charts_have_series(self, window):
        window.calculate_now()
        chart = window.result_page.chart_views["cashflow"].chart()
        assert len(chart.series()) >= 1
        assert chart.series()[0].count() > 0


class TestValidationFeedback:
    def test_invalid_input_reports_chinese_message(self, window):
        """§132：界面必须提示哪个参数有问题。"""
        window.parameters_page.forms["光伏参数"].rows["pv.pv_capacity_kwp"].set_value(None)
        window.parameters_page.forms["光伏参数"].rows["pv.usable_roof_area_m2"].set_value(0.0)
        assert window.calculate_now() is None
        from cenep.calculation.validator import validate_project
        from cenep.calculation.errors import ValidationError

        with pytest.raises(ValidationError) as exc:
            validate_project(window.project)
        assert "光伏装机容量" in str(exc.value)

    def test_tou_ratio_change_kept_in_sync(self, window):
        """比例字段以百分数显示、以小数存储（§14）。"""
        form = window.parameters_page.forms["电价参数"]
        form.rows["tariff.peak_ratio"].set_value(0.4)
        assert form.rows["tariff.peak_ratio"].value() == pytest.approx(0.4)
        assert form.rows["tariff.peak_ratio"].editor.value() == pytest.approx(40.0)


class TestNoCalculationInUi:
    """架构测试：界面层不得引入计算逻辑（§8、§148）。"""

    def test_ui_imports_only_errors_from_calculation(self):
        offenders: list[str] = []
        for path in UI_DIR.glob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if re.match(r"from\s+\.{1,2}calculation", stripped) and "errors" not in stripped:
                    offenders.append(f"{path.name}: {stripped}")
                if re.match(r"from\s+cenep\.calculation", stripped) and "errors" not in stripped:
                    offenders.append(f"{path.name}: {stripped}")
        assert offenders == []

    def test_ui_does_not_reference_metric_functions(self):
        forbidden = ["financial_metrics", "npv(", "irr(", "payback_period", "lcoe(", "lcos("]
        offenders: list[str] = []
        for path in UI_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in source:
                    offenders.append(f"{path.name}: {token}")
        assert offenders == []

    def test_ui_reads_values_from_calculation_result(self):
        source = (UI_DIR / "pages.py").read_text(encoding="utf-8")
        assert "result." in source
        assert "CalculationResult" in source


class TestProjectFileFromGui:
    def test_save_and_open_round_trip(self, window, tmp_path: Path):
        window.project_page.name_edit.setText("界面保存测试")
        window.project_page.type_combo.setCurrentIndex(
            window.project_page.type_combo.findData(ProjectType.COMMERCIAL_STORAGE.value)
        )
        project, errors = window.collect_project()
        assert errors == []
        saved = window.project_service.save_project(project, tmp_path / "gui")
        assert saved.exists()

        reopened = window.project_service.open_project(saved)
        assert reopened.basic_info.project_name == "界面保存测试"
        assert reopened.basic_info.project_type == ProjectType.COMMERCIAL_STORAGE

    def test_new_project_resets_path(self, window, tmp_path: Path):
        window.current_path = tmp_path / "x.nep"
        window.on_new_project()
        assert window.current_path is None
        assert window.project_page.current_path_label.text().startswith("尚未保存")
