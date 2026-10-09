"""「负荷与消纳分析」页面（V2.2 §6.3、§8.4）。

页面分三段（规格书 §6.3 原文的分段）：

**A. 数据来源** —— 月账单估算 / 高频数据导入 / 已有数据集；导入预览、列映射、时区与
数据范围；覆盖率、缺失点、重复点、异常值；**估算数据始终显示橙色文字提示"估算曲线，不是实测"**。

**B. 负荷画像** —— 年/月/典型工作日与休息日负荷曲线、月总用电量与年化估算、
最大功率、平均功率、负荷率、数据覆盖率与质量等级。

**C. 消纳分析** —— 光伏发电量、自发自用电量、上网电量、电网购电量、
光伏自用率、负荷覆盖率、上网率、电网依赖率、月度自用率趋势、光伏与负荷叠加曲线、
数据质量及关键假设；**四项指标每个都同时显示计算口径**（§3.3「不得仅显示含糊的消纳率」）。

架构约束（规格书 §0.2、V2 §61、§148）
------------------------------------
* 本模块**不含任何计算**：估算、消纳、画像、光伏出力全部经
  :class:`cenep.application.load_profile_service.LoadProfileService` 取得；
  本文件只导入 ``cenep.application``、``cenep.data``、``cenep.domain`` 与
  ``cenep.calculation.errors``（静态扫描见 ``tests/test_gui.py``）。
* 页面上唯一的"换算"是显示单位换算（kWh → kW、小数 → 百分数），由
  :class:`cenep.ui.charts.LoadPvChart` 内部完成，不产生新的业务结论。
* 输入变更后旧结果立刻标记"已过期，需重新计算"（§8.4）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..application.load_profile_service import LoadProfileService
from ..application.scenario_service import ScenarioBillService
from ..calculation.errors import ValidationError
from ..domain.enums import LoadEstimateSource, MissingDataPolicy
from ..domain.load_estimate import (
    BUILTIN_LOAD_TEMPLATES,
    LoadEstimateParams,
    MonthlyLoadEnergy,
)
from ..domain.models import Project
from .charts import RANGE_DAY, RANGE_MONTH, RANGE_YEAR, LoadPvChart, MonthlyRateChart

#: 数据来源选项（§6.1 的两类输入 + 已有数据集）
SOURCE_ESTIMATE = "monthly_bill_estimate"
SOURCE_MEASURED = "high_frequency_import"

SOURCE_CHOICES: tuple[tuple[str, str], ...] = (
    (SOURCE_ESTIMATE, "月账单估算（月电量 + 可编辑典型负荷模板）"),
    (SOURCE_MEASURED, "实测高频负荷导入（15/30/60 分钟 Excel/CSV）"),
)

#: 间隔选项（§6.1）
INTERVAL_CHOICES: tuple[int, ...] = (15, 30, 60)

#: 估计"估算"提示的固定文案（§6.3 A：估算数据始终显示橙色提示）
ESTIMATE_BANNER = (
    "⚠ 估算曲线，不是实测：由月电量与典型负荷模板生成，仅可用于范围估算，"
    "不得作为实测逐时数据使用（V2.2 §0.2、§6.5）"
)

#: 实测提示文案
MEASURED_BANNER = "实测高频负荷导入：曲线来自用户导入的原始计量数据（V2.2 §6.1）"

#: 结果过期提示（§8.4）
STALE_TEXT = "⚠ 输入已变更：下方结果已过期，请重新计算"


def _no_edit(table: QTableWidget) -> None:
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)


def _text(value) -> str:
    """把服务返回的原始值转成表格文本（``None`` → 「未提供」，不显示 0）。"""
    if value is None:
        return "未提供"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return str(value)


def _set_rows(table: QTableWidget, rows: list[list[str]], headers: list[str]) -> None:
    """把二维文本写入表格（界面只展示字符串，不做计算）。"""
    table.clear()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.setItem(r, c, QTableWidgetItem("" if value is None else str(value)))
    table.resizeColumnsToContents()


class LoadAnalysisPage(QWidget):
    """负荷与消纳分析页（V2.2 §6.3）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service: LoadProfileService | None = None
        self.project: Project | None = None
        self.scenario_service: ScenarioBillService | None = None
        self.last_result = None
        self.last_estimate = None
        self.last_portrait = None
        self.last_scenario = None
        self.preview = None
        self._load_path: Path | None = None
        self._pv_dataset_id: str = ""

        root = QVBoxLayout(self)
        self.header = QLabel("负荷与消纳分析（V2.2 §6.3）")
        self.header.setStyleSheet("font-weight: bold; font-size: 14px;")
        root.addWidget(self.header)

        self.banner = QLabel(MEASURED_BANNER)
        self.banner.setWordWrap(True)
        root.addWidget(self.banner)

        self.stale_label = QLabel("")
        self.stale_label.setStyleSheet("color:#B00020; font-weight: bold;")
        root.addWidget(self.stale_label)

        self.tabs = QTabWidget(self)
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_source_tab(), "A 数据来源")
        self.tabs.addTab(self._build_portrait_tab(), "B 负荷画像")
        self.tabs.addTab(self._build_analysis_tab(), "C 消纳分析")
        # V2.3 §7.1 / V2.4 §8.4：D 区为光储四场景**账单**对比（阶段 6 的业务层早已就绪，
        # 界面在此接入；**不新增标签页**，避免改变既有的主界面导航结构）
        self.tabs.addTab(self._build_scenario_tab(), "D 光储场景对比")

    # ------------------------------------------------------------------ #
    # A. 数据来源
    # ------------------------------------------------------------------ #
    def _build_source_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        self.source_combo = QComboBox(page)
        for value, label in SOURCE_CHOICES:
            self.source_combo.addItem(label, value)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        row = QHBoxLayout()
        row.addWidget(QLabel("数据来源："))
        row.addWidget(self.source_combo, 1)
        layout.addLayout(row)

        self.source_stack = QStackedWidget(page)
        self.source_stack.addWidget(self._build_estimate_group())
        self.source_stack.addWidget(self._build_measured_group())
        layout.addWidget(self.source_stack)

        dataset_box = QGroupBox("已登记的负荷数据集（切换时旧数据集会保留，可复现历史方案，§6.4）")
        dataset_layout = QVBoxLayout(dataset_box)
        self.dataset_table = QTableWidget(dataset_box)
        _no_edit(self.dataset_table)
        self.dataset_table.itemSelectionChanged.connect(self._on_dataset_selected)
        dataset_layout.addWidget(self.dataset_table, 1)
        buttons = QHBoxLayout()
        self.activate_button = QPushButton("设为当前激活数据集", dataset_box)
        self.activate_button.clicked.connect(self.activate_selected_dataset)
        self.remove_button = QPushButton("删除选中的数据集", dataset_box)
        self.remove_button.clicked.connect(self.remove_selected_dataset)
        buttons.addWidget(self.activate_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        dataset_layout.addLayout(buttons)
        self.dataset_hint = QLabel("", dataset_box)
        self.dataset_hint.setWordWrap(True)
        dataset_layout.addWidget(self.dataset_hint)
        layout.addWidget(dataset_box, 1)
        return page

    def _build_estimate_group(self) -> QWidget:
        box = QGroupBox("月账单估算：月电量 + 可编辑典型负荷模板（§3.2、§6.5）")
        outer = QVBoxLayout(box)
        note = QLabel(
            "口径：E_i = E_month × w_i ÷ Σ w_i（w_i 是非负权重，不是 kW）；"
            "每月 ΣE_i 严格回归到输入月电量。模板是可编辑的权重曲线，**不是行业实测事实**。"
        )
        note.setWordWrap(True)
        outer.addWidget(note)

        form = QFormLayout()
        self.estimate_year = QSpinBox(box)
        self.estimate_year.setRange(2000, 2100)
        self.estimate_year.setValue(date.today().year)
        form.addRow("估算年份", self.estimate_year)

        self.template_combo = QComboBox(box)
        for template in BUILTIN_LOAD_TEMPLATES:
            self.template_combo.addItem(f"{template.name}（{template.template_id}）", template.template_id)
        self.template_combo.currentIndexChanged.connect(lambda _: self._set_estimate_note())
        form.addRow("典型负荷模板", self.template_combo)
        self.template_note = QLabel("")
        self.template_note.setWordWrap(True)
        form.addRow("模板说明", self.template_note)

        self.estimate_interval = QComboBox(box)
        for minutes in INTERVAL_CHOICES:
            self.estimate_interval.addItem(f"{minutes} 分钟", minutes)
        self.estimate_interval.setCurrentIndex(0)
        form.addRow("估算间隔", self.estimate_interval)

        self.estimate_annual = QDoubleSpinBox(box)
        self.estimate_annual.setRange(0.0, 1e12)
        self.estimate_annual.setDecimals(2)
        self.estimate_annual.setSuffix(" kWh")
        form.addRow("年用电量（用于补齐缺月）", self.estimate_annual)

        self.weekdays_per_week = QSpinBox(box)
        self.weekdays_per_week.setRange(1, 7)
        self.weekdays_per_week.setValue(5)
        form.addRow("每周生产天数", self.weekdays_per_week)

        self.weekend_ratio = QDoubleSpinBox(box)
        self.weekend_ratio.setRange(0.0, 1.0)
        self.weekend_ratio.setSingleStep(0.05)
        self.weekend_ratio.setDecimals(2)
        self.weekend_ratio.setValue(0.35)
        form.addRow("周末运行比例（相对工作日）", self.weekend_ratio)

        self.holiday_ratio = QDoubleSpinBox(box)
        self.holiday_ratio.setRange(0.0, 1.0)
        self.holiday_ratio.setSingleStep(0.05)
        self.holiday_ratio.setDecimals(2)
        self.holiday_ratio.setValue(0.10)
        form.addRow("节假日运行比例（相对工作日）", self.holiday_ratio)

        self.maintenance_ratio = QDoubleSpinBox(box)
        self.maintenance_ratio.setRange(0.0, 1.0)
        self.maintenance_ratio.setSingleStep(0.05)
        self.maintenance_ratio.setDecimals(2)
        self.maintenance_ratio.setValue(0.00)
        form.addRow("停产/检修日比例", self.maintenance_ratio)

        self.shift_row = QWidget(box)
        shift_layout = QHBoxLayout(self.shift_row)
        shift_layout.setContentsMargins(0, 0, 0, 0)
        self.shift_start = QSpinBox(self.shift_row)
        self.shift_start.setRange(0, 23)
        self.shift_start.setValue(8)
        self.shift_end = QSpinBox(self.shift_row)
        self.shift_end.setRange(1, 24)
        self.shift_end.setValue(20)
        self.offshift_ratio = QDoubleSpinBox(self.shift_row)
        self.offshift_ratio.setRange(0.0, 1.0)
        self.offshift_ratio.setSingleStep(0.05)
        self.offshift_ratio.setDecimals(2)
        self.offshift_ratio.setValue(0.10)
        self.shift_enabled = QCheckBox("启用班次裁剪", self.shift_row)
        shift_layout.addWidget(self.shift_enabled)
        shift_layout.addWidget(QLabel("班次"))
        shift_layout.addWidget(self.shift_start)
        shift_layout.addWidget(QLabel("时 至"))
        shift_layout.addWidget(self.shift_end)
        shift_layout.addWidget(QLabel("时，班外比例"))
        shift_layout.addWidget(self.offshift_ratio)
        shift_layout.addStretch(1)
        form.addRow("班次开始/结束时间", self.shift_row)

        self.day_night_row = QWidget(box)
        dn_layout = QHBoxLayout(self.day_night_row)
        dn_layout.setContentsMargins(0, 0, 0, 0)
        self.day_night_enabled = QCheckBox("启用白天/夜间电量比例", self.day_night_row)
        self.daytime_ratio = QDoubleSpinBox(self.day_night_row)
        self.daytime_ratio.setRange(0.0, 1.0)
        self.daytime_ratio.setSingleStep(0.05)
        self.daytime_ratio.setDecimals(2)
        self.daytime_ratio.setValue(0.60)
        dn_layout.addWidget(self.day_night_enabled)
        dn_layout.addWidget(QLabel("白天（06:00–18:00）占比"))
        dn_layout.addWidget(self.daytime_ratio)
        dn_layout.addStretch(1)
        form.addRow("白天/夜间负荷比例", self.day_night_row)

        self.shutdown_days_edit = QComboBox(box, editable=True)
        self.shutdown_days_edit.setEditable(True)
        self.shutdown_days_edit.addItem("")
        form.addRow("停产/检修日（YYYY-MM-DD，逗号分隔）", self.shutdown_days_edit)

        self.holidays_edit = QComboBox(box, editable=True)
        self.holidays_edit.setEditable(True)
        self.holidays_edit.addItem("")
        form.addRow("节假日（YYYY-MM-DD，逗号分隔）", self.holidays_edit)
        outer.addLayout(form)

        self.bill_coverage_label = QLabel("尚未读取月度账单电量。", box)
        self.bill_coverage_label.setWordWrap(True)
        outer.addWidget(self.bill_coverage_label)

        actions = QHBoxLayout()
        self.bill_check_button = QPushButton("检查账单月度电量覆盖", box)
        self.bill_check_button.clicked.connect(self.refresh_bill_coverage)
        self.estimate_button = QPushButton("由月电量生成估算曲线", box)
        self.estimate_button.clicked.connect(self.generate_estimate)
        actions.addWidget(self.bill_check_button)
        actions.addWidget(self.estimate_button)
        actions.addStretch(1)
        outer.addLayout(actions)

        self.estimate_result_label = QLabel("", box)
        self.estimate_result_label.setWordWrap(True)
        outer.addWidget(self.estimate_result_label)
        self._set_estimate_note()
        return box

    def _build_measured_group(self) -> QWidget:
        box = QGroupBox("实测高频负荷导入（六步流程：选文件 → 选表 → 映射 → 预览 → 校验 → 确认）")
        outer = QVBoxLayout(box)
        note = QLabel(
            "导入、列映射、间隔识别、时间对齐与质量检查全部由 CENEP 负荷导入器完成（阶段 3）；"
            "本页只负责展示与确认。数值口径（kW / kWh）判定不出时会报中文错误，不会替你猜。"
        )
        note.setWordWrap(True)
        outer.addWidget(note)

        form = QFormLayout()
        self.load_file_label = QLabel("尚未选择文件。", box)
        self.load_file_label.setWordWrap(True)
        form.addRow("负荷文件", self.load_file_label)

        self.load_sheet_combo = QComboBox(box)
        form.addRow("工作表", self.load_sheet_combo)

        self.load_interval_combo = QComboBox(box)
        self.load_interval_combo.addItem("自动识别", None)
        for minutes in INTERVAL_CHOICES:
            self.load_interval_combo.addItem(f"{minutes} 分钟", minutes)
        form.addRow("时间间隔", self.load_interval_combo)

        self.load_kind_combo = QComboBox(box)
        self.load_kind_combo.addItem("按表头自动判定", None)
        self.load_kind_combo.addItem("间隔平均功率（kW）", "power_kw")
        self.load_kind_combo.addItem("间隔电量（kWh）", "interval_energy_kwh")
        form.addRow("数值口径", self.load_kind_combo)

        self.pv_file_label = QLabel("未选择（默认按项目光伏参数生成出力曲线）", box)
        self.pv_file_label.setWordWrap(True)
        form.addRow("光伏实测文件（可选）", self.pv_file_label)
        outer.addLayout(form)

        actions = QHBoxLayout()
        self.pick_load_button = QPushButton("选择负荷文件…", box)
        self.pick_load_button.clicked.connect(self.choose_load_file)
        self.preview_button = QPushButton("生成预览", box)
        self.preview_button.clicked.connect(self.preview_load_file)
        self.confirm_button = QPushButton("确认导入（标记为实测）", box)
        self.confirm_button.clicked.connect(self.confirm_load_import)
        self.pick_pv_button = QPushButton("选择光伏实测文件…", box)
        self.pick_pv_button.clicked.connect(self.choose_pv_file)
        for widget in (
            self.pick_load_button,
            self.preview_button,
            self.confirm_button,
            self.pick_pv_button,
        ):
            actions.addWidget(widget)
        actions.addStretch(1)
        outer.addLayout(actions)

        self.preview_label = QLabel("", box)
        self.preview_label.setWordWrap(True)
        outer.addWidget(self.preview_label)
        self.preview_table = QTableWidget(box)
        _no_edit(self.preview_table)
        outer.addWidget(self.preview_table, 1)
        return box

    # ------------------------------------------------------------------ #
    # B. 负荷画像
    # ------------------------------------------------------------------ #
    def _build_portrait_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        self.portrait_button = QPushButton("生成/刷新负荷画像", page)
        self.portrait_button.clicked.connect(self.refresh_portrait)
        bar.addWidget(self.portrait_button)
        self.portrait_source_label = QLabel("尚未选择负荷数据集。", page)
        self.portrait_source_label.setWordWrap(True)
        bar.addWidget(self.portrait_source_label, 1)
        layout.addLayout(bar)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("画像概要", page))
        self.portrait_table = QTableWidget(page)
        _no_edit(self.portrait_table)
        left.addWidget(self.portrait_table, 1)
        left.addWidget(QLabel("逐月电量与负荷率", page))
        self.monthly_table = QTableWidget(page)
        _no_edit(self.monthly_table)
        left.addWidget(self.monthly_table, 2)
        body.addLayout(left, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("典型日负荷曲线（逐时刻平均功率 kW）", page))
        self.typical_table = QTableWidget(page)
        _no_edit(self.typical_table)
        right.addWidget(self.typical_table, 3)
        body.addLayout(right, 1)
        layout.addLayout(body, 1)
        return page

    # ------------------------------------------------------------------ #
    # C. 消纳分析
    # ------------------------------------------------------------------ #
    def _build_analysis_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        pv_box = QGroupBox("光伏出力参数（复用 §9 光伏引擎；不在此实现任何公式）")
        pv_form = QFormLayout(pv_box)
        self.pv_capacity = QDoubleSpinBox(pv_box)
        self.pv_capacity.setRange(0.0, 1e7)
        self.pv_capacity.setDecimals(3)
        self.pv_capacity.setSuffix(" kWp")
        self.pv_hours = QDoubleSpinBox(pv_box)
        self.pv_hours.setRange(0.0, 10000.0)
        self.pv_hours.setDecimals(2)
        self.pv_hours.setSuffix(" h")
        self.pv_pr = QDoubleSpinBox(pv_box)
        self.pv_pr.setRange(0.01, 1.0)
        self.pv_pr.setDecimals(4)
        self.pv_pr.setSingleStep(0.01)
        pv_form.addRow("装机容量", self.pv_capacity)
        pv_form.addRow("年等效利用小时", self.pv_hours)
        pv_form.addRow("性能比 PR", self.pv_pr)
        self.missing_policy_combo = QComboBox(pv_box)
        for policy in MissingDataPolicy:
            self.missing_policy_combo.addItem(policy.label, policy.value)
        pv_form.addRow("缺失数据处理", self.missing_policy_combo)
        layout.addWidget(pv_box)

        actions = QHBoxLayout()
        self.apply_pv_button = QPushButton("写回项目光伏参数", page)
        self.apply_pv_button.clicked.connect(self.apply_pv_params)
        self.analyze_button = QPushButton("计算光伏消纳四项指标", page)
        self.analyze_button.clicked.connect(self.run_analysis)
        self.range_box, self.range_mode, self.range_month, self.range_day = _build_range_row(page)
        actions.addWidget(self.apply_pv_button)
        actions.addWidget(self.analyze_button)
        actions.addWidget(self.range_box, 1)
        layout.addLayout(actions)
        self.range_mode.currentIndexChanged.connect(self._apply_range)
        self.range_month.currentIndexChanged.connect(self._apply_range)
        self.range_day.currentIndexChanged.connect(self._apply_range)

        self.analysis_source_label = QLabel("尚未计算。", page)
        self.analysis_source_label.setWordWrap(True)
        layout.addWidget(self.analysis_source_label)

        self.metric_table = QTableWidget(page)
        _no_edit(self.metric_table)
        layout.addWidget(self.metric_table, 1)

        self.energy_table = QTableWidget(page)
        _no_edit(self.energy_table)
        layout.addWidget(self.energy_table, 1)

        self.chart = LoadPvChart(page)
        layout.addWidget(self.chart, 2)

        self.monthly_chart = MonthlyRateChart(page)
        layout.addWidget(self.monthly_chart, 2)

        layout.addWidget(QLabel("月度消纳明细（分母为 0 的月份显示「不适用」）", page))
        self.analysis_monthly_table = QTableWidget(page)
        _no_edit(self.analysis_monthly_table)
        layout.addWidget(self.analysis_monthly_table, 1)

        layout.addWidget(QLabel("数据质量与关键假设（报告 assumptions 同源）", page))
        self.assumption_view = QPlainTextEdit(page)
        self.assumption_view.setReadOnly(True)
        layout.addWidget(self.assumption_view, 1)
        return page

    # ------------------------------------------------------------------ #
    # D. 光储场景对比（V2.3 §7.1、§7.6；V2.4 §8.4）
    # ------------------------------------------------------------------ #
    def _build_scenario_tab(self) -> QWidget:
        """D 区：同一负荷、同一电价计划下的四场景电费对比（界面不实现任何公式）。

        所有数值来自 :class:`cenep.application.scenario_service.ScenarioBillService`：
        ``compare()`` 生成四场景与去重清单，``scenario_rows()`` / ``comparison_rows()`` /
        ``dedup_rows()`` 只做行组装，``verify_finance_consistency()`` 校验"财务现金流里的
        运营收益 == 去重后的唯一收益"（§7.6 第 5 条）。界面只负责显示与状态提示。
        """
        page = QWidget(self)
        layout = QVBoxLayout(page)

        note = QLabel(
            "口径：四场景（无光伏 / 仅光伏 / 仅储能 / 光伏+储能）在**同一负荷、同一电价计划**下模拟；"
            "账单节省额是**唯一**收益入口，储能套利与光伏自用只做分解展示、不再次累加（V2.3 §7.6）。"
            "电价计划必须有核验状态；未核验计划不得用于正式结论（§4.1、§7.3）。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        row = QHBoxLayout()
        row.addWidget(QLabel("电价计划："))
        self.scenario_plan_combo = QComboBox(page)
        row.addWidget(self.scenario_plan_combo, 1)
        self.scenario_refresh_button = QPushButton("刷新电价计划", page)
        self.scenario_refresh_button.clicked.connect(self.reload_tariff_plans)
        row.addWidget(self.scenario_refresh_button)
        self.scenario_run_button = QPushButton("执行四场景对比", page)
        self.scenario_run_button.clicked.connect(self.run_scenario_compare)
        row.addWidget(self.scenario_run_button)
        layout.addLayout(row)

        self.scenario_note_label = QLabel(
            "尚未选择电价计划。请先点击「刷新电价计划」，再选择一套已核验的电价版本。"
        )
        self.scenario_note_label.setWordWrap(True)
        layout.addWidget(self.scenario_note_label)

        self.scenario_stale_label = QLabel("")
        self.scenario_stale_label.setStyleSheet("color:#B00020; font-weight: bold;")
        layout.addWidget(self.scenario_stale_label)

        layout.addWidget(QLabel("① 四场景年度账单（口径：方案模拟，不是实际账单）", page))
        self.scenario_table = QTableWidget(page)
        _no_edit(self.scenario_table)
        layout.addWidget(self.scenario_table, 2)

        layout.addWidget(QLabel("② 方案 vs 基准：分项电费差额（元）", page))
        self.comparison_table = QTableWidget(page)
        _no_edit(self.comparison_table)
        layout.addWidget(self.comparison_table, 2)

        layout.addWidget(QLabel("③ 收益去重清单（哪些计了、哪些没计、为什么）", page))
        self.dedup_table = QTableWidget(page)
        _no_edit(self.dedup_table)
        layout.addWidget(self.dedup_table, 2)

        layout.addWidget(QLabel("④ 财务一致性校验与关键假设（§7.6 第 5 条）", page))
        self.scenario_detail_view = QPlainTextEdit(page)
        self.scenario_detail_view.setReadOnly(True)
        layout.addWidget(self.scenario_detail_view, 1)
        return page

    def bind_scenario_service(self, service) -> None:
        """由主窗口注入场景服务（界面不自行拼装服务，§0.2 分层）。"""
        self.scenario_service = service
        self.reload_tariff_plans()

    def reload_tariff_plans(self) -> None:
        """刷新电价计划下拉（只列计划与核验状态，不替用户决定用哪一套）。"""
        combo = self.scenario_plan_combo
        combo.clear()
        if self.scenario_service is None:
            self.scenario_note_label.setText(
                "场景服务不可用：请先在「设置」中确认电价版本库（SQLite）可写。"
            )
            return
        try:
            plans = self.scenario_service.list_tariff_plans()
        except Exception as exc:  # pragma: no cover - 版本库异常
            self.scenario_note_label.setText(f"读取电价计划失败：{exc}")
            return
        for plan in plans:
            combo.addItem(f"{plan.name}｜{plan.status.label}", plan.tariff_plan_id)
        if plans:
            self.scenario_note_label.setText(self.scenario_service.describe_tariff_plan(combo.currentData()))
        else:
            self.scenario_note_label.setText(
                "电价版本库中没有可用计划：请在「月度账单」页载入内置计划、按账单反算或手工录入"
                "（软件**不会**预填任何未经核验的电价，V2.3 §4.1）。"
            )

    def run_scenario_compare(self) -> None:
        """执行四场景对比并刷新 ①②③④（全部数值来自服务，界面不做计算）。"""
        if self.scenario_service is None or self.scenario_combo_plan_id() == "":
            self.scenario_note_label.setStyleSheet("color:#B00020;")
            self.scenario_note_label.setText(
                "请先选择一套电价计划；若列表为空，请先在「月度账单」页录入或载入电价计划。"
            )
            return
        try:
            self.last_scenario = self.scenario_service.compare(self.scenario_combo_plan_id())
        except ValidationError as exc:
            self.scenario_note_label.setStyleSheet("color:#B00020;")
            self.scenario_note_label.setText(f"四场景对比未执行：{exc}")
            return
        except Exception as exc:  # 兜底，不把英文异常抛给用户
            self.scenario_note_label.setStyleSheet("color:#B00020;")
            self.scenario_note_label.setText(f"四场景对比失败，请检查输入：{exc}")
            return

        result = self.last_scenario
        self.scenario_stale_label.setText("")
        self.scenario_note_label.setStyleSheet("color:#1B7F3B; font-weight: bold;")
        self.scenario_note_label.setText(
            f"电价计划：{result.tariff_plan_name}（{result.tariff_plan_status or '未标注状态'}）｜"
            f"负荷来源：{result.load_source or '未标注'}｜"
            f"{'实测' if result.load_is_measured else '估算（不是实测）'}｜"
            f"时间间隔 {result.interval_minutes} 分钟 / {result.point_count} 点"
        )

        rows = ScenarioBillService.scenario_rows(result)
        _set_rows(self.scenario_table, [[_text(c) for c in row] for row in rows[1:]], rows[0])
        rows = ScenarioBillService.comparison_rows(result)
        _set_rows(self.comparison_table, [[_text(c) for c in row] for row in rows[1:]], rows[0])
        rows = ScenarioBillService.dedup_rows(result)
        _set_rows(self.dedup_table, [[_text(c) for c in row] for row in rows[1:]], rows[0])

        lines: list[str] = []
        lines.append(f"收益去重校验：{'通过' if result.dedup_verified else '未通过'}")
        lines.append(
            f"唯一去重后的年度运营收益：{result.unique_annual_benefit_yuan:,.2f} 元"
            f"（取自场景：{result.reference_scenario.label}）"
        )
        try:
            ok, deviation, messages = self.scenario_service.verify_finance_consistency(result)
            lines.append(
                f"财务现金流一致性：{'与唯一收益一致' if ok else '不一致'}"
                f"（偏差 {deviation:.6f} 元）"
            )
            for message in messages:
                lines.append(f"· {message}")
        except ValidationError as exc:
            lines.append(f"财务一致性校验未执行：{exc}")
        except Exception as exc:  # pragma: no cover - 财务引擎异常
            lines.append(f"财务一致性校验失败：{exc}")
        for text in (*result.warnings, *result.assumptions):
            lines.append(f"• {text}")
        self.scenario_detail_view.setPlainText("\n".join(lines))

    def scenario_combo_plan_id(self) -> str:
        """当前选中的电价计划编号（空字符串 = 未选择）。"""
        data = self.scenario_plan_combo.currentData()
        return "" if data is None else str(data)

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    def bind(self, project: Project, service: LoadProfileService | None = None) -> None:
        """绑定项目与服务（由主窗口调用；界面不自行拼装服务）。"""
        self.project = project
        self.service = service or LoadProfileService(project)
        self.last_result = None
        self.last_estimate = None
        self.last_portrait = None
        self.last_scenario = None
        self.preview = None
        self._load_path = None
        self._pv_dataset_id = ""
        self.scenario_stale_label.setText("")
        self.scenario_detail_view.setPlainText("")
        for table in (self.scenario_table, self.comparison_table, self.dedup_table):
            table.setRowCount(0)
        self.load_file_label.setText("尚未选择文件。")
        self.pv_file_label.setText("未选择（默认按项目光伏参数生成出力曲线）")
        self.load_sheet_combo.clear()
        self.preview_table.setRowCount(0)
        self.preview_label.setText("")
        self.estimate_result_label.setText("")
        self.bill_coverage_label.setText("尚未读取月度账单电量。")
        self.stale_label.setText("")
        self.analysis_source_label.setText("尚未计算。")
        self.assumption_view.setPlainText("")
        for table in (
            self.dataset_table,
            self.portrait_table,
            self.monthly_table,
            self.typical_table,
            self.metric_table,
            self.energy_table,
            self.analysis_monthly_table,
        ):
            table.setRowCount(0)
        self.chart.set_series([], [], [], 60)
        self.monthly_chart.set_rows([])
        self._sync_pv_fields()
        self.reload()

    def _sync_pv_fields(self) -> None:
        """把项目光伏参数显示到页面（只读展示，可写回）。"""
        if self.project is None:
            return
        self.pv_capacity.setValue(float(self.project.pv.pv_capacity_kwp or 0.0))
        self.pv_hours.setValue(float(self.project.pv.equivalent_hours or 0.0))
        self.pv_pr.setValue(float(self.project.pv.performance_ratio or 1.0))

    def reload(self) -> None:
        """刷新数据集列表与来源提示（不做任何计算）。"""
        datasets = self.service.datasets() if self.service else []
        rows = []
        for dataset in datasets:
            marker = "✔ 当前激活" if dataset.profile_id == self.project.active_load_dataset_id else ""
            rows.append(
                [
                    marker,
                    dataset.name or dataset.profile_id,
                    dataset.source_type.label,
                    "估算" if dataset.estimated else "实测",
                    f"{dataset.interval_minutes} 分钟",
                    str(dataset.point_count),
                    f"{dataset.coverage_ratio:.2%}",
                    dataset.quality_status.label,
                ]
            )
        _set_rows(
            self.dataset_table,
            rows,
            ["激活", "名称", "来源", "实测/估算", "间隔", "点数", "覆盖率", "质量"],
        )
        active = self.service.active_dataset() if self.service else None
        if active is None:
            self.banner.setText(MEASURED_BANNER)
            self.banner.setStyleSheet("color:#555555;")
            self.dataset_hint.setText("尚未登记负荷数据集（空状态，不生成任何虚构数据）。")
        else:
            if active.estimated:
                self.banner.setText(f"{ESTIMATE_BANNER}｜{active.provenance_text}")
                self.banner.setStyleSheet("color:#C05600; font-weight: bold;")
            else:
                self.banner.setText(f"{MEASURED_BANNER}｜{active.provenance_text}")
                self.banner.setStyleSheet("color:#1B7F3B; font-weight: bold;")
            self.dataset_hint.setText(active.quality_summary_text())

    # ------------------------------------------------------------------ #
    # A. 交互
    # ------------------------------------------------------------------ #
    def _on_source_changed(self) -> None:
        self.source_stack.setCurrentIndex(self.source_combo.currentIndex())
        self._mark_stale()

    def _set_estimate_note(self) -> None:
        template_id = self.template_combo.currentData()
        for template in BUILTIN_LOAD_TEMPLATES:
            if template.template_id == template_id:
                self.template_note.setText(
                    f"{template.description}；适用：{template.industry}。"
                    f"{template.notes} 可编辑权重：{list(template.workday_weights)[:4]}…"
                )
                return

    def refresh_bill_coverage(self) -> None:
        """显示账单电量对 12 个月的覆盖（缺少哪些月会明确列出，§6.5）。"""
        if self.service is None:
            return
        year = self.estimate_year.value()
        coverage = self.service.bill_month_coverage(year)
        covered = "、".join(str(m) for m in coverage["covered_months"]) or "无"
        missing = "、".join(str(m) for m in coverage["missing_months"]) or "无"
        self.bill_coverage_label.setText(
            f"{year} 年账单电量覆盖：{coverage['count']} 个月（{covered}），缺 {missing}；"
            f"账单电量合计 {coverage['total_kwh']:,.2f} kWh。"
            f"缺失月份将按「年用电量 + 月度比例」（未填比例则均匀分摊）补齐，属明确估算假设。"
        )
        if coverage["missing_months"] and self.estimate_annual.value() <= 0.0:
            self.bill_coverage_label.setStyleSheet("color:#B00020;")
            self.bill_coverage_label.setText(
                self.bill_coverage_label.text()
                + "★ 缺月且未填年用电量：这些月份无法估算，请补充年用电量。"
            )
        else:
            self.bill_coverage_label.setStyleSheet("color:#333333;")

    def _parse_dates(self, text: str, label: str) -> list[date]:
        """把 ``YYYY-MM-DD`` 逗号/顿号分隔文本解析为日期列表（中文错误）。"""
        items = [
            chunk.strip()
            for chunk in (text or "").replace("，", ",").replace("、", ",").split(",")
            if chunk.strip()
        ]
        result: list[date] = []
        for item in items:
            try:
                result.append(date.fromisoformat(item))
            except ValueError:
                raise ValidationError(
                    f"{label}「{item}」不是合法日期；请使用 YYYY-MM-DD 格式，多条用逗号分隔"
                    f"（V2.2 §6.5）",
                    field="load_estimate.dates",
                ) from None
        return result

    def build_params(self) -> LoadEstimateParams:
        """由界面控件构造估算参数（只做单位与类型转换，不含任何业务公式）。

        :raises ValidationError: 日期格式非法、白天比例与年电量缺失等（中文报错）
        """
        workdays = self.weekdays_per_week.value()
        interval = int(self.estimate_interval.currentData())
        offshift = float(self.offshift_ratio.value()) if self.shift_enabled.isChecked() else None
        day_ratio = float(self.daytime_ratio.value()) if self.day_night_enabled.isChecked() else None
        return LoadEstimateParams(
            year=self.estimate_year.value(),
            interval_minutes=interval,
            template_id=self.template_combo.currentData(),
            workdays_per_week=workdays,
            shift_start_hour=self.shift_start.value(),
            shift_end_hour=self.shift_end.value(),
            offshift_run_ratio=offshift,
            weekend_run_ratio=float(self.weekend_ratio.value()),
            holiday_run_ratio=float(self.holiday_ratio.value()),
            maintenance_run_ratio=float(self.maintenance_ratio.value()),
            daytime_load_ratio=day_ratio,
            nighttime_load_ratio=(1.0 - day_ratio) if day_ratio is not None else None,
            shutdown_days=self._parse_dates(self.shutdown_days_edit.currentText(), "停产/检修日"),
            holidays=self._parse_dates(self.holidays_edit.currentText(), "节假日"),
            annual_energy_kwh=float(self.estimate_annual.value()),
        )

    def generate_estimate(self) -> None:
        """按当前界面参数生成估算曲线（估算标记由服务与模型共同保证）。"""
        if self.service is None:
            return
        try:
            params = self.build_params()
            monthly = self.service.monthly_energies_from_bills(params.year)
            self.last_estimate = self.service.estimate_load(params, monthly=monthly)
        except ValidationError as exc:
            self.estimate_result_label.setStyleSheet("color:#B00020;")
            self.estimate_result_label.setText(f"生成估算曲线失败：{exc}")
            return
        except Exception as exc:  # 兜底：不把裸异常抛给用户（§0.2）
            self.estimate_result_label.setStyleSheet("color:#B00020;")
            self.estimate_result_label.setText(f"生成估算曲线失败，请检查输入：{exc}")
            return

        result = self.last_estimate
        rows = result.monthly
        sources = "、".join(sorted({row.source.label for row in rows}))
        self.estimate_result_label.setStyleSheet("color:#C05600; font-weight: bold;")
        self.estimate_result_label.setText(
            f"已生成【估算】曲线：{result.dataset.profile_id}｜{result.dataset.point_count} 点｜"
            f"间隔 {self.last_estimate.params.interval_minutes} 分钟｜月电量来源：{sources}｜"
            f"输入合计 {result.total_input_energy_kwh:,.3f} kWh、估算合计 "
            f"{result.total_estimated_energy_kwh:,.3f} kWh、最大单月残差 "
            f"{result.max_abs_residual_kwh:.3e} kWh（容差 {result.tolerance_kwh:g} kWh，"
            f"{'全部月份通过' if result.all_months_regressed else '★存在超限月份'}）。"
            f"⚠ {result.dataset.source_type.report_badge}"
        )
        self.stale_label.setText("")
        self.reload()
        self.refresh_portrait()

    def choose_load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择负荷文件", "", "负荷文件 (*.xlsx *.xlsm *.csv);;所有文件 (*)"
        )
        if path:
            self.set_load_file(path)

    def set_load_file(self, path: str | Path) -> bool:
        """设置负荷文件并列出工作表（自动化测试直接调用）。"""
        if self.service is None:
            return False
        try:
            sheets = self.service.list_sheets(path)
        except ValidationError as exc:
            self.preview_label.setStyleSheet("color:#B00020;")
            self.preview_label.setText(f"无法读取该文件：{exc}")
            return False
        except Exception as exc:  # 兜底
            self.preview_label.setStyleSheet("color:#B00020;")
            self.preview_label.setText(f"读取文件失败，请确认文件未损坏且未被占用：{exc}")
            return False
        self._load_path = Path(path)
        self.load_sheet_combo.clear()
        for name in sheets:
            self.load_sheet_combo.addItem(name)
        self.load_file_label.setText(f"已选择：{self._load_path}\n工作表：{'、'.join(sheets)}")
        self.preview_label.setText("")
        self._mark_stale()
        return True

    def choose_pv_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择光伏实测文件", "", "光伏文件 (*.xlsx *.xlsm *.csv);;所有文件 (*)"
        )
        if path:
            self.pv_file_label.setText(f"已选择：{path}")

    def _preview_kwargs(self) -> dict:
        """把界面选择翻译成导入器参数（口径判定仍在 ``data`` 层，界面不猜）。"""
        from ..domain.enums import LoadValueKind

        kind_text = self.load_kind_combo.currentData()
        kind = None
        if kind_text == "power_kw":
            kind = LoadValueKind.POWER_KW
        elif kind_text == "interval_energy_kwh":
            kind = LoadValueKind.INTERVAL_ENERGY_KWH
        return {
            "sheet": self.load_sheet_combo.currentText() or None,
            "interval_minutes": self.load_interval_combo.currentData(),
            "value_kind": kind,
        }

    def preview_load_file(self) -> None:
        """生成导入预览并展示质量检查结果（第 ④/⑤ 步）。"""
        if self.service is None or self._load_path is None:
            self.preview_label.setStyleSheet("color:#B00020;")
            self.preview_label.setText("请先选择负荷文件。")
            return
        try:
            self.preview = self.service.preview_import(self._load_path, **self._preview_kwargs())
        except ValidationError as exc:
            self.preview = None
            self.preview_label.setStyleSheet("color:#B00020;")
            self.preview_label.setText(
                f"导入预览未通过：{exc}\n可尝试：显式指定时间间隔或数值口径，"
                f"或改选其它工作表/数据块。"
            )
            return
        except Exception as exc:  # 兜底
            self.preview = None
            self.preview_label.setStyleSheet("color:#B00020;")
            self.preview_label.setText(f"生成预览失败：{exc}")
            return

        preview = self.preview
        self.preview_label.setStyleSheet("color:#333333;")
        self.preview_label.setText(
            f"{preview.counts_text()}\n列映射：{preview.column_mapping}；"
            f"间隔说明：{preview.interval_note}\n{preview.issues_text()}"
        )
        rows = [
            [
                str(row.row_number),
                row.timestamp_text,
                row.value_text,
                row.status.label,
                "；".join(row.messages),
            ]
            for row in preview.rows
        ]
        _set_rows(self.preview_table, rows, ["行号", "时间", "数值", "状态", "问题"])
        self._mark_stale()

    def confirm_load_import(self) -> None:
        """确认导入（标记为**实测**；估算数据不经此路径）。"""
        if self.service is None or self.preview is None:
            self.preview_label.setStyleSheet("color:#B00020;")
            self.preview_label.setText("请先生成导入预览，再确认导入。")
            return
        try:
            dataset = self.service.import_preview(self.preview)
        except ValidationError as exc:
            self.preview_label.setStyleSheet("color:#B00020;")
            self.preview_label.setText(f"导入未通过：{exc}")
            return
        self.preview_label.setStyleSheet("color:#1B7F3B;")
        self.preview_label.setText(
            f"已导入并登记为**实测**负荷数据集：{dataset.provenance_text}\n"
            f"{dataset.quality_summary_text()}"
        )
        self.stale_label.setText("")
        self.preview = None
        self.reload()
        self.refresh_portrait()

    def _selected_dataset_id(self) -> str:
        row = self.dataset_table.currentRow()
        if row < 0 or self.service is None:
            return ""
        datasets = self.service.datasets()
        if row >= len(datasets):
            return ""
        return datasets[row].profile_id

    def _on_dataset_selected(self) -> None:
        self._mark_stale()

    def activate_selected_dataset(self) -> None:
        if self.service is None:
            return
        profile_id = self._selected_dataset_id()
        if not profile_id:
            self.dataset_hint.setText("请先在列表中选择一个数据集。")
            return
        try:
            dataset = self.service.activate(profile_id)
        except ValidationError as exc:
            self.dataset_hint.setText(str(exc))
            return
        self.dataset_hint.setText(f"已切换激活数据集：{dataset.provenance_text}")
        self.stale_label.setText("")
        self.reload()

    def remove_selected_dataset(self) -> None:
        if self.service is None:
            return
        profile_id = self._selected_dataset_id()
        if not profile_id:
            self.dataset_hint.setText("请先在列表中选择一个数据集。")
            return
        try:
            removed = self.service.remove(profile_id)
        except ValidationError as exc:
            self.dataset_hint.setText(str(exc))
            return
        self.dataset_hint.setText(f"已删除数据集：{removed.profile_id}（切换数据集不会删除历史版本）")
        self.reload()

    # ------------------------------------------------------------------ #
    # B. 交互
    # ------------------------------------------------------------------ #
    def refresh_portrait(self) -> None:
        """生成负荷画像（§6.3 B）。"""
        if self.service is None:
            return
        try:
            self.last_portrait = self.service.portrait()
        except ValidationError as exc:
            self.portrait_source_label.setStyleSheet("color:#B00020;")
            self.portrait_source_label.setText(str(exc))
            return
        portrait = self.last_portrait
        self.portrait_source_label.setStyleSheet(
            "color:#C05600; font-weight: bold;" if portrait.estimated else "color:#1B7F3B;"
        )
        self.portrait_source_label.setText(
            f"{portrait.name or portrait.profile_id}｜{portrait.provenance_text}"
        )
        _set_rows(self.portrait_table, [[k, v] for k, v in portrait.portrait_rows()], ["项目", "值"])
        _set_rows(
            self.monthly_table,
            [
                [
                    item.month_key,
                    f"{item.energy_kwh:,.2f}",
                    f"{item.peak_power_kw:,.2f}",
                    f"{item.avg_power_kw:,.2f}",
                    f"{item.load_factor:.2%}",
                    str(item.missing_value_count),
                ]
                for item in portrait.monthly
            ],
            ["月份", "电量 kWh", "最大功率 kW", "平均功率 kW", "负荷率", "缺失点"],
        )
        _set_rows(
            self.typical_table,
            self._typical_rows(portrait),
            ["时刻", "工作日 kW", "休息日 kW", "全部日 kW"],
        )

    @staticmethod
    def _typical_rows(portrait) -> list[list[str]]:
        """把典型日曲线整理成"时刻 × 日类型"的显示行（纯展示换算 kWh → kW）。"""
        curves = {curve.day_type: curve for curve in portrait.typical_days}
        reference = curves.get("WORKDAY") or curves.get("ALL") or curves.get("WEEKEND")
        if reference is None:
            return []
        delta_hours = reference.interval_minutes / 60.0
        rows: list[list[str]] = []
        for index in range(len(reference.values_kwh)):
            minutes = index * reference.interval_minutes
            clock = f"{minutes // 60:02d}:{minutes % 60:02d}"
            row = [clock]
            for day_type in ("WORKDAY", "WEEKEND", "ALL"):
                curve = curves.get(day_type)
                row.append(
                    f"{curve.values_kwh[index] / delta_hours:,.2f}"
                    if curve is not None and index < len(curve.values_kwh)
                    else "—"
                )
            rows.append(row)
        return rows

    # ------------------------------------------------------------------ #
    # C. 交互
    # ------------------------------------------------------------------ #
    def apply_pv_params(self) -> None:
        """把页面上的光伏参数写回项目（字段级赋值，不含任何计算）。"""
        if self.project is None:
            return
        try:
            self.project.pv.pv_capacity_kwp = float(self.pv_capacity.value())
            self.project.pv.equivalent_hours = float(self.pv_hours.value())
            self.project.pv.performance_ratio = float(self.pv_pr.value())
        except Exception as exc:  # 兜底：Pydantic 校验失败也给中文提示
            self.analysis_source_label.setStyleSheet("color:#B00020;")
            self.analysis_source_label.setText(f"光伏参数写回失败：{exc}")
            return
        self.analysis_source_label.setStyleSheet("color:#1B7F3B;")
        self.analysis_source_label.setText("光伏参数已写回项目。")
        self._mark_stale()

    def run_analysis(self) -> None:
        """计算消纳四项指标并刷新曲线、表格与假设文本（§6.3 C）。"""
        if self.service is None:
            return
        policy = MissingDataPolicy(self.missing_policy_combo.currentData())
        pv_series = None
        if self._pv_dataset_id:
            try:
                pv_dataset = self.service.dataset_by_id(self._pv_dataset_id)
                load_dataset = self.service.active_dataset()
                if load_dataset is None:
                    raise ValidationError("尚未选择负荷数据集（V2.2 §6.3 A）", field="load.dataset_id")
                pv_series = self.service.pv_series_from_dataset(pv_dataset, load_dataset)
            except ValidationError as exc:
                self.analysis_source_label.setStyleSheet("color:#B00020;")
                self.analysis_source_label.setText(f"消纳计算失败：{exc}")
                return
        try:
            self.last_result = self.service.analyze(
                pv_series=pv_series, missing_policy=policy
            )
        except ValidationError as exc:
            self.analysis_source_label.setStyleSheet("color:#B00020;")
            self.analysis_source_label.setText(f"消纳计算失败：{exc}")
            return
        except Exception as exc:  # 兜底
            self.analysis_source_label.setStyleSheet("color:#B00020;")
            self.analysis_source_label.setText(f"消纳计算失败，请检查输入：{exc}")
            return

        result = self.last_result
        self.stale_label.setText("")
        self.analysis_source_label.setStyleSheet(
            "color:#C05600; font-weight: bold;" if result.is_based_on_estimate else "color:#1B7F3B;"
        )
        self.analysis_source_label.setText(result.provenance_text)

        metric_rows = result.metric_rows()
        _set_rows(
            self.metric_table,
            [[name, value, caliber] for name, value, caliber in _with_names(metric_rows)],
            ["指标", "数值", "计算口径（分子 / 分母 / 单位 / 边界）"],
        )
        self.metric_table.resizeRowsToContents()
        _set_rows(
            self.energy_table,
            [[name, f"{value:,.3f}", note] for name, value, note in result.energy_rows()],
            ["项目", "数值 kWh", "口径"],
        )
        _set_rows(
            self.analysis_monthly_table,
            [
                [
                    row.month_key,
                    f"{row.load_energy_kwh:,.2f}",
                    f"{row.pv_generation_kwh:,.2f}",
                    f"{row.pv_used_on_site_kwh:,.2f}",
                    f"{row.pv_export_kwh:,.2f}",
                    f"{row.grid_import_kwh:,.2f}",
                    row.rate_text("self_consumption"),
                    row.rate_text("load_coverage"),
                    row.rate_text("export"),
                    row.rate_text("grid_dependency"),
                ]
                for row in result.monthly
            ],
            [
                "月份",
                "负荷 kWh",
                "光伏 kWh",
                "自用 kWh",
                "上网 kWh",
                "购电 kWh",
                "自用率",
                "负荷覆盖率",
                "上网率",
                "电网依赖率",
            ],
        )
        self.assumption_view.setPlainText("\n".join(f"• {line}" for line in result.assumption_lines()))

        dataset = self.service.active_dataset()
        if dataset is not None:
            self.chart.set_series(
                [point.timestamp for point in dataset.points],
                self.service.load_series(dataset),
                _pv_series_for_chart(self.service, dataset, pv_series),
                dataset.interval_minutes,
            )
        self.monthly_chart.set_rows(result.monthly)
        self._apply_range()

    def _apply_range(self) -> None:
        mode = self.range_mode.currentData()
        month = self.range_month.currentData() or 1
        day = self.range_day.currentData() or 1
        self.chart.set_range(mode, int(month), int(day))

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _mark_stale(self) -> None:
        """输入变更后把旧结果标记为过期（§8.4）。"""
        if self.last_result is not None or self.last_portrait is not None:
            self.stale_label.setText(STALE_TEXT)
        # V2.3 §7.1：负荷 / 电价 / 关键参数变更后，四场景结果同样必须标记过期
        if self.last_scenario is not None:
            self.scenario_stale_label.setText(
                "⚠ 输入已变更：四场景对比结果已过期，请重新执行对比（V2.4 §8.4）"
            )

    def state(self) -> dict:
        """当前页面状态（供测试断言，不含计算）。"""
        result = self.last_result
        return {
            "datasets": len(self.service.datasets()) if self.service else 0,
            "active": self.project.active_load_dataset_id if self.project else "",
            "estimate_points": self.last_estimate.dataset.point_count if self.last_estimate else 0,
            "estimate_max_residual": (
                self.last_estimate.max_abs_residual_kwh if self.last_estimate else None
            ),
            "self_consumption_rate": None if result is None else result.self_consumption_rate,
            "load_coverage_rate": None if result is None else result.load_coverage_rate,
            "export_rate": None if result is None else result.export_rate,
            "grid_dependency_rate": None if result is None else result.grid_dependency_rate,
            "based_on_estimate": None if result is None else result.is_based_on_estimate,
            "stale": bool(self.stale_label.text()),
            "metric_rows": 0 if result is None else self.metric_table.rowCount(),
            "monthly_rows": 0 if result is None else self.analysis_monthly_table.rowCount(),
            # —— V2.3 §7.1 四场景（D 区）——
            "scenario_plans": self.scenario_plan_combo.count(),
            "scenario_plan_id": self.scenario_combo_plan_id(),
            "scenario_rows": 0 if self.last_scenario is None else self.scenario_table.rowCount(),
            "comparison_rows": 0 if self.last_scenario is None else self.comparison_table.rowCount(),
            "dedup_rows": 0 if self.last_scenario is None else self.dedup_table.rowCount(),
            "unique_annual_benefit": (
                None if self.last_scenario is None else self.last_scenario.unique_annual_benefit_yuan
            ),
            "dedup_verified": None if self.last_scenario is None else self.last_scenario.dedup_verified,
            "scenario_stale": bool(self.scenario_stale_label.text()),
        }


def _with_names(metric_rows) -> list[tuple[str, str, str]]:
    """给指标行补上中文名（口径文本的第一段即"名称（公式摘要）"）。"""
    out: list[tuple[str, str, str]] = []
    for key, value, caliber in metric_rows:
        name = {
            "self_consumption": "光伏自用率（自发自用 ÷ 光伏发电量）",
            "load_coverage": "负荷覆盖率（自发自用 ÷ 负荷电量，电量口径）",
            "export": "光伏上网率（上网电量 ÷ 光伏发电量）",
            "grid_dependency": "电网依赖率（电网购电 ÷ 负荷电量）",
        }.get(key, key)
        out.append((name, value, caliber))
    return out


def _pv_series_for_chart(service, dataset, pv_series):
    """图表用的光伏电量序列；未显式给出时按项目参数解析（失败返回零序列，不阻塞刷新）。"""
    if pv_series is not None:
        return pv_series
    try:
        return service.resolve_pv_series(dataset)
    except ValidationError:
        return np.zeros(dataset.point_count, dtype=float)


def _build_range_row(page: QWidget):
    """构造"全年 / 按月 / 按日"选择行（复用既有时间筛选范式，V2 §50）。"""
    box = QWidget(page)
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(QLabel("曲线时间范围"))
    mode = QComboBox(box)
    mode.addItem("全年", RANGE_YEAR)
    mode.addItem("按月", RANGE_MONTH)
    mode.addItem("按日", RANGE_DAY)
    month = QComboBox(box)
    for value in range(1, 13):
        month.addItem(f"{value} 月", value)
    day = QComboBox(box)
    for value in range(1, 32):
        day.addItem(f"{value} 日", value)
    row.addWidget(mode)
    row.addWidget(month)
    row.addWidget(day)
    row.addStretch(1)
    return box, mode, month, day


#: 便于主窗口与测试引用
__all__ = [
    "ESTIMATE_BANNER",
    "INTERVAL_CHOICES",
    "MEASURED_BANNER",
    "SOURCE_CHOICES",
    "STALE_TEXT",
    "LoadAnalysisPage",
]
