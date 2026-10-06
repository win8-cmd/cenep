"""七个页面：项目 / 参数 / 计算 / 结果 / 敏感性 / 报告 / 设置（规范 §97–§107）。

**本模块不含任何计算**：所有数值都来自 ``CalculationResult``。
"""

from __future__ import annotations

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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..domain.enums import ProjectType, ScenarioType, SensitivityVariable
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
from .sections import ALL_SECTIONS, SOURCE_LEGEND


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
