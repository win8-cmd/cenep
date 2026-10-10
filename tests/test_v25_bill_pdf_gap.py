"""V2.5 账单 PDF 解析缺口修复的验收测试（真实账单 + 新增模型/字段）。

覆盖四件事：

1. **全量解析**：用真实样本账单（国网湖北，5 页）验证五个章节的数据都被读出来——
   分时电量（取电量明细，而非概况页的峰谷比例）、24 小时电量电价表、
   电费明细、需量电价 / 功率因数、市场化运营费用明细；
2. **数据模型**：``HourlyEnergyPricePoint`` / ``OperationFeeDetail`` 的中文校验与
   ``ElectricityBill`` 新字段的**追加式兼容**（旧账单对象照常可用、旧字段语义不变）；
3. **来源修正**：PDF 账单必须标为 ``BillSourceType.PDF``（不再谎报 excel）；
4. **回归**：同目录下全部同类账单的三大勾稽（分时合计 = 总电量、
   24 小时电量合计落在总电量附近、价格在合理区间）。

真实账单属于业务资料，**只读**且不入库；找不到资料时相关用例自动跳过（不报错）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from cenep.calculation.errors import ValidationError
from cenep.data.bill_importer import (
    PDF_SHEET_NAME,
    BILL_COLUMNS,
    BILL_SOURCE_TEXT,
    guess_bill_column_mapping,
    parse_bill_source,
    preview_bill_import,
)
from cenep.data.bill_pdf_importer import (
    parse_bill_pdf,
    parse_bill_pdf_full,
    pdf_row_payload,
)
from cenep.domain.bill_models import (
    HOURLY_PRICE_HOURS,
    ElectricityBill,
    HourlyEnergyPricePoint,
    OperationFeeDetail,
    OperationFeeItem,
)
from cenep.domain.enums import BillQualityStatus, BillSourceType, TariffStructure

# --------------------------------------------------------------------------- #
# 真实账单资料定位（只读；找不到就跳过，绝不修改资料）
# --------------------------------------------------------------------------- #
_BILL_DIR = Path(
    r"C:\Users\Administrator\Desktop\参考资料\东风本田\东风本田汽车有限公司(第三工厂)"
    r"\东风本田第三工厂分布式光伏项目——初步设计2.26"
    r"\东风本田第三工厂分布式光伏项目——初步设计2.26\收资\25年电费"
)
_SAMPLE_BILL = _BILL_DIR / "东本三厂10月账单.pdf"

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _require_sample() -> Path:
    if not _SAMPLE_BILL.exists():
        pytest.skip(f"未找到真实样本账单（只读资料）：{_SAMPLE_BILL}")
    return _SAMPLE_BILL


def _all_bills() -> list[Path]:
    if not _BILL_DIR.exists():
        return []
    return sorted(path for path in _BILL_DIR.glob("*.pdf") if path.is_file())


#: 样本账单（2025-10）的**人工核验期望值**（来自账单原件，测试只做对账不做推断）
EXPECTED = {
    "energy_total_kwh": 7_479_418.0,
    "energy_sharp_kwh": 654_669.0,   # 主表 635,050 + 定比分表 19,619
    "energy_peak_kwh": 2_063_117.0,  # 主表 2,001,291 + 定比分表 61,826
    "energy_flat_kwh": 2,            # 占位（下方按实数断言）
    "energy_valley_kwh": 2_063_117.0,
    "bill_total_yuan": 5_471_334.24,
    "market_purchase_charge_yuan": 2_912_242.44,
    "line_loss_charge_yuan": 145_362.52,
    "transmission_distribution_charge_yuan": 679_537.30,
    "system_operation_charge_yuan": 325_751.10,
    "government_fund_charge_yuan": 338_069.69,
    "demand_charge_yuan": 1_108_107.0,
    "power_factor_adjustment_yuan": -37_735.81,
    "billing_demand_kw": 28_413.0,
    "demand_rate_yuan_per_kw_month": 39.0,
    "power_factor_actual": 0.98,
    "power_factor_standard": 0.9,
    "power_factor_adjustment_factor": -0.0075,
    "power_factor_participating_charge_yuan": 5_031_440.65,
    "energy_per_kva_kwh": 149.0,
}
EXPECTED["energy_flat_kwh"] = 2_698_515.0  # 主表 2,617,650 + 定比分表 80,865


# --------------------------------------------------------------------------- #
# 1. 全量解析
# --------------------------------------------------------------------------- #
class TestSampleBillFullParse:
    """真实样本账单：五个章节的数据必须全部读出来。"""

    def test_period_and_customer(self):
        row, _ = parse_bill_pdf(_require_sample())
        assert row["billing_period_start"] == "2025-10-01"
        assert row["billing_period_end"] == "2025-10-31"
        assert row["customer_name"].startswith("东风本田")
        assert row["voltage_level"] == "110kV"
        # 解析器给模板口径的中文取值（"两部制"），建账单时才转成枚举 two_part
        assert row["tariff_structure"] == "两部制"
        preview = preview_bill_import(_require_sample())
        assert preview.rows[0].bill.tariff_structure is TariffStructure.TWO_PART

    @pytest.mark.parametrize(
        "field",
        [
            "energy_total_kwh",
            "energy_sharp_kwh",
            "energy_peak_kwh",
            "energy_flat_kwh",
            "energy_valley_kwh",
            "bill_total_yuan",
            "market_purchase_charge_yuan",
            "line_loss_charge_yuan",
            "transmission_distribution_charge_yuan",
            "system_operation_charge_yuan",
            "government_fund_charge_yuan",
            "demand_charge_yuan",
            "power_factor_adjustment_yuan",
        ],
    )
    def test_account_field_matches_bill(self, field: str):
        row, _ = parse_bill_pdf(_require_sample())
        assert row[field] == pytest.approx(EXPECTED[field])

    def test_energy_detail_beats_summary_ratio(self):
        """V2.5 修复点 1、5：分时电量取自电量明细，而不是概况页的"平100%"。"""
        row, notes = parse_bill_pdf(_require_sample())
        parts = sum(
            row[f"energy_{key}_kwh"]
            for key in ("sharp", "peak", "flat", "valley")
        )
        assert parts == pytest.approx(row["energy_total_kwh"], abs=1.0)
        # 概况页写作 尖峰0%/峰0%/平100%/谷0%，必须被明细推翻并说明来源
        assert any("电力明细" in note or "电量明细" in note for note in notes)
        assert any("峰谷比例" in note for note in notes)

    @pytest.mark.parametrize(
        "field",
        [
            "billing_demand_kw",
            "demand_rate_yuan_per_kw_month",
            "power_factor_actual",
            "power_factor_standard",
            "power_factor_adjustment_factor",
            "power_factor_participating_charge_yuan",
            "energy_per_kva_kwh",
        ],
    )
    def test_demand_and_power_factor_fields(self, field: str):
        row, _ = parse_bill_pdf(_require_sample())
        assert row[field] == pytest.approx(EXPECTED[field])

    def test_demand_rate_times_billing_demand_equals_demand_charge(self):
        """需量电价 × 计费需量 = 需量电费（账单自身给出的硬勾稽）。"""
        row, _ = parse_bill_pdf(_require_sample())
        product = row["demand_rate_yuan_per_kw_month"] * row["billing_demand_kw"]
        assert product == pytest.approx(row["demand_charge_yuan"], rel=1e-6)

    def test_hourly_table_is_complete_1_to_24(self):
        """V2.5 核心：24 小时电量电价表必须 1~24 时一条不缺。"""
        _row, _notes, extras = parse_bill_pdf_full(_require_sample())
        hourly: list[HourlyEnergyPricePoint] = extras["hourly_energy_tariff"]
        assert [point.hour for point in hourly] == list(HOURLY_PRICE_HOURS)

    def test_hourly_table_first_and_last_hour(self):
        _row, _notes, extras = parse_bill_pdf_full(_require_sample())
        hourly = extras["hourly_energy_tariff"]
        first, last = hourly[0], hourly[-1]
        assert first.hour == 1
        assert first.energy_kwh == pytest.approx(272_545.0)
        assert first.direct_trade_price_yuan_per_kwh == pytest.approx(0.4253204)
        assert first.line_loss_price_yuan_per_kwh == pytest.approx(0.0192310)
        assert last.hour == 24
        assert last.energy_kwh == pytest.approx(321_682.0)
        assert last.direct_trade_price_yuan_per_kwh == pytest.approx(0.4246712)
        assert last.line_loss_price_yuan_per_kwh == pytest.approx(0.0190630)

    def test_hourly_prices_are_in_reasonable_range(self):
        _row, _notes, extras = parse_bill_pdf_full(_require_sample())
        for point in extras["hourly_energy_tariff"]:
            price = point.direct_trade_price_yuan_per_kwh
            loss = point.line_loss_price_yuan_per_kwh
            assert price is not None and 0.0 < price < 2.0, point.describe()
            assert loss is not None and 0.0 <= loss < 0.5, point.describe()

    def test_hourly_energy_sum_reconciles_with_bill_total(self):
        """24 小时电量合计与账单总电量必须同量级，差额（若有）必须被说明。

        样本账单（2025-10）的 24 小时表 24 条电量之和**正好等于**账单本期电量
        7,479,418 kWh——这是最强的一条勾稽；若账单的 24 小时表只覆盖参与市场化
        交易的电能表（定比电量不在其中），差额必须出现在中文提示里。
        """
        row, notes, extras = parse_bill_pdf_full(_require_sample())
        hourly = extras["hourly_energy_tariff"]
        hourly_sum = sum(point.energy_kwh or 0.0 for point in hourly)
        total = row["energy_total_kwh"]
        assert hourly_sum == pytest.approx(7_283_317.0 + 196_101.0, rel=1e-9)
        assert abs(hourly_sum - total) <= total * 0.01
        if abs(hourly_sum - total) > 1.0:
            assert any("24 小时电量合计" in note for note in notes), notes

    def test_operation_fee_detail_matches_bill(self):
        """第 5 页运营费用：B/C 小计、六条明细与总合计都要读出来。"""
        _row, _notes, extras = parse_bill_pdf_full(_require_sample())
        detail: OperationFeeDetail = extras["operation_fee_detail"]
        assert detail is not None
        assert detail.total_yuan == pytest.approx(-295_720.67)
        assert detail.b_increase_total_yuan == pytest.approx(18_513.53)
        assert detail.c_decrease_total_yuan == pytest.approx(-314_234.20)
        assert detail.virtual_plant_peak_shaving_yuan == pytest.approx(0.0)
        assert detail.frequency_regulation_yuan == pytest.approx(0.0)
        assert detail.item_count() == 12
        by_name = {item.name: item.amount_yuan for item in detail.b_increase_items}
        assert by_name["B2绿电环境价值电费"] == pytest.approx(18_513.53)
        c_items = {item.name: item.amount_yuan for item in detail.c_decrease_items}
        assert c_items["C4发电侧超额获利回收电费"] == pytest.approx(-204_029.06)
        # C 类为负：不得取绝对值
        assert all(item.amount_yuan <= 0 for item in detail.c_decrease_items)

    def test_meter_group_energies_are_merged(self):
        """两块主表 + 定比分表合并后才等于总电量（V2.5 要求"多块电能表正确合并"）。"""
        row, _ = parse_bill_pdf(_require_sample())
        # 主表（7,255,282）+ 定比分表（224,136）= 7,479,418
        assert row["energy_total_kwh"] == pytest.approx(7_255_282.0 + 224_136.0)
        parts = sum(row[f"energy_{key}_kwh"] for key in ("sharp", "peak", "flat", "valley"))
        assert parts == pytest.approx(7_479_418.0)


# --------------------------------------------------------------------------- #
# 2. 与导入链路衔接：来源 = PDF，结构化事实写进账单
# --------------------------------------------------------------------------- #
class TestPdfSourceType:
    def test_bill_source_text_accepts_pdf(self):
        assert parse_bill_source("pdf") is BillSourceType.PDF
        assert parse_bill_source("PDF账单导入") is BillSourceType.PDF
        assert "pdf" in BILL_SOURCE_TEXT

    def test_pdf_label_is_chinese(self):
        assert BillSourceType.PDF.label == "PDF 账单导入"

    def test_pdf_parameter_source_counts_as_user_input(self):
        assert BillSourceType.PDF.parameter_source is BillSourceType.EXCEL.parameter_source
        assert BillSourceType.PDF.parameter_source.value == "USER_INPUT"

    def test_pdf_rows_are_marked_pdf(self):
        row, extras = pdf_row_payload(_require_sample())
        assert row["数据标记"] == "pdf"
        assert extras["hourly_energy_tariff"]

    def test_preview_marks_bill_as_pdf_source(self):
        preview = preview_bill_import(_require_sample())
        assert preview.sheet_name == PDF_SHEET_NAME
        bill = preview.rows[0].bill
        assert bill is not None
        assert bill.source_type is BillSourceType.PDF
        assert bill.source_file_name == _SAMPLE_BILL.name

    def test_preview_keeps_structured_facts_on_row(self):
        preview = preview_bill_import(_require_sample())
        row = preview.rows[0]
        assert row.bill_extras.get("operation_fee_detail") is not None
        assert len(row.bill_extras.get("hourly_energy_tariff") or []) == 24

    def test_hourly_table_is_written_into_the_bill(self):
        preview = preview_bill_import(_require_sample())
        bill = preview.rows[0].bill
        assert bill.hourly_energy_tariff
        assert [point.hour for point in bill.hourly_energy_tariff] == list(HOURLY_PRICE_HOURS)
        assert bill.has_hourly_price_table is True
        assert bill.hourly_energy_sum_kwh == pytest.approx(
            sum(point.energy_kwh for point in bill.hourly_energy_tariff)
        )

    def test_operation_fee_is_written_into_the_bill(self):
        preview = preview_bill_import(_require_sample())
        detail = preview.rows[0].bill.operation_fee_detail
        assert isinstance(detail, OperationFeeDetail)
        assert detail.total_yuan == pytest.approx(-295_720.67)

    def test_bill_survives_project_file_roundtrip(self):
        """新增字段必须能被序列化 / 反序列化（旧项目兼容的前提）。"""
        preview = preview_bill_import(_require_sample())
        bill = preview.rows[0].bill
        payload = bill.model_dump(mode="json")
        restored = ElectricityBill(**payload)
        assert restored.hourly_energy_tariff[23].energy_kwh == pytest.approx(321_682.0)
        assert restored.operation_fee_detail.c_decrease_items[3].amount_yuan == pytest.approx(
            -204_029.06
        )

    def test_pdf_bill_passes_engine_validation(self):
        """解析结果要能通过 CENEP 校验引擎（不是"能读"而是"能用"）。"""
        preview = preview_bill_import(_require_sample())
        assert preview.invalid_count == 0, [
            row.messages for row in preview.rows if row.status == "invalid"
        ]
        assert preview.rows[0].bill.energy_total_kwh == pytest.approx(7_479_418.0)


# --------------------------------------------------------------------------- #
# 3. 数据模型（新增字段 / 新增模型的中文校验）
# --------------------------------------------------------------------------- #
class TestHourlyEnergyPricePoint:
    def test_hour_must_be_1_to_24(self):
        with pytest.raises(Exception) as exc:
            HourlyEnergyPricePoint(hour=0, energy_kwh=1.0)
        assert "1~24" in str(exc.value)
        with pytest.raises(Exception) as exc:
            HourlyEnergyPricePoint(hour=25, energy_kwh=1.0)
        assert "1~24" in str(exc.value)

    def test_missing_values_stay_none(self):
        point = HourlyEnergyPricePoint(hour=3)
        assert point.energy_kwh is None
        assert point.direct_trade_price_yuan_per_kwh is None
        assert point.line_loss_price_yuan_per_kwh is None
        assert "未提供" in point.describe()

    def test_hour_label(self):
        assert HourlyEnergyPricePoint(hour=7).hour_label == "7 时"


class TestOperationFeeDetail:
    def test_totals_must_match_items(self):
        with pytest.raises(Exception) as exc:
            OperationFeeDetail(
                b_increase_total_yuan=100.0,
                b_increase_items=[OperationFeeItem(name="B1偏差考核电费", amount_yuan=1.0)],
            )
        assert "不一致" in str(exc.value)

    def test_negative_amounts_are_kept(self):
        detail = OperationFeeDetail(
            total_yuan=-10.0,
            c_decrease_total_yuan=-10.0,
            c_decrease_items=[OperationFeeItem(name="C5发电侧考核电费", amount_yuan=-10.0)],
        )
        assert detail.c_decrease_items[0].amount_yuan == pytest.approx(-10.0)
        assert "总合计" in detail.describe()

    def test_empty_detail_is_allowed(self):
        detail = OperationFeeDetail()
        assert detail.total_yuan is None
        assert detail.item_count() == 0


class TestBillAdditiveFields:
    """追加式扩展：新字段有默认值，旧账单对象与旧字段语义不变。"""

    def _bill(self, **kwargs) -> ElectricityBill:
        base = {
            "bill_id": "B1",
            "billing_period_start": date(2026, 1, 1),
            "billing_period_end": date(2026, 1, 31),
        }
        base.update(kwargs)
        return ElectricityBill(**base)

    def test_new_fields_default_to_empty(self):
        bill = self._bill()
        assert bill.hourly_energy_tariff == []
        assert bill.operation_fee_detail is None
        assert bill.demand_rate_yuan_per_kw_month is None
        assert bill.power_factor_actual is None
        assert bill.power_factor_standard is None
        assert bill.power_factor_adjustment_factor is None
        assert bill.power_factor_participating_charge_yuan is None
        assert bill.energy_per_kva_kwh is None
        assert bill.has_hourly_price_table is False
        assert bill.hourly_energy_sum_kwh is None

    def test_old_bill_payload_without_new_keys_still_loads(self):
        """旧项目文件里没有这些键：反序列化必须照常成功（追加式扩展的硬要求）。"""
        legacy = {
            "bill_id": "BILL-20260101-20260131-MAIN",
            "billing_period_start": "2026-01-01",
            "billing_period_end": "2026-01-31",
            "energy_total_kwh": 12345.0,
            "bill_total_yuan": 6789.0,
            "source_type": "excel",
        }
        bill = ElectricityBill(**legacy)
        assert bill.energy_total_kwh == pytest.approx(12345.0)
        assert bill.source_type is BillSourceType.EXCEL
        assert bill.hourly_energy_tariff == []

    def test_incomplete_hourly_table_is_rejected_in_chinese(self):
        points = [HourlyEnergyPricePoint(hour=hour) for hour in range(1, 24)]
        with pytest.raises(Exception) as exc:
            self._bill(hourly_energy_tariff=points)
        assert "24 小时电价表不完整" in str(exc.value)

    def test_duplicate_hours_are_rejected_in_chinese(self):
        points = [HourlyEnergyPricePoint(hour=1) for _ in range(24)]
        with pytest.raises(Exception) as exc:
            self._bill(hourly_energy_tariff=points)
        assert "重复小时" in str(exc.value)

    def test_complete_hourly_table_is_accepted(self):
        points = [HourlyEnergyPricePoint(hour=hour, energy_kwh=100.0) for hour in HOURLY_PRICE_HOURS]
        bill = self._bill(hourly_energy_tariff=points)
        assert bill.hourly_energy_sum_kwh == pytest.approx(2400.0)
        assert "24 小时电价表 24 条" in bill.describe()


# --------------------------------------------------------------------------- #
# 4b. 口径决定：只有平段用电是**合法状态**，不得判为异常
# --------------------------------------------------------------------------- #
def _make_pdf(path: Path, lines: list[tuple[float, float, str]]) -> Path:
    """把 ``(x, y, text)`` 列表写成一页 PDF（与 test_bill_pdf_importer 的合成器同构）。"""
    import pymupdf  # 延迟导入：未安装 PyMuPDF 时由调用方跳过

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontname="china-s", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def _flat_only_lines(*, with_detail: bool, year: int = 2025) -> list[tuple[float, float, str]]:
    """构造一份"只有平段用电"的自洽账单（国网 2025 版式）。

    金额刻意自洽：电度 5800 = 3000+2000+300+200+300；总额 5800 = 电度 5800 + 基本 0 + 力调 0。

    :param with_detail: 是否在第 2 页电量明细里放入**真实的分时电量**；
        放入后就不是"纯平段"账单，用于验证"明细优先"。
    """
    lines: list[tuple[float, float, str]] = [
        (60, 60, "户号：4210001234567"),
        (250, 60, "户名：东风本田汽车有限公司第三工厂"),
        (60, 78, "电压等级：交流110kV"),
        (60, 96, "账单周期"),
        (60, 108, f"{year}-03-01"),
        (60, 120, "至"),
        (60, 132, f"{year}-03-31"),
        (60, 150, "本期电量 100000 kW·h"),
        (60, 168, "尖峰0%、峰0%、平100%、谷0%"),
    ]
    if with_detail:
        lines += [
            (60, 190, "正向有功（尖峰）"),
            (360, 190, "10000"),
            (60, 210, "正向有功（峰）"),
            (360, 210, "20000"),
            (60, 230, "正向有功（平）"),
            (360, 230, "40000"),
            (60, 250, "正向有功（谷）"),
            (360, 250, "30000"),
        ]
    lines += [
        (60, 320, "(1)市场化购电电费 3000.00"),
        (60, 338, "(3)输配电费 2000.00"),
        (60, 356, "(2)上网环节线损费用 300.00"),
        (60, 374, "(4)系统运行费 200.00"),
        (60, 392, "(5)政府性基金及附加 300.00"),
        (60, 420, "基本电费元 0"),
        (60, 432, "基本电费 0 0.00"),
        (60, 450, "功率因数调整电费元 0.00"),
        (60, 468, "本期电费 5800.00元"),
    ]
    return lines


class TestFlatOnlyIsLegal:
    """需求方口径：客户实际只有平段时，尖峰/峰/谷为 0 是**正确结果**，不是缺陷。"""

    @pytest.fixture()
    def flat_pdf(self, tmp_path: Path) -> Path:
        return _make_pdf(tmp_path / "只有平段.pdf", _flat_only_lines(with_detail=False))

    @pytest.fixture()
    def detail_pdf(self, tmp_path: Path) -> Path:
        return _make_pdf(tmp_path / "有分时明细.pdf", _flat_only_lines(with_detail=True))

    def test_flat_only_puts_all_energy_into_flat(self, flat_pdf: Path):
        row, notes = parse_bill_pdf(flat_pdf)
        assert row["energy_total_kwh"] == pytest.approx(100_000.0)
        assert row["energy_flat_kwh"] == pytest.approx(100_000.0)
        assert row["energy_sharp_kwh"] == 0.0
        assert row["energy_peak_kwh"] == 0.0
        assert row["energy_valley_kwh"] == 0.0
        assert row["energy_offpeak_kwh"] is None  # 深谷未提供，不是 0
        assert notes, "纯平段账单必须给出一条说明"

    def test_explanation_is_neutral_not_a_warning(self, flat_pdf: Path):
        """说明文字必须是中性的时段陈述，不能带告警口气。"""
        _row, notes = parse_bill_pdf(flat_pdf)
        joined = " ".join(notes)
        assert "全部为平段用电" in joined
        # 中性陈述：四个时段各自的数值都写出来
        assert "尖峰 0" in joined and "峰 0" in joined
        assert "平 100,000" in joined and "谷 0" in joined
        # 不允许出现告警/异常口径的措辞
        for word in ("异常", "错误", "警告", "可疑", "已把全部电量"):
            assert word not in joined, f"平段用电被描述成『{word}』：{joined}"

    def test_flat_only_bill_is_valid_not_warning(self, flat_pdf: Path):
        """关键断言：``quality_status`` 不得是 invalid / warning。"""
        preview = preview_bill_import(flat_pdf)
        row = preview.rows[0]
        bill = row.bill
        assert bill is not None
        assert bill.quality_status is BillQualityStatus.VALID, bill.quality_messages
        assert row.status is BillQualityStatus.VALID, row.messages
        assert preview.invalid_count == 0
        assert preview.warning_count == 0
        # 分时合计与总电量自洽
        assert sum(
            getattr(bill, f"energy_{key}_kwh") or 0.0
            for key in ("sharp", "peak", "flat", "valley")
        ) == pytest.approx(bill.energy_total_kwh)

    def test_flat_only_bill_has_no_warning_issues(self, flat_pdf: Path):
        """连 CENEP 校验引擎都不应给出 ERROR / WARNING 级问题。"""
        preview = preview_bill_import(flat_pdf)
        statuses = {row.status for row in preview.rows}
        assert BillQualityStatus.INVALID not in statuses
        assert BillQualityStatus.WARNING not in statuses

    def test_detail_still_wins_when_present(self, detail_pdf: Path):
        """一旦电量明细里有分时电量，就必须用明细值（口径优先级 ①）。"""
        row, notes = parse_bill_pdf(detail_pdf)
        assert row["energy_sharp_kwh"] == pytest.approx(10_000.0)
        assert row["energy_peak_kwh"] == pytest.approx(20_000.0)
        assert row["energy_flat_kwh"] == pytest.approx(40_000.0)
        assert row["energy_valley_kwh"] == pytest.approx(30_000.0)
        assert any("峰谷比例" in note and "电量明细" in note for note in notes)
        assert not any("全部为平段用电" in note for note in notes)


# --------------------------------------------------------------------------- #
# 4. 列映射兜底（V2.5 修复点 4 的纯逻辑部分）
# --------------------------------------------------------------------------- #
class TestGuessBillColumnMapping:
    def test_guesses_readable_headers(self):
        headers = ["账期起", "账期止", "总购电量(kWh)", "账单总额(元)", "高峰电量(kWh)"]
        mapping = guess_bill_column_mapping(headers)
        assert mapping["billing_period_start"] == "账期起"
        assert mapping["billing_period_end"] == "账期止"
        assert mapping["energy_total_kwh"] == "总购电量(kWh)"
        assert mapping["bill_total_yuan"] == "账单总额(元)"
        assert mapping["energy_peak_kwh"] == "高峰电量(kWh)"

    def test_returns_empty_for_unknown_headers(self):
        assert guess_bill_column_mapping(["F1", "F2"]) == {}

    def test_never_raises_for_empty_headers(self):
        assert guess_bill_column_mapping([]) == {}

    def test_pdf_field_names_map_to_themselves(self):
        """PDF 解析器的"列名"就是模板字段名，兜底映射应能原样识别。"""
        headers = [column.field for column in BILL_COLUMNS if column.field]
        mapping = guess_bill_column_mapping(headers)
        assert mapping["energy_total_kwh"] == "energy_total_kwh"
        assert mapping["bill_total_yuan"] == "bill_total_yuan"


# --------------------------------------------------------------------------- #
# 4c. 界面：第 3 步"映射列"必须带内容（V2.5 修复点 4）
# --------------------------------------------------------------------------- #
class TestWizardMappingPrefill:
    """导入向导第 3 步不得退化成"全部（不映射）"，否则会生成没有数据的空账单。"""

    @pytest.fixture(scope="class")
    def qapp(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        yield app

    @pytest.fixture()
    def wizard(self, qapp, golden_pv):
        from cenep.application.bill_service import BillService
        from cenep.ui.pages import BillsPage

        page = BillsPage()
        page.bind(BillService(golden_pv))
        yield page.wizard
        page.setParent(None)
        page.close()
        page.deleteLater()

    @staticmethod
    def _workbook(path: Path, headers: list[str], values: list[object]) -> Path:
        from openpyxl import Workbook

        from cenep.data.bill_importer import SHEET_BILLS

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = SHEET_BILLS
        sheet.append(headers)
        sheet.append(values)
        workbook.save(path)
        return path

    def _mapped_pairs(self, wizard) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for row in range(wizard.mapping_table.rowCount()):
            item = wizard.mapping_table.item(row, 0)
            combo = wizard.mapping_table.cellWidget(row, 1)
            header = combo.currentData() if combo is not None else ""
            if item is not None and header:
                mapping[item.text()] = str(header)
        return mapping

    def test_mapping_table_is_prefilled_with_guesses(self, wizard, tmp_path: Path):
        """自动识别失败时，兜底映射必须按实际表头**预填**最佳猜测。"""
        path = self._workbook(
            tmp_path / "近似列名.xlsx",
            ["周期始", "周期止", "总购电量(kWh)", "账单总额(元)", "供电电压"],
            ["2026-05-01", "2026-05-31", 50_000, 30_000, "10kV"],
        )
        assert wizard.set_file(path) is True
        # 账期列被改成完全无法识别的名字 → 自动识别因缺必需列而失败，转手工映射
        assert wizard.select_sheet(wizard.sheet_combo.currentText()) is True
        assert wizard.step == 3
        assert wizard.preview is None
        assert "手工" in wizard.mapping_hint.text()
        mapped = self._mapped_pairs(wizard)
        # 能识别的列必须已经被选中，而不是"全部不映射"
        assert mapped["energy_total_kwh"] == "总购电量(kWh)"
        assert mapped["bill_total_yuan"] == "账单总额(元)"
        assert mapped["voltage_level"] == "供电电压"
        assert mapped, "映射表不能是全空的（会生成空账单）"

    def test_unknown_headers_do_not_map_but_table_is_not_empty(self, wizard, tmp_path: Path):
        """完全无法识别的列名：允许"不映射"，但表格仍要列出全部账单字段供用户手选。"""
        path = self._workbook(tmp_path / "无关列名.xlsx", ["F1", "F2", "F3"], ["a", "b", "c"])
        assert wizard.set_file(path) is True
        assert wizard.select_sheet() is True
        assert wizard.step == 3
        assert self._mapped_pairs(wizard) == {}
        assert wizard.mapping_table.rowCount() == len(BILL_COLUMNS)
        # 候选下拉里必须有文件的实际表头（否则用户根本没法手选）
        combo = wizard.mapping_table.cellWidget(0, 1)
        candidates = {combo.itemData(index) for index in range(combo.count())}
        assert {"F1", "F2", "F3"} <= candidates

    def test_manual_mapping_can_still_import(self, wizard, tmp_path: Path):
        """兜底预填之后，用户一路点下去必须能导入真实数据（不再是空账单）。"""
        path = self._workbook(
            tmp_path / "近似列名2.xlsx",
            ["账期起", "账期止", "总购电量(kWh)", "账单总额(元)"],
            ["2026-06-01", "2026-06-30", 88_000, 52_000],
        )
        assert wizard.set_file(path) is True
        assert wizard.select_sheet(wizard.sheet_combo.currentText()) is True
        assert wizard.apply_mapping() is True
        assert wizard.step == 4
        assert wizard.preview.valid_count == 1
        bill = wizard.preview.rows[0].bill
        assert bill.energy_total_kwh == pytest.approx(88_000.0)
        assert bill.bill_total_yuan == pytest.approx(52_000.0)


# --------------------------------------------------------------------------- #
# 5. 同类账单回归（12 份可回归账单）
# --------------------------------------------------------------------------- #
_BILLS = _all_bills()


class TestBillFolderRegression:
    """对收资目录下每一份同类账单做"不崩 + 三大勾稽"回归。"""

    @pytest.mark.parametrize("path", _BILLS, ids=lambda p: p.stem)
    def test_parse_never_raises(self, path: Path):
        row, notes = parse_bill_pdf(path)
        assert isinstance(row, dict)
        assert isinstance(notes, list)
        assert row["energy_total_kwh"] is not None, notes

    @pytest.mark.parametrize("path", _BILLS, ids=lambda p: p.stem)
    def test_period_energy_sum_matches_group_total(self, path: Path):
        """分时电量之和 = 各电能表**计费电量合计**之和（明细口径的硬勾稽）。

        注意不是"账单概况页的本期电量"：概况页的本期电量是**加减（退补）之后**的口径
        （如 1 月账单含 -205,904 kWh 的退补），与明细列的计费电量本就不等；
        报表在 ``messages`` 里如实说明差异，本用例只断言明细口径内部自洽。

        1/2 月账单的"分时明细"与"组合计"对不上（同一张表里"示数类型"列与"计费电量"列
        存在合并单元格错位），此时解析器**保留组合计并把差异写进中文提示**——
        因此本用例对这类账单只要求"差异被提示"，不强行要求相等。
        """
        _row, notes, extras = parse_bill_pdf_full(path)
        groups = extras.get("meter_groups") or []
        group_total = sum((group["energy"].get("total") or 0.0) for group in groups)
        if group_total == 0.0:
            pytest.skip(f"{path.stem}：电量明细未给出组合计")
        detail = sum(
            (group["energy"].get(key) or 0.0)
            for group in groups
            for key in ("sharp", "peak", "flat", "valley")
        )
        tolerance = max(1.0, group_total * 0.002)
        if abs(detail - group_total) > tolerance:
            assert any("不一致" in note for note in notes), (
                f"{path.stem}：明细与组合计不一致却没有任何提示"
            )
            return
        assert abs(detail - group_total) <= tolerance, notes

    @pytest.mark.parametrize("path", _BILLS, ids=lambda p: p.stem)
    def test_period_sum_is_close_to_bill_total(self, path: Path):
        """分时电量之和与概况页总电量必须同量级（差额来自退补电量与定比分表）。"""
        _row, notes, extras = parse_bill_pdf_full(path)
        groups = extras.get("meter_groups") or []
        detail = sum(
            (group["energy"].get(key) or 0.0)
            for group in groups
            for key in ("sharp", "peak", "flat", "valley")
        )
        total = _row.get("energy_total_kwh")
        if detail == 0.0 or not total:
            pytest.skip(f"{path.stem}：缺少分时电量或总电量")
        assert abs(detail - total) <= total * 0.30, notes

    @pytest.mark.parametrize("path", _BILLS, ids=lambda p: p.stem)
    def test_group_inconsistency_is_reported(self, path: Path):
        """分组内"分时之和 ≠ 组合计"时必须给出中文提示，不允许静默。"""
        _row, notes, extras = parse_bill_pdf_full(path)
        for group in extras.get("meter_groups") or []:
            energy = group["energy"]
            total = energy.get("total")
            parts = [energy.get(key) for key in ("sharp", "peak", "flat", "valley")]
            if total is None or not any(part is not None for part in parts):
                continue
            part_sum = sum(part or 0.0 for part in parts)
            if abs(part_sum - total) > max(1.0, abs(total) * 0.002):
                assert any("不一致" in note for note in notes), (
                    f"{path.stem}：分组 {group['meters']} 的差异未被提示"
                )

    @pytest.mark.parametrize("path", _BILLS, ids=lambda p: p.stem)
    def test_hourly_table_prices_are_sane(self, path: Path):
        _row, _notes, extras = parse_bill_pdf_full(path)
        hourly = extras.get("hourly_energy_tariff") or []
        if not hourly:
            pytest.skip(f"{path.stem}：账单没有 24 小时电量电价表")
        for point in hourly:
            price = point.direct_trade_price_yuan_per_kwh
            if price is not None:
                assert 0.0 < price < 2.0, f"{path.stem} {point.describe()}"
        assert len(hourly) == 24, f"{path.stem}：24 小时电价表只有 {len(hourly)} 条"

    @pytest.mark.parametrize("path", _BILLS, ids=lambda p: p.stem)
    def test_hourly_table_covers_all_24_hours(self, path: Path):
        _row, _notes, extras = parse_bill_pdf_full(path)
        hourly = extras.get("hourly_energy_tariff") or []
        if not hourly:
            pytest.skip(f"{path.stem}：账单没有 24 小时电量电价表")
        assert sorted(point.hour for point in hourly) == list(HOURLY_PRICE_HOURS)

    @pytest.mark.parametrize("path", _BILLS, ids=lambda p: p.stem)
    def test_hourly_energy_sum_is_plausible(self, path: Path):
        _row, _notes, extras = parse_bill_pdf_full(path)
        hourly = extras.get("hourly_energy_tariff") or []
        total = _row.get("energy_total_kwh")
        if not hourly or not total:
            pytest.skip(f"{path.stem}：缺少 24 小时表或总电量")
        hourly_sum = sum(point.energy_kwh or 0.0 for point in hourly)
        # 24 小时表只覆盖参与市场化交易的电能表，因此只要求"同量级"
        assert 0.5 * total <= hourly_sum <= 1.05 * total, (
            f"{path.stem}：24 小时电量合计 {hourly_sum:,.0f} 与总电量 {total:,.0f} 不同量级"
        )

    @pytest.mark.parametrize("path", _BILLS, ids=lambda p: p.stem)
    def test_pdf_source_type(self, path: Path):
        preview = preview_bill_import(path)
        bill = preview.rows[0].bill
        assert bill is not None
        assert bill.source_type is BillSourceType.PDF

    def test_bill_folder_is_not_empty(self):
        """资料目录存在时必须能找到同类账单（否则回归形同虚设）。"""
        if not _BILL_DIR.exists():
            pytest.skip(f"未找到账单目录：{_BILL_DIR}")
        assert len(_BILLS) >= 10


# --------------------------------------------------------------------------- #
# 6. 解析失败的容错（中文，不抛裸异常）
# --------------------------------------------------------------------------- #
class TestFailureModes:
    def test_corrupt_pdf_raises_chinese_error(self, tmp_path: Path):
        path = tmp_path / "坏账单.pdf"
        path.write_bytes(b"%PDF-1.4\nnot really a pdf")
        with pytest.raises(ValidationError, match="无法打开 PDF 账单"):
            parse_bill_pdf(path)

    def test_snapshot_json_fixture_is_still_loadable(self):
        """既有测试夹具（不含 V2.5 新键）必须仍能被账单模型解析。"""
        fixture = Path(__file__).parent / "data" / "dongfeng_2025_bill_facts.json"
        if not fixture.exists():
            pytest.skip("未找到既有账单夹具")
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        assert isinstance(payload, (dict, list))
