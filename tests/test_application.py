"""应用服务层测试（规范 §133、§134、§147–§150、§155）。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cenep.application.calculation_service import CalculationService
from cenep.application.project_service import ProjectService
from cenep.calculation.errors import CalculationError
from cenep.domain.enums import ProjectType
from cenep.infrastructure.db import Database


class TestProjectService:
    def test_open_registers_history(self, golden_pv, tmp_path: Path):
        from cenep.infrastructure.project_file import save_project

        path = save_project(golden_pv, tmp_path / "p.nep")
        with Database(tmp_path / "t.db") as db:
            service = ProjectService(db=db, autosave_enabled=False)
            project = service.open_project(path)
            assert project.basic_info.project_name == golden_pv.basic_info.project_name
            recent = service.recent_projects()
            assert len(recent) == 1
            assert recent[0]["path"] == str(path)

    def test_save_and_save_as(self, golden_pv, tmp_path: Path):
        service = ProjectService(autosave_enabled=False)
        first = service.save_project(golden_pv, tmp_path / "a")
        assert first.name == "a.nep"
        second = service.save_project_as(golden_pv, tmp_path / "b.nep")
        assert second.name == "b.nep"
        assert first.exists() and second.exists()

    def test_autosave_can_be_disabled(self, golden_pv, tmp_path: Path):
        service = ProjectService(autosave_enabled=False)
        assert service.autosave(golden_pv, tmp_path / "a.nep") is None

    def test_file_info_via_service(self, golden_pv, tmp_path: Path):
        service = ProjectService(autosave_enabled=False)
        saved = service.save_project(golden_pv, tmp_path / "a.nep")
        assert service.file_info(saved).project_type == "COMMERCIAL_PV"


class TestCalculationService:
    def test_returns_result_and_timing(self, golden_pv_storage):
        service = CalculationService()
        outcome = service.calculate_with_outcome(golden_pv_storage)
        assert outcome.result.project_irr is not None
        assert outcome.elapsed_seconds >= 0.0
        assert outcome.autosaved_to is None  # 未提供文件路径时不做自动保存

    def test_autosave_before_calculation(self, golden_pv_storage, tmp_path: Path):
        """§134：计算前自动保存。"""
        service = CalculationService(project_service=ProjectService(autosave_enabled=True))
        path = tmp_path / "p.nep"
        outcome = service.calculate_with_outcome(golden_pv_storage, path)
        assert outcome.autosaved_to is not None
        assert outcome.autosaved_to.exists()
        assert outcome.autosaved_to.name == "p.autosave.nep"

    def test_calculation_error_is_chinese_and_actionable(self, golden_pv):
        """§132：报错必须指出哪个参数有问题。"""
        project = golden_pv.model_copy(deep=True)
        project.pv.pv_capacity_kwp = None
        project.pv.usable_roof_area_m2 = 0.0
        service = CalculationService()
        with pytest.raises(CalculationError) as exc:
            service.calculate(project)
        assert "光伏装机容量" in str(exc.value)
        assert exc.value.field == "pv.pv_capacity_kwp"

    def test_performance_seconds_level(self, golden_pv_storage):
        """§155：普通电脑上点计算应在几秒内完成。"""
        service = CalculationService()
        started = time.perf_counter()
        service.calculate(golden_pv_storage)
        assert time.perf_counter() - started < 5.0

    def test_result_available_without_scenario_and_sensitivity(self, golden_pv_storage):
        service = CalculationService()
        result = service.calculate(golden_pv_storage, include_scenario=False, include_sensitivity=False)
        assert result.scenarios == []
        assert result.sensitivity == []
        assert result.project_irr is not None


class TestLogging:
    @pytest.fixture(autouse=True)
    def _restore_global_logging(self):
        """测试后还原全局 logger 配置。

        ``setup_logging(..., force=True)`` 会**关闭并替换**全局 logger 的 handler，
        把输出指向 ``tmp_path``。而 ``tmp_path`` 在本测试结束后即被 pytest 删除，
        于是同一进程内**后续测试**写日志时会落到已删除的目录 —— 表现为
        "单独跑通过、全量按时偶发失败"（曾误伤 test_selftest 与 test_optimization）。

        这里保存原 handler/级别/传播设置，测试后精确还原，消除测试间耦合。
        """
        from cenep.infrastructure.logging_setup import get_logger

        logger = get_logger()
        saved_handlers = list(logger.handlers)
        saved_level = logger.level
        saved_propagate = logger.propagate
        try:
            yield
        finally:
            for handler in list(logger.handlers):
                if handler not in saved_handlers:
                    logger.removeHandler(handler)
                    try:
                        handler.close()
                    except Exception:  # noqa: BLE001 - 关闭失败不应影响测试收尾
                        pass
            for handler in saved_handlers:
                if handler not in logger.handlers:
                    logger.addHandler(handler)
            logger.setLevel(saved_level)
            logger.propagate = saved_propagate

    def test_log_file_created(self, tmp_path: Path, golden_pv):
        """§133：日志记录项目加载/计算开始/结束。"""
        from cenep.infrastructure.logging_setup import setup_logging

        logger = setup_logging(tmp_path / "logs", force=True)
        logger.info("测试日志")
        for handler in logger.handlers:
            handler.flush()
        log_file = tmp_path / "logs" / "cenep.log"
        assert log_file.exists()
        assert "测试日志" in log_file.read_text(encoding="utf-8")
