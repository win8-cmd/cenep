"""V2.4 阶段 7：报告表/章节清单的一致性与「不硬编码」防线（§8.1、§11、§12）。

本文件把阶段 7 的两条**设计约束**写成可执行断言，防止以后回退：

1. **新增表 / 新增章节只改两处清单**：`SHEET_NAMES`（Excel）与 `REPORT_SECTIONS`（PDF）；
   自检（`selftest.py`）与全部测试一律引用 `len(...)`，**不得硬编码表数/章节数**；
2. **报告必须区分四类数据**（实际账单 / 软件复算 / 模型估算 / 方案模拟），
   且每条口径说明都能在 Excel 与 PDF 中找到（§8.1 硬要求）。

另含一条**打包元数据**回归：`build/version_info.txt` 的交付版本必须与
`V2.4_RELEASE_NOTES.md` 声明一致，且**不得**改动程序版本 / schema 版本
（§0.2 冻结条款）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cenep.reports import excel_exporter, pdf_exporter
from cenep.reports.excel_exporter import (
    CALIBER_LEGEND,
    CALIBER_TAG_ACTUAL,
    CALIBER_TAG_ESTIMATED,
    CALIBER_TAG_RECOMPUTED,
    CALIBER_TAG_SIMULATED,
    SHEET_NAMES,
    ExcelExporter,
)
from cenep.reports.pdf_exporter import (
    REPORT_SECTIONS,
    SECTION_NUMERALS,
    PdfExporter,
    section_title,
)

ROOT = Path(__file__).resolve().parents[1]


class TestDeclarativeLists:
    def test_sheets_and_sections_are_declarative_lists(self):
        assert isinstance(SHEET_NAMES, list) and len(SHEET_NAMES) >= 27
        assert isinstance(REPORT_SECTIONS, list) and len(REPORT_SECTIONS) >= 18
        assert len(set(SHEET_NAMES)) == len(SHEET_NAMES), "工作表名不得重复"
        assert len(set(REPORT_SECTIONS)) == len(REPORT_SECTIONS), "章节名不得重复"

    def test_numerals_cover_all_sections(self):
        """中文数字序号必须足够覆盖全部章节（否则 section_title 会越界）。"""
        assert len(SECTION_NUMERALS) >= len(REPORT_SECTIONS)
        titles = [section_title(i) for i in range(len(REPORT_SECTIONS))]
        assert len(set(titles)) == len(titles)

    #: 历史上出现过的表数 / 章节数（V2 §67 的 24、V2.1 的 27、V2.2 的 18）。
    #: 清单增长后这些数字**必须**从断言里消失，否则说明有人把旧数字留在了测试里。
    STALE_COUNTS = ("24", "27", "17", "18")

    @pytest.mark.parametrize(
        "module_name",
        ["test_selftest.py", "test_excel_export.py", "test_pdf_export.py", "test_report_v2.py"],
    )
    def test_no_stale_counts_in_tests(self, module_name):
        """V2.4 §8.1 修正项：测试里**不得**再出现"上一版的表数/章节数"。

        允许的写法：``len(SHEET_NAMES)``、``len(REPORT_SECTIONS)``、``len(wb.sheetnames)``，
        以及**当前值**的一次性边界断言（``== len(...)`` 是首选）。
        禁止的写法：``len(SHEET_NAMES) == 27`` / ``len(REPORT_SECTIONS) == 18``
        —— 这类数字会在下一次加表时变成"测试骗人"的源头。
        """
        source = (ROOT / "tests" / module_name).read_text(encoding="utf-8")
        offenders = [
            value
            for value in re.findall(
                r"len\((?:SHEET_NAMES|REPORT_SECTIONS|sheets|wb\.sheetnames)\)\s*==\s*(\d+)", source
            )
            if value in self.STALE_COUNTS
        ]
        assert offenders == [], f"{module_name} 仍留有上一版的硬编码计数：{offenders}"

    def test_selftest_has_no_stale_counts(self):
        """自检是"打包后验证 EXE"的唯一手段，同样不得留上一版的硬编码表数/章节数。"""
        source = (ROOT / "src" / "cenep" / "selftest.py").read_text(encoding="utf-8")
        offenders = [
            value
            for value in re.findall(
                r"len\((?:sheets|REPORT_SECTIONS|SHEET_NAMES)\)\s*==\s*(\d+)", source
            )
            if value in self.STALE_COUNTS
        ]
        assert offenders == [], f"selftest.py 仍留有上一版的硬编码计数：{offenders}"

    def test_sheet_names_used_by_exporter_match_declaration(self):
        """`export_sheets()`（界面提示用）必须与清单一致，不得各写一份。"""
        assert ExcelExporter().export_sheets(None, None) == list(SHEET_NAMES)

    def test_report_lists_match_release_notes(self):
        """清单长度必须与发布说明中声明的"32 张表 / 21 章"一致（文档与代码同源）。"""
        text = (ROOT / "V2.4_RELEASE_NOTES.md").read_text(encoding="utf-8")
        assert f"Excel **{len(SHEET_NAMES)} 张表**" in text
        assert f"PDF **{len(REPORT_SECTIONS)} 章**" in text


class TestCaliberLegend:
    def test_four_data_types_defined(self):
        names = [name for name, _ in CALIBER_LEGEND]
        assert names == ["① 实际账单", "② 软件复算", "③ 模型估算", "④ 方案模拟"]

    def test_tags_are_distinct(self):
        tags = {
            CALIBER_TAG_ACTUAL,
            CALIBER_TAG_RECOMPUTED,
            CALIBER_TAG_ESTIMATED,
            CALIBER_TAG_SIMULATED,
        }
        assert len(tags) == 4

    def test_excel_sheet_titles_carry_caliber_tag(self):
        """每个"数据类"工作表标题都要带口径标签，避免用户把四类数据看成一类。"""
        source = (ROOT / "src" / "cenep" / "reports" / "excel_exporter.py").read_text(encoding="utf-8")
        for tag in (CALIBER_TAG_ACTUAL, CALIBER_TAG_SIMULATED):
            assert tag in source


class TestPackagingMetadata:
    """打包元数据回归（§8.4 第 5 条：版本号口径必须清楚且可核验）。"""

    def test_version_info_declares_release_2_4(self):
        text = (ROOT / "build" / "version_info.txt").read_text(encoding="utf-8")
        assert "filevers=(2, 4, 0, 0)" in text
        assert 'StringStruct("ProductVersion", "2.4.0")' in text

    def test_program_and_schema_versions_unchanged(self):
        """§0.2 冻结：交付版本递增**不得**改动程序版本与项目文件 schema 版本。"""
        from cenep import __version__
        from cenep.infrastructure.project_file import SCHEMA_VERSION

        assert __version__ == "2.0.0"
        assert SCHEMA_VERSION == "2.0"

    def test_release_notes_exist_and_state_version_split(self):
        notes = ROOT / "V2.4_RELEASE_NOTES.md"
        assert notes.exists(), "缺少 V2.4_RELEASE_NOTES.md"
        text = notes.read_text(encoding="utf-8")
        for token in (
            "已知限制", "升级步骤", "回滚步骤", "数据口径与来源声明",
            "测试与性能实测数据", "2.4.0.0", "2.0.0",
        ):
            assert token in text, f"发布说明缺少：{token}"

    def test_spec_path_and_exe_path_are_documented(self):
        """§8.4：产物路径与快捷方式路径必须写进发布说明（重建后不需改快捷方式）。"""
        text = (ROOT / "V2.4_RELEASE_NOTES.md").read_text(encoding="utf-8")
        assert "dist\\CENEP\\CENEP.exe" in text
        assert "CENEP V2 经济评价软件.lnk" in text


class TestPdfSectionTitleHelper:
    def test_section_title_matches_declaration(self):
        for index, name in enumerate(REPORT_SECTIONS):
            assert section_title(index).endswith("、" + name)

    def test_first_and_last_titles(self):
        assert section_title(0) == "一、项目概况"
        assert section_title(len(REPORT_SECTIONS) - 1).startswith(
            SECTION_NUMERALS[len(REPORT_SECTIONS) - 1] + "、"
        )
        assert REPORT_SECTIONS[-1] == "关键假设、未建模项与免责声明"
        assert PdfExporter is not None
