"""PDF 报告导出测试（规范 §110、§111、§154）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.platypus import Paragraph, Table

from cenep.calculation.engine import calculation_engine
from cenep.reports.pdf_exporter import (
    DISCLAIMER,
    DISCLAIMER_2,
    REPORT_SECTIONS,
    PdfExporter,
    register_cjk_font,
)


def _collect_text(flowables) -> str:
    """递归收集文档流中的全部文本（含表格单元格内的 Paragraph）。"""
    parts: list[str] = []
    for item in flowables:
        if isinstance(item, Paragraph):
            parts.append(item.text)
        elif isinstance(item, Table):
            for row in item._cellvalues:
                for cell in row:
                    if isinstance(cell, str):
                        parts.append(cell)
                    else:
                        parts.append(_collect_text([cell]))
    return "\n".join(parts)


@pytest.fixture
def exported(golden_pv_storage, tmp_path: Path):
    result = calculation_engine.calculate(golden_pv_storage)
    path = PdfExporter().export(golden_pv_storage, result, tmp_path / "报告.pdf")
    return golden_pv_storage, result, path


class TestReportStructure:
    def test_eighteen_sections_declared(self):
        """V2 §66 由 V1 §110 的 15 章重组为 16 部分；V2.1 §8.1 新增「账单事实与校验」→ 17 部分；
        V2.2 §6.3（阶段 4）新增「负荷估算与光伏消纳」→ **18 部分**。"""
        assert len(REPORT_SECTIONS) == 18
        assert REPORT_SECTIONS[0] == "项目概况"
        assert REPORT_SECTIONS[-1] == "免责声明"

    def test_v21_bill_section_declared(self):
        """V2.1 §8.1（阶段 2）：账单章节必须声明，且位于「输入参数」之后、「负荷分析」之前。"""
        assert "账单事实与校验" in REPORT_SECTIONS
        assert REPORT_SECTIONS.index("账单事实与校验") == REPORT_SECTIONS.index("输入参数") + 1
        assert REPORT_SECTIONS.index("账单事实与校验") < REPORT_SECTIONS.index("负荷分析")

    def test_v22_load_consumption_section_declared(self):
        """V2.2 §6.3（阶段 4）：负荷估算与光伏消纳章节必须声明，且位于「负荷分析」之后。"""
        assert "负荷估算与光伏消纳" in REPORT_SECTIONS
        assert REPORT_SECTIONS.index("负荷估算与光伏消纳") == REPORT_SECTIONS.index("负荷分析") + 1

    def test_v2_sections_declared(self):
        """V2 §66 新增的 6 个时序章节必须出现在清单中。"""
        for name in ("负荷分析", "PV时序分析", "储能SOC分析", "能源流", "电费分析", "储能收益"):
            assert name in REPORT_SECTIONS, f"缺少 V2 §66 要求的章节：{name}"

    def test_all_sections_present_in_story(self, golden_pv_storage):
        result = calculation_engine.calculate(golden_pv_storage)
        exporter = PdfExporter()
        styles = __import__("cenep.reports.pdf_exporter", fromlist=["_styles"])._styles(register_cjk_font())
        story = exporter.build_story(golden_pv_storage, result, styles)
        text = _collect_text(story)
        # 封面标题、"一、项目概况"… 依次出现
        assert "工商业新能源项目经济评价报告" in text
        for section in REPORT_SECTIONS[1:]:
            keyword = section
            assert keyword in text, f"报告缺少章节：{section}"


class TestFileOutput:
    def test_pdf_created(self, exported):
        _, _, path = exported
        assert path.exists()
        assert path.suffix == ".pdf"
        assert path.stat().st_size > 5000

    def test_suffix_appended(self, golden_pv_storage, tmp_path: Path):
        result = calculation_engine.calculate(golden_pv_storage)
        path = PdfExporter().export(golden_pv_storage, result, tmp_path / "无后缀")
        assert path.name == "无后缀.pdf"

    def test_dotted_name_not_truncated(self, golden_pv_storage, tmp_path: Path):
        """回归：文件名里的 2061.8kWp / V1.2 这类片段不得被 with_suffix 截掉。"""
        result = calculation_engine.calculate(golden_pv_storage)
        path = PdfExporter().export(golden_pv_storage, result, tmp_path / "全屋面2061.8kWp_经济评价报告")
        assert path.name == "全屋面2061.8kWp_经济评价报告.pdf"
        assert path.exists()

    def test_is_real_pdf(self, exported):
        _, _, path = exported
        raw = path.read_bytes()
        assert raw.startswith(b"%PDF")
        assert b"%%EOF" in raw[-2048:]
        assert raw.count(b"/Type /Page") >= 1

    def test_multipage(self, exported):
        _, _, path = exported
        raw = path.read_bytes()
        # 至少封面 + 正文若干页
        assert raw.count(b"/Type /Page") >= 3


class TestDisclaimers:
    def test_disclaimer_text_matches_spec(self):
        assert DISCLAIMER.startswith("本软件用于新能源项目开发阶段的前期经济测算和投资决策辅助")
        assert "不替代项目正式可行性研究" in DISCLAIMER
        assert "不替代" in DISCLAIMER
        assert "政府审批文件" in DISCLAIMER
        assert "应以项目实施时的最新正式政策及实际合同为准" in DISCLAIMER_2

    def test_disclaimer_in_story_twice(self, golden_pv_storage):
        result = calculation_engine.calculate(golden_pv_storage)
        styles = __import__("cenep.reports.pdf_exporter", fromlist=["_styles"])._styles(register_cjk_font())
        story = PdfExporter().build_story(golden_pv_storage, result, styles)
        texts = [f.text for f in story if isinstance(f, Paragraph)]
        assert sum(1 for t in texts if DISCLAIMER in t) == 2  # 封面 + 结尾


class TestValuesComeFromResult:
    def test_irr_appears_in_story(self, golden_pv_storage):
        result = calculation_engine.calculate(golden_pv_storage)
        styles = __import__("cenep.reports.pdf_exporter", fromlist=["_styles"])._styles(register_cjk_font())
        story = PdfExporter().build_story(golden_pv_storage, result, styles)
        text = _collect_text(story)
        assert f"{result.project_irr:.2%}" in text
        assert f"{result.total_capex:,.2f}" in text

    def test_risk_section_does_not_declare_feasible(self, golden_pv_storage):
        """§106：不得通过固定阈值直接判断项目"可行/不可行"。"""
        result = calculation_engine.calculate(golden_pv_storage)
        text = PdfExporter._risk_text(golden_pv_storage, result)
        assert "可行" not in text
        assert "不可行" not in text
        assert "不构成投资决策结论" in text

    def test_risk_section_mentions_sensitivity_ranking(self, golden_pv_storage):
        result = calculation_engine.calculate(golden_pv_storage)
        text = PdfExporter._risk_text(golden_pv_storage, result)
        assert "敏感性" in text
        assert "政策依赖程度" in text
        assert "融资风险" in text


class TestPolicySection:
    def test_without_policy(self, golden_pv_storage):
        result = calculation_engine.calculate(golden_pv_storage)
        styles = __import__("cenep.reports.pdf_exporter", fromlist=["_styles"])._styles(register_cjk_font())
        story = PdfExporter().build_story(golden_pv_storage, result, styles)
        text = _collect_text(story)
        assert "未关联政策模板" in text

    def test_with_policy(self, golden_pv_storage, hubei_policy):
        project = golden_pv_storage.model_copy(deep=True)
        project.policy = hubei_policy
        result = calculation_engine.calculate(project)
        styles = __import__("cenep.reports.pdf_exporter", fromlist=["_styles"])._styles(register_cjk_font())
        story = PdfExporter().build_story(project, result, styles)
        text = _collect_text(story)
        assert "2025-10-01" in text
        assert "本测算采用政策" in text


class TestThreeProjectTypes:
    @pytest.mark.parametrize("fixture_name", ["golden_pv", "golden_storage", "golden_pv_storage"])
    def test_all_three_types_export(self, fixture_name, request, tmp_path: Path):
        project = request.getfixturevalue(fixture_name)
        result = calculation_engine.calculate(project)
        path = PdfExporter().export(project, result, tmp_path / f"{fixture_name}.pdf")
        assert path.exists()
        assert path.stat().st_size > 5000


class TestFont:
    def test_cjk_font_registered(self):
        font = register_cjk_font()
        assert font in {"CENEP-CJK", "STSong-Light", "Helvetica"}
