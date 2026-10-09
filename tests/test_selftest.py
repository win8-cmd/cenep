"""自检模式测试（规范 §152、§166 的打包前验证）。

自检本身是**打包后验证 EXE 是否完好**的手段，这里先在源码态跑通它。
"""

from __future__ import annotations

import json
from pathlib import Path

from cenep.__main__ import main
from cenep.selftest import run_selftest


class TestSelfTest:
    def test_selftest_passes_and_writes_report(self, tmp_path: Path):
        output = tmp_path / "selftest.json"
        code = run_selftest(str(output))
        assert code == 0
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["ok"] is True
        assert data["errors"] == []
        assert len(data["projects"]) == 3
        assert {p["type"] for p in data["projects"]} == {
            "COMMERCIAL_PV",
            "COMMERCIAL_STORAGE",
            "PV_STORAGE",
        }
        for item in data["projects"]:
            # V2 §67 起工作表由 13 张扩展为 24 张（V1 的 13 张全部保留）；
            # V2.1 §8.1（阶段 2）再新增「账单原始数据」「账单校验」两张；
            # V2.2 §6.3（阶段 4）再新增「消纳率分析」一张，共 27 张。
            assert item["excel_sheets"] == 27
            assert item["pdf_bytes"] > 5000

    def test_selftest_values_match_engine(self, tmp_path: Path):
        """自检报告里的数字必须来自真实引擎（与直接调用引擎结果一致）。"""
        from cenep.calculation.engine import calculation_engine
        from cenep.selftest import _golden_project
        from cenep.domain.enums import ProjectType

        output = tmp_path / "selftest.json"
        run_selftest(str(output))
        data = json.loads(output.read_text(encoding="utf-8"))
        by_type = {p["type"]: p for p in data["projects"]}

        project = _golden_project(ProjectType.PV_STORAGE, "对照")
        result = calculation_engine.calculate(project)
        assert by_type["PV_STORAGE"]["project_irr"] == result.project_irr
        assert by_type["PV_STORAGE"]["total_capex"] == result.total_capex
        assert by_type["PV_STORAGE"]["project_npv"] == result.project_npv

    def test_command_line_entry(self, tmp_path: Path):
        """``python -m cenep --selftest <文件>`` 的返回码必须为 0。"""
        output = tmp_path / "cli.json"
        assert main(["--selftest", str(output)]) == 0
        assert json.loads(output.read_text(encoding="utf-8"))["ok"] is True
