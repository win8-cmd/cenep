"""PDF 账单解析测试（V2.5 新增）。

覆盖三类电网原生版式（表格版 / 电量明细版 / 账单概况版）的字段抽取、口径归一化与
三条勾稽关系，以及"PDF 走 Excel 导入链路"时 ``preview_bill_import`` 的分支行为。

真实账单 PDF 属于业务数据，不入库。这里用 PyMuPDF 现场合成**文字布局等价**的 PDF：
字面量（标签写法、坐标列位）严格对齐 ``bill_pdf_importer`` 的正则与坐标阈值，
因此这些用例同时是"解析契约"的可执行文档——若有人改动坐标阈值或正则，这里会先红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="未安装 PyMuPDF，跳过 PDF 账单解析测试")

from cenep.calculation.errors import ValidationError
from cenep.data.bill_importer import (
    PDF_SHEET_NAME,
    is_pdf_bill,
    list_bill_headers,
    list_bill_sheets,
    preview_bill_import,
)
from cenep.data.bill_pdf_importer import parse_bill_pdf, pdf_row_dict


# --------------------------------------------------------------------------- #
# PDF 合成工具：按坐标摆放文字，模拟电网账单的多列版式
# --------------------------------------------------------------------------- #
def _make_pdf(path: Path, lines: list[tuple[float, float, str]]) -> Path:
    """把 ``(x, y, text)`` 列表写成一页 PDF。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontname="china-s", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def _period_tokens(start: str, end: str, y: float = 96.0) -> list[tuple[float, float, str]]:
    """账期写成**独立的日期 token**（对齐真实版式与坐标回退逻辑）。

    ``_parse_header_pos`` 要求两个 ``\\d{4}-\\d{2}-\\d{2}`` 独立词且 ``x < 100``
    才能推出账期起/止——所以不能让日期和中文标签挤在同一行文本里。
    """
    return [
        (60, y, "账单周期"),
        (60, y + 12, start),
        (60, y + 24, "至"),
        (60, y + 36, end),
    ]


def _detail_lines() -> list[tuple[float, float, str]]:
    """「电量明细版」账单：分时块 + 五项费用 + 基本电费 + 力调。

    金额自洽：电度 5800 = 3000+2000+300+200+300
              总额 7250 = 5800 + 1500(基本) + (-50)(力调)
    """
    return [
        (60, 60, "户号：4210001234567"),
        (250, 60, "户名：东风本田汽车有限公司第三工厂"),
        (60, 78, "电压等级：交流110kV"),
        *_period_tokens("2024-10-01", "2024-10-31", y=96.0),
        (60, 150, "本期电量 100000 kW·h"),
        # 分时块：4 个时段标签在 x=60，电量在 x=352~380
        (60, 170, "正向有功（尖峰）"),
        (360, 170, "10000"),
        (60, 190, "正向有功（峰）"),
        (360, 190, "20000"),
        (60, 210, "正向有功（平）"),
        (360, 210, "40000"),
        (60, 230, "正向有功（谷）"),
        (360, 230, "30000"),
        (360, 250, "100000"),
        # 费用明细（对齐 _parse_charges 的正则写法）
        (60, 300, "(1)市场化购电电费 3000.00"),
        (60, 318, "(3)输配电费 2000.00"),
        (60, 336, "(2)上网环节线损费用 300.00"),
        (60, 354, "(4)系统运行费 200.00"),
        (60, 372, "(5)政府性基金及附加 300.00"),
        # 基本电费：对齐两行式正则
        #   基本电费元 <金额> \n 基本电费 <容量> <金额>
        # 注意：正则的 group(1) 一律映射到 basic_demand（按需量），
        # 因此这里必须写成"按需量"口径，勾稽才自洽（详见 TestBasicFeeSplit）。
        (60, 400, "基本电费元 1500"),
        (60, 412, "基本电费 500 1500.00"),
        (60, 430, "功率因数调整电费元 -50.00"),
        (60, 448, "本期电费 7250.00元"),
        (60, 466, "其中增值税专用发票金额：833.63元"),
    ]


@pytest.fixture()
def detail_pdf(tmp_path: Path) -> Path:
    return _make_pdf(tmp_path / "电量明细版.pdf", _detail_lines())


# --------------------------------------------------------------------------- #
# 类型判定 / 工作表语义
# --------------------------------------------------------------------------- #
class TestPdfDispatch:
    def test_is_pdf_bill(self, tmp_path):
        assert is_pdf_bill(tmp_path / "a.pdf") is True
        assert is_pdf_bill(tmp_path / "a.PDF") is True
        assert is_pdf_bill(tmp_path / "a.xlsx") is False
        assert is_pdf_bill(tmp_path / "a.csv") is False

    def test_list_bill_sheets_for_pdf(self, detail_pdf: Path):
        assert list_bill_sheets(detail_pdf) == [PDF_SHEET_NAME]

    def test_list_bill_sheets_missing_file(self, tmp_path):
        with pytest.raises(ValidationError, match="文件不存在"):
            list_bill_sheets(tmp_path / "不存在.pdf")

    def test_list_bill_headers_returns_field_names(self, detail_pdf: Path):
        headers = list_bill_headers(detail_pdf)
        assert "energy_sharp_kwh" in headers
        assert "bill_total_yuan" in headers
        # 中文表头不应出现（否则预览里会报"未识别的列"）
        assert "尖峰电量(kWh)" not in headers
        assert "_pdf_notes" not in headers


# --------------------------------------------------------------------------- #
# 字段抽取
# --------------------------------------------------------------------------- #
class TestParseFields:
    def test_tariff_energy_blocks(self, detail_pdf: Path):
        row, _ = parse_bill_pdf(detail_pdf)
        assert row["energy_sharp_kwh"] == pytest.approx(10000.0)
        assert row["energy_peak_kwh"] == pytest.approx(20000.0)
        assert row["energy_flat_kwh"] == pytest.approx(40000.0)
        assert row["energy_valley_kwh"] == pytest.approx(30000.0)
        assert row["energy_total_kwh"] == pytest.approx(100000.0)

    def test_charge_items(self, detail_pdf: Path):
        row, _ = parse_bill_pdf(detail_pdf)
        assert row["market_purchase_charge_yuan"] == pytest.approx(3000.0)
        assert row["transmission_distribution_charge_yuan"] == pytest.approx(2000.0)
        assert row["line_loss_charge_yuan"] == pytest.approx(300.0)
        assert row["system_operation_charge_yuan"] == pytest.approx(200.0)
        assert row["government_fund_charge_yuan"] == pytest.approx(300.0)

    def test_energy_total_charge_is_sum_of_parts(self, detail_pdf: Path):
        row, _ = parse_bill_pdf(detail_pdf)
        assert row["energy_charge_yuan"] == pytest.approx(5800.0)

    def test_basic_and_power_factor(self, detail_pdf: Path):
        row, _ = parse_bill_pdf(detail_pdf)
        # 解析器把"基本电费元 <金额>"统一归入 basic_demand（基本电费-按需量），
        # 第二段的 <容量> 归入 billing_demand_kw。
        assert row["demand_charge_yuan"] == pytest.approx(1500.0)
        assert row["billing_demand_kw"] == pytest.approx(500.0)
        assert row["power_factor_adjustment_yuan"] == pytest.approx(-50.0)

    def test_bill_total_untouched_by_vat_invoice(self, detail_pdf: Path):
        """「其中增值税专用发票金额」是**价内税**，绝不落到 vat_yuan。"""
        row, _ = parse_bill_pdf(detail_pdf)
        assert row["bill_total_yuan"] == pytest.approx(7250.0)
        # 解析器不产出 vat 字段（_FIELD_MAP 无 vat、_SKIP_KEYS 含 _vat_invoice），
        # 即"发票金额"不会以任何形式进入模板口径。
        assert not row.get("vat_yuan")

    def test_header_fields(self, detail_pdf: Path):
        row, _ = parse_bill_pdf(detail_pdf)
        assert row["meter_id"] == "4210001234567"
        assert "东风本田" in row["customer_name"]
        assert row["voltage_level"] == "110kV"  # "交流" 前缀已剥离
        assert row["billing_period_end"] == "2024-10-31"

    def test_missing_fields_stay_none(self, detail_pdf: Path):
        """未出现在账单里的字段应为 None（未提供），而不是 0。"""
        row, _ = parse_bill_pdf(detail_pdf)
        assert row["basic_capacity_charge_yuan"] is None
        assert row["other_charge_yuan"] is None
        assert row["adjustment_charge_yuan"] is None


# --------------------------------------------------------------------------- #
# 勾稽关系
# --------------------------------------------------------------------------- #
class TestReconciliations:
    def test_all_three_identities_hold(self, detail_pdf: Path):
        row, notes = parse_bill_pdf(detail_pdf)
        # ① 分时合计 = 总电量
        parts = sum(
            row[k] or 0.0
            for k in (
                "energy_sharp_kwh",
                "energy_peak_kwh",
                "energy_flat_kwh",
                "energy_valley_kwh",
            )
        )
        assert parts == pytest.approx(row["energy_total_kwh"], abs=0.01)
        # ② 电度合计 = 五项明细
        detail = sum(
            row[k] or 0.0
            for k in (
                "market_purchase_charge_yuan",
                "transmission_distribution_charge_yuan",
                "line_loss_charge_yuan",
                "system_operation_charge_yuan",
                "government_fund_charge_yuan",
            )
        )
        assert detail == pytest.approx(row["energy_charge_yuan"], abs=0.01)
        # ③ 总额 = 电度 + 基本 + 力调
        assert row["bill_total_yuan"] == pytest.approx(
            row["energy_charge_yuan"]
            + (row["basic_capacity_charge_yuan"] or 0.0)
            + (row["demand_charge_yuan"] or 0.0)
            + (row["power_factor_adjustment_yuan"] or 0.0),
            abs=2.0,
        )
        assert not [n for n in notes if "不一致" in n], notes

    def test_inconsistent_total_is_reported(self, tmp_path: Path):
        lines = [
            (60, 96, "账单周期"),
            (60, 108, "2024-10-01"),
            (60, 120, "至"),
            (60, 132, "2024-10-31"),
            (60, 170, "正向有功（平）"),
            (360, 170, "100000"),
            (360, 190, "100000"),
            (60, 300, "(1)市场化购电电费 100.00"),
            (60, 400, "基本电费元 0"),
            (60, 412, "基本电费 0 0.00"),
            (60, 430, "功率因数调整电费元 0.00"),
            (60, 448, "本期电费 999.00元"),
        ]
        path = _make_pdf(tmp_path / "不一致.pdf", lines)
        _, notes = parse_bill_pdf(path)
        assert any("不一致" in n for n in notes), notes

    def test_negative_power_factor_is_allowed(self, detail_pdf: Path):
        row, notes = parse_bill_pdf(detail_pdf)
        assert row["power_factor_adjustment_yuan"] < 0
        # 力调为负是正常业务（奖励），不该被判成"负值异常"
        assert not [n for n in notes if "负值" in n and "力调" in n]


# --------------------------------------------------------------------------- #
# 口径归一化：平段单一电价
# --------------------------------------------------------------------------- #
class TestFlatOnlyNormalization:
    def test_summary_ratio_yields_to_energy_detail(self, tmp_path: Path):
        """V2.5 §5：账单概况的"峰谷比例"与第 2/3 页电量明细冲突时，**以明细为准**。

        2025 年起国网概况页写作"平段单一电价"（尖峰0% / 峰0% / 平100% / 谷0%），
        但电量明细里仍有真实的尖峰 / 峰 / 谷 / 平分时电量。修复前解析器只读概况页
        比例，会把尖峰 / 峰 / 谷全部清零、把全部电量塞进平段（V2.5 修复点 1、5）。
        """
        lines = [
            (60, 96, "账单周期"),
            (60, 108, "2025-03-01"),
            (60, 120, "至"),
            (60, 132, "2025-03-31"),
            (60, 114, "本期电量 100000 kW·h"),
            # 峰谷比例写法须严格对齐 _parse_header_text 的正则：
            #   尖峰0%、峰0%、平100%、谷0%
            (60, 132, "尖峰0%、峰0%、平100%、谷0%"),
            (60, 170, "正向有功（尖峰）"),
            (360, 170, "10000"),
            (60, 190, "正向有功（峰）"),
            (360, 190, "20000"),
            (60, 210, "正向有功（平）"),
            (360, 210, "40000"),
            (60, 230, "正向有功（谷）"),
            (360, 230, "30000"),
            (360, 250, "100000"),
            (60, 300, "(1)市场化购电电费 3000.00"),
            (60, 318, "(3)输配电费 2000.00"),
            (60, 336, "(2)上网环节线损费用 300.00"),
            (60, 354, "(4)系统运行费 200.00"),
            (60, 372, "(5)政府性基金及附加 300.00"),
            (60, 400, "基本电费元 800"),
            (60, 412, "基本电费 400 800.00"),
            (60, 430, "功率因数调整电费元 0.00"),
            (60, 448, "本期电费 6600.00元"),
        ]
        path = _make_pdf(tmp_path / "平段单一电价.pdf", lines)
        row, notes = parse_bill_pdf(path)
        # 明细优先：四个时段都取电量明细的值，不再被概况页比例清零
        assert row["energy_sharp_kwh"] == pytest.approx(10000.0)
        assert row["energy_peak_kwh"] == pytest.approx(20000.0)
        assert row["energy_flat_kwh"] == pytest.approx(40000.0)
        assert row["energy_valley_kwh"] == pytest.approx(30000.0)
        assert row["energy_total_kwh"] == pytest.approx(100000.0)
        # 冲突必须被说明（取数来源 + 明细数值），不允许静默改判
        joined = " ".join(notes)
        assert "电量明细" in joined
        assert "峰谷比例" in joined

    def test_flat_only_without_detail_still_concentrates_energy(self, tmp_path: Path):
        """若电量明细里**确实没有**分时电量，才按概况页口径把电量全部计入平段。"""
        lines = [
            (60, 96, "账单周期"),
            (60, 108, "2025-03-01"),
            (60, 120, "至"),
            (60, 132, "2025-03-31"),
            (60, 114, "本期电量 100000 kW·h"),
            (60, 132, "尖峰0%、峰0%、平100%、谷0%"),
            (60, 300, "(1)市场化购电电费 3000.00"),
            (60, 400, "基本电费元 800"),
            (60, 412, "基本电费 400 800.00"),
            (60, 430, "功率因数调整电费元 0.00"),
            (60, 448, "本期电费 6600.00元"),
        ]
        path = _make_pdf(tmp_path / "无明细.pdf", lines)
        row, notes = parse_bill_pdf(path)
        assert row["energy_sharp_kwh"] == 0.0
        assert row["energy_peak_kwh"] == 0.0
        assert row["energy_valley_kwh"] == 0.0
        assert row["energy_flat_kwh"] == pytest.approx(100000.0)
        assert any("平段单一电价" in n for n in notes), notes


# --------------------------------------------------------------------------- #
# 口径归一化：基本电费从输配电费里剥离
# --------------------------------------------------------------------------- #
class TestBasicFeeSplit:
    def test_demand_fee_split_out_of_transmission(self, tmp_path: Path):
        """账单的「输配电费」若含需量电费，应拆出 基本电费-按需量。

        自洽账：电度 1800 = 市场化 1000 + 输配(电量部分) 800
                基本-按需量 1200
                总额 3000 = 1800 + 1200
        """
        lines = [
            (60, 96, "账单周期"),
            (60, 108, "2024-11-01"),
            (60, 120, "至"),
            (60, 132, "2024-11-30"),
            (60, 114, "本期电量 100000 kW·h"),
            (60, 170, "正向有功（平）"),
            (360, 170, "100000"),
            (360, 190, "100000"),
            (60, 300, "(1)市场化购电电费 1000.00"),
            # 输配电费 2000 = 输配电量电费 800 + 需量电费 1200
            (60, 318, "(3)输配电费 2000.00"),
            (60, 336, "电量电费 800.00"),
            # 需量块（对齐正则）：基本电费元 <需量金额> \n 需量基本费 <需量kW> <金额>
            (60, 360, "基本电费元 1200"),
            (60, 372, "需量基本费 500 1200.00"),
            (60, 430, "本期电费 3000.00元"),
        ]
        path = _make_pdf(tmp_path / "需量拆分.pdf", lines)
        row, notes = parse_bill_pdf(path)
        # 拆分后：电度合计 = 1000 + 800 = 1800；基本-按需量 = 1200
        assert row["energy_charge_yuan"] == pytest.approx(1800.0)
        assert row["transmission_distribution_charge_yuan"] == pytest.approx(800.0)
        assert row["demand_charge_yuan"] == pytest.approx(1200.0)
        assert row["billing_demand_kw"] == pytest.approx(500.0)
        assert any("需量电费" in n for n in notes), notes
        assert not [n for n in notes if "不一致" in n], notes
        assert row["energy_charge_yuan"] + row["demand_charge_yuan"] == pytest.approx(
            row["bill_total_yuan"], abs=2.0
        )


# --------------------------------------------------------------------------- #
# 与 bill_importer 的衔接
# --------------------------------------------------------------------------- #
class TestPdfRowDict:
    def test_row_dict_keys_are_template_fields(self, detail_pdf: Path):
        row = pdf_row_dict(detail_pdf)
        assert row["energy_sharp_kwh"] == pytest.approx(10000.0)
        assert row["bill_total_yuan"] == pytest.approx(7250.0)
        assert "_pdf_notes" in row
        # 中文表头不进 keys，避免"未识别的列"噪音
        assert "尖峰电量(kWh)" not in row

    def test_row_dict_marker_is_chinese_header(self, detail_pdf: Path):
        row = pdf_row_dict(detail_pdf)
        # 「数据标记」是 BILL_COLUMNS 里首个无 field 的列（模板的示例行标记列），
        # 用它的中文表头输出，才能被 resolve_bill_columns 命中而非落进"未识别列"。
        # V2.5：PDF 账单的来源就是「PDF 账单导入」，不再谎报 excel。
        assert row.get("数据标记") == "pdf"


class TestPreviewBillImportWithPdf:
    def test_preview_accepts_pdf(self, detail_pdf: Path):
        preview = preview_bill_import(detail_pdf)
        assert preview.sheet_name == PDF_SHEET_NAME
        assert len(preview.rows) == 1
        assert not preview.unmapped_headers

    def test_preview_accepts_explicit_pdf_sheet_name(self, detail_pdf: Path):
        """向导第 2 步对 PDF 也会回传 sheet="PDF 账单"，服务层必须放行。"""
        preview = preview_bill_import(detail_pdf, sheet=PDF_SHEET_NAME)
        assert preview.total_rows == 1
        assert preview.sheet_name == PDF_SHEET_NAME

    def test_preview_available_sheets_is_pdf_pseudo_sheet(self, detail_pdf: Path):
        preview = preview_bill_import(detail_pdf)
        assert preview.available_sheets == [PDF_SHEET_NAME]

    def test_preview_pdf_message_mentions_pdf(self, detail_pdf: Path):
        preview = preview_bill_import(detail_pdf)
        joined = " ".join(str(m) for m in preview.messages)
        assert "PDF" in joined

    def test_preview_corrupt_pdf_raises_chinese_error(self, tmp_path: Path):
        path = tmp_path / "坏账单.pdf"
        path.write_bytes(b"%PDF-1.4\nnot really a pdf")
        with pytest.raises(ValidationError, match="无法打开 PDF 账单"):
            preview_bill_import(path)

    def test_preview_unknown_suffix_rejected(self, tmp_path: Path):
        path = tmp_path / "账单.docx"
        path.write_bytes(b"PK\x03\x04")
        with pytest.raises(ValidationError, match="不支持的文件类型"):
            preview_bill_import(path)


class TestPdfEndToEnd:
    """端到端：PDF → 预览 → CENEP 校验引擎（证明数据真的"能用"，不只是"能读"）。"""

    def test_parsed_pdf_passes_engine_validation(self, detail_pdf: Path):
        preview = preview_bill_import(detail_pdf)
        assert preview.total_rows == 1
        assert preview.invalid_count == 0, [
            r.messages for r in preview.rows if r.status == "invalid"
        ]
        row = preview.rows[0]
        assert row.bill is not None
        # 校验引擎应当判为「有效」（三条勾稽全过）
        assert row.status == "valid", row.messages

    def test_bill_totals_survive_the_engine(self, detail_pdf: Path):
        preview = preview_bill_import(detail_pdf)
        bill = preview.rows[0].bill
        assert bill.bill_total_yuan == pytest.approx(7250.0)
        # 分时电量在引擎侧保持自洽
        assert (
            bill.energy_sharp_kwh + bill.energy_peak_kwh + bill.energy_flat_kwh
            + bill.energy_valley_kwh
        ) == pytest.approx(bill.energy_total_kwh)

    def test_pdf_and_excel_paths_agree(self, detail_pdf: Path, tmp_path: Path):
        """同一份账单，走 PDF 与走 Excel 模板，落到引擎的账单对象应当等价。"""
        from openpyxl import Workbook

        from cenep.data.bill_importer import BILL_COLUMNS, SHEET_BILLS

        pdf_row = pdf_row_dict(detail_pdf)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = SHEET_BILLS
        sheet.append([c.header for c in BILL_COLUMNS])
        marker_header = next(c.header for c in BILL_COLUMNS if not c.field)
        # 标记列留空（不是"示例行"），否则该行会被当成示例行跳过
        values: dict[str, object] = {
            k: v for k, v in pdf_row.items() if not k.startswith("_")
        }
        sheet.append(
            [
                values.get(c.field if c.field else marker_header)
                for c in BILL_COLUMNS
            ]
        )
        xlsx = tmp_path / "同源.xlsx"
        workbook.save(xlsx)

        excel_preview = preview_bill_import(xlsx)
        excel_row = excel_preview.rows[0].bill
        pdf_bill = preview_bill_import(detail_pdf).rows[0].bill
        assert excel_row.energy_total_kwh == pytest.approx(pdf_bill.energy_total_kwh)
        assert excel_row.bill_total_yuan == pytest.approx(pdf_bill.bill_total_yuan)
        assert excel_row.energy_sharp_kwh == pytest.approx(pdf_bill.energy_sharp_kwh)
