"""V2.5 回归测试：**需量子表「跨行单元格」被表头行吞掉**导致的计费需量误读（缺陷 G1）。

缺陷复现（先复现、后修复；本文件在修复前必须失败）：

* 9 月账单第 3 页「输配容（需）量电费 / 功率因数调整电费」子表里，
  `需量值` 单元格被排版拆成**两行**：`30842.`（整数部分）与 `066223`（小数部分）；
* 而子表**表头行**（`输配容（需）量电费` ｜ `功率因数调整电费`）在 y 上与 `30842.` 那一行
  **被 ``_pair_rows`` 合并成同一行**；
* 解析器在识别表头后 `continue`，把这一行整行丢弃 → `30842.` 消失，
  只剩下一行的 `066223` 被当成需量值 → ``billing_demand_kw = 66223.0``（**错**）。

正确值：``30,844 kW``。推导（三处互证）：

1. ``需量电费 1,202,916.00 ÷ 39 元/kW·月 = 30,844.0``——**整除，无误差**；
2. 账单概况页 P1L056 计费数量 `30844`、电量明细 P2L059 最大需量计费电量 `30844`；
3. 需量子表主表 `30,842.066223` + 定比分表 `1.933777` = **`30,844.000000`**。

另有真实账单向的断言：10 月同字段（单行印作 `28413`）必须保持正确，
不得因修复 9 月而回退。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pymupdf", reason="构造跨行单元格样本需要 PyMuPDF")

from cenep.data.bill_pdf_importer import parse_bill_pdf_full  # noqa: E402

REAL_BILL_DIR = Path(
    r"C:\Users\Administrator\Desktop\参考资料\东风本田\开标资料\式样附件-三工厂"
    r"\式样书给出的相关文件\电费单\25年电费"
)
REAL_BILL_09 = REAL_BILL_DIR / "本三9月账单.pdf"
REAL_BILL_10 = REAL_BILL_DIR / "东本三厂10月账单.pdf"

#: 真实 9 月账单的计费需量（三处互证，见模块文档）
EXPECTED_DEMAND_09 = 30844.0
#: 9 月需量电费（元）；39 × 30844 = 1,202,916 —— 整除是"30,844 才对"的硬证据
EXPECTED_DEMAND_CHARGE_09 = 1_202_916.0
DEMAND_RATE = 39.0


def _build_split_cell_demand_pdf(path: Path) -> Path:
    """构造"表头行与需量值整数部分被合并、小数部分在下一行"的最小复现样本。

    坐标完全照抄真实账单第 3 页（x 列带见 ``_DEMAND_FIELD_ZONES``），
    并同样包含**两张并列子表**（主表 + 定比分表），以保证走进与真实账单相同的分支：

    * 主表 `需量值` 单元格被拆成 `30842.` / `066223` 两行，且 `30842.` 那一行
      与子表**表头行**同处一个 y（这正是 9 月账单的实际排版）；
    * 定比分表 `需量值` = `1.933777`，`需量电费（按实际）` = `75.42`。

    字号取 6pt：真实账单的标签很窄，若字号过大会让"标签+数值"被
    ``_pair_rows`` 粘成同一个单元格（那是样本构造问题，不是解析问题）。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    def put(x: float, y: float, text: str) -> None:
        page.insert_text((x, y), text, fontname="china-s", fontsize=6)

    # ---- 子表一：主表（拆行单元格 + 表头行同 y） ---- #
    y_head = 100.0
    put(35.2, y_head, "输配容（需）量电费")
    put(104.2, y_head, "30842.")
    put(180.8, y_head, "30842.")
    put(315.8, y_head, "功率因数调整电费")

    y_label = y_head + 4.0
    put(35.2, y_label, "按月实际最大需量")
    put(144.0, y_label, "需量值")
    put(216.0, y_label, "需量电价")
    put(261.8, y_label, "39")
    put(315.8, y_label, "功率因数实际值")
    put(378.0, y_label, "0.91")
    put(414.0, y_label, "功率因数标准")
    put(467.2, y_label, "0.9")
    put(496.5, y_label, "调整系数")
    put(531.8, y_label, "-0.0015")

    y_frac = y_head + 8.0
    put(104.2, y_frac, "066223")
    put(180.8, y_frac, "066223")

    y_more = y_head + 19.0
    put(35.2, y_more, "月每千伏安用电量")
    put(104.2, y_more, "175")
    put(144.0, y_more, "合同容量")
    put(216.0, y_more, "折扣需量电费")
    put(315.8, y_more, "参与调整电费金额")
    put(388.5, y_more, "6137186.5")

    y_charge = y_head + 37.0
    put(35.2, y_charge, "需量电费（按实际）")
    put(104.2, y_charge, "1202840.58")
    put(144.0, y_charge, "容量电价")
    put(216.0, y_charge, "输配容量电费")
    put(315.8, y_charge, "功率因数调整电费")
    put(388.5, y_charge, "-9205.79")

    # ---- 子表二：定比分表 ---- #
    y2 = y_head + 60.0
    put(35.2, y2, "输配容（需）量电费")
    put(315.8, y2, "功率因数调整电费")
    put(35.2, y2 + 4.0, "按月实际最大需量")
    put(104.2, y2 + 4.0, "1.933777")
    put(144.0, y2 + 4.0, "需量值")
    put(180.8, y2 + 4.0, "1.933777")
    put(216.0, y2 + 4.0, "需量电价")
    put(261.8, y2 + 4.0, "39")
    put(315.8, y2 + 4.0, "功率因数实际值")
    put(378.0, y2 + 4.0, "0.91")
    put(414.0, y2 + 4.0, "功率因数标准")
    put(467.2, y2 + 4.0, "0.9")
    put(496.5, y2 + 4.0, "调整系数")
    put(531.8, y2 + 4.0, "-0.0015")
    put(35.2, y2 + 23.0, "需量电费（按实际）")
    put(104.2, y2 + 23.0, "75.42")
    put(144.0, y2 + 23.0, "容量电价")
    put(216.0, y2 + 23.0, "输配容量电费")
    put(315.8, y2 + 23.0, "功率因数调整电费")
    put(388.5, y2 + 23.0, "-0.58")

    doc.save(str(path))
    doc.close()
    return path


def test_demand_split_cell_is_not_swallowed_by_header_row(tmp_path: Path) -> None:
    """**缺陷 G1 的最小复现**：拆行单元格的整数部分不能被表头行带走。

    修复前：``billing_demand_kw == 66223.0``（只剩小数片段）。
    修复后：``billing_demand_kw == 30844.0``（整数部分 `30842.` 与小数部分 `066223` 拼接，
    四舍五入到账单口径的 30,844）。
    """
    path = _build_split_cell_demand_pdf(tmp_path / "split_demand.pdf")

    row, _notes, _extras = parse_bill_pdf_full(path)

    assert row["billing_demand_kw"] == pytest.approx(30844.0, abs=0.5), (
        "计费需量被误读：账单为 30842.066223 kW（≈30,844），"
        "若得到 66223.0 说明拆行单元格的整数部分被表头行吞掉（缺陷 G1）"
    )
    assert row["billing_demand_kw"] != pytest.approx(66223.0, abs=1.0)


def test_demand_rate_and_other_fields_survive_the_fix(tmp_path: Path) -> None:
    """修复不得带坏同子表的其它字段（需量电价 / 力调 / 参与调整金额 / 月每千伏安）。"""
    path = _build_split_cell_demand_pdf(tmp_path / "split_demand2.pdf")

    row, _notes, _extras = parse_bill_pdf_full(path)

    assert row["demand_rate_yuan_per_kw_month"] == pytest.approx(DEMAND_RATE)
    assert row["power_factor_actual"] == pytest.approx(0.91)
    assert row["power_factor_standard"] == pytest.approx(0.9)
    assert row["power_factor_adjustment_factor"] == pytest.approx(-0.0015)
    assert row["power_factor_participating_charge_yuan"] == pytest.approx(6_137_186.5)
    assert row["energy_per_kva_kwh"] == pytest.approx(175.0)

    # 需量电费：账单子表值 1,202,840.58（主表）应保留，不能被 30842 顶掉。
    # 该样本没有"账单概况页"，所以 row["demand_charge_yuan"] 天然为空；
    # 这里直接检查子表解析器的输出，口径更准确。
    import pymupdf

    from cenep.data.bill_pdf_importer import _parse_demand_and_pf

    document = pymupdf.open(str(path))
    try:
        parsed = _parse_demand_and_pf(document)
    finally:
        document.close()
    assert parsed["demand"] == pytest.approx(30_844.0, abs=0.5)
    assert parsed["_demand_charge_from_block"] == pytest.approx(1_202_840.58)


def test_real_september_2025_demand_is_30844() -> None:
    """**真实 9 月账单**：计费需量必须是 30,844 kW（账单三处互证）。"""
    if not REAL_BILL_09.exists():
        pytest.skip(f"真实账单样本缺失（业务资料不入仓库）：{REAL_BILL_09}")

    row, notes, _extras = parse_bill_pdf_full(REAL_BILL_09)

    assert row["billing_demand_kw"] == pytest.approx(EXPECTED_DEMAND_09, abs=0.5), (
        f"9 月计费需量应为 {EXPECTED_DEMAND_09}，实得 {row['billing_demand_kw']}"
    )
    assert row["demand_rate_yuan_per_kw_month"] == pytest.approx(DEMAND_RATE)
    assert row["demand_charge_yuan"] == pytest.approx(EXPECTED_DEMAND_CHARGE_09, abs=0.01)
    # 需量电价 × 计费需量 = 需量电费（账单自身的乘式必须闭合）
    assert (
        row["billing_demand_kw"] * row["demand_rate_yuan_per_kw_month"]
        == pytest.approx(row["demand_charge_yuan"], abs=0.01)
    ), "需量电费与「需量电价 × 计费需量」对不上，说明需量或单价仍有一项取错"
    # 修复后不应再出现"需量不一致"的中文提示
    assert not any("与账单的需量电费" in note for note in notes), notes


def test_real_october_2025_demand_unchanged() -> None:
    """**真实 10 月账单**：同字段单行印作 28413，修复不得让它回退。"""
    if not REAL_BILL_10.exists():
        pytest.skip(f"真实账单样本缺失（业务资料不入仓库）：{REAL_BILL_10}")

    row, _notes, _extras = parse_bill_pdf_full(REAL_BILL_10)

    assert row["billing_demand_kw"] == pytest.approx(28_413.0, abs=0.5)
    assert row["demand_rate_yuan_per_kw_month"] == pytest.approx(DEMAND_RATE)
    assert row["demand_charge_yuan"] == pytest.approx(1_108_107.0, abs=0.01)
    assert (
        row["billing_demand_kw"] * row["demand_rate_yuan_per_kw_month"]
        == pytest.approx(row["demand_charge_yuan"], abs=0.01)
    )
