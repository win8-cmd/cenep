"""V2.1 阶段 2：账单页面与报告（规格书 §5.4、§8.1、§8.4、§9.1、§11）。

本文件覆盖阶段 2 的验收要点：

1. ``BillsPage`` 能构造、能填入 / 清空、无账单时不报错并显示录入指引；
2. 六步导入向导（选文件 → 选表 → 映射列 → 预览 → 校验 → 确认）的关键状态转换；
3. 校验提示 ERROR / WARNING / INFO 分级正确显示（级别文本 + 颜色）；
4. ``None`` 显示为「账单未提供」而**不是 0**（页面 / Excel / PDF 三处专测）；
5. Excel 两张新表在有 / 无账单两种情况下都存在且内容正确，且工作簿内公式数为 0；
6. PDF 在有 / 无账单两种情况下都生成、章节数正确（17 部分）；
7. §105 一致性：页面显示的数字与 ``BillService`` 返回的对象一致。

GUI 依赖（PySide6）已安装，全部用例在 ``QT_QPA_PLATFORM=offscreen`` 下**真实执行**
（规格书 §0.2 红线：不得虚报通过）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from reportlab.platypus import Paragraph, Table

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过账单页测试")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cenep.application.bill_service import BillService  # noqa: E402
from cenep.calculation.engine import calculation_engine  # noqa: E402
from cenep.data.bill_importer import BILL_COLUMNS, SHEET_BILLS  # noqa: E402
from cenep.domain.bill_models import BILL_FIELD_LABELS, ElectricityBill  # noqa: E402
from cenep.domain.enums import BillQualityStatus  # noqa: E402
from cenep.infrastructure.project_file import load_project  # noqa: E402
from cenep.reports.excel_exporter import SHEET_NAMES, ExcelExporter  # noqa: E402
from cenep.reports.pdf_exporter import (  # noqa: E402
    BILL_EMPTY_TEXT,
    BILL_NOT_PROVIDED_TEXT,
    REPORT_SECTIONS,
    PdfExporter,
    _styles,
    register_cjk_font,
)
from cenep.ui.pages import (  # noqa: E402
    BILL_EMPTY_GUIDE,
    BILL_LEVEL_COLORS,
    BILL_NOT_PROVIDED,
    BILL_SIMULATION_NOTICE,
    BillsPage,
    bill_kwh,
    bill_price,
    bill_yuan,
)

HEADERS = [column.header for column in BILL_COLUMNS]
#: 模板表头（字段 → 列名），用于断言自动列映射结果
TEMPLATE_HEADERS = {column.field: column.header for column in BILL_COLUMNS}


# --------------------------------------------------------------------------- #
# 夹具与工具
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def project(golden_pv):
    """账单页测试项目：沿用黄金案例（未启用时序，运行快）。"""
    golden_pv.basic_info.project_name = "账单页测试项目"
    return golden_pv


@pytest.fixture
def service(project) -> BillService:
    return BillService(project)


@pytest.fixture
def page(qapp, service) -> BillsPage:
    widget = BillsPage()
    widget.bind(service)
    yield widget
    widget.setParent(None)
    widget.close()
    widget.deleteLater()
    QApplication.processEvents()


def _create(service: BillService, **overrides) -> ElectricityBill:
    """经服务录入一条自洽的账单（分时电量合计 = 总电量，金额分项合计 = 总额）。"""
    payload: dict = {
        "billing_period_start": date(2026, 1, 1),
        "billing_period_end": date(2026, 1, 31),
        "meter_id": "M001",
        "tariff_structure": "单一制",
        "energy_total_kwh": 100_000.0,
        "energy_sharp_kwh": 10_000.0,
        "energy_peak_kwh": 30_000.0,
        "energy_flat_kwh": 40_000.0,
        "energy_valley_kwh": 20_000.0,
        "energy_charge_yuan": 65_000.0,
        "bill_total_yuan": 65_000.0,
    }
    payload.update(overrides)
    return service.create_bill(**payload)


def _raw_bill(**overrides) -> ElectricityBill:
    """直接构造账单对象（用于经服务 'validate=False' 存入带 ERROR 的草稿）。"""
    payload: dict = {
        "bill_id": "BILL-DRAFT-ERROR",
        "project_id": "账单页测试项目",
        "billing_period_start": date(2026, 3, 1),
        "billing_period_end": date(2026, 3, 31),
        "energy_total_kwh": -1000.0,
        "bill_total_yuan": 1000.0,
    }
    payload.update(overrides)
    return ElectricityBill(**payload)


def _row(**fields) -> list:
    return [fields.get(column.field, "") for column in BILL_COLUMNS]


def _bill_workbook(path: Path, rows: list[list], sheet: str = SHEET_BILLS) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(row)
    workbook.save(path)
    return path


def _good_row(**overrides) -> list:
    values = {
        "billing_period_start": "2026-01-01",
        "billing_period_end": "2026-01-31",
        "meter_id": "M001",
        "tariff_structure": "单一制",
        "energy_total_kwh": 100_000,
        "energy_sharp_kwh": 10_000,
        "energy_peak_kwh": 30_000,
        "energy_flat_kwh": 40_000,
        "energy_valley_kwh": 20_000,
        "energy_charge_yuan": 65_000,
        "bill_total_yuan": 65_000,
    }
    values.update(overrides)
    return _row(**values)


def _collect_text(flowables) -> str:
    """递归收集 PDF 文档流中的全部文本（含表格单元格）。"""
    parts: list[str] = []
    for item in flowables:
        if isinstance(item, Paragraph):
            parts.append(item.text)
        elif isinstance(item, Table):
            for row in item._cellvalues:
                for cell in row:
                    parts.append(cell if isinstance(cell, str) else _collect_text([cell]))
    return "\n".join(parts)


def _story_text(project) -> str:
    result = calculation_engine.calculate(project)
    styles = _styles(register_cjk_font())
    return _collect_text(PdfExporter().build_story(project, result, styles))


def _sheet_text(ws) -> str:
    return "\n".join(
        str(cell.value) for row in ws.iter_rows() for cell in row if cell.value is not None
    )


def _formula_count(path: Path) -> int:
    wb = load_workbook(path)
    return sum(
        1
        for ws in wb.worksheets
        for row in ws.iter_rows()
        for cell in row
        if isinstance(cell.value, str) and cell.value.startswith("=")
    )


# --------------------------------------------------------------------------- #
# 1. 页面构造与空状态
# --------------------------------------------------------------------------- #
class TestPageConstruction:
    def test_constructs_without_service_and_refresh_is_safe(self, qapp):
        widget = BillsPage()
        widget.refresh()
        assert "尚未绑定" in widget.empty_label.text()
        assert widget.list_table.rowCount() == 0
        widget.deleteLater()

    def test_empty_project_shows_entry_guide(self, page):
        """无账单时不报错，并显示录入指引（§5.4、§8.2）。"""
        assert page.list_table.rowCount() == 0
        text = page.empty_label.text()
        assert "尚未录入电费账单" in text
        assert "录入方法" in text
        assert "导入向导" in text
        assert "不会自动生成任何虚构账单" in text
        assert text == BILL_EMPTY_GUIDE

    def test_empty_project_summaries_do_not_fabricate_numbers(self, page):
        values = page.annual_values()
        assert values["年度总购电量"] == BILL_NOT_PROVIDED
        assert values["年度账单总额"] == BILL_NOT_PROVIDED
        assert values["平均综合电价"] == "无法计算（账单未提供电量或金额）"
        assert values["模拟账单（阶段 5/6）"].startswith("待确认 / 未建模")
        assert page.monthly_rows() == []
        assert page.validation_rows() == []

    def test_banner_declares_fact_and_simulation_boundary(self, page):
        """界面上必须写明「账单事实 vs 模拟结果」的分界（阶段 5/6 才有模拟结果）。"""
        assert page.banner.text() == BILL_SIMULATION_NOTICE
        for keyword in ("账单事实", "未建模", "待确认"):
            assert keyword in page.banner.text()

    def test_wizard_declares_six_steps(self, page):
        assert len(page.wizard.STEP_TITLES) == 6
        assert page.wizard.step == 1
        for title in page.wizard.STEP_TITLES:
            assert title in page.wizard.breadcrumb.text()
        assert "第 1 / 6 步" in page.wizard.step_label.text()

    def test_price_caliber_notice_visible(self, page):
        """平均综合电价的口径限制必须写在界面上（§3.1）。"""
        assert "边际节省电价" in page.monthly_hint.text()


# --------------------------------------------------------------------------- #
# 2. 手动录入 / 填入 / 清空（§5.4）
# --------------------------------------------------------------------------- #
class TestManualEntryThroughPage:
    def test_new_bill_defaults_to_current_natural_month(self, page):
        page.new_bill()
        payload = page.form_payload()
        today = date.today()
        assert payload["billing_period_start"] == date(today.year, today.month, 1)
        assert payload["billing_period_end"].month == today.month
        assert page.form_payload()["source_type"] == "manual"

    def test_clear_form_does_not_touch_saved_bills(self, page, service):
        _create(service)
        page.refresh()
        assert page.list_table.rowCount() == 1
        page.clear_form()
        assert service.bills and len(service) == 1
        assert page.list_table.rowCount() == 1

    def test_save_form_creates_bill_and_updates_list(self, page, service):
        page.new_bill()
        form = page.forms["账单基本信息"]
        form.rows["billing_period_start"].set_value(date(2026, 2, 1))
        form.rows["billing_period_end"].set_value(date(2026, 2, 28))
        form.rows["meter_id"].set_value("M-02")
        page.forms["电量（kWh）"].rows["energy_total_kwh"].set_value(120_000.0)
        page.forms["费用（元）"].rows["bill_total_yuan"].set_value(78_000.0)
        bill = page.save_form()

        assert bill is not None
        assert len(service) == 1
        assert service.bills[0].energy_total_kwh == pytest.approx(120_000.0)
        assert service.bills[0].billing_month == "2026-02"
        assert page.list_table.rowCount() == 1
        assert page.list_rows()[0][0] == bill.bill_id
        assert page.list_rows()[0][4] == "120,000.00 kWh"
        assert page.list_rows()[0][5] == "78,000.00 元"
        assert page.list_rows()[0][6] == "手动录入"
        assert page.list_rows()[0][7] == "有效"

    def test_load_bill_into_form_round_trip(self, page, service):
        bill = _create(service)
        page.load_bill_into_form(bill)
        payload = page.form_payload()
        assert payload["billing_period_start"] == bill.billing_period_start
        assert payload["billing_period_end"] == bill.billing_period_end
        assert payload["energy_total_kwh"] == pytest.approx(bill.energy_total_kwh)
        assert payload["bill_total_yuan"] == pytest.approx(bill.bill_total_yuan)
        assert payload["meter_id"] == bill.meter_id
        assert payload["tariff_structure"] == bill.tariff_structure.value
        assert payload["source_type"] == bill.source_type.value

    def test_update_bill_through_form(self, page, service):
        bill = _create(service)
        page.load_bill_into_form(bill)
        page.forms["费用（元）"].rows["bill_total_yuan"].set_value(70_000.0)
        updated = page.save_form()
        assert updated is not None
        assert updated.bill_id == bill.bill_id
        assert len(service) == 1
        assert service.get_bill(bill.bill_id).bill_total_yuan == pytest.approx(70_000.0)

    def test_duplicate_period_reports_chinese_error(self, page, service):
        """重复账期不允许静默写入，而是给出**中文**提示（§0.2、§5.5）。"""
        page.new_bill()
        form = page.forms["账单基本信息"]
        form.rows["billing_period_start"].set_value(date(2026, 4, 1))
        form.rows["billing_period_end"].set_value(date(2026, 4, 30))
        page.forms["电量（kWh）"].rows["energy_total_kwh"].set_value(1_000.0)
        page.forms["费用（元）"].rows["bill_total_yuan"].set_value(1_000.0)
        assert page.save_form() is not None

        # 再次"新增"同一账期（清空编辑状态）→ 必须被拒绝
        page.new_bill()
        form = page.forms["账单基本信息"]
        form.rows["billing_period_start"].set_value(date(2026, 4, 1))
        form.rows["billing_period_end"].set_value(date(2026, 4, 30))
        page.forms["电量（kWh）"].rows["energy_total_kwh"].set_value(2_000.0)
        page.forms["费用（元）"].rows["bill_total_yuan"].set_value(2_000.0)
        assert page.save_form() is None
        assert "跳过" in page.message_label.text() or "重复" in page.message_label.text()
        assert len(service) == 1

    def test_delete_and_copy(self, page, service):
        bill = _create(service)
        page.refresh()
        page.list_table.setCurrentCell(0, 0)
        assert page.selected_bill_id() == bill.bill_id
        assert page.delete_selected(confirm=False) is True
        assert len(service) == 0

        again = _create(service)
        page.refresh()
        page.month_combo.setCurrentIndex(page.month_combo.findData("2026-02"))
        copied = page.copy_previous_month()
        assert copied is not None
        assert copied.billing_month == "2026-02"
        assert len(service) == 2
        assert service.get_bill(again.bill_id) is not None


# --------------------------------------------------------------------------- #
# 3. None → 「账单未提供」，不得显示为 0（§2.1）—— 本阶段最容易写错的一条
# --------------------------------------------------------------------------- #
class TestMissingValueDisplay:
    def test_helpers_distinguish_none_from_zero(self):
        assert bill_kwh(None) == BILL_NOT_PROVIDED
        assert bill_kwh(0.0) == "0.00 kWh"
        assert bill_yuan(None) == BILL_NOT_PROVIDED
        assert bill_yuan(0.0) == "0.00 元"
        assert bill_price(None) == "无法计算（账单未提供电量或金额）"
        assert bill_price(0.5) == "0.5000 元/kWh"

    def test_list_shows_not_provided_for_none_and_zero_for_zero(self, page, service):
        _create(
            service,
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            energy_total_kwh=None,
            bill_total_yuan=None,
            energy_sharp_kwh=None,
            energy_peak_kwh=None,
            energy_flat_kwh=None,
            energy_valley_kwh=None,
            energy_charge_yuan=None,
        )
        _create(
            service,
            billing_period_start=date(2026, 2, 1),
            billing_period_end=date(2026, 2, 28),
            energy_total_kwh=0.0,
            bill_total_yuan=0.0,
            energy_sharp_kwh=0.0,
            energy_peak_kwh=0.0,
            energy_flat_kwh=0.0,
            energy_valley_kwh=0.0,
            energy_charge_yuan=0.0,
        )
        page.refresh()
        rows = {row[1]: row for row in page.list_rows()}

        missing_row = rows["2026-01"]
        assert missing_row[4] == BILL_NOT_PROVIDED
        assert missing_row[5] == BILL_NOT_PROVIDED
        assert "0.00" not in missing_row[4] and "0.00" not in missing_row[5]

        zero_row = rows["2026-02"]
        assert zero_row[4] == "0.00 kWh"
        assert zero_row[5] == "0.00 元"
        assert BILL_NOT_PROVIDED not in " ".join(zero_row)

    def test_monthly_summary_uses_not_provided(self, page, service):
        _create(service, energy_total_kwh=None, bill_total_yuan=None)
        page.refresh()
        row = page.monthly_rows()[0]
        assert row[0] == "2026-01"
        assert row[2] == BILL_NOT_PROVIDED
        assert row[3] == BILL_NOT_PROVIDED
        assert row[4] == "无法计算（账单未提供电量或金额）"

    def test_annual_summary_uses_not_provided(self, page, service):
        _create(service, energy_total_kwh=None, bill_total_yuan=None)
        page.refresh()
        values = page.annual_values()
        assert values["年度总购电量"] == BILL_NOT_PROVIDED
        assert values["年度账单总额"] == BILL_NOT_PROVIDED
        assert values["平均综合电价"] == "无法计算（账单未提供电量或金额）"

    def test_zero_annual_total_is_displayed_as_zero(self, page, service):
        _create(
            service,
            energy_total_kwh=0.0,
            bill_total_yuan=0.0,
            energy_sharp_kwh=0.0,
            energy_peak_kwh=0.0,
            energy_flat_kwh=0.0,
            energy_valley_kwh=0.0,
            energy_charge_yuan=0.0,
        )
        page.refresh()
        values = page.annual_values()
        assert values["年度总购电量"] == "0.00 kWh"
        assert values["年度账单总额"] == "0.00 元"
        assert values["平均综合电价"] == "无法计算（账单未提供电量或金额）"

    def test_unchecked_form_field_displays_not_provided_not_zero(self, page):
        """表单层面同样不得把"未提供"显示成 0（未勾选「填写」→ 显示「账单未提供」）。"""
        page.new_bill()
        row = page.forms["电量（kWh）"].rows["energy_total_kwh"]
        assert row.checkbox is not None and not row.checkbox.isChecked()
        assert row.editor.text() == BILL_NOT_PROVIDED
        assert row.value() is None
        assert page.form_payload()["energy_total_kwh"] is None

        # 勾选后从 0 开始录入，此时 0 是"用户确实填了 0"，必须显示为 0
        row.checkbox.setChecked(True)
        row.editor.setValue(0.0)
        assert row.value() == 0.0
        assert page.form_payload()["energy_total_kwh"] == 0.0

        page.clear_form()
        assert row.editor.text() == BILL_NOT_PROVIDED
        assert page.form_payload()["energy_total_kwh"] is None

    def test_loaded_bill_keeps_real_values_in_form(self, page, service):
        bill = _create(service, energy_total_kwh=123_456.0, bill_total_yuan=80_000.0)
        page.load_bill_into_form(bill)
        energy_editor = page.forms["电量（kWh）"].rows["energy_total_kwh"].editor
        amount_editor = page.forms["费用（元）"].rows["bill_total_yuan"].editor
        assert float(energy_editor.text().replace(",", "")) == pytest.approx(123_456.0)
        assert float(amount_editor.text().replace(",", "")) == pytest.approx(80_000.0)
        # 账单里没有提供的分项 → 显示「账单未提供」
        assert page.forms["费用（元）"].rows["vat_yuan"].editor.text() == BILL_NOT_PROVIDED

    def test_excel_and_pdf_never_show_zero_for_missing(self, project, service, tmp_path: Path):
        _create(service, energy_total_kwh=None, bill_total_yuan=None)
        result = calculation_engine.calculate(project)

        wb = load_workbook(ExcelExporter().export(project, result, tmp_path / "missing.xlsx"))
        ws = wb["账单原始数据"]
        header = [cell.value for cell in ws[3]]
        energy_col = header.index(BILL_FIELD_LABELS["energy_total_kwh"]) + 1
        amount_col = header.index(BILL_FIELD_LABELS["bill_total_yuan"]) + 1
        data_row = 4
        assert ws.cell(row=data_row, column=energy_col).value == BILL_NOT_PROVIDED_TEXT
        assert ws.cell(row=data_row, column=amount_col).value == BILL_NOT_PROVIDED_TEXT

        text = _story_text(project)
        assert BILL_NOT_PROVIDED_TEXT in text


# --------------------------------------------------------------------------- #
# 4. 校验提示分级（ERROR / WARNING / INFO）
# --------------------------------------------------------------------------- #
class TestValidationLevels:
    def test_error_warning_info_levels_are_displayed_and_colored(self, page, service):
        # ERROR：负数电量（只能作为草稿存入，validate=False 是显式逃生口）
        service.add_bill(_raw_bill(), validate=False)
        # WARNING：分时电量合计与总电量差异超容差（100000 vs 90000）
        _create(service, energy_total_kwh=100_000.0, energy_flat_kwh=30_000.0)
        # INFO：完全自洽的账单
        _create(
            service,
            billing_period_start=date(2026, 2, 1),
            billing_period_end=date(2026, 2, 28),
        )
        page.refresh()
        levels = page.validation_levels()
        assert levels.get("ERROR", 0) >= 1
        assert levels.get("WARNING", 0) >= 1
        assert levels.get("INFO", 0) >= 1

        rows = page.validation_rows()
        assert any(row[0] == "ERROR" for row in rows)
        assert any(row[0] == "WARNING" for row in rows)
        assert any(row[0] == "INFO" for row in rows)

        # 级别配色：ERROR 红 / WARNING 橙 / INFO 蓝（"看得清"）
        seen: dict[str, str] = {}
        for index, row in enumerate(rows):
            item = page.validation_table.item(index, 0)
            seen.setdefault(row[0], item.foreground().color().name())
        assert seen["ERROR"].lower() == BILL_LEVEL_COLORS["ERROR"].lower()
        assert seen["WARNING"].lower() == BILL_LEVEL_COLORS["WARNING"].lower()
        assert seen["INFO"].lower() == BILL_LEVEL_COLORS["INFO"].lower()

    def test_error_message_is_chinese_and_locates_field(self, page, service):
        service.add_bill(_raw_bill(), validate=False)
        page.refresh()
        text = " ".join(" ".join(row) for row in page.validation_rows())
        assert "不能为负" in text
        assert "总购电量" in text
        assert "B01" in text  # 规则编号便于追溯

    def test_validation_label_summarises_counts(self, page, service):
        service.add_bill(_raw_bill(), validate=False)
        page.refresh()
        label = page.validation_label.text()
        assert "错误（ERROR）" in label and "警告（WARNING）" in label and "提示（INFO）" in label

    def test_assumptions_are_rendered(self, page, service):
        _create(service)
        page.refresh()
        text = page.assumptions_view.toPlainText()
        assert "边际节省电价" in text  # 平均综合电价的统计口径限制（§3.1）
        assert "不按 0" in text or "未知" in text

    def test_quality_status_reflects_invalid_draft(self, page, service):
        service.add_bill(_raw_bill(), validate=False)
        page.refresh()
        assert service.bills[0].quality_status is BillQualityStatus.INVALID
        assert page.list_rows()[0][7] == "无效"


# --------------------------------------------------------------------------- #
# 5. 六步导入向导
# --------------------------------------------------------------------------- #
class TestImportWizard:
    def test_six_step_transitions(self, page, service, tmp_path: Path):
        path = _bill_workbook(tmp_path / "bills.xlsx", [HEADERS, _good_row()])
        wizard = page.wizard
        assert wizard.step == 1

        assert wizard.set_file(path) is True
        assert wizard.step == 2
        assert wizard.sheet_names == [SHEET_BILLS]
        assert wizard.sheet_combo.itemText(0) == SHEET_BILLS

        assert wizard.select_sheet(SHEET_BILLS) is True
        assert wizard.step == 3
        assert wizard.preview is not None
        assert wizard.preview.column_mapping["energy_total_kwh"] == TEMPLATE_HEADERS["energy_total_kwh"]
        assert wizard.mapping_table.rowCount() == len(wizard.preview.column_mapping)

        assert wizard.apply_mapping() is True
        assert wizard.step == 4
        assert wizard.preview.valid_count == 1
        assert wizard.preview_table.rowCount() == 1
        assert wizard.preview_table.item(0, 0).text() == SHEET_BILLS
        assert wizard.preview_table.item(0, 1).text() == "2"  # 表头第 1 行 → 数据首行 = 2
        assert wizard.preview_table.item(0, 3).text() == "提示"  # VALID → INFO

        assert wizard.validate_rows() is True
        assert wizard.step == 5
        assert wizard.issue_table.rowCount() >= 1
        assert any(level == "INFO" for level, _, _ in wizard.issues)

        result = wizard.confirm_import("skip")
        assert result is not None
        assert wizard.step == 6
        assert len(service) == 1
        assert page.list_table.rowCount() == 1  # imported 信号已触发刷新
        assert "导入完成" in wizard.result_label.text()

    def test_mapping_override_and_renamed_columns(self, page, service, tmp_path: Path):
        """列名完全无法自动识别时：转手工列映射 → 仍能预览并导入（§5.5）。"""
        headers = ["F1", "F2", "F3", "F4"]
        path = _bill_workbook(
            tmp_path / "renamed.xlsx", [headers, ["2026-05-01", "2026-05-31", 50_000, 30_000]]
        )
        wizard = page.wizard
        assert wizard.set_file(path) is True
        # 自动识别失败，但**不能卡住**：已进入第 3 步并给出中文说明
        assert wizard.select_sheet(wizard.sheet_combo.currentText()) is True
        assert wizard.step == 3
        assert wizard.preview is None
        assert "手工" in wizard.mapping_hint.text()
        assert wizard.mapping_table.rowCount() == len(BILL_COLUMNS)

        # 用户手工指定列映射（覆盖全部必需列）
        assert wizard.apply_mapping(
            {
                "billing_period_start": "F1",
                "billing_period_end": "F2",
                "energy_total_kwh": "F3",
                "bill_total_yuan": "F4",
            }
        ) is True
        assert wizard.step == 4
        assert wizard.preview.valid_count == 1
        assert wizard.validate_rows() is True
        result = wizard.confirm_import("skip")
        assert result is not None
        assert len(service) == 1
        assert service.bills[0].billing_month == "2026-05"
        assert service.bills[0].energy_total_kwh == pytest.approx(50_000.0)

    def test_missing_required_columns_switches_to_manual_mapping(self, page, tmp_path: Path):
        path = _bill_workbook(tmp_path / "wrong.xlsx", [["姓名", "金额"], ["张三", 100]])
        wizard = page.wizard
        assert wizard.set_file(path) is True
        assert wizard.select_sheet() is True  # 进入第 3 步（手工映射），而不是卡在第 2 步
        assert wizard.step == 3
        assert wizard.preview is None
        assert "手工" in wizard.mapping_hint.text()
        # 手工映射也缺账期列 → 中文错误，不抛裸异常
        assert wizard.apply_mapping({"energy_total_kwh": "金额"}) is False
        assert "无法生成预览" in wizard.message_label.text() or "缺少必需列" in wizard.message_label.text()

    def test_missing_file_reports_chinese(self, page, tmp_path: Path):
        wizard = page.wizard
        assert wizard.set_file(tmp_path / "不存在.xlsx") is False
        assert "文件不存在" in wizard.message_label.text()
        assert wizard.step == 1

    def test_invalid_rows_are_listed_and_not_imported(self, page, service, tmp_path: Path):
        rows = [
            HEADERS,
            _good_row(),
            _good_row(billing_period_start="2026-02-01", billing_period_end="2026-02-28",
                      energy_total_kwh=-500),
        ]
        path = _bill_workbook(tmp_path / "mixed.xlsx", rows)
        result = page.wizard.run_all(path, sheet=SHEET_BILLS, strategy="skip")
        assert result is not None
        assert len(service) == 1  # 只有有效行入库
        assert result.invalid_rows  # 无效行行号可定位
        levels = {level for level, _, _ in page.wizard.issues}
        assert "ERROR" in levels and "INFO" in levels
        assert page.wizard.step == 6

    def test_duplicate_strategy_skip_then_replace(self, page, service, tmp_path: Path):
        path = _bill_workbook(tmp_path / "dup.xlsx", [HEADERS, _good_row()])
        assert page.wizard.run_all(path, strategy="skip") is not None
        assert len(service) == 1

        # 第二次导入同一条账单：skip → 不新增
        page.wizard.reset()
        assert page.wizard.run_all(path, strategy="skip") is not None
        assert len(service) == 1

        # replace → 仍然只有一条（就地替换）
        page.wizard.reset()
        assert page.wizard.run_all(path, strategy="replace") is not None
        assert len(service) == 1

        # keep_both → 两条（新编号）
        page.wizard.reset()
        assert page.wizard.run_all(path, strategy="keep_both") is not None
        assert len(service) == 2

    def test_duplicate_rows_flagged_in_preview(self, page, service, tmp_path: Path):
        _create(service, billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31))
        path = _bill_workbook(tmp_path / "dup.xlsx", [HEADERS, _good_row()])
        wizard = page.wizard
        assert wizard.set_file(path)
        assert wizard.select_sheet()
        assert wizard.apply_mapping()
        assert wizard.preview.duplicate_count == 1
        assert wizard.validate_rows()
        detail = " ".join(text for _, _, text in wizard.issues)
        assert "疑似重复" in detail

    def test_reset_returns_to_step_one(self, page, tmp_path: Path):
        path = _bill_workbook(tmp_path / "bills.xlsx", [HEADERS, _good_row()])
        page.wizard.run_all(path)
        page.wizard.reset()
        assert page.wizard.step == 1
        assert page.wizard.preview is None
        assert page.wizard.issues == []


# --------------------------------------------------------------------------- #
# 6. Excel 两张新表（有 / 无账单）
# --------------------------------------------------------------------------- #
class TestExcelBillSheets:
    def test_sheets_exist_with_bills_and_match_service(self, project, service, tmp_path: Path):
        _create(service, billing_period_start=date(2026, 2, 1), billing_period_end=date(2026, 2, 28))
        first = _create(service, billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31))
        result = calculation_engine.calculate(project)
        path = ExcelExporter().export(project, result, tmp_path / "bills.xlsx")
        wb = load_workbook(path)

        assert wb.sheetnames == SHEET_NAMES
        assert "账单原始数据" in wb.sheetnames and "账单校验" in wb.sheetnames

        ws = wb["账单原始数据"]
        headers = [cell.value for cell in ws[3]]
        assert BILL_FIELD_LABELS["energy_total_kwh"] in headers
        assert BILL_FIELD_LABELS["bill_total_yuan"] in headers
        data = [
            row
            for row in ws.iter_rows(min_row=4)
            if row[1].value not in (None, "")
        ]
        assert len(data) == 2
        # 按账期排序：1 月在前（即使录入顺序相反）
        assert data[0][1].value == first.bill_id
        assert data[1][1].value != first.bill_id
        assert ws.cell(row=4, column=4).value == "2026-01-01"  # 账期起列

        check = wb["账单校验"]
        check_text = _sheet_text(check)
        assert "分时电量合计差" in check_text
        assert "费用分项合计差" in check_text
        assert "口径与假设" in check_text
        assert "边际节省电价" in check_text  # assumptions 直接来自 BillService

        # 校验数值与服务返回的核对结果一致（§105）
        outcomes = service.reconcile_all()
        for item in outcomes:
            assert item.bill_id in check_text
        numbers = [
            cell.value
            for row in check.iter_rows()
            for cell in row
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)
        ]
        for item in outcomes:
            for expected in (item.amount_tolerance_yuan, item.energy_tolerance_kwh):
                assert any(value == pytest.approx(expected) for value in numbers)

        assert _formula_count(path) == 0

    def test_sheets_exist_without_bills(self, project, tmp_path: Path):
        assert project.bills == []
        result = calculation_engine.calculate(project)
        path = ExcelExporter().export(project, result, tmp_path / "nobills.xlsx")
        wb = load_workbook(path)
        assert wb.sheetnames == SHEET_NAMES
        for name in ("账单原始数据", "账单校验"):
            text = _sheet_text(wb[name])
            assert "本项目尚未录入电费账单" in text
            assert "录入方法" in text
            assert "导入向导" in text
        assert _formula_count(path) == 0

    def test_no_formula_in_bill_sheets_with_bills(self, project, service, tmp_path: Path):
        _create(service)
        result = calculation_engine.calculate(project)
        path = ExcelExporter().export(project, result, tmp_path / "f.xlsx")
        for name in ("账单原始数据", "账单校验"):
            wb = load_workbook(path)
            offenders = [
                f"{ws.title}!{cell.coordinate}"
                for ws in [wb[name]]
                for row in ws.iter_rows()
                for cell in row
                if isinstance(cell.value, str) and cell.value.startswith("=")
            ]
            assert offenders == []


# --------------------------------------------------------------------------- #
# 7. PDF 账单章节（有 / 无账单）
# --------------------------------------------------------------------------- #
class TestPdfBillSection:
    def test_eighteen_sections(self):
        """V2.1 §8.1 插入账单章节（17 部分）；V2.2 §6.3 再插入负荷估算与消纳章节（18 部分）。"""
        assert len(REPORT_SECTIONS) == 18
        assert REPORT_SECTIONS[2] == "账单事实与校验"
        assert REPORT_SECTIONS[4] == "负荷估算与光伏消纳"

    def test_section_without_bills_explains_how_to_enter(self, project):
        assert project.bills == []
        text = _story_text(project)
        assert BILL_EMPTY_TEXT in text
        assert "录入方法" in text
        assert "三、账单事实与校验" in text
        for section in REPORT_SECTIONS[1:]:
            assert section in text, f"报告缺少章节：{section}"

    def test_section_with_bills_shows_facts_and_checks(self, project, service):
        _create(service, energy_total_kwh=None, bill_total_yuan=None)
        text = _story_text(project)

        assert "三、账单事实与校验" in text
        assert BILL_NOT_PROVIDED_TEXT in text
        assert "月度趋势" in text
        assert "账单事实明细" in text
        assert "ΔE" in text and "ΔC" in text
        assert "assumptions" in text or "口径假设" in text
        assert "边际节省电价" in text
        assert "待确认 / 未建模" in text  # 事实与模拟结果分界（§1、§8.1）

    def test_pdf_file_generated_both_cases(self, project, service, tmp_path: Path):
        empty_result = calculation_engine.calculate(project)
        empty_pdf = PdfExporter().export(project, empty_result, tmp_path / "无账单")
        assert empty_pdf.name == "无账单.pdf"
        assert empty_pdf.stat().st_size > 5000

        _create(service)
        result = calculation_engine.calculate(project)
        pdf = PdfExporter().export(project, result, tmp_path / "有账单")
        assert pdf.exists() and pdf.stat().st_size > 5000
        raw = pdf.read_bytes()
        assert raw.startswith(b"%PDF") and b"%%EOF" in raw[-2048:]

    def test_section_order_keeps_bill_chapter_third(self, project):
        text = _story_text(project)
        prefixes = ("一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、",
                    "九、", "十、", "十一、", "十二、", "十三、", "十四、", "十五、",
                    "十六、", "十七、")
        positions = [text.find(prefix) for prefix in prefixes]
        assert all(index >= 0 for index in positions)
        assert positions == sorted(positions)


# --------------------------------------------------------------------------- #
# 8. §105 一致性：页面显示 = 服务返回
# --------------------------------------------------------------------------- #
class TestPageMatchesService:
    def test_list_rows_match_service(self, page, service):
        _create(service, billing_period_start=date(2026, 2, 1), billing_period_end=date(2026, 2, 28))
        _create(service)
        page.refresh()
        expected = service.list_bills(sort_by="period_start", descending=False)
        rows = page.list_rows()
        assert [row[0] for row in rows] == [bill.bill_id for bill in expected]
        for row, bill in zip(rows, expected):
            assert row[1] == bill.billing_month
            assert row[4] == bill_kwh(bill.energy_total_kwh)
            assert row[5] == bill_yuan(bill.bill_total_yuan)
            assert row[6] == bill.source_type.label
            assert row[7] == bill.quality_status.label

    def test_monthly_rows_match_service(self, page, service):
        _create(service)
        _create(service, billing_period_start=date(2026, 2, 1), billing_period_end=date(2026, 2, 28),
                bill_total_yuan=None)
        page.refresh()
        expected = service.monthly_summary()
        rows = page.monthly_rows()
        assert len(rows) == len(expected)
        for row, item in zip(rows, expected):
            assert row[0] == item.billing_month
            assert row[1] == str(item.bill_count)
            assert row[2] == bill_kwh(item.energy_total_kwh)
            assert row[3] == bill_yuan(item.amount_total_yuan)
            assert row[4] == bill_price(item.average_price_yuan_per_kwh)

    def test_annual_values_match_service(self, page, service):
        _create(service)
        page.refresh()
        summary = service.annual_summary()
        values = page.annual_values()
        assert values["统计年度"] == str(summary.year)
        assert values["年度总购电量"] == bill_kwh(summary.total_energy_kwh)
        assert values["年度账单总额"] == bill_yuan(summary.total_amount_yuan)
        assert values["平均综合电价"] == bill_price(summary.average_price_yuan_per_kwh)
        assert values["月份覆盖率"] == f"{summary.coverage_ratio:.2%}"
        assert values["是否可直接相加"].startswith("是" if summary.can_sum_directly else "否")

    def test_validation_rows_match_service_issues(self, page, service):
        _create(service)
        service.add_bill(_raw_bill(), validate=False)
        page.refresh()
        expected = sum(len(item.issues) or 1 for item in service.reconcile_all())
        assert len(page.validation_rows()) == expected
        assert service.reconcile_all()[0].bill_id in " ".join(page.validation_rows()[0])

    def test_form_values_match_saved_bill(self, page, service):
        bill = _create(service)
        page.load_bill_into_form(bill.bill_id)
        payload = page.form_values()
        assert payload["energy_peak_kwh"] == pytest.approx(bill.energy_peak_kwh)
        assert payload["energy_charge_yuan"] == pytest.approx(bill.energy_charge_yuan)
        assert service.get_bill(bill.bill_id).model_dump()["bill_total_yuan"] == pytest.approx(
            payload["bill_total_yuan"]
        )


# --------------------------------------------------------------------------- #
# 9. 主窗口装配与保存 / 重开（阶段 2 验收：用户无需改代码即可导入并查看账单）
# --------------------------------------------------------------------------- #
class TestMainWindowBillFlow:
    def test_bill_created_in_page_survives_save_and_reopen(self, qapp, tmp_path: Path):
        from cenep.ui.main_window import MainWindow

        window = MainWindow(db_path=tmp_path / "bills_gui.db")
        try:
            assert window.tabs.count() == 10
            assert window.bills_page.service is not None
            window.project_page.name_edit.setText("账单界面保存测试")
            page = window.bills_page
            page.new_bill()
            form = page.forms["账单基本信息"]
            form.rows["billing_period_start"].set_value(date(2026, 6, 1))
            form.rows["billing_period_end"].set_value(date(2026, 6, 30))
            page.forms["电量（kWh）"].rows["energy_total_kwh"].set_value(88_000.0)
            page.forms["费用（元）"].rows["bill_total_yuan"].set_value(50_000.0)
            bill = page.save_form()
            assert bill is not None

            project, errors = window.collect_project()
            assert errors == []
            saved = window.project_service.save_project(project, tmp_path / "bills.nep")
            assert saved.exists()
        finally:
            window.close()

        # 保存后重开：账单必须完整恢复（§8.2）
        reopened = load_project(saved)
        assert len(reopened.bills) == 1
        assert reopened.bills[0].energy_total_kwh == pytest.approx(88_000.0)

        window2 = MainWindow(db_path=tmp_path / "bills_gui2.db")
        try:
            window2.project = reopened
            window2._reload_all()
            assert window2.bills_page.list_table.rowCount() == 1
            assert window2.bills_page.list_rows()[0][4] == "88,000.00 kWh"
        finally:
            window2.close()

    def test_template_download_writes_xlsx(self, page: BillsPage, tmp_path: Path):
        target = page.download_template(tmp_path / "CENEP_电费账单导入模板")
        assert target is not None
        assert target.name == "CENEP_电费账单导入模板.xlsx"
        assert target.exists() and target.stat().st_size > 1000
        assert "已下载导入模板" in page.message_label.text()


# --------------------------------------------------------------------------- #
# 10. GUI 真实启动与"点击"交互（§8.4；规格书红线：不得虚报通过）
#
# 本类不使用任何模态对话框：第 1 步用 set_file 代替文件选择框，
# 删除 / 清空确认框由 confirm=False 绕过（对话框路径另有用例覆盖）。
# --------------------------------------------------------------------------- #
class TestGuiStartupAndInteraction:
    def test_window_shows_and_all_tabs_switch(self, qapp, tmp_path: Path):
        from cenep.ui.main_window import MainWindow

        window = MainWindow(db_path=tmp_path / "startup.db")
        try:
            window.show()
            assert window.isVisible()
            titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
            assert titles == [
                "项目", "参数", "计算", "结果", "时序仿真", "月度账单", "负荷与消纳",
                "敏感性", "报告", "设置",
            ]
            for index in range(window.tabs.count()):
                window.tabs.setCurrentIndex(index)
                QApplication.processEvents()
        finally:
            window.close()

    def test_manual_entry_and_six_step_import_via_button_clicks(self, qapp, tmp_path: Path):
        """按钮点击驱动的端到端交互：新增 → 保存 → 六步导入 → 保存 / 重开。"""
        from cenep.ui.main_window import MainWindow

        window = MainWindow(db_path=tmp_path / "clicks.db")
        try:
            window.show()
            page = window.bills_page
            assert len(page.list_rows()) == 0

            page.new_button.click()
            QApplication.processEvents()
            form = page.forms["账单基本信息"]
            form.rows["billing_period_start"].set_value(date(2026, 1, 1))
            form.rows["billing_period_end"].set_value(date(2026, 1, 31))
            form.rows["meter_id"].set_value("M-CLICK")
            page.forms["电量（kWh）"].rows["energy_total_kwh"].set_value(123_456.0)
            page.forms["电量（kWh）"].rows["energy_peak_kwh"].set_value(23_456.0)
            page.forms["电量（kWh）"].rows["energy_valley_kwh"].set_value(100_000.0)
            page.forms["费用（元）"].rows["energy_charge_yuan"].set_value(80_000.0)
            page.forms["费用（元）"].rows["bill_total_yuan"].set_value(80_000.0)
            page.save_button.click()
            QApplication.processEvents()

            assert len(page.list_rows()) == 1
            assert page.list_rows()[0][4] == "123,456.00 kWh"
            summary = page.service.monthly_summary()[0]
            assert page.monthly_rows()[0][4] == bill_price(summary.average_price_yuan_per_kwh)
            assert page.validation_levels() == {"INFO": 1}

            # 六步导入：每一步都用按钮点击推进
            book = _bill_workbook(
                tmp_path / "click_bills.xlsx",
                [HEADERS, _good_row(billing_period_start="2026-02-01",
                                    billing_period_end="2026-02-28",
                                    meter_id="M-CLICK-2", energy_total_kwh=90_000,
                                    energy_flat_kwh=90_000, energy_charge_yuan=60_000,
                                    bill_total_yuan=60_000)],
            )
            wizard = page.wizard
            assert wizard.set_file(book) is True
            wizard.sheet_button.click()
            assert wizard.step == 3
            assert wizard.mapping_from_table().get("energy_total_kwh") == TEMPLATE_HEADERS["energy_total_kwh"]
            wizard.mapping_button.click()
            assert wizard.step == 4
            wizard.preview_button.click()
            assert wizard.step == 5
            wizard.confirm_button.click()
            QApplication.processEvents()
            assert wizard.step == 6
            assert len(page.list_rows()) == 2
            assert page.service is not None and len(page.service) == 2

            template = page.download_template(tmp_path / "CENEP_电费账单导入模板")
            assert template is not None and template.exists()

            project, errors = window.collect_project()
            assert errors == []
            saved = window.project_service.save_project(project, tmp_path / "clicks.nep")
        finally:
            window.close()

        reopened = load_project(saved)
        assert len(reopened.bills) == 2
