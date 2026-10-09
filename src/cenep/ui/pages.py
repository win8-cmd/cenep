"""主工作区页面：项目 / 参数 / 计算 / 结果 / 时序仿真 / 月度账单 / 敏感性 / 报告 / 设置。

**本模块不含任何计算**：所有数值都来自 ``CalculationResult``（经济评价）或
``BillService``（V2.1 账单事实，§5.4）。
"""

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import numpy as np
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..application.bill_service import SORT_FIELDS
from ..calculation.errors import ValidationError
from ..data.bill_importer import BILL_COLUMNS, list_sheets
from ..data.importer import read_table_from_sheet
from ..domain.bill_models import ElectricityBill, label_of
from ..domain.enums import (
    BillQualityStatus,
    DuplicateStrategy,
    ProjectType,
    ScenarioType,
    SensitivityVariable,
)
from ..domain.models import Project
from ..domain.results import CalculationResult
from .charts import (
    CHART_TITLES,
    RANGE_DAY,
    RANGE_MONTH,
    RANGE_YEAR,
    create_chart,
    create_range_selector,
)
from .field_spec import SectionForm
from .sections import (
    ALL_SECTIONS,
    BILL_SECTIONS,
    DUPLICATE_STRATEGY_CHOICES,
    SOURCE_LEGEND,
)


def _no_edit(table: QTableWidget) -> None:
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "无法计算"
    return f"{value:,.{digits}f}{suffix}"


def _fmt_pct(value: float | None) -> str:
    return "无法计算" if value is None else f"{value:.2%}"


def _fmt_payback(value: float | None) -> str:
    return "未回收" if value is None else f"{value:,.2f} 年"


# --------------------------------------------------------------------------- #
# 1. 项目页（规范 §98）
# --------------------------------------------------------------------------- #
class ProjectPage(QWidget):
    """项目基本信息与文件操作。"""

    new_project_requested = Signal()
    open_project_requested = Signal()
    save_project_requested = Signal()
    save_as_project_requested = Signal()
    project_type_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        file_box = QGroupBox("项目文件", self)
        file_layout = QHBoxLayout(file_box)
        for text, signal in (
            ("新建项目", self.new_project_requested),
            ("打开项目…", self.open_project_requested),
            ("保存", self.save_project_requested),
            ("另存为…", self.save_as_project_requested),
        ):
            button = QPushButton(text, file_box)
            button.clicked.connect(signal.emit)
            file_layout.addWidget(button)
        layout.addWidget(file_box)

        info_box = QGroupBox("项目基本信息", self)
        form = QFormLayout(info_box)
        self.name_edit = QLineEdit(info_box)
        self.province_edit = QLineEdit(info_box)
        self.city_edit = QLineEdit(info_box)
        self.type_combo = QComboBox(info_box)
        for ptype in ProjectType:
            self.type_combo.addItem(ptype.label, ptype.value)
        self.type_combo.currentIndexChanged.connect(
            lambda _: self.project_type_changed.emit(self.project_type())
        )
        self.customer_edit = QLineEdit(info_box)
        self.industry_edit = QLineEdit(info_box)
        self.date_edit = QDateEdit(info_box)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.notes_edit = QTextEdit(info_box)
        self.notes_edit.setMaximumHeight(70)

        form.addRow("项目名称：", self.name_edit)
        form.addRow("省份：", self.province_edit)
        form.addRow("城市：", self.city_edit)
        form.addRow("项目类型：", self.type_combo)
        form.addRow("业主名称：", self.customer_edit)
        form.addRow("所属行业：", self.industry_edit)
        form.addRow("评价日期：", self.date_edit)
        form.addRow("备注：", self.notes_edit)
        layout.addWidget(info_box)

        current_box = QGroupBox("当前文件", self)
        current_layout = QVBoxLayout(current_box)
        self.current_path_label = QLabel("尚未保存（未命名项目）", current_box)
        self.current_path_label.setWordWrap(True)
        current_layout.addWidget(self.current_path_label)
        layout.addWidget(current_box)

        recent_box = QGroupBox("最近打开", self)
        recent_layout = QVBoxLayout(recent_box)
        self.recent_list = QListWidget(recent_box)
        recent_layout.addWidget(self.recent_list)
        layout.addWidget(recent_box, 1)

    # ------------------------------------------------------------------ #
    def project_type(self) -> ProjectType:
        return ProjectType(self.type_combo.currentData())

    def set_current_path(self, path: Path | None) -> None:
        self.current_path_label.setText(str(path) if path else "尚未保存（未命名项目）")

    def load(self, project: Project) -> None:
        info = project.basic_info
        self.name_edit.setText(info.project_name)
        self.province_edit.setText(info.province)
        self.city_edit.setText(info.city)
        index = self.type_combo.findData(info.project_type.value)
        if index >= 0:
            self.type_combo.setCurrentIndex(index)
        self.customer_edit.setText(info.customer_name or "")
        self.industry_edit.setText(info.industry or "")
        self.date_edit.setDate(
            date(info.evaluation_date.year, info.evaluation_date.month, info.evaluation_date.day)
        )
        self.notes_edit.setPlainText(info.notes or "")

    def apply(self, project: Project) -> list[str]:
        errors: list[str] = []
        try:
            info = project.basic_info
            info.project_name = self.name_edit.text().strip() or "未命名项目"
            info.province = self.province_edit.text().strip()
            info.city = self.city_edit.text().strip()
            info.customer_name = self.customer_edit.text().strip() or None
            info.industry = self.industry_edit.text().strip() or None
            info.evaluation_date = self.date_edit.date().toPython()
            info.notes = self.notes_edit.toPlainText().strip() or None
        except Exception as exc:
            errors.append(f"项目基本信息：{exc}")
        return errors

    def set_recent(self, items: list[dict]) -> None:
        self.recent_list.clear()
        for item in items:
            self.recent_list.addItem(f"{item.get('project_name') or '未命名'} — {item.get('path')}")


# --------------------------------------------------------------------------- #
# 2. 参数页（规范 §99–§104）
# --------------------------------------------------------------------------- #
class ParametersPage(QWidget):
    """按分组自动生成的参数表单。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget(self)
        self.forms: dict[str, SectionForm] = {}
        for section in ALL_SECTIONS:
            form = SectionForm(section, self.tabs)
            self.forms[section.title] = form
            self.tabs.addTab(form, section.title)
        layout.addWidget(self.tabs, 1)

        legend = QHBoxLayout()
        legend.addWidget(QLabel("参数配色：", self))
        for color, text in SOURCE_LEGEND:
            chip = QLabel("　", self)
            chip.setStyleSheet(f"background-color: {color}; border: 1px solid #BFBFBF;")
            chip.setFixedWidth(24)
            legend.addWidget(chip)
            legend.addWidget(QLabel(text, self))
        legend.addStretch(1)
        layout.addLayout(legend)

        self.hint_label = QLabel("", self)
        self.hint_label.setStyleSheet("color: #C00000;")
        layout.addWidget(self.hint_label)

    def load(self, project: Project) -> None:
        for form in self.forms.values():
            form.load(project)
        self.hint_label.setText("")

    def apply(self, project: Project) -> list[str]:
        errors: list[str] = []
        for form in self.forms.values():
            errors.extend(form.apply(project))
        self.hint_label.setText("；".join(errors) if errors else "")
        return errors


# --------------------------------------------------------------------------- #
# 3. 计算页
# --------------------------------------------------------------------------- #
class CalculatePage(QWidget):
    """计算入口与校验提示。"""

    calculate_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        box = QGroupBox("计算", self)
        box_layout = QVBoxLayout(box)
        self.calculate_button = QPushButton("开始计算", box)
        self.calculate_button.setMinimumHeight(44)
        self.calculate_button.clicked.connect(self.calculate_requested.emit)
        box_layout.addWidget(self.calculate_button)

        self.scenario_check = QCheckBox("同时计算情景分析（保守 / 基准 / 乐观）", box)
        self.scenario_check.setChecked(True)
        self.sensitivity_check = QCheckBox("同时计算敏感性分析", box)
        self.sensitivity_check.setChecked(True)
        box_layout.addWidget(self.scenario_check)
        box_layout.addWidget(self.sensitivity_check)

        self.status_label = QLabel("尚未计算", box)
        box_layout.addWidget(self.status_label)
        layout.addWidget(box)

        self.error_box = QPlainTextEdit(self)
        self.error_box.setReadOnly(True)
        self.error_box.setPlaceholderText("校验与计算信息会显示在这里")
        layout.addWidget(self.error_box, 1)

    def options(self) -> tuple[bool, bool]:
        return self.scenario_check.isChecked(), self.sensitivity_check.isChecked()


# --------------------------------------------------------------------------- #
# 4. 结果页（规范 §105、§107）
# --------------------------------------------------------------------------- #
class ResultPage(QWidget):
    """结果首页指标卡 + 年度明细表 + 7 张图表。"""

    INDICATORS = (
        ("total_capex", "总投资（元）", lambda r: f"{r.total_capex:,.2f}"),
        ("first_year_generation", "首年发电量（kWh）", lambda r: f"{r.first_year_generation:,.0f}"),
        ("first_year_revenue", "首年收入（元）", lambda r: f"{r.first_year_revenue:,.2f}"),
        ("project_irr", "项目 IRR", lambda r: _fmt_pct(r.project_irr)),
        ("equity_irr", "资本金 IRR", lambda r: _fmt_pct(r.equity_irr)),
        ("project_npv", "项目 NPV（元）", lambda r: f"{r.project_npv:,.2f}"),
        ("static_payback", "静态回收期", lambda r: _fmt_payback(r.static_payback)),
        ("discounted_payback", "动态回收期", lambda r: _fmt_payback(r.discounted_payback)),
        ("lcoe", "LCOE（元/kWh）", lambda r: "不适用" if r.lcoe is None else f"{r.lcoe:.4f}"),
        ("lcos", "LCOS（元/kWh）", lambda r: "不适用" if r.lcos is None else f"{r.lcos:.4f}"),
        ("min_dscr", "最低 DSCR", lambda r: "—" if r.min_dscr is None else f"{r.min_dscr:.4f}"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        cards = QGroupBox("核心指标", self)
        grid = QGridLayout(cards)
        self.value_labels: dict[str, QLabel] = {}
        for index, (key, label, _fn) in enumerate(self.INDICATORS):
            row, col = divmod(index, 6)
            title = QLabel(label, cards)
            title.setStyleSheet("color: #606060;")
            value = QLabel("—", cards)
            value.setStyleSheet("font-size: 15px; font-weight: bold;")
            grid.addWidget(title, row * 2, col)
            grid.addWidget(value, row * 2 + 1, col)
            self.value_labels[key] = value
        layout.addWidget(cards)

        self.summary_label = QLabel("尚未计算", self)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        self.table = QTableWidget(0, 11, self)
        self.table.setHorizontalHeaderLabels(
            ["年份", "负荷(kWh)", "发电量(kWh)", "自用(kWh)", "上网(kWh)", "储能放电(kWh)", "收入(元)", "运维费(元)", "EBITDA(元)", "项目现金流(元)", "累计现金流(元)"]
        )
        _no_edit(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tabs.addTab(self.table, "年度明细")

        self.chart_tabs = QTabWidget(self.tabs)
        self.tabs.addTab(self.chart_tabs, "图表")
        self.chart_views: dict[str, QChartView] = {}
        for key, title in (
            ("generation", "年度发电量"),
            ("revenue", "年度收入"),
            ("opex", "年度运维费"),
            ("cashflow", "年度现金流"),
            ("cumulative", "累计现金流"),
            ("irr_sensitivity", "项目IRR敏感性"),
            ("npv_sensitivity", "NPV敏感性"),
        ):
            view = QChartView(QChart(), self.chart_tabs)
            view.setRenderHint(QPainter.RenderHint.Antialiasing)
            self.chart_views[key] = view
            self.chart_tabs.addTab(view, title)

    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        for label in self.value_labels.values():
            label.setText("—")
        self.table.setRowCount(0)
        self.summary_label.setText("尚未计算")

    def show_result(self, result: CalculationResult) -> None:
        for key, _label, formatter in self.INDICATORS:
            self.value_labels[key].setText(formatter(result))
        self.summary_label.setText(
            f"{result.project_name} · 计算期 {result.analysis_period} 年 · "
            f"光伏 {result.pv_capacity_kwp:,.2f} kWp · 储能 {result.storage_power_kw:,.2f} kW / "
            f"{result.storage_energy_kwh:,.2f} kWh · 年均收入 {result.annual_revenue:,.2f} 元"
        )
        self._fill_table(result)
        self._fill_charts(result)

    def _fill_table(self, result: CalculationResult) -> None:
        self.table.setRowCount(len(result.annual_results))
        for row, item in enumerate(result.annual_results):
            values = [
                item.year,
                f"{item.load_kwh:,.0f}",
                f"{item.pv_generation_kwh:,.0f}",
                f"{item.pv_self_use_kwh:,.0f}",
                f"{item.pv_export_kwh:,.0f}",
                f"{item.storage_discharge_kwh:,.0f}",
                f"{item.total_revenue:,.2f}",
                f"{item.opex:,.2f}",
                f"{item.ebitda:,.2f}",
                f"{item.project_cashflow:,.2f}",
                f"{item.cumulative_project_cashflow:,.2f}",
            ]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if col > 0:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, cell)

    def _fill_charts(self, result: CalculationResult) -> None:
        years = [r.year for r in result.annual_results]
        self._set_line_chart(
            "generation", "年度发电量（kWh）", years, [("发电量", [r.pv_generation_kwh for r in result.annual_results])]
        )
        self._set_line_chart(
            "revenue", "年度收入（元）", years, [("总收入", [r.total_revenue for r in result.annual_results])]
        )
        self._set_line_chart(
            "opex", "年度运维费（元）", years, [("运维费", [r.opex for r in result.annual_results])]
        )
        self._set_line_chart(
            "cashflow",
            "年度现金流（元）",
            years,
            [
                ("项目现金流", [r.project_cashflow for r in result.annual_results]),
                ("资本金现金流", [r.equity_cashflow for r in result.annual_results]),
            ],
        )
        self._set_line_chart(
            "cumulative",
            "累计现金流（元）",
            [0] + years,
            [("累计项目现金流", result.cumulative_cashflow)],
        )

        variables = sorted({row.variable_label for row in result.sensitivity})
        irr_series: list[tuple[str, list[float]]] = []
        npv_series: list[tuple[str, list[float]]] = []
        first_var = variables[0] if variables else None
        if first_var is not None:
            steps = sorted({row.change for row in result.sensitivity if row.variable_label == first_var})
            for label in variables:
                rows = sorted(
                    (r for r in result.sensitivity if r.variable_label == label), key=lambda r: r.change
                )
                if len(rows) != len(steps):
                    continue
                irr_series.append((label, [(r.project_irr or 0.0) * 100 for r in rows]))
                npv_series.append((label, [r.project_npv for r in rows]))
            x_values = [s * 100 for s in steps]
        else:
            x_values = [0.0]
        self._set_line_chart("irr_sensitivity", "项目IRR敏感性（%）", x_values, irr_series, x_title="参数变化率（%）")
        self._set_line_chart("npv_sensitivity", "NPV敏感性（元）", x_values, npv_series, x_title="参数变化率（%）")

    def _set_line_chart(
        self,
        key: str,
        title: str,
        x_values: list[float],
        series: list[tuple[str, list[float]]],
        x_title: str = "年份",
    ) -> None:
        chart = QChart()
        chart.setTitle(title)
        max_y = 0.0
        min_y = 0.0
        for name, values in series:
            line = QLineSeries()
            line.setName(name)
            for x, y in zip(x_values, values, strict=False):
                line.append(float(x), float(y))
                max_y = max(max_y, float(y))
                min_y = min(min_y, float(y))
            chart.addSeries(line)
        if x_values:
            axis_x = QValueAxis()
            axis_x.setTitleText(x_title)
            axis_x.setLabelFormat("%.0f")
            axis_x.setRange(min(x_values), max(x_values) if len(x_values) > 1 else min(x_values) + 1)
            chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
            axis_y = QValueAxis()
            axis_y.setLabelFormat("%.0f")
            span = max(max_y - min_y, 1.0)
            axis_y.setRange(min_y - span * 0.05, max_y + span * 0.05)
            chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
            for line in chart.series():
                line.attachAxis(axis_x)
                line.attachAxis(axis_y)
        chart.legend().setVisible(bool(series))
        self.chart_views[key].setChart(chart)


# --------------------------------------------------------------------------- #
# 5. 敏感性页
# --------------------------------------------------------------------------- #
class SensitivityPage(QWidget):
    """敏感性 + 情景分析表格。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)

        self.table = QTableWidget(0, 7, self)
        self.table.setHorizontalHeaderLabels(
            ["变化因素", "变化率", "项目IRR", "资本金IRR", "项目NPV（元）", "静态回收期（年）", "敏感度系数"]
        )
        _no_edit(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tabs.addTab(self.table, "敏感性分析")

        self.scenario_table = QTableWidget(0, 6, self)
        self.scenario_table.setHorizontalHeaderLabels(
            ["情景", "总投资（元）", "首年收入（元）", "项目IRR", "资本金IRR", "情景乘数"]
        )
        _no_edit(self.scenario_table)
        self.scenario_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tabs.addTab(self.scenario_table, "情景分析")

        layout.addWidget(self.tabs, 1)

    def show_result(self, result: CalculationResult) -> None:
        self.table.setRowCount(len(result.sensitivity))
        for row, item in enumerate(result.sensitivity):
            values = [
                item.variable_label,
                f"{item.change:+.0%}",
                _fmt_pct(item.project_irr),
                _fmt_pct(item.equity_irr),
                f"{item.project_npv:,.2f}",
                _fmt_payback(item.static_payback),
                "—" if item.coefficient is None else f"{item.coefficient:.3f}",
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))

        self.scenario_table.setRowCount(len(result.scenarios))
        for row, item in enumerate(result.scenarios):
            values = [
                item.label,
                f"{item.total_capex:,.2f}",
                f"{item.first_year_revenue:,.2f}",
                _fmt_pct(item.project_irr),
                _fmt_pct(item.equity_irr),
                "、".join(item.deltas) if item.deltas else "基准",
            ]
            for col, value in enumerate(values):
                self.scenario_table.setItem(row, col, QTableWidgetItem(str(value)))


# --------------------------------------------------------------------------- #
# 6. 报告页
# --------------------------------------------------------------------------- #
class ReportPage(QWidget):
    """Excel / PDF 导出入口（数据一律来自 CalculationResult）。"""

    export_excel_requested = Signal()
    export_pdf_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        box = QGroupBox("导出", self)
        box_layout = QVBoxLayout(box)
        self.excel_button = QPushButton("导出 Excel（13 张工作表）", box)
        self.excel_button.clicked.connect(self.export_excel_requested.emit)
        self.pdf_button = QPushButton("导出 PDF 报告（15 章）", box)
        self.pdf_button.clicked.connect(self.export_pdf_requested.emit)
        box_layout.addWidget(self.excel_button)
        box_layout.addWidget(self.pdf_button)
        layout.addWidget(box)

        notes_box = QGroupBox("报告口径说明（来自 CalculationResult）", self)
        notes_layout = QVBoxLayout(notes_box)
        self.notes_view = QTextEdit(notes_box)
        self.notes_view.setReadOnly(True)
        notes_layout.addWidget(self.notes_view)
        layout.addWidget(notes_box, 1)

    def show_result(self, project: Project, result: CalculationResult) -> None:
        lines = [f"项目：{project.basic_info.project_name}（{project.basic_info.project_type.label}）"]
        if project.policy is not None:
            lines.append(f"政策版本：{project.policy.display_version}")
        lines.append("")
        lines.extend(f"• {note}" for note in result.notes)
        self.notes_view.setPlainText("\n".join(lines))


# --------------------------------------------------------------------------- #
# 7. 设置页
# --------------------------------------------------------------------------- #
class SettingsPage(QWidget):
    """情景乘数、敏感性步长与策略模板提示。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        scenario_box = QGroupBox("情景乘数（系统默认值，属“假设值”，可修改）", self)
        scenario_form = QFormLayout(scenario_box)
        self.conservative_edits: dict[str, QDoubleSpinBox] = {}
        self.optimistic_edits: dict[str, QDoubleSpinBox] = {}
        labels = [
            ("capex_multiplier", "总投资"),
            ("electricity_price_multiplier", "电价"),
            ("generation_multiplier", "发电量"),
            ("opex_multiplier", "运维成本"),
            ("self_consumption_ratio_multiplier", "自用比例"),
            ("storage_cycles_multiplier", "储能循环次数"),
            ("storage_capex_multiplier", "储能投资"),
            ("interest_rate_multiplier", "贷款利率"),
        ]
        for key, label in labels:
            row = QWidget(scenario_box)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            cons = QDoubleSpinBox(row)
            cons.setRange(0.1, 5.0)
            cons.setSingleStep(0.01)
            cons.setDecimals(3)
            opti = QDoubleSpinBox(row)
            opti.setRange(0.1, 5.0)
            opti.setSingleStep(0.01)
            opti.setDecimals(3)
            row_layout.addWidget(QLabel("保守", row))
            row_layout.addWidget(cons)
            row_layout.addWidget(QLabel("乐观", row))
            row_layout.addWidget(opti)
            scenario_form.addRow(f"{label}：", row)
            self.conservative_edits[key] = cons
            self.optimistic_edits[key] = opti
        layout.addWidget(scenario_box)

        sens_box = QGroupBox("敏感性分析", self)
        sens_layout = QVBoxLayout(sens_box)
        self.steps_edit = QLineEdit(sens_box)
        self.steps_edit.setPlaceholderText("用英文逗号分隔，例如：-0.2,-0.1,0,0.1,0.2")
        sens_layout.addWidget(QLabel("变化率步长（小数）：", sens_box))
        sens_layout.addWidget(self.steps_edit)
        self.variables_label = QLabel("", sens_box)
        self.variables_label.setWordWrap(True)
        sens_layout.addWidget(self.variables_label)
        layout.addWidget(sens_box)

        policy_box = QGroupBox("政策（不硬编码，需用户填写）", self)
        policy_layout = QVBoxLayout(policy_box)
        self.policy_label = QLabel("尚未关联政策模板。", policy_box)
        self.policy_label.setWordWrap(True)
        policy_layout.addWidget(self.policy_label)
        layout.addWidget(policy_box, 1)

    def load(self, project: Project) -> None:
        for key, widget in self.conservative_edits.items():
            widget.setValue(getattr(project.scenario.conservative, key))
        for key, widget in self.optimistic_edits.items():
            widget.setValue(getattr(project.scenario.optimistic, key))
        self.steps_edit.setText(",".join(f"{s:g}" for s in project.sensitivity.steps))
        self.variables_label.setText(
            "参与变量：" + "、".join(v.label for v in project.sensitivity.variables)
        )
        if project.policy is None:
            self.policy_label.setText(
                "尚未关联政策模板。所有电价与政策性参数均为用户输入，请自行核对项目所在地现行政策。"
            )
        else:
            self.policy_label.setText(
                f"{project.policy.display_version}\n来源：{project.policy.source or '未填写'}\n"
                f"备注：{project.policy.notes or '—'}"
            )

    def apply(self, project: Project) -> list[str]:
        errors: list[str] = []
        try:
            for key, widget in self.conservative_edits.items():
                setattr(project.scenario.conservative, key, widget.value())
            for key, widget in self.optimistic_edits.items():
                setattr(project.scenario.optimistic, key, widget.value())
        except Exception as exc:
            errors.append(f"情景乘数：{exc}")
        try:
            raw = [part.strip() for part in self.steps_edit.text().split(",") if part.strip()]
            project.sensitivity.steps = [float(part) for part in raw]
        except Exception as exc:
            errors.append(f"敏感性步长：{exc}")
        return errors


# =========================================================================== #
# V2 时序仿真页（V2 §5、§49–§50、§68–§71）
# =========================================================================== #
#: §69 时序明细表最多显示的行数（避免把 8760 行塞进控件）
HOURLY_PREVIEW_ROWS = 48


def _pair_table(rows: list[tuple[str, str]]) -> QTableWidget:
    """构造「项目 / 取值」两列表。"""
    table = QTableWidget(len(rows), 2)
    table.setHorizontalHeaderLabels(["项目", "取值"])
    table.verticalHeader().setVisible(False)
    for i, (name, value) in enumerate(rows):
        table.setItem(i, 0, QTableWidgetItem(name))
        table.setItem(i, 1, QTableWidgetItem(value))
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    _no_edit(table)
    return table


def _time_table(headers: list[str], rows: list[list[str]]) -> QTableWidget:
    """构造逐时明细表。"""
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(text))
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    _no_edit(table)
    return table


class TimeSeriesPage(QWidget):
    """V2 时序仿真结果页（V2 §68–§71）与交互图表（V2 §49–§50）。

    **本页不做任何计算**（V2 §61）：全部数值取自 ``CalculationResult`` 内的
    ``time_series_results``（含 ``metrics`` / ``balance`` / ``hourly`` 列式结果）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.banner = QLabel("尚未计算。启用「时序仿真」并执行计算后，可在此查看 8760 小时结果。")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)

        self.inner = QTabWidget(self)
        self._overview = QWidget()
        self._hourly = QWidget()
        self._balance = QWidget()
        self._storage = QWidget()
        self._charts = QWidget()
        for widget, title in (
            (self._overview, "结果概要"),
            (self._hourly, "时序数据"),
            (self._balance, "能源平衡"),
            (self._storage, "储能"),
            (self._charts, "图表"),
        ):
            self.inner.addTab(widget, title)
        layout.addWidget(self.inner, 1)

        self._build_overview()
        self._build_hourly()
        self._build_balance()
        self._build_storage()
        self._build_charts()

        self._report = None
        self._result = None
        self._project = None
        self.clear()

    # ------------------------------------------------------------------ #
    # §68 结果概要
    # ------------------------------------------------------------------ #
    def _build_overview(self) -> None:
        layout = QVBoxLayout(self._overview)
        box = QGroupBox("首要指标（V2 §68）", self._overview)
        inner = QVBoxLayout(box)
        self.overview_table = _pair_table([])
        inner.addWidget(self.overview_table)
        layout.addWidget(box)

        box2 = QGroupBox("时序指标（V2 §41）", self._overview)
        inner2 = QVBoxLayout(box2)
        self.metrics_table = _pair_table([])
        inner2.addWidget(self.metrics_table)
        layout.addWidget(box2)

    # ------------------------------------------------------------------ #
    # §69 时序数据
    # ------------------------------------------------------------------ #
    def _build_hourly(self) -> None:
        layout = QVBoxLayout(self._hourly)
        row = QHBoxLayout()
        row.addWidget(QLabel("时间范围"))
        self.range_box, self.range_mode, self.range_month, self.range_day = create_range_selector()
        # create_range_selector 自带「时间范围」标签，这里直接并入本行
        row.addWidget(self.range_box, 1)
        layout.addLayout(row)

        self.hourly_hint = QLabel("")
        layout.addWidget(self.hourly_hint)
        self.hourly_table = _time_table(["时间"], [])
        layout.addWidget(self.hourly_table, 1)

        self.range_mode.currentIndexChanged.connect(lambda _: self._refresh_hourly())
        self.range_month.currentIndexChanged.connect(lambda _: self._refresh_hourly())
        self.range_day.currentIndexChanged.connect(lambda _: self._refresh_hourly())

    # ------------------------------------------------------------------ #
    # §70 能源平衡
    # ------------------------------------------------------------------ #
    def _build_balance(self) -> None:
        layout = QVBoxLayout(self._balance)
        self.balance_banner = QLabel("")
        self.balance_banner.setWordWrap(True)
        layout.addWidget(self.balance_banner)
        self.balance_table = _pair_table([])
        layout.addWidget(self.balance_table, 1)

    # ------------------------------------------------------------------ #
    # §71 储能
    # ------------------------------------------------------------------ #
    def _build_storage(self) -> None:
        layout = QVBoxLayout(self._storage)
        self.storage_table = _pair_table([])
        layout.addWidget(self.storage_table, 1)

    # ------------------------------------------------------------------ #
    # §49–§50 图表
    # ------------------------------------------------------------------ #
    def _build_charts(self) -> None:
        layout = QVBoxLayout(self._charts)
        row = QHBoxLayout()
        row.addWidget(QLabel("图表"))
        self.chart_box = QComboBox()
        for kind, title in CHART_TITLES.items():
            self.chart_box.addItem(title, kind)
        row.addWidget(self.chart_box)
        self.chart_range_box, self.chart_range_mode, self.chart_month, self.chart_day = (
            create_range_selector()
        )
        row.addWidget(self.chart_range_box, 1)
        layout.addLayout(row)

        self.chart_stack = QTabWidget(self._charts)
        self._chart_widgets: dict[str, QWidget] = {}
        for kind, title in CHART_TITLES.items():
            widget = create_chart(kind)
            self._chart_widgets[kind] = widget
            self.chart_stack.addTab(widget, title)
        layout.addWidget(self.chart_stack, 1)

        self.chart_box.currentIndexChanged.connect(
            lambda index: self.chart_stack.setCurrentIndex(index)
        )
        self.chart_stack.currentChanged.connect(
            lambda index: self.chart_box.setCurrentIndex(index)
        )
        for combo in (self.chart_range_mode, self.chart_month, self.chart_day):
            combo.currentIndexChanged.connect(lambda _: self._apply_chart_range())

    # ------------------------------------------------------------------ #
    # 数据装配（只读 CalculationResult）
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        self._report = None
        self._result = None
        for table in (self.overview_table, self.metrics_table, self.balance_table, self.storage_table):
            table.setRowCount(0)
            table.setColumnCount(2)
            table.setHorizontalHeaderLabels(["项目", "取值"])
        self.hourly_table.setRowCount(0)
        self.hourly_table.setColumnCount(1)
        self.hourly_table.setHorizontalHeaderLabels(["时间"])
        self.hourly_hint.setText("")
        self.balance_banner.setText("")
        for widget in self._chart_widgets.values():
            widget.set_result(None)

    def show_result(self, project: Project | None, result: CalculationResult | None) -> None:
        """装入计算结果；无 V2 结果时显示提示且不报错。"""
        self._result = result
        self._project = project
        report = getattr(result, "time_series_results", None) if result is not None else None

        if report is None or not report.enabled:
            self.banner.setText(
                "未启用时序仿真：本次计算采用 V1 年度模式，因此没有 8760 小时结果。\n"
                "如需时序结果，请在「参数 → 时序仿真总开关」中打开「启用 8760 时序仿真」后重新计算。"
            )
            self.clear()
            return

        self._report = report
        hourly = report.hourly
        count = len(hourly) if hourly is not None else 0
        self.banner.setText(
            f"{report.base_year} 年 · {report.resolution.label} · "
            f"调度策略「{report.dispatch_strategy.label}」· {count} 个计算周期"
        )
        self._fill_overview(report, result)
        self._fill_hourly(report)
        self._fill_balance(report)
        self._fill_storage(report, project)
        for widget in self._chart_widgets.values():
            widget.set_result(result)
        self._apply_chart_range()

    def _fill_overview(self, report, result: CalculationResult) -> None:
        m = report.metrics
        hourly = report.hourly
        first_gen = result.first_year_generation
        if hourly is not None and len(hourly) > 0:
            pv_column = hourly.column("pv_generation")
            if pv_column:
                first_gen = sum(pv_column)

        rows = [
            ("项目投资额", f"{result.total_capex:,.2f} 元"),
            ("光伏容量", f"{result.pv_capacity_kwp:,.2f} kWp"),
            ("储能容量", f"{result.storage_energy_kwh:,.2f} kWh / {result.storage_power_kw:,.2f} kW"),
            ("首年发电量", f"{first_gen:,.2f} kWh"),
            ("首年自用率", _fmt_pct(m.self_consumption_rate)),
            ("首年自给率", _fmt_pct(m.self_sufficiency_rate)),
            ("首年电费节省", f"{m.electricity_cost_saving:,.2f} 元"),
            ("首年储能收益", f"{m.storage_arbitrage_revenue:,.2f} 元"),
            ("项目 IRR", _fmt_pct(result.project_irr)),
            ("股东 IRR", _fmt_pct(result.equity_irr)),
            ("项目 NPV", f"{result.project_npv:,.2f} 元"),
            ("静态回收期", _fmt_payback(result.static_payback)),
            ("动态回收期", _fmt_payback(result.discounted_payback)),
            ("LCOE", "—" if result.lcoe is None else f"{result.lcoe:.4f} 元/kWh"),
            ("LCOS", "—" if result.lcos is None else f"{result.lcos:.4f} 元/kWh"),
        ]
        self._set_pairs(self.overview_table, rows)

        metrics_rows = [
            ("年发电量", f"{m.annual_pv_generation:,.2f} kWh"),
            ("年弃光电量", f"{m.annual_pv_curtailment:,.2f} kWh"),
            ("年购电量", f"{m.annual_grid_purchase:,.2f} kWh"),
            ("年上网电量", f"{m.annual_grid_export:,.2f} kWh"),
            ("年用电量", f"{m.annual_load:,.2f} kWh"),
            ("储能年充电量", f"{m.annual_storage_charge:,.2f} kWh"),
            ("储能年放电量", f"{m.annual_storage_discharge:,.2f} kWh"),
            ("其中电网充电量", f"{m.annual_grid_charge:,.2f} kWh"),
            ("配置循环次数", f"{m.configured_cycles:,.2f} 次"),
            ("实际等效循环次数", f"{m.equivalent_cycles:,.2f} 次"),
            ("最大需量（基准）", f"{m.peak_demand_before:,.2f} kW"),
            ("最大需量（本项目）", f"{m.peak_demand_after:,.2f} kW"),
            ("需量削减", f"{m.demand_saving:,.2f} kW"),
            ("基准电费", f"{m.baseline_electricity_cost:,.2f} 元"),
            ("实际电费", f"{m.actual_electricity_cost:,.2f} 元"),
            ("需量电费节省", f"{m.demand_cost_saving:,.2f} 元"),
            ("光伏自用节省", f"{m.pv_self_consumption_saving:,.2f} 元"),
            ("光伏上网收入", f"{m.pv_export_revenue:,.2f} 元"),
            ("储能套利收益", f"{m.storage_arbitrage_revenue:,.2f} 元"),
            ("储能容量收益", f"{m.storage_capacity_revenue:,.2f} 元"),
            ("储能辅助服务收益", f"{m.storage_ancillary_revenue:,.2f} 元"),
            ("其他收益", f"{m.other_revenue:,.2f} 元"),
            ("总收益", f"{m.total_benefit:,.2f} 元"),
        ]
        self._set_pairs(self.metrics_table, metrics_rows)

    def _fill_hourly(self, report) -> None:
        self._report = report
        self._refresh_hourly()

    def _current_indices(self, hourly) -> list[int]:
        mode = self.range_mode.currentData() or RANGE_YEAR
        month = self.range_month.currentData() or 1
        day = self.range_day.currentData() or 1
        stamps = list(hourly.timestamps)
        if mode == RANGE_MONTH:
            mask = np.array([ts.month == month for ts in stamps])
        elif mode == RANGE_DAY:
            mask = np.array([(ts.month == month and ts.day == day) for ts in stamps])
        else:
            mask = np.ones(len(stamps), dtype=bool)
        return list(np.flatnonzero(mask))

    def _refresh_hourly(self) -> None:
        report = self._report
        hourly = report.hourly if report is not None else None
        if hourly is None or len(hourly) == 0:
            self.hourly_table.setRowCount(0)
            self.hourly_hint.setText("")
            return

        indices = self._current_indices(hourly)
        shown = indices[:HOURLY_PREVIEW_ROWS]
        headers = ["时间", "负荷 kWh", "PV kWh", "PV→负荷", "PV→储能", "PV→上网",
                   "购电 kWh", "上网 kWh", "储能充电", "储能放电", "SOC %", "电价 元/kWh", "动作 / 原因"]
        rows: list[list[str]] = []
        for i in shown:
            row = hourly.row(i)
            rows.append([
                f"{row.timestamp:%Y-%m-%d %H:%M}",
                f"{row.load:,.2f}",
                f"{row.pv_generation:,.2f}",
                f"{row.pv_to_load:,.2f}",
                f"{row.pv_to_storage:,.2f}",
                f"{row.pv_to_grid:,.2f}",
                f"{row.grid_import:,.2f}",
                f"{row.grid_export:,.2f}",
                f"{row.storage_charge:,.2f}",
                f"{row.storage_discharge:,.2f}",
                f"{row.storage_soc_end * 100:,.2f}",
                f"{row.electricity_price:,.4f}",
                f"{row.dispatch_action.label} / {row.dispatch_reason}",
            ])
        table = _time_table(headers, rows)
        self._replace_table(self.hourly_table, table)
        self.hourly_table = table
        self.hourly_hint.setText(
            f"当前筛选共 {len(indices)} 个周期，显示前 {len(shown)} 条"
            f"（浏览器式明细见 Excel 的逐时工作表）"
        )

    def _replace_table(self, old: QTableWidget, new: QTableWidget) -> None:
        parent_layout = old.parentWidget().layout()
        if parent_layout is not None:
            parent_layout.replaceWidget(old, new)
        old.deleteLater()

    def _fill_balance(self, report) -> None:
        balance = report.balance
        if balance is None:
            self.balance_banner.setText("本次计算没有能量平衡结果。")
            self._set_pairs(self.balance_table, [])
            return
        exceeded = abs(balance.error) > balance.tolerance
        self.balance_banner.setText(
            f"平衡式：PV + 购电 + 储能放电 = 负荷 + 储能充电 + 上网 + 弃光（V2 §19）　"
            f"平衡误差 {balance.error:.3e} kWh，容差 {balance.tolerance:.1e} kWh　→ "
            + ("❌ 超出容差，计算应判定失败" if exceeded else "✅ 通过")
        )
        self.balance_banner.setStyleSheet("color:#B00020;" if exceeded else "color:#1B7F3B;")
        rows = [
            ("PV 发电量", f"{balance.pv_generation:,.2f} kWh"),
            ("PV → 负荷", f"{balance.pv_to_load:,.2f} kWh"),
            ("PV → 储能", f"{balance.pv_to_storage:,.2f} kWh"),
            ("PV → 上网", f"{balance.pv_to_grid:,.2f} kWh"),
            ("PV 弃光", f"{balance.pv_curtailed:,.2f} kWh"),
            ("电网 → 负荷", f"{balance.grid_to_load:,.2f} kWh"),
            ("电网 → 储能", f"{balance.grid_to_storage:,.2f} kWh"),
            ("电网购电合计", f"{balance.grid_import:,.2f} kWh"),
            ("储能充电", f"{balance.storage_charge:,.2f} kWh"),
            ("储能放电", f"{balance.storage_discharge:,.2f} kWh"),
            ("储能 → 负荷", f"{balance.storage_to_load:,.2f} kWh"),
            ("储能 → 上网", f"{balance.storage_to_grid:,.2f} kWh"),
            ("上网电量合计", f"{balance.grid_export:,.2f} kWh"),
            ("负荷合计", f"{balance.load_total:,.2f} kWh"),
            ("供给侧合计", f"{balance.supply_total:,.2f} kWh"),
            ("需求侧合计", f"{balance.demand_total:,.2f} kWh"),
            ("平衡误差（合计）", f"{balance.error:.6e} kWh"),
            ("单周期最大误差", f"{balance.max_hourly_error:.6e} kWh"),
            ("判定结果", "平衡" if balance.is_balanced else "不平衡"),
        ]
        self._set_pairs(self.balance_table, rows)

    def _fill_storage(self, report, project: Project | None) -> None:
        m = report.metrics
        result = self._result
        hourly = report.hourly
        soc_min_actual = soc_max_actual = None
        if hourly is not None and len(hourly) > 0:
            soc = hourly.column("storage_soc_end")
            if soc:
                soc_min_actual = min(soc)
                soc_max_actual = max(soc)

        dispatch = project.timeseries.dispatch if project is not None else None
        energy = result.storage_energy_kwh if result is not None else 0.0
        power = result.storage_power_kw if result is not None else 0.0
        duration = result.storage_duration_hours if result is not None else 0.0

        rows = [
            ("储能容量", f"{energy:,.2f} kWh"),
            ("储能功率", f"{power:,.2f} kW"),
            ("储能时长", f"{duration:,.2f} h"),
            ("SOC 上下限（设置）",
             "—" if dispatch is None else f"{dispatch.soc_min:.2%} ~ {dispatch.soc_max:.2%}"),
            ("起始 SOC（设置）", "—" if dispatch is None else f"{dispatch.initial_soc:.2%}"),
            ("SOC 实际区间",
             "—" if soc_min_actual is None else f"{soc_min_actual:.2%} ~ {soc_max_actual:.2%}"),
            ("充电效率", "—" if dispatch is None else f"{dispatch.charge_efficiency:.4%}"),
            ("放电效率", "—" if dispatch is None else f"{dispatch.discharge_efficiency:.4%}"),
            ("往返效率", "—" if dispatch is None else f"{dispatch.round_trip_efficiency:.4%}"),
            ("最大充电功率",
             "按额定功率" if dispatch is None or not dispatch.max_charge_power
             else f"{dispatch.max_charge_power:,.2f} kW"),
            ("最大放电功率",
             "按额定功率" if dispatch is None or not dispatch.max_discharge_power
             else f"{dispatch.max_discharge_power:,.2f} kW"),
            ("年充电量", f"{m.annual_storage_charge:,.2f} kWh"),
            ("年放电量", f"{m.annual_storage_discharge:,.2f} kWh"),
            ("其中电网充电量", f"{m.annual_grid_charge:,.2f} kWh"),
            ("配置循环次数", f"{m.configured_cycles:,.2f} 次"),
            ("实际等效循环次数", f"{m.equivalent_cycles:,.2f} 次"),
            ("储能套利收益", f"{m.storage_arbitrage_revenue:,.2f} 元"),
            ("储能容量收益", f"{m.storage_capacity_revenue:,.2f} 元"),
            ("储能辅助服务收益", f"{m.storage_ancillary_revenue:,.2f} 元"),
            ("LCOS", "—" if result is None or result.lcos is None else f"{result.lcos:.4f} 元/kWh"),
        ]
        self._set_pairs(self.storage_table, rows)

    def _apply_chart_range(self) -> None:
        mode = self.chart_range_mode.currentData() or RANGE_YEAR
        month = self.chart_month.currentData() or 1
        day = self.chart_day.currentData() or 1
        for widget in self._chart_widgets.values():
            if hasattr(widget, "set_range"):
                widget.set_range(mode, month=month, day=day)

    @staticmethod
    def _set_pairs(table: QTableWidget, rows: list[tuple[str, str]]) -> None:
        table.setRowCount(len(rows))
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["项目", "取值"])
        for i, (name, value) in enumerate(rows):
            table.setItem(i, 0, QTableWidgetItem(name))
            table.setItem(i, 1, QTableWidgetItem(value))

    # ------------------------------------------------------------------ #
    # 测试辅助
    # ------------------------------------------------------------------ #
    def overview_values(self) -> dict[str, str]:
        """结果概要表（名称 → 取值），供测试断言。"""
        return self._table_map(self.overview_table)

    def metrics_values(self) -> dict[str, str]:
        return self._table_map(self.metrics_table)

    def balance_values(self) -> dict[str, str]:
        return self._table_map(self.balance_table)

    def storage_values(self) -> dict[str, str]:
        return self._table_map(self.storage_table)

    def hourly_row_count(self) -> int:
        return self.hourly_table.rowCount()

    def chart_widget(self, kind: str) -> QWidget:
        return self._chart_widgets[kind]

    @staticmethod
    def _table_map(table: QTableWidget) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in range(table.rowCount()):
            name_item = table.item(r, 0)
            value_item = table.item(r, 1)
            if name_item is not None:
                out[name_item.text()] = value_item.text() if value_item is not None else ""
        return out


# --------------------------------------------------------------------------- #
# 9. 月度账单页（V2.1 阶段 2；规格书 §5.4、§5.5、§8.4；交接文档 §7）
#
# 分层（§0.2 铁律 1、2）：本页**不含任何公式**——列表、汇总、差异、平均电价、
# 校验级别全部来自 ``cenep.application.bill_service.BillService``（其内部再委托
# ``cenep.calculation.bill_calculator``）。界面只做三件事：取值、显示、把用户动作
# 转成一次服务调用。
# --------------------------------------------------------------------------- #
#: 账单字段"未提供"的统一文案（V2.1 §2.1：``None`` ≠ ``0``，不得显示为 0）
BILL_NOT_PROVIDED = "账单未提供"

#: 「账单事实 / 模拟结果」分界横幅（§1 重要设计边界、§8.1；阶段 5/6 才有模拟结果）
BILL_SIMULATION_NOTICE = (
    "【账单事实 / 模拟结果分界】本页展示与编辑的全部是**账单事实**"
    "（手动录入 / Excel 导入 / 用户明确标注的估算）；账单复算与光储方案模拟"
    "（§3.4「电费账单复算」、§7「V2.3 湖北电价与模拟账单联动」）属于阶段 5 / 6，"
    "本阶段**尚未建模**——凡涉及「复算电费 / 节省额 / 模拟需量」的位置一律标注"
    "「待确认 / 未建模」，不得把账单事实当成模拟结果展示。"
)

#: 平均综合电价的口径提示（§3.1：该指标只是账单统计，不是边际节省电价）
BILL_PRICE_CALIBER_NOTICE = (
    "口径提示：账单平均综合电价 P_avg = 账单总额 ÷ 总购电量，**仅作账单统计**，"
    "不等于光伏自用电量的边际节省电价（固定基本电费、需量电费、税费等未必随购电量同比例变化，§3.1）。"
)

#: 校验级别中文标签与配色（§2.1：ERROR 拒收 / WARNING 告警 / INFO 提示）
BILL_LEVEL_LABELS: dict[str, str] = {"ERROR": "错误", "WARNING": "警告", "INFO": "提示"}
BILL_LEVEL_COLORS: dict[str, str] = {
    "ERROR": "#B00020",
    "WARNING": "#B26A00",
    "INFO": "#1F5FA8",
}
#: 预览行状态 → 校验级别（预览用项目自身的质量状态枚举）
BILL_ROW_LEVELS: dict[BillQualityStatus, str] = {
    BillQualityStatus.INVALID: "ERROR",
    BillQualityStatus.WARNING: "WARNING",
    BillQualityStatus.VALID: "INFO",
}

#: 排序字段的中文标签（字段本身取自 ``BillService.SORT_FIELDS``，不在界面里另立清单）
BILL_SORT_LABELS: dict[str, str] = {
    "period_start": "按账期起始日",
    "billing_month": "按账单月份",
    "bill_total_yuan": "按账单总额",
    "energy_total_kwh": "按总购电量",
    "created_at": "按录入时间",
    "updated_at": "按最后修改时间",
}

#: 账单列表列标题
BILL_LIST_HEADERS: tuple[str, ...] = (
    "账单编号",
    "账单月份",
    "账期",
    "计量点",
    "总购电量",
    "账单总额",
    "数据来源",
    "质量状态",
)

#: 表单里"空文本 = 未提供"的字段（§2.1：可空字符串字段）
BILL_OPTIONAL_TEXT_FIELDS: tuple[str, ...] = (
    "meter_id",
    "customer_name",
    "voltage_level",
    "notes",
)

#: 空状态录入指引（§5.4、§8.2：旧项目打开后显示空状态，不虚构数据）
BILL_EMPTY_GUIDE = (
    "本项目尚未录入电费账单。录入方法：\n"
    "① 手动录入：点击「新增账单」，填写账期、电量与费用分项后点击「保存账单」；\n"
    "② 模板导入：点击「下载导入模板」得到《CENEP_电费账单导入模板.xlsx》，填写后回到"
    "「导入向导」按六步（选文件 → 选表 → 映射列 → 预览 → 校验 → 确认）导入；\n"
    "③ 直接导入已有的 Excel / CSV 账单表（列名可中英文，列顺序可变）。\n"
    "已录入的账单随项目文件（.nep）一起保存，重开项目后自动恢复；旧项目没有账单时本页保持空状态，"
    "不会自动生成任何虚构账单。"
)

#: 无账单时月度 / 年度汇总的提示
BILL_NO_SUMMARY_HINT = "本项目尚未录入电费账单，暂无汇总数据（录入方法见「账单列表」页签）。"


def bill_kwh(value: float | None, digits: int = 2) -> str:
    """电量显示：``None`` → **账单未提供**（绝不显示 0，§2.1）。"""
    return BILL_NOT_PROVIDED if value is None else f"{value:,.{digits}f} kWh"


def bill_yuan(value: float | None, digits: int = 2) -> str:
    """金额显示：``None`` → **账单未提供**（绝不显示 0，§2.1）。"""
    return BILL_NOT_PROVIDED if value is None else f"{value:,.{digits}f} 元"


def bill_price(value: float | None) -> str:
    """平均综合电价显示：``None`` → 无法计算（账单未提供电量或金额，§3.1）。"""
    return "无法计算（账单未提供电量或金额）" if value is None else f"{value:,.4f} 元/kWh"


def bill_text(value: object | None) -> str:
    """文本字段显示：``None`` / 空串 → **账单未提供**。"""
    if value is None or not str(value).strip():
        return BILL_NOT_PROVIDED
    return str(value)


def _set_table(
    table: QTableWidget,
    headers: list[str] | tuple[str, ...],
    rows: list[list[str]],
    *,
    readonly: bool = True,
) -> None:
    """把二维文本写入表格（只做显示，不做任何计算）。"""
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels([str(h) for h in headers])
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(str(text)))
    if readonly:
        _no_edit(table)
    table.resizeColumnsToContents()


class BillImportWizard(QWidget):
    """账单导入向导：**六步**（V2.1 §5.3、§5.5、§8.4）。

    步骤：选文件 → 选表 → 映射列 → 预览 → 校验 → 确认导入。

    每一步都是一个可独立断言的状态转换（``self.step`` 与 ``self.stack`` 同步），
    因此无需弹出任何模态对话框即可自动测试。**本类不含任何计算**：读表、列映射、
    逐行校验、重复识别全部由 :class:`~cenep.application.bill_service.BillService`
    完成（第 1 步的 ``list_sheets`` 只是 ``data`` 层的文件清单读取，不是计算）。
    """

    #: 六步标题（界面上的步骤条与测试断言共用）
    STEP_TITLES: tuple[str, ...] = (
        "1 选择文件",
        "2 选择工作表",
        "3 映射列",
        "4 预览",
        "5 校验",
        "6 确认导入",
    )

    #: 导入完成信号（账单页据此刷新列表与汇总）
    imported = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = None
        self.step = 1
        self.path: Path | None = None
        self.sheet_names: list[str] = []
        self.sheet: str | None = None
        self.preview = None
        self.result = None
        self.mapping_overrides: dict[str, str] = {}
        self.issues: list[tuple[str, str, str]] = []

        layout = QVBoxLayout(self)
        self.step_label = QLabel("", self)
        self.step_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.step_label)
        self.breadcrumb = QLabel("", self)
        self.breadcrumb.setWordWrap(True)
        layout.addWidget(self.breadcrumb)
        self.message_label = QLabel("", self)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)

        # ---- 第 1 步：选择文件 ----
        page1 = QWidget(self)
        p1 = QVBoxLayout(page1)
        self.file_label = QLabel("尚未选择文件。", page1)
        self.file_label.setWordWrap(True)
        p1.addWidget(self.file_label)
        self.file_button = QPushButton("选择账单文件（.xlsx / .csv）…", page1)
        self.file_button.clicked.connect(self.choose_file)
        p1.addWidget(self.file_button)
        p1.addWidget(
            QLabel(
                "说明：模板为《CENEP_电费账单导入模板.xlsx》，工作表「月账单」一行一条账单；"
                "列名可中英文、列顺序可变，第 3 步会显示自动识别的列映射并允许手工改判（§5.3、§5.5）。",
                page1,
            )
        )
        p1.addStretch(1)
        self.stack.addWidget(page1)

        # ---- 第 2 步：选择工作表 ----
        page2 = QWidget(self)
        p2 = QVBoxLayout(page2)
        p2.addWidget(QLabel("选择账单所在的工作表：", page2))
        self.sheet_combo = QComboBox(page2)
        p2.addWidget(self.sheet_combo)
        self.sheet_button = QPushButton("确认工作表，进入列映射", page2)
        self.sheet_button.clicked.connect(lambda: self.select_sheet())
        p2.addWidget(self.sheet_button)
        p2.addWidget(QLabel("提示：模板中的「填写说明」「数据字典」工作表不含账单数据，请选择「月账单」。", page2))
        p2.addStretch(1)
        self.stack.addWidget(page2)

        # ---- 第 3 步：映射列 ----
        page3 = QWidget(self)
        p3 = QVBoxLayout(page3)
        p3.addWidget(
            QLabel("自动识别的列映射（可在「识别到的列」下拉框中手工改判）：", page3)
        )
        self.mapping_table = QTableWidget(page3)
        p3.addWidget(self.mapping_table, 1)
        self.mapping_hint = QLabel("", page3)
        self.mapping_hint.setWordWrap(True)
        p3.addWidget(self.mapping_hint)
        self.mapping_button = QPushButton("应用列映射，生成预览", page3)
        self.mapping_button.clicked.connect(lambda: self.apply_mapping())
        p3.addWidget(self.mapping_button)
        self.stack.addWidget(page3)

        # ---- 第 4 步：预览 ----
        page4 = QWidget(self)
        p4 = QVBoxLayout(page4)
        self.preview_label = QLabel("", page4)
        self.preview_label.setWordWrap(True)
        p4.addWidget(self.preview_label)
        self.preview_table = QTableWidget(page4)
        p4.addWidget(self.preview_table, 1)
        self.preview_button = QPushButton("校验（分级显示问题）", page4)
        self.preview_button.clicked.connect(self.validate_rows)
        p4.addWidget(self.preview_button)
        self.stack.addWidget(page4)

        # ---- 第 5 步：校验 ----
        page5 = QWidget(self)
        p5 = QVBoxLayout(page5)
        self.issue_label = QLabel("", page5)
        self.issue_label.setWordWrap(True)
        p5.addWidget(self.issue_label)
        self.issue_table = QTableWidget(page5)
        p5.addWidget(self.issue_table, 1)
        strategy_row = QHBoxLayout()
        strategy_row.addWidget(QLabel("重复账单处理策略：", page5))
        self.strategy_combo = QComboBox(page5)
        for value, label in DUPLICATE_STRATEGY_CHOICES:
            self.strategy_combo.addItem(label, value)
        strategy_row.addWidget(self.strategy_combo, 1)
        p5.addLayout(strategy_row)
        self.confirm_button = QPushButton("确认导入（仅导入可导入行）", page5)
        self.confirm_button.clicked.connect(lambda: self.confirm_import())
        p5.addWidget(self.confirm_button)
        self.stack.addWidget(page5)

        # ---- 第 6 步：完成 ----
        page6 = QWidget(self)
        p6 = QVBoxLayout(page6)
        self.result_label = QLabel("", page6)
        self.result_label.setWordWrap(True)
        p6.addWidget(self.result_label)
        self.restart_button = QPushButton("继续导入下一个文件", page6)
        self.restart_button.clicked.connect(self.reset)
        p6.addWidget(self.restart_button)
        p6.addStretch(1)
        self.stack.addWidget(page6)

        self._goto(1)

    # ------------------------------------------------------------------ #
    # 服务与状态
    # ------------------------------------------------------------------ #
    def bind(self, service) -> None:
        """绑定账单服务（由账单页在项目切换时调用）。"""
        self._service = service

    def state(self) -> dict:
        """当前向导状态（供测试与界面提示复用）。"""
        preview = self.preview
        return {
            "step": self.step,
            "step_title": self.STEP_TITLES[self.step - 1],
            "file": str(self.path) if self.path else None,
            "sheets": list(self.sheet_names),
            "sheet": self.sheet,
            "mapping": dict(preview.column_mapping)
            if preview is not None
            else dict(self.mapping_overrides),
            "mapping_overrides": dict(self.mapping_overrides),
            "total_rows": getattr(preview, "total_rows", 0),
            "valid": getattr(preview, "valid_count", 0),
            "warning": getattr(preview, "warning_count", 0),
            "invalid": getattr(preview, "invalid_count", 0),
            "duplicate": getattr(preview, "duplicate_count", 0),
            "issues": list(self.issues),
            "imported": None if self.result is None else self.result.total_written,
        }

    def reset(self) -> None:
        """回到第 1 步并清空本次导入的全部中间状态。"""
        self.path = None
        self.sheet_names = []
        self.sheet = None
        self.preview = None
        self.result = None
        self.mapping_overrides = {}
        self.issues = []
        self.file_label.setText("尚未选择文件。")
        self.sheet_combo.clear()
        self.mapping_table.setRowCount(0)
        self.preview_table.setRowCount(0)
        self.issue_table.setRowCount(0)
        self.preview_label.setText("")
        self.issue_label.setText("")
        self.result_label.setText("")
        self._set_message("")
        self._goto(1)

    def _goto(self, step: int) -> None:
        self.step = max(1, min(step, len(self.STEP_TITLES)))
        self.stack.setCurrentIndex(self.step - 1)
        self.step_label.setText(f"第 {self.step} / {len(self.STEP_TITLES)} 步：{self.STEP_TITLES[self.step - 1]}")
        self.breadcrumb.setText(
            "导入流程：" + " → ".join(
                f"【{title}】" if index == self.step - 1 else title
                for index, title in enumerate(self.STEP_TITLES)
            )
        )

    def _set_message(self, text: str, *, ok: bool = False) -> None:
        self.message_label.setText(text)
        self.message_label.setStyleSheet("color: #1B7F3B;" if ok else "color: #B00020;")

    def _require_service(self):
        if self._service is None:
            self._set_message("尚未绑定账单服务，无法导入；请先在账单页打开或新建项目。")
            return None
        return self._service

    # ------------------------------------------------------------------ #
    # 第 1 步：选文件
    # ------------------------------------------------------------------ #
    def choose_file(self) -> None:
        """弹出文件选择框（自动化测试请直接调用 :meth:`set_file`）。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择账单文件", "", "账单文件 (*.xlsx *.xlsm *.csv);;所有文件 (*)"
        )
        if path:
            self.set_file(path)

    def set_file(self, path: str | Path) -> bool:
        """第 1 → 2 步：选择文件并列出工作表（§5.3）。"""
        if self._require_service() is None:
            return False
        try:
            sheets = list_sheets(path)
        except ValidationError as exc:
            self._set_message(f"无法读取该文件，请检查后重试：{exc}")
            return False
        except Exception as exc:  # 兜底：不把裸异常抛给用户（§0.2）
            self._set_message(f"读取文件失败，请确认文件未损坏且未被占用：{exc}")
            return False

        self.path = Path(path)
        self.sheet_names = list(sheets)
        self.sheet = None
        self.preview = None
        self.mapping_overrides = {}
        self.file_label.setText(f"已选择文件：{self.path}\n包含工作表：{'、'.join(sheets)}")
        self.sheet_combo.clear()
        for name in sheets:
            self.sheet_combo.addItem(name)
        self._set_message("")
        self._goto(2)
        return True

    # ------------------------------------------------------------------ #
    # 第 2 步：选表
    # ------------------------------------------------------------------ #
    def select_sheet(self, name: str | None = None) -> bool:
        """第 2 → 3 步：确认工作表并读取自动列映射（§5.3）。"""
        if self._require_service() is None:
            return False
        if self.path is None:
            self._set_message("请先选择账单文件。")
            return False
        sheet = (name or self.sheet_combo.currentText() or "").strip()
        if not sheet:
            self._set_message("请选择工作表。")
            return False
        self.sheet = sheet
        self.mapping_overrides = {}
        return self.load_mapping()

    def load_mapping(self, column_mapping: dict[str, str] | None = None) -> bool:
        """读入列映射并在第 3 步展示。

        两种情况都进入第 3 步（映射列）：

        * 自动识别成功：显示自动识别结果，用户可以改判；
        * 自动识别失败（列被重命名 / 缺必需列）：**改用手工映射**，
          用「文件实际表头 + 全部账单字段」填表，让用户自己指定（§5.5
          "支持用户重命名列，导入界面提供列映射"），并给出中文说明。
        """
        service = self._require_service()
        if service is None or self.path is None or not self.sheet:
            self._set_message("请先完成「选择文件」与「选择工作表」两步。")
            return False
        try:
            preview = service.preview_import(
                self.path, sheet=self.sheet, column_mapping=column_mapping
            )
        except ValidationError as exc:
            headers = self.read_headers()
            if not headers:
                self._set_message(f"无法读取该工作表，请确认选择的表包含账单数据：{exc}")
                return False
            self.preview = None
            self._fill_mapping_table(None, headers)
            self.mapping_hint.setText(
                f"自动列映射未通过：{exc}\n请在下表手工指定每列对应的账单字段，"
                "再点击「应用列映射，生成预览」。"
            )
            self._goto(3)
            return True
        except Exception as exc:  # 兜底
            self._set_message(f"读取账单表失败：{exc}")
            return False

        self.preview = preview
        self._fill_mapping_table(preview)
        self.mapping_hint.setText(
            "未识别的列（将被忽略）："
            + ("、".join(preview.unmapped_headers) if preview.unmapped_headers else "无")
        )
        self._set_message("已自动识别列映射；如需改判，请在「识别到的列」中重新选择。", ok=True)
        self._goto(3)
        return True

    def read_headers(self) -> list[str]:
        """读取所选工作表的表头（手工列映射用；纯文件读取，不做任何计算）。"""
        if self.path is None or not self.sheet:
            return []
        try:
            rows = read_table_from_sheet(self.path, self.sheet, with_row_numbers=True)
        except ValidationError:
            return []
        if not rows:
            return []
        return [str(header) for header in rows[0][1].keys()]

    def _fill_mapping_table(self, preview=None, headers: list[str] | None = None) -> None:
        if headers is None:
            headers = sorted({*preview.column_mapping.values(), *preview.unmapped_headers})
        if preview is not None:
            items = list(preview.column_mapping.items())
        else:
            items = [(column.field, "") for column in BILL_COLUMNS]
        self.mapping_table.setColumnCount(3)
        self.mapping_table.setHorizontalHeaderLabels(["账单字段", "识别到的列（可改判）", "字段含义"])
        self.mapping_table.setRowCount(len(items))
        for row, (field, header) in enumerate(items):
            self.mapping_table.setItem(row, 0, QTableWidgetItem(field))
            combo = QComboBox(self.mapping_table)
            combo.addItem("（不映射）", "")
            for candidate in headers:
                combo.addItem(candidate, candidate)
            index = combo.findData(header)
            combo.setCurrentIndex(index if index >= 0 else 0)
            self.mapping_table.setCellWidget(row, 1, combo)
            self.mapping_table.setItem(row, 2, QTableWidgetItem(label_of(field)))
        self.mapping_table.resizeColumnsToContents()

    def mapping_from_table(self) -> dict[str, str]:
        """读取第 3 步表格上的手工列映射（``字段 → 表头``，空值表示不映射）。"""
        mapping: dict[str, str] = {}
        for row in range(self.mapping_table.rowCount()):
            field_item = self.mapping_table.item(row, 0)
            combo = self.mapping_table.cellWidget(row, 1)
            if field_item is None or not isinstance(combo, QComboBox):
                continue
            header = combo.currentData() or ""
            if header:
                mapping[field_item.text()] = str(header)
        return mapping

    def set_mapping_in_table(self, mapping: dict[str, str]) -> None:
        """把 ``字段 → 表头`` 写入第 3 步表格的下拉框（测试与"恢复自动映射"共用）。"""
        for row in range(self.mapping_table.rowCount()):
            field_item = self.mapping_table.item(row, 0)
            combo = self.mapping_table.cellWidget(row, 1)
            if field_item is None or not isinstance(combo, QComboBox):
                continue
            header = mapping.get(field_item.text(), "")
            index = combo.findData(header)
            combo.setCurrentIndex(index if index >= 0 else 0)

    # ------------------------------------------------------------------ #
    # 第 3 → 4 步：应用映射并预览
    # ------------------------------------------------------------------ #
    def apply_mapping(self, overrides: dict[str, str] | None = None) -> bool:
        """第 3 → 4 步：应用（可改判的）列映射并生成预览表（§5.5）。

        手工映射路径下 ``self.preview`` 可能为空（自动识别失败），因此这里以
        "服务能否按当前映射生成预览"为唯一判据，而不是要求先有自动识别的预览。
        """
        service = self._require_service()
        if service is None:
            return False
        if self.path is None or not self.sheet:
            self._set_message("请先完成「选择文件」与「选择工作表」两步。")
            return False
        chosen = dict(overrides) if overrides is not None else self.mapping_from_table()
        clean = {key: value for key, value in chosen.items() if str(value or "").strip()}
        if not clean:
            self._set_message("请至少为账期起始日、账期结束日两个字段指定列映射。")
            return False
        if overrides is not None:
            self.set_mapping_in_table(clean)
        self.mapping_overrides = clean
        try:
            preview = service.preview_import(
                self.path, sheet=self.sheet, column_mapping=clean
            )
        except ValidationError as exc:
            self._set_message(f"按当前列映射无法生成预览：{exc}")
            return False
        except Exception as exc:
            self._set_message(f"生成预览失败：{exc}")
            return False

        self.preview = preview
        rows = [
            [
                preview.sheet_name,
                str(row.row_number),
                "—" if row.bill is None else row.bill.bill_id,
                BILL_LEVEL_LABELS[BILL_ROW_LEVELS[row.status]],
                "；".join(row.messages) if row.messages else "通过",
                row.duplicate_of or "—",
            ]
            for row in preview.rows
        ]
        _set_table(
            self.preview_table,
            ["工作表", "行号", "账单编号", "级别", "问题 / 说明", "疑似重复于"],
            rows,
        )
        self.preview_label.setText(
            preview.counts_text()
            + "\n预览只读入内存，**尚未写入项目**；点击「校验」查看分级问题，确认无误后再导入。"
        )
        self._set_message("预览已生成，请核对行号与问题说明。", ok=True)
        self._goto(4)
        return True

    # ------------------------------------------------------------------ #
    # 第 4 → 5 步：校验（ERROR / WARNING / INFO 分级）
    # ------------------------------------------------------------------ #
    def validate_rows(self) -> bool:
        """第 4 → 5 步：把预览结果按 ERROR / WARNING / INFO 分级显示（§2.1、§5.5）。"""
        if self.preview is None:
            self._set_message("请先生成预览。")
            return False
        issues: list[tuple[str, str, str]] = []
        for row in self.preview.rows:
            level = BILL_ROW_LEVELS[row.status]
            detail = "；".join(row.messages) if row.messages else "该行校验通过，可导入"
            location = f"{self.preview.sheet_name} 第 {row.row_number} 行"
            if row.bill is not None:
                bill_id = row.bill.bill_id
                if row.duplicate_of:
                    detail = f"疑似重复（与 {row.duplicate_of} 的项目 + 账期 + 计量点相同）：{detail}"
            else:
                bill_id = "（无法解析为账单）"
            issues.append((level, bill_id, f"{location}：{detail}"))
        for message in self.preview.messages:
            issues.append(("INFO", "—", f"工作表级提示：{message}"))

        self.issues = issues
        counts = {"ERROR": 0, "WARNING": 0, "INFO": 0}
        for level, _, _ in issues:
            counts[level] += 1
        _set_table(
            self.issue_table,
            ["级别", "账单 / 位置", "说明"],
            [[level, bill_id, text] for level, bill_id, text in issues],
        )
        for row, (level, _, _) in enumerate(issues):
            item = self.issue_table.item(row, 0)
            if item is not None:
                item.setForeground(QColor(BILL_LEVEL_COLORS[level]))
        self.issue_label.setText(
            f"校验结果：错误（ERROR）{counts['ERROR']} 条、警告（WARNING）{counts['WARNING']} 条、"
            f"提示（INFO）{counts['INFO']} 条。"
            "错误行**不会入库**，警告行可导入但请先核对；请选择重复账单处理策略后确认导入。"
        )
        self._set_message("校验完成。", ok=True)
        self._goto(5)
        return True

    # ------------------------------------------------------------------ #
    # 第 5 → 6 步：确认导入
    # ------------------------------------------------------------------ #
    def confirm_import(self, strategy: str | None = None):
        """第 5 → 6 步：按所选重复策略写入项目（§5.5）。"""
        service = self._require_service()
        if service is None:
            return None
        if self.preview is None:
            self._set_message("请先完成预览与校验。")
            return None
        raw = strategy if strategy is not None else self.strategy_combo.currentData()
        try:
            chosen = DuplicateStrategy(str(raw))
        except ValueError:
            self._set_message(f"重复账单处理策略『{raw}』无效，请重新选择。")
            return None
        try:
            result = service.import_bills(preview=self.preview, strategy=chosen)
        except ValidationError as exc:
            self._set_message(f"导入未完成：{exc}")
            return None
        except Exception as exc:
            self._set_message(f"导入失败：{exc}")
            return None

        self.result = result
        self.result_label.setText(
            f"{result.summary_text()}\n"
            f"重复策略：{chosen.label}；无效行（未入库）："
            + ("、".join(str(row) for row in result.invalid_rows) if result.invalid_rows else "无")
            + "\n导入的账单已写入项目，请及时保存项目文件（.nep）。"
        )
        self._set_message("导入完成。", ok=True)
        self._goto(6)
        self.imported.emit()
        return result

    def run_all(self, path: str | Path, sheet: str | None = None, strategy: str = "skip"):
        """一次跑完六步（供自动化与"一键导入"使用），返回导入结果或 ``None``。"""
        if not self.set_file(path):
            return None
        if not self.select_sheet(sheet):
            return None
        if not self.apply_mapping():
            return None
        if not self.validate_rows():
            return None
        return self.confirm_import(strategy)


class BillsPage(QWidget):
    """月度账单页（V2.1 §5.4）：账单列表 / 录入编辑 / 月度汇总 / 年度汇总 / 校验提示 / 导入向导。

    **不含任何计算**：所有数值来自 :class:`~cenep.application.bill_service.BillService`
    （``list_bills`` / ``monthly_summary`` / ``annual_summary`` / ``reconcile_all`` /
    ``month_summary`` …）。界面只负责取值、显示与把用户动作转成服务调用。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = None
        self._editing_id: str | None = None
        self._row_bill_ids: list[str] = []

        layout = QVBoxLayout(self)

        banner = QLabel(BILL_SIMULATION_NOTICE, self)
        banner.setWordWrap(True)
        banner.setStyleSheet("background-color: #FFF2CC; border: 1px solid #E0C060; padding: 6px;")
        layout.addWidget(banner)
        self.banner = banner

        price_notice = QLabel(BILL_PRICE_CALIBER_NOTICE, self)
        price_notice.setWordWrap(True)
        price_notice.setStyleSheet("color: #555555;")
        layout.addWidget(price_notice)

        # ---- 工具条 ----
        toolbar = QHBoxLayout()
        self.new_button = QPushButton("新增账单", self)
        self.new_button.clicked.connect(self.new_bill)
        toolbar.addWidget(self.new_button)
        self.save_button = QPushButton("保存账单", self)
        self.save_button.clicked.connect(self.save_form)
        toolbar.addWidget(self.save_button)
        self.copy_button = QPushButton("复制上月", self)
        self.copy_button.clicked.connect(self.copy_previous_month)
        toolbar.addWidget(self.copy_button)
        self.delete_button = QPushButton("删除所选", self)
        self.delete_button.clicked.connect(lambda: self.delete_selected())
        toolbar.addWidget(self.delete_button)
        self.clear_button = QPushButton("清空全部账单", self)
        self.clear_button.clicked.connect(lambda: self.clear_all())
        toolbar.addWidget(self.clear_button)
        toolbar.addStretch(1)
        self.template_button = QPushButton("下载导入模板", self)
        self.template_button.clicked.connect(lambda: self.download_template())
        toolbar.addWidget(self.template_button)
        layout.addLayout(toolbar)

        self.message_label = QLabel("", self)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        self._build_list_tab()
        self._build_form_tab()
        self._build_monthly_tab()
        self._build_annual_tab()
        self._build_validation_tab()
        self._build_wizard_tab()
        self.refresh()

    # ------------------------------------------------------------------ #
    # 各页签
    # ------------------------------------------------------------------ #
    def _build_list_tab(self) -> None:
        page = QWidget(self)
        box = QVBoxLayout(page)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("月份筛选：", page))
        self.month_combo = QComboBox(page)
        self.month_combo.currentIndexChanged.connect(lambda _: self.refresh())
        filters.addWidget(self.month_combo)
        filters.addWidget(QLabel("排序：", page))
        self.sort_combo = QComboBox(page)
        for field in SORT_FIELDS:
            self.sort_combo.addItem(BILL_SORT_LABELS.get(field, field), field)
        self.sort_combo.currentIndexChanged.connect(lambda _: self.refresh())
        filters.addWidget(self.sort_combo)
        self.descending_check = QCheckBox("倒序", page)
        self.descending_check.toggled.connect(lambda _: self.refresh())
        filters.addWidget(self.descending_check)
        self.edit_button = QPushButton("编辑所选账单", page)
        self.edit_button.clicked.connect(self.edit_selected)
        filters.addWidget(self.edit_button)
        filters.addStretch(1)
        box.addLayout(filters)

        self.list_table = QTableWidget(page)
        _no_edit(self.list_table)
        box.addWidget(self.list_table, 1)

        self.empty_label = QLabel(BILL_EMPTY_GUIDE, page)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: #8A5A00;")
        box.addWidget(self.empty_label)

        self.list_hint = QLabel("", page)
        self.list_hint.setWordWrap(True)
        box.addWidget(self.list_hint)
        self.tabs.addTab(page, "账单列表")

    def _build_form_tab(self) -> None:
        page = QWidget(self)
        box = QVBoxLayout(page)
        self.form_hint = QLabel(
            "录入提示：不需要填写的分项请**不要勾选**「填写」，未勾选表示『账单未提供』"
            "（不会按 0 参与合计，§2.1）；保存前会做中文校验并给出差异提示。",
            page,
        )
        self.form_hint.setWordWrap(True)
        box.addWidget(self.form_hint)
        self.forms: dict[str, SectionForm] = {}
        for section in BILL_SECTIONS:
            group = QGroupBox(section.title, page)
            group_layout = QVBoxLayout(group)
            form = SectionForm(section, group)
            self.forms[section.title] = form
            group_layout.addWidget(form)
            box.addWidget(group)
        self.form_status = QLabel("", page)
        self.form_status.setWordWrap(True)
        box.addWidget(self.form_status)
        box.addStretch(1)
        self.tabs.addTab(page, "录入 / 编辑")

    def _build_monthly_tab(self) -> None:
        page = QWidget(self)
        box = QVBoxLayout(page)
        self.monthly_hint = QLabel(BILL_PRICE_CALIBER_NOTICE, page)
        self.monthly_hint.setWordWrap(True)
        box.addWidget(self.monthly_hint)
        self.monthly_table = QTableWidget(page)
        _no_edit(self.monthly_table)
        box.addWidget(self.monthly_table, 1)
        self.monthly_notes = QPlainTextEdit(page)
        self.monthly_notes.setReadOnly(True)
        self.monthly_notes.setMaximumHeight(90)
        box.addWidget(self.monthly_notes)
        self.tabs.addTab(page, "月度汇总")

    def _build_annual_tab(self) -> None:
        page = QWidget(self)
        box = QVBoxLayout(page)
        self.annual_table = QTableWidget(page)
        _no_edit(self.annual_table)
        box.addWidget(self.annual_table, 1)
        self.annual_notes = QPlainTextEdit(page)
        self.annual_notes.setReadOnly(True)
        self.annual_notes.setMaximumHeight(130)
        box.addWidget(self.annual_notes)
        self.tabs.addTab(page, "年度汇总")

    def _build_validation_tab(self) -> None:
        page = QWidget(self)
        box = QVBoxLayout(page)
        self.validation_label = QLabel("", page)
        self.validation_label.setWordWrap(True)
        box.addWidget(self.validation_label)
        self.validation_table = QTableWidget(page)
        _no_edit(self.validation_table)
        box.addWidget(self.validation_table, 1)
        box.addWidget(QLabel("口径与假设（来自账单校验的 assumptions，§3.1）：", page))
        self.assumptions_view = QPlainTextEdit(page)
        self.assumptions_view.setReadOnly(True)
        self.assumptions_view.setMaximumHeight(130)
        box.addWidget(self.assumptions_view)
        self.tabs.addTab(page, "校验提示")

    def _build_wizard_tab(self) -> None:
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.addWidget(
            QLabel(
                "导入向导（六步）：选文件 → 选表 → 映射列 → 预览 → 校验 → 确认导入（§5.3、§5.5、§8.4）。",
                page,
            )
        )
        self.wizard = BillImportWizard(page)
        self.wizard.imported.connect(self.refresh)
        box.addWidget(self.wizard, 1)
        self.tabs.addTab(page, "导入向导")

    # ------------------------------------------------------------------ #
    # 服务
    # ------------------------------------------------------------------ #
    def bind(self, service) -> None:
        """绑定账单服务（由主窗口在项目切换 / 打开后调用）并刷新全部展示。"""
        self._service = service
        self._editing_id = None
        if service is not None:
            self.wizard.bind(service)
        self.refresh()

    @property
    def service(self):
        return self._service

    def _set_message(self, text: str, *, ok: bool = False) -> None:
        self.message_label.setText(text)
        self.message_label.setStyleSheet("color: #1B7F3B;" if ok else "color: #B00020;")

    def refresh(self) -> None:
        """重新读取服务数据并刷新列表 / 汇总 / 校验（无账单时显示录入指引且不报错）。"""
        if self._service is None:
            _set_table(self.list_table, BILL_LIST_HEADERS, [])
            self.empty_label.setText("尚未绑定项目与账单服务（请先新建或打开项目）。")
            self.empty_label.show()
            self.list_hint.setText("")
            _set_table(self.monthly_table, ["月份", "账单条数"], [])
            _set_table(self.annual_table, ["项目", "取值"], [])
            self.monthly_notes.setPlainText(BILL_NO_SUMMARY_HINT)
            self.annual_notes.setPlainText(BILL_NO_SUMMARY_HINT)
            _set_table(self.validation_table, ["级别", "账单", "说明"], [])
            self.validation_label.setText(BILL_NO_SUMMARY_HINT)
            self.assumptions_view.setPlainText("")
            return
        try:
            self._refresh_month_combo()
            self._refresh_list()
            self._refresh_monthly()
            self._refresh_annual()
            self._refresh_validation()
        except ValidationError as exc:  # 中文校验错误，绝不裸抛给用户（§0.2）
            self._set_message(f"账单数据读取失败：{exc}")
        except Exception as exc:  # 兜底
            self._set_message(f"账单页面刷新失败：{exc}")

    def _refresh_month_combo(self) -> None:
        current = self.month_combo.currentData() if self.month_combo.count() else None
        months = list(self._service.month_coverage().keys())
        self.month_combo.blockSignals(True)
        self.month_combo.clear()
        self.month_combo.addItem("全部月份", None)
        for month in months:
            self.month_combo.addItem(month, month)
        index = self.month_combo.findData(current) if current else 0
        self.month_combo.setCurrentIndex(max(index, 0))
        self.month_combo.blockSignals(False)

    def _refresh_list(self) -> None:
        month = self.month_combo.currentData()
        sort_by = self.sort_combo.currentData() or SORT_FIELDS[0]
        bills = self._service.list_bills(
            month=month, sort_by=sort_by, descending=self.descending_check.isChecked()
        )
        rows: list[list[str]] = []
        self._row_bill_ids = []
        for bill in bills:
            period = f"{bill.billing_period_start:%Y-%m-%d} ~ {bill.billing_period_end:%Y-%m-%d}"
            if bill.is_cross_month:
                period = f"{period}（跨月账期）"
            rows.append(
                [
                    bill.bill_id,
                    bill.billing_month,
                    period,
                    bill_text(bill.meter_id),
                    bill_kwh(bill.energy_total_kwh),
                    bill_yuan(bill.bill_total_yuan),
                    bill.source_type.label,
                    bill.quality_status.label,
                ]
            )
            self._row_bill_ids.append(bill.bill_id)
        _set_table(self.list_table, BILL_LIST_HEADERS, rows)

        total = len(self._service)
        if total == 0:
            self.empty_label.setText(BILL_EMPTY_GUIDE)
            self.empty_label.show()
            self.list_hint.setText("当前没有账单记录（0 条）。")
        else:
            self.empty_label.hide()
            missing = sum(
                1
                for bill in bills
                if bill.energy_total_kwh is None or bill.bill_total_yuan is None
            )
            self.list_hint.setText(
                f"共 {total} 条账单，当前筛选显示 {len(bills)} 条；"
                f"其中 {missing} 条存在『账单未提供』的电量或金额字段"
                "（显示为“账单未提供”，不按 0 计算）。"
            )

    def _refresh_monthly(self) -> None:
        summaries = self._service.monthly_summary()
        if not summaries:
            _set_table(
                self.monthly_table,
                ["月份", "账单条数", "总购电量", "账单总额", "平均综合电价", "跨月", "质量状态"],
                [],
            )
            self.monthly_notes.setPlainText(BILL_NO_SUMMARY_HINT)
            return
        rows = [
            [
                item.billing_month,
                str(item.bill_count),
                bill_kwh(item.energy_total_kwh),
                bill_yuan(item.amount_total_yuan),
                bill_price(item.average_price_yuan_per_kwh),
                "是" if item.has_cross_month else "否",
                item.quality_status.label,
            ]
            for item in summaries
        ]
        _set_table(
            self.monthly_table,
            ["月份", "账单条数", "总购电量", "账单总额", "平均综合电价", "跨月", "质量状态"],
            rows,
        )
        notes: list[str] = []
        for item in summaries:
            for message in item.messages:
                notes.append(f"{item.billing_month}：{message}")
        self.monthly_notes.setPlainText(
            "\n".join(notes) if notes else "各月账单分项合计与总电量校验未发现问题。"
        )

    def _refresh_annual(self) -> None:
        summary = self._service.annual_summary()
        rows = [
            ["统计年度", str(summary.year)],
            ["覆盖月份数", f"{len(summary.months_covered)} / 12"],
            ["月份覆盖率", f"{summary.coverage_ratio:.2%}"],
            ["年度总购电量", bill_kwh(summary.total_energy_kwh)],
            ["年度账单总额", bill_yuan(summary.total_amount_yuan)],
            ["平均综合电价", bill_price(summary.average_price_yuan_per_kwh)],
            ["是否可直接相加", "是" if summary.can_sum_directly else "否（缺月 / 重叠 / 跨月或非自然月）"],
            [
                "数值口径",
                "年度电量与电费只在「每月账单周期完整且不重叠」时可直接相加（§3.1）",
            ],
            [
                "跨月账期账单",
                "、".join(summary.cross_month_bills) if summary.cross_month_bills else "无",
            ],
            [
                "非自然月账期账单",
                "、".join(summary.non_natural_month_bills) if summary.non_natural_month_bills else "无",
            ],
            [
                "同月多条账单",
                "、".join(summary.duplicate_months) if summary.duplicate_months else "无",
            ],
            [
                "缺失月份",
                "、".join(summary.missing_months) if summary.missing_months else "无（12 个月齐全）",
            ],
            ["模拟账单（阶段 5/6）", "待确认 / 未建模：本阶段不做账单复算与光储方案模拟"],
        ]
        _set_table(self.annual_table, ["项目", "取值"], rows)
        notes = [*summary.messages, *summary.assumptions]
        self.annual_notes.setPlainText(
            "\n".join(notes) if notes else BILL_NO_SUMMARY_HINT
        )

    def _refresh_validation(self) -> None:
        outcomes = self._service.reconcile_all()
        rows: list[list[str]] = []
        for item in outcomes:
            if not item.issues:
                rows.append(["INFO", item.bill_id, "未发现问题（合计与分项一致或无法判断）"])
            for issue in item.issues:
                field = label_of(issue.field) if issue.field else "—"
                rows.append(
                    [
                        issue.level,
                        item.bill_id,
                        f"[{issue.code or '—'}] {field}：{issue.message}",
                    ]
                )
        if not rows:
            _set_table(
                self.validation_table,
                ["级别", "账单", "说明"],
                [],
            )
            self.validation_label.setText(BILL_NO_SUMMARY_HINT)
            self.assumptions_view.setPlainText("")
            return
        _set_table(self.validation_table, ["级别", "账单", "说明"], rows)
        counts = {"ERROR": 0, "WARNING": 0, "INFO": 0}
        for row in rows:
            counts[row[0]] = counts.get(row[0], 0) + 1
        self.validation_label.setText(
            f"共核对 {len(outcomes)} 条账单：错误（ERROR）{counts['ERROR']} 条、"
            f"警告（WARNING）{counts['WARNING']} 条、提示（INFO）{counts['INFO']} 条。"
            "错误级别会在保存时拒绝入库；警告级别只提示差异，不隐藏差异数值（§2.1）。"
        )
        for row, (level, _, _) in enumerate(rows):
            item = self.validation_table.item(row, 0)
            if item is not None:
                item.setForeground(QColor(BILL_LEVEL_COLORS.get(level, "#000000")))
        assumptions: list[str] = [BILL_PRICE_CALIBER_NOTICE]
        for item in outcomes:
            for text in item.assumptions:
                assumptions.append(f"{item.bill_id}：{text}")
            for text in item.messages:
                assumptions.append(f"{item.bill_id}：{text}")
        self.assumptions_view.setPlainText("\n".join(dict.fromkeys(assumptions)))

    # ------------------------------------------------------------------ #
    # 录入 / 编辑 / 删除 / 复制（§5.4）
    # ------------------------------------------------------------------ #
    def new_bill(self) -> None:
        """清空表单，准备录入一条新账单（账期默认取本月自然月）。"""
        self._editing_id = None
        today = date.today()
        last_day = calendar.monthrange(today.year, today.month)[1]
        for form in self.forms.values():
            for path, row in form.rows.items():
                row.set_value(None)
        self.forms[BILL_SECTIONS[0].title].rows["billing_period_start"].set_value(
            date(today.year, today.month, 1)
        )
        self.forms[BILL_SECTIONS[0].title].rows["billing_period_end"].set_value(
            date(today.year, today.month, last_day)
        )
        self.form_status.setText("正在录入新账单（尚未保存）。填写完成后点击「保存账单」。")
        self.tabs.setCurrentIndex(1)

    def clear_form(self) -> None:
        """清空表单并退出编辑状态（不触碰项目里已保存的账单）。"""
        self._editing_id = None
        for form in self.forms.values():
            for row in form.rows.values():
                row.set_value(None)
        self.form_status.setText("表单已清空（项目中的账单未受影响）。")

    def form_payload(self) -> dict:
        """读取表单值：未勾选「填写」的可空字段为 ``None``（= 账单未提供，§2.1）。"""
        payload: dict[str, object] = {}
        for form in self.forms.values():
            for path, row in form.rows.items():
                value = row.value()
                if path in BILL_OPTIONAL_TEXT_FIELDS and isinstance(value, str):
                    value = value.strip() or None
                payload[path] = value
        return payload

    def form_values(self) -> dict:
        """表单当前值（供测试断言"界面显示 = 服务返回"）。"""
        return dict(self.form_payload())

    def load_bill_into_form(self, bill: ElectricityBill | str) -> None:
        """把一条账单（对象或 ID）读入表单，进入编辑状态。"""
        if self._service is None:
            self._set_message("尚未绑定账单服务，无法编辑账单。")
            return
        if isinstance(bill, str):
            try:
                target = self._service.get_bill(bill)
            except ValidationError as exc:
                self._set_message(str(exc))
                return
        else:
            target = bill
        self._editing_id = target.bill_id
        for form in self.forms.values():
            for path, row in form.rows.items():
                row.set_value(getattr(target, path, None))
        self.form_status.setText(
            f"正在编辑账单 {target.bill_id}"
            f"（来源：{target.source_type.label}；质量状态：{target.quality_status.label}；"
            f"账单事实，不含任何模拟结果）。账单编号与创建时间不可修改。"
        )
        self.tabs.setCurrentIndex(1)

    def save_form(self):
        """保存表单（新增或更新）。校验失败时给出**中文**提示且不入库（§0.2）。"""
        if self._service is None:
            self._set_message("尚未绑定账单服务，无法保存账单。")
            return None
        payload = self.form_payload()
        try:
            if self._editing_id:
                bill = self._service.update_bill(self._editing_id, payload)
                action = "已更新"
            else:
                bill = self._service.create_bill(**payload)
                self._editing_id = bill.bill_id
                action = "已新增"
        except ValidationError as exc:
            self._set_message(f"账单未通过校验，未保存：{exc}")
            return None
        except Exception as exc:  # 兜底：不把裸异常抛给用户
            self._set_message(f"保存账单失败：{exc}")
            return None

        self._set_message(f"{action}账单 {bill.bill_id}（{bill.billing_month}）。", ok=True)
        self.form_status.setText(
            f"{action}账单 {bill.bill_id}：{bill.describe()}"
        )
        self.refresh()
        return bill

    def selected_bill_id(self) -> str | None:
        """列表当前选中的账单编号（未选中返回 ``None``）。"""
        row = self.list_table.currentRow()
        if 0 <= row < len(self._row_bill_ids):
            return self._row_bill_ids[row]
        return None

    def edit_selected(self) -> None:
        bill_id = self.selected_bill_id()
        if bill_id is None:
            self._set_message("请先在「账单列表」中选中一条账单。")
            return
        self.load_bill_into_form(bill_id)

    def delete_selected(self, confirm: bool = True) -> bool:
        """删除所选账单（默认先弹中文确认框，§5.4）。"""
        if self._service is None:
            self._set_message("尚未绑定账单服务，无法删除账单。")
            return False
        bill_id = self.selected_bill_id()
        if bill_id is None:
            self._set_message("请先在「账单列表」中选中一条账单。")
            return False
        if confirm:
            answer = QMessageBox.question(
                self,
                "删除确认",
                f"确定删除账单 {bill_id} 吗？删除后无法撤销（需重新录入或导入）。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_message("已取消删除。")
                return False
        try:
            removed = self._service.delete_bill(bill_id)
        except ValidationError as exc:
            self._set_message(str(exc))
            return False
        if self._editing_id == bill_id:
            self._editing_id = None
        self._set_message(f"已删除账单 {removed.bill_id}。", ok=True)
        self.refresh()
        return True

    def copy_previous_month(self):
        """复制上一条账单到下一个月（§5.4）。"""
        if self._service is None:
            self._set_message("尚未绑定账单服务，无法复制账单。")
            return None
        month = self.month_combo.currentData()
        if not month:
            latest = self._service.list_bills(sort_by="billing_month", descending=True)
            if not latest:
                self._set_message("当前项目没有账单可复制，请先手动录入或导入账单。")
                return None
            month = _next_month(latest[0].billing_month)
        try:
            copied = self._service.copy_previous_month(month)
        except ValidationError as exc:
            self._set_message(f"复制失败：{exc}")
            return None
        self._set_message(f"已复制生成账单 {copied.bill_id}（{copied.billing_month}）。", ok=True)
        self.refresh()
        return copied

    def clear_all(self, confirm: bool = True) -> bool:
        """清空全部账单（危险操作，默认二次确认）。"""
        if self._service is None:
            self._set_message("尚未绑定账单服务，无法清空账单。")
            return False
        if confirm:
            answer = QMessageBox.warning(
                self,
                "清空确认",
                "确定清空本项目全部账单吗？删除后无法撤销（需重新录入或导入）。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_message("已取消清空。")
                return False
        count = self._service.clear()
        self._editing_id = None
        self._set_message(f"已清空 {count} 条账单。", ok=True)
        self.refresh()
        return True

    # ------------------------------------------------------------------ #
    # 模板与导入（§5.3）
    # ------------------------------------------------------------------ #
    def download_template(self, target: str | Path | None = None) -> Path | None:
        """下载账单导入模板（§5.3）。

        :param target: 目标文件或目录；``None`` 时弹出"另存为"对话框。
            后缀处理用 ``with_name(name + 后缀)``：项目名常含 ``2061.8kWp`` 这类小数点。
        """
        if self._service is None:
            self._set_message("尚未绑定账单服务，无法下载模板。")
            return None
        path = Path(target) if target is not None else None
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self, "保存账单导入模板", "CENEP_电费账单导入模板.xlsx", "Excel (*.xlsx)"
            )
            if not chosen:
                self._set_message("已取消下载模板。")
                return None
            path = Path(chosen)
        if path.suffix.lower() != ".xlsx":
            path = path.with_name(path.name + ".xlsx")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self._service.template_bytes())
        except OSError as exc:
            self._set_message(f"写入模板文件失败：{exc}")
            return None
        self._set_message(f"已下载导入模板：{path}", ok=True)
        return path

    # ------------------------------------------------------------------ #
    # 测试辅助（只读）
    # ------------------------------------------------------------------ #
    def list_rows(self) -> list[list[str]]:
        """账单列表的二维文本（供测试断言显示口径，如"账单未提供"）。"""
        return [
            [
                self.list_table.item(r, c).text() if self.list_table.item(r, c) else ""
                for c in range(self.list_table.columnCount())
            ]
            for r in range(self.list_table.rowCount())
        ]

    def list_headers(self) -> list[str]:
        return [
            self.list_table.horizontalHeaderItem(c).text()
            for c in range(self.list_table.columnCount())
        ]

    def monthly_rows(self) -> list[list[str]]:
        return [
            [
                self.monthly_table.item(r, c).text() if self.monthly_table.item(r, c) else ""
                for c in range(self.monthly_table.columnCount())
            ]
            for r in range(self.monthly_table.rowCount())
        ]

    def annual_values(self) -> dict[str, str]:
        return self._table_map(self.annual_table)

    def validation_rows(self) -> list[list[str]]:
        return [
            [
                self.validation_table.item(r, c).text() if self.validation_table.item(r, c) else ""
                for c in range(self.validation_table.columnCount())
            ]
            for r in range(self.validation_table.rowCount())
        ]

    def validation_levels(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.validation_rows():
            if row:
                counts[row[0]] = counts.get(row[0], 0) + 1
        return counts

    def wizard_state(self) -> dict:
        return self.wizard.state()

    @staticmethod
    def _table_map(table: QTableWidget) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in range(table.rowCount()):
            name_item = table.item(r, 0)
            value_item = table.item(r, 1)
            if name_item is not None:
                out[name_item.text()] = value_item.text() if value_item is not None else ""
        return out


def _next_month(month: str) -> str:
    """``YYYY-MM`` 的下一个月（只做字符串进位，不涉及任何业务计算）。"""
    year, number = (int(part) for part in month.split("-"))
    return f"{year + 1}-01" if number == 12 else f"{year}-{number + 1:02d}"
