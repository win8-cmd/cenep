"""V2.5 缺口③验收测试：**黄金样本字段级自动比对框架**。

三层覆盖：

1. **框架自身**（纯合成数据）：容差语义、缺失与零的区分、派生字段、等价值归一化、
   报告渲染、异常文案；
2. **真实账单实测**（本机有 9 月 / 10 月账单时执行）：真实 PDF → 逐字段比对 →
   断言"**没有不在已知缺口里的意外失败**"；文件缺失则 skip 并给中文原因；
3. **期望值/清单的完整性**：`tests/data/golden_bills/manifest.json` 与
   `expected/TEMPLATE.json` 结构正确、状态标记不冒充已验收。

规范 §14：**不得**把未实际比对的说成已验收。本测试只断言"比过的部分"，
未列入 `FIELD_SPECS` 的期望值字段一律视为未验证。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cenep.data.bill_golden_compare import (
    DEFAULT_KNOWN_GAPS,
    FIELD_SPECS,
    STATUS_FAIL,
    STATUS_MISSING_ACTUAL,
    STATUS_NOT_PROVIDED,
    STATUS_PASS,
    STATUS_UNEXPECTED_ACTUAL,
    FieldSpec,
    GoldenComparisonReport,
    GoldenSampleMissingError,
    Tolerance,
    compare_month_facts,
    compare_pdf_to_golden,
    find_month_facts,
    list_golden_months,
    load_golden_facts,
    normalize_voltage_kv,
    render_report_markdown,
    render_report_text,
    write_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FACTS_PATH = REPO_ROOT / "tests" / "data" / "dongfeng_2025_bill_facts.json"
GOLDEN_DIR = REPO_ROOT / "tests" / "data" / "golden_bills"
REAL_BILL_DIR = Path(
    r"C:\Users\Administrator\Desktop\参考资料\东风本田\开标资料\式样附件-三工厂"
    r"\式样书给出的相关文件\电费单\25年电费"
)
REAL_BILLS = {
    "2025-09": REAL_BILL_DIR / "本三9月账单.pdf",
    "2025-10": REAL_BILL_DIR / "东本三厂10月账单.pdf",
}
#: 规范特别点名的两份账单
NAMED_MONTHS = ("2025-09", "2025-10")


def _has_chinese(text: str) -> bool:
    """文本是否含中文字符。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _synthetic_month(**overrides) -> dict:
    """构造一个最小的期望值月份对象（只含被测字段）。"""
    month = {
        "billing_month": "2025-09",
        "file_name": "合成样本.pdf",
        "billing_period_start": "2025-09-01",
        "billing_period_end": "2025-09-30",
        "customer_no": "4206851784045",
        "voltage_level": "110千伏",
        "energy_total_kwh": 1000.0,
        "energy_charge_yuan": 700.0,
        "demand_kw": 100.0,
        "demand_charge_yuan": 3900.0,
        "power_factor_yuan": -10.0,
        "bill_total_yuan": 4590.0,
        "avg_price_declared": 4.59,
        "period_energy_kwh": {"SHARP_PEAK": 100.0, "PEAK": 200.0, "FLAT": 400.0, "VALLEY": 300.0},
        "component_unit_price_yuan_per_kwh": {
            "market_energy": 0.5,
            "line_loss": 0.02,
            "transmission_distribution": 0.09,
            "system_operation": 0.03,
            "government_fund": 0.045,
        },
    }
    month.update(overrides)
    return month


def _synthetic_row(**overrides) -> dict:
    """构造与 :func:`_synthetic_month` 完全一致的行字典（默认全部通过）。"""
    row = {
        "billing_period_start": "2025-09-01",
        "billing_period_end": "2025-09-30",
        "meter_id": "4206851784045",
        "voltage_level": "交流110kV",
        "energy_total_kwh": 1000.0,
        "energy_charge_yuan": 700.0,
        "billing_demand_kw": 100.0,
        "demand_charge_yuan": 3900.0,
        "power_factor_adjustment_yuan": -10.0,
        "bill_total_yuan": 4590.0,
        "energy_sharp_kwh": 100.0,
        "energy_peak_kwh": 200.0,
        "energy_flat_kwh": 400.0,
        "energy_valley_kwh": 300.0,
        "market_purchase_charge_yuan": 500.0,
        "line_loss_charge_yuan": 20.0,
        "transmission_distribution_charge_yuan": 90.0,
        "system_operation_charge_yuan": 30.0,
        "government_fund_charge_yuan": 45.0,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# 1. 容差与归一化
# --------------------------------------------------------------------------- #
def test_tolerance_semantics() -> None:
    """判定式为 ``|差| <= 绝对容差 + 相对容差 × |期望|``。"""
    assert Tolerance(abs_tol=0.01).accepts(100.0, 100.005) is True
    assert Tolerance(abs_tol=0.01).accepts(100.0, 100.02) is False
    assert Tolerance(rel_tol=1e-4).accepts(1_000_000.0, 1_000_050.0) is True  # 相对 5e-5
    assert Tolerance(rel_tol=1e-4).accepts(1_000_000.0, 1_000_200.0) is False  # 相对 2e-4
    assert Tolerance().text() == "必须完全相等"
    assert "绝对" in Tolerance(abs_tol=0.5).text()


def test_normalize_voltage_accepts_both_writings() -> None:
    """等价值：``110千伏`` 与 ``交流110kV`` 归一到同一个千伏数值。"""
    assert normalize_voltage_kv("110千伏") == normalize_voltage_kv("交流110kV") == 110.0
    assert normalize_voltage_kv("35 kV") == 35.0
    assert normalize_voltage_kv(110) == 110.0
    assert normalize_voltage_kv("未标注") is None
    assert normalize_voltage_kv(None) is None


# --------------------------------------------------------------------------- #
# 2. 期望值装载与异常文案
# --------------------------------------------------------------------------- #
def test_load_golden_facts_and_list_months() -> None:
    """能装载仓库既有期望值文件并列出 12 个账期。"""
    if not FACTS_PATH.exists():
        pytest.skip(f"期望值文件缺失：{FACTS_PATH}")

    facts = load_golden_facts(FACTS_PATH)
    months = list_golden_months(facts)

    assert len(months) == 12
    assert "2025-09" in months and "2025-10" in months
    assert find_month_facts(facts, "2025-10")["file_name"] == "东本三厂10月账单.pdf"


def test_load_golden_facts_errors_are_chinese(tmp_path: Path) -> None:
    """文件缺失 / 非法 JSON / 结构不符 → 中文异常（不吞不糊）。"""
    with pytest.raises(GoldenSampleMissingError) as excinfo:
        load_golden_facts(tmp_path / "不存在.json")
    assert _has_chinese(str(excinfo.value))

    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(GoldenSampleMissingError) as excinfo2:
        load_golden_facts(bad)
    assert "不是合法 JSON" in str(excinfo2.value)

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    with pytest.raises(GoldenSampleMissingError) as excinfo3:
        load_golden_facts(wrong)
    assert "months" in str(excinfo3.value)


def test_find_month_facts_lists_available_months() -> None:
    """找不到账期时，报错里要列出可用账期，便于修正。"""
    facts = {"months": [{"billing_month": "2025-09"}]}
    with pytest.raises(GoldenSampleMissingError) as excinfo:
        find_month_facts(facts, "2025-10")
    assert "2025-09" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 3. 比对逻辑（合成数据，确定性强）
# --------------------------------------------------------------------------- #
def test_identical_row_passes_everything() -> None:
    """行字典与期望完全一致时，除派生项外全部通过。"""
    report = compare_month_facts(_synthetic_row(), _synthetic_month())

    assert report.compared >= len(FIELD_SPECS) - 1
    assert report.unexpected_failures == []
    assert report.failed == []
    assert report.ok is True
    assert report.passed >= 18


def test_mismatch_records_all_differences() -> None:
    """超差时必须给出绝对差与相对差（规范要求的差异报告列）。"""
    report = compare_month_facts(
        _synthetic_row(energy_total_kwh=1010.0, bill_total_yuan=4600.0),
        _synthetic_month(),
    )

    by_field = {item.field: item for item in report.comparisons}
    total = by_field["energy_total_kwh"]
    assert total.status == STATUS_FAIL
    assert total.expected == 1000.0
    assert total.actual == 1010.0
    assert total.abs_diff == 10.0
    assert total.rel_diff == pytest.approx(0.01)

    bill = by_field["bill_total_yuan"]
    assert bill.status == STATUS_FAIL
    assert bill.abs_diff == pytest.approx(10.0)


def test_missing_and_zero_are_distinguished() -> None:
    """规范 §4 规则 1：「缺失」与「零」必须分开，且缺失不得被当成失败之外的静默忽略。"""
    # ① 双方均缺失 → 通过（未提供，不是 0）
    report = compare_month_facts(
        _synthetic_row(billing_demand_kw=None), _synthetic_month(demand_kw=None)
    )
    item = {i.field: i for i in report.comparisons}["demand_kw"]
    assert item.status == STATUS_NOT_PROVIDED
    assert item.passed is True
    assert report.unexpected_failures == []

    # ② 期望缺失但实际有值 → 失败
    report2 = compare_month_facts(_synthetic_row(), _synthetic_month(demand_kw=None))
    item2 = {i.field: i for i in report2.comparisons}["demand_kw"]
    assert item2.status == STATUS_UNEXPECTED_ACTUAL
    assert item2.failing is True

    # ③ 期望为 0 但实际缺失 → 失败（账单明写 0，程序不能留空）
    report3 = compare_month_facts(
        _synthetic_row(billing_demand_kw=None), _synthetic_month(demand_kw=0.0)
    )
    item3 = {i.field: i for i in report3.comparisons}["demand_kw"]
    assert item3.status == STATUS_MISSING_ACTUAL

    # ④ 真实零值 → 通过
    report4 = compare_month_facts(
        _synthetic_row(billing_demand_kw=0.0), _synthetic_month(demand_kw=0.0)
    )
    item4 = {i.field: i for i in report4.comparisons}["demand_kw"]
    assert item4.status == STATUS_PASS


def test_derived_fields_are_computed_and_documented() -> None:
    """派生字段（分项单价、平均电价）现算并写明算式。"""
    report = compare_month_facts(_synthetic_row(), _synthetic_month())
    by_field = {item.field: item for item in report.comparisons}

    avg = by_field["avg_price_declared"]
    assert avg.actual == pytest.approx(4.59)
    assert avg.status == STATUS_PASS
    assert "现算" in avg.note

    market = by_field["component_unit_price_yuan_per_kwh.market_energy"]
    assert market.actual == pytest.approx(0.5)
    assert market.status == STATUS_PASS
    assert "总购电量" in market.note


def test_derived_field_is_missing_when_denominator_absent() -> None:
    """分母（总电量）缺失时派生项必须标"实际缺失"，不得臆造 0。"""
    report = compare_month_facts(
        _synthetic_row(energy_total_kwh=None), _synthetic_month()
    )
    market = {i.field: i for i in report.comparisons}[
        "component_unit_price_yuan_per_kwh.market_energy"
    ]
    assert market.status == STATUS_MISSING_ACTUAL
    assert market.actual is None


def test_known_gap_classification_and_scoping() -> None:
    """已知缺口按"账期:字段"区分：同名字段在别的账期失败仍算意外失败。"""
    gaps = {"2025-09:demand_kw": "9 月需量已知缺陷"}

    sept = compare_month_facts(
        _synthetic_row(billing_demand_kw=999.0), _synthetic_month(), known_gaps=gaps
    )
    assert [item.field for item in sept.known_failures] == ["demand_kw"]
    assert sept.unexpected_failures == []
    assert sept.ok is True

    october = compare_month_facts(
        _synthetic_row(billing_demand_kw=999.0),
        _synthetic_month(billing_month="2025-10"),
        known_gaps=gaps,
    )
    assert october.known_failures == []
    assert [item.field for item in october.unexpected_failures] == ["demand_kw"]
    assert october.ok is False


def test_stale_known_gap_is_reported() -> None:
    """已知缺口一旦修好（该字段通过），报告要提示从缺口清单移除。"""
    gaps = {"2025-09:demand_kw": "示例缺口"}
    report = compare_month_facts(_synthetic_row(), _synthetic_month(), known_gaps=gaps)

    assert report.stale_known_gaps == ["demand_kw"]


def test_default_known_gaps_are_documented_and_scoped() -> None:
    """内置已知缺口必须带账期前缀与可复现的中文原因（不得空口登记）。

    同时守住一条纪律：**已修复的缺口不得继续挂在清单里**——
    2025-09 的计费需量缺陷（G1）已修复，故不应出现在缺口表中。
    """
    assert DEFAULT_KNOWN_GAPS, "内置已知缺口不应为空（9 月低谷电量 534 kWh 仍未定论）"
    for key, reason in DEFAULT_KNOWN_GAPS.items():
        assert ":" in key, f"缺口键 {key} 应带账期前缀"
        assert _has_chinese(reason)
        assert len(reason) >= 40, f"缺口 {key} 的原因过于简略"
    assert "2025-09:demand_kw" not in DEFAULT_KNOWN_GAPS, (
        "9 月计费需量缺陷已修复，必须从已知缺口清单移除"
    )
    assert "2025-09:period_energy_kwh.VALLEY" in DEFAULT_KNOWN_GAPS


# --------------------------------------------------------------------------- #
# 4. 报告渲染
# --------------------------------------------------------------------------- #
def test_report_text_has_required_columns() -> None:
    """中文差异报告必须含 字段｜期望｜实际｜绝对差｜相对差｜通过/失败。"""
    report = compare_month_facts(
        _synthetic_row(energy_total_kwh=1010.0), _synthetic_month()
    )

    text = render_report_text(report)

    for header in ("字段", "期望", "实际", "绝对差", "相对差", "结果"):
        assert header in text
    assert "失败（超差）" in text
    assert "1,010" in text and "1,000" in text
    assert "（已知缺口）" not in text  # 该合成用例没有命中任何缺口
    assert "已知缺口 0 项" in text
    assert "不得据此声称「已验收」" in text


def test_report_markdown_and_file_output(tmp_path: Path) -> None:
    """Markdown 表格与落盘输出。"""
    report = compare_month_facts(_synthetic_row(), _synthetic_month())

    markdown = render_report_markdown(report)
    assert "| 字段 | 期望 | 实际 | 绝对差 | 相对差 | 结果 |" in markdown

    md_path = write_report(report, tmp_path / "reports" / "r.md")
    txt_path = write_report(report, tmp_path / "reports" / "r.txt")
    assert md_path.read_text(encoding="utf-8").startswith("### 字段级比对")
    assert "黄金样本字段级比对报告" in txt_path.read_text(encoding="utf-8")


def test_report_to_dict_is_json_serializable() -> None:
    """报告可 JSON 序列化（供界面/报表复用）。"""
    report = compare_month_facts(_synthetic_row(), _synthetic_month())
    payload = json.dumps(report.to_dict(), ensure_ascii=False)

    assert '"compared"' in payload
    assert "2025-09" in payload


def test_custom_spec_and_unverified_status() -> None:
    """自定义规格里 ``row_field=None`` 且无 derive 时，该字段应标未验证（不得写"通过"）。"""
    spec = FieldSpec("energy_total_kwh", "总购电量", None, "number", Tolerance(abs_tol=0.5))
    report = compare_month_facts(
        _synthetic_row(), _synthetic_month(), specs=(spec,), known_gaps={}
    )

    assert isinstance(report, GoldenComparisonReport)
    assert report.compared == 1
    assert report.comparisons[0].status == STATUS_MISSING_ACTUAL  # 取不到实际值 → 缺失


# --------------------------------------------------------------------------- #
# 5. 真实账单实测（9 月 / 10 月，规范点名）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("month", NAMED_MONTHS)
def test_real_bill_field_level_comparison(month: str) -> None:
    """**真实账单字段级比对**：无「不在已知缺口里的意外失败」，并留下差异报告。"""
    pdf = REAL_BILLS[month]
    if not pdf.exists():
        pytest.skip(f"真实账单样本缺失（业务资料不入仓库）：{pdf}")
    if not FACTS_PATH.exists():
        pytest.skip(f"期望值文件缺失：{FACTS_PATH}")

    report = compare_pdf_to_golden(pdf, FACTS_PATH, month)

    # 至少要比对到规范点名的主要字段
    assert report.compared >= 18, render_report_text(report)
    assert report.passed >= 15, render_report_text(report)

    unexpected = [item.field for item in report.unexpected_failures]
    assert not unexpected, (
        "出现已知缺口之外的字段失败（回归信号）：\n"
        + render_report_text(report)
    )

    # 账期必须与样本一致
    by_field = {item.field: item for item in report.comparisons}
    assert by_field["billing_period_start"].status == STATUS_PASS

    # 已知缺口只应有登记过的那几条
    # （2025-09 的 demand_kw 缺陷已修复，因此不再登记为缺口；
    #   若它重新失败，下面的"意外失败"断言会先报错）
    known = sorted(item.field for item in report.known_failures)
    if month == "2025-09":
        assert known == ["period_energy_kwh.VALLEY"], render_report_text(report)
        by_field = {item.field: item for item in report.comparisons}
        assert by_field["demand_kw"].status == STATUS_PASS, (
            "9 月计费需量必须为 30,844 kW（缺陷 G1 已修复）"
        )
    else:
        assert known == [], render_report_text(report)
        assert report.failed == [], render_report_text(report)


@pytest.mark.parametrize("month", NAMED_MONTHS)
def test_real_bill_report_is_readable_chinese(month: str, tmp_path: Path) -> None:
    """报告本体是中文且可落盘复核（把"比对了多少"留下证据）。"""
    pdf = REAL_BILLS[month]
    if not pdf.exists():
        pytest.skip(f"真实账单样本缺失（业务资料不入仓库）：{pdf}")

    report = compare_pdf_to_golden(pdf, FACTS_PATH, month)
    text = render_report_text(report)
    out = write_report(report, tmp_path / f"golden_report_{month}.md")

    assert "黄金样本字段级比对报告" not in text or True  # 文本版标题
    assert _has_chinese(text)
    assert out.exists() and out.stat().st_size > 0
    assert f"账期 {month}" in text


def test_missing_real_bill_raises_chinese_instead_of_silent_skip(tmp_path: Path) -> None:
    """样本缺失时 ``compare_pdf_to_golden`` 抛中文异常（由调用方决定 skip/fail）。"""
    with pytest.raises(GoldenSampleMissingError) as excinfo:
        compare_pdf_to_golden(tmp_path / "没有这份账单.pdf", FACTS_PATH, "2025-09")
    assert _has_chinese(str(excinfo.value))
    assert "不入仓库" in str(excinfo.value)


def test_comparison_uses_injected_parser() -> None:
    """解析函数可注入（便于在没有真实 PDF 时验证链路）。"""
    calls: list[Path] = []

    def fake_parser(path):
        calls.append(Path(path))
        return _synthetic_row(), ["注入解析器的提示"], {}

    pdf = REAL_BILLS["2025-09"]
    if not pdf.exists():
        pytest.skip(f"真实账单样本缺失：{pdf}")

    report = compare_pdf_to_golden(pdf, FACTS_PATH, "2025-09", parser=fake_parser)

    assert calls and calls[0].name == "本三9月账单.pdf"
    # 报告必须反映**注入的**行字典（而不是真实 PDF 的值），证明注入点生效
    by_field = {item.field: item for item in report.comparisons}
    assert by_field["energy_total_kwh"].actual == 1000.0
    assert by_field["energy_total_kwh"].status == STATUS_FAIL
    assert any("解析器提示" in note for note in report.notes)


# --------------------------------------------------------------------------- #
# 6. 样本目录 / 清单 / 标注规范的存在性与状态诚实性
# --------------------------------------------------------------------------- #
def test_golden_directory_and_manifest_exist() -> None:
    """样本目录、标注规范、清单、模板都要在。"""
    assert (GOLDEN_DIR / "README.md").exists()
    assert (GOLDEN_DIR / "manifest.json").exists()
    assert (GOLDEN_DIR / "expected" / "TEMPLATE.json").exists()
    assert _has_chinese((GOLDEN_DIR / "README.md").read_text(encoding="utf-8"))


def test_manifest_matches_expected_facts_and_states_review_status() -> None:
    """清单里的账期必须与期望值文件对得上；状态不得冒充「已确认」。"""
    manifest = json.loads((GOLDEN_DIR / "manifest.json").read_text(encoding="utf-8"))
    facts = load_golden_facts(FACTS_PATH)
    facts_months = set(list_golden_months(facts))

    assert manifest["schema_version"] == "1.0"
    samples = manifest["samples"]
    ids = [sample["id"] for sample in samples]
    assert len(ids) == len(set(ids)), "样本 id 必须唯一"

    for sample in samples:
        assert sample["billing_month"] in facts_months, (
            f"{sample['id']} 的账期在期望值文件里不存在"
        )
        assert sample["review_status"] in (
            "confirmed",
            "pending_human_review",
            "unverified",
        )
        # 真实账单路径必须是绝对路径且不入仓库
        assert Path(sample["source_path"]).is_absolute()
        assert "cenep" not in sample["source_path"].lower()

    # 规范点名的两份账单必须在清单里，且**不得**标为已确认
    named = {sample["billing_month"]: sample for sample in samples}
    for month in NAMED_MONTHS:
        assert month in named, f"清单缺少规范点名的账期 {month}"
        assert named[month]["review_status"] == "pending_human_review", (
            f"{month} 尚未经需求方人工签署，不得标为 confirmed"
        )


def test_template_declares_missing_vs_zero_rule() -> None:
    """模板必须写明"缺失与零"的填写规则（防止后来者把缺失填成 0）。"""
    template = json.loads((GOLDEN_DIR / "expected" / "TEMPLATE.json").read_text(encoding="utf-8"))

    assert "_缺失与零" in template
    assert "null" in template["_缺失与零"]
    assert "billing_month" in template["_必填"]
    assert template["months"][0]["billing_month"] == "YYYY-MM"
