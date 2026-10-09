"""V2.4 阶段 7：旧项目全链路迁移与回归（规格书 §8.2、§9.4、§12）。

覆盖三类旧项目与四种"缺少新数据"的组合：

1. **V1(1.0) / V1(1.1) → V2.0**：既有参数与计算结果一个都不能变（V2 §65、§1.1）；
2. **V2.0 → V2.1/V2.2/V2.3**：无 ``timeseries`` / 无 ``bills`` / 无 ``load_datasets`` /
   无电价计划的旧项目必须能打开，新增页面显示空状态，**不自动生成任何虚构数据**（§8.2）；
3. **新增字段一律有默认值**：迁移只补缺失字段，绝不覆盖已存在的值。

本文件与既有的 ``tests/test_migration.py``（V2 的 V1→V2 迁移）互补，
**不修改**其任何断言。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cenep.calculation.engine import calculation_engine
from cenep.domain.models import Project
from cenep.infrastructure.migration import (
    BILL_SECTION_SCHEMA_VERSION,
    CURRENT_SCHEMA_VERSION,
    LOAD_SECTION_SCHEMA_VERSION,
    SCENARIO_SECTION_SCHEMA_VERSION,
    ensure_bill_section,
    ensure_sections,
    migrate_project_payload,
)
from cenep.infrastructure.project_file import (
    FILE_FORMAT,
    load_project,
    save_project,
)

#: V2.1 / V2.2 / V2.3 之后才出现的字段（旧项目里应当**全部缺失**）
NEW_FIELDS = (
    "timeseries",
    "bills",
    "load_datasets",
    "active_load_dataset_id",
    "migration_notes",
)


def _legacy_payload(project: Project, *, schema: str = "1.0", drop: tuple[str, ...] = NEW_FIELDS) -> dict:
    """构造旧版项目载荷：删掉全部新增字段，并把 schema_version 改回旧版本。"""
    payload = json.loads(project.model_dump_json())
    for field in drop:
        payload.pop(field, None)
    payload["schema_version"] = schema
    return payload


def _legacy_envelope(project: Project, *, schema: str = "1.0", app_version: str = "1.0.0",
                     drop: tuple[str, ...] = NEW_FIELDS) -> dict:
    """构造旧版 ``.nep`` 信封（V1 风格：信封里也没有 bills_schema_version / load_schema_version）。"""
    return {
        "format": FILE_FORMAT,
        "schema_version": schema,
        "app_version": app_version,
        "saved_at": "2026-01-01T00:00:00",
        "project": _legacy_payload(project, schema=schema, drop=drop),
    }


def _write(path: Path, envelope: dict) -> Path:
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return path


class TestSectionSchemaVersions:
    def test_section_versions_exist(self):
        """§8.2：新增数据必须带 schema/version 版本标识。"""
        assert BILL_SECTION_SCHEMA_VERSION == "1.0"
        assert LOAD_SECTION_SCHEMA_VERSION == "1.0"
        assert SCENARIO_SECTION_SCHEMA_VERSION == "1.0"

    def test_envelope_carries_section_versions(self, tmp_path: Path, golden_pv_storage):
        """信封里必须同时记录 bills / load 两个段的版本号（§8.2、§95）。"""
        path = save_project(golden_pv_storage, tmp_path / "项目")
        env = json.loads(path.read_text(encoding="utf-8"))
        assert env["bills_schema_version"] == BILL_SECTION_SCHEMA_VERSION
        assert env["load_schema_version"] == LOAD_SECTION_SCHEMA_VERSION
        assert env["schema_version"] == CURRENT_SCHEMA_VERSION


class TestEnsureNewSections:
    """新增段补齐的**行为契约**（§8.2：只在缺失时补，绝不覆盖）。"""

    def test_all_missing_fields_get_defaults(self):
        payload = {"schema_version": "2.0"}
        out, notes = ensure_sections(payload)
        assert out["bills"] == []
        assert out["load_datasets"] == []
        assert out["active_load_dataset_id"] == ""
        assert out["scenario"] == {}
        assert out["sensitivity"] == {}
        assert out["parameter_registry"] == {}
        assert len(notes) == 6
        assert any("load_datasets" in note for note in notes)

    def test_idempotent(self):
        payload = {"schema_version": "2.0"}
        once, notes = ensure_sections(payload)
        assert notes
        twice, again = ensure_sections(once)
        assert again == []
        assert twice["bills"] == once["bills"]
        assert twice["migration_notes"] == once["migration_notes"]

    def test_never_overwrites_existing_values(self):
        """已存在的值（哪怕为空或形状更旧）一律原样保留——这是 §8.2 的硬约束。"""
        payload = {
            "bills": [{"bill_id": "B1"}],
            "load_datasets": [{"profile_id": "P1"}],
            "active_load_dataset_id": "P1",
            "scenario": {"deltas": [{"label": "自定义"}]},
            "sensitivity": {"variables": ["generation"]},
            "parameter_registry": {"pv.pv_capacity_kwp": {"value": 1.0}},
        }
        before = json.loads(json.dumps(payload))
        out, notes = ensure_sections(payload)
        assert notes == []
        assert out == before

    def test_bill_helper_delegates_to_same_contract(self):
        once, notes = ensure_bill_section({"schema_version": "1.0"})
        assert once["bills"] == [] and notes
        twice, again = ensure_bill_section(once)
        assert again == [] and twice["bills"] == []


class TestLegacyProjectOpens:
    """§8.2：无 timeseries / 无 bills / 无 load_datasets / 无电价计划的旧项目必须能打开。"""

    @pytest.mark.parametrize("schema", ["1.0", "1.1", "2.0"])
    def test_v1_and_v20_files_open(self, tmp_path: Path, golden_pv_storage, schema):
        path = _write(tmp_path / f"legacy_{schema}.nep", _legacy_envelope(golden_pv_storage, schema=schema))
        loaded = load_project(path)
        assert loaded.schema_version == CURRENT_SCHEMA_VERSION
        assert loaded.bills == []
        assert loaded.load_datasets == []
        assert loaded.active_load_dataset_id == ""
        assert loaded.timeseries.enabled is False
        assert loaded.migration_notes

    def test_empty_state_means_no_fabricated_data(self, tmp_path: Path, golden_pv_storage):
        """§8.2：旧项目缺少账单/负荷数据时**不得自动生成虚构数据**。"""
        path = _write(tmp_path / "v1.nep", _legacy_envelope(golden_pv_storage))
        loaded = load_project(path)
        assert loaded.bills == []
        assert loaded.load_datasets == []
        assert loaded.active_load_dataset_id == ""
        # 报告层所依赖的"账单事实/负荷画像"在空状态下必须无数据可算
        from cenep.application.bill_service import BillService
        from cenep.application.load_profile_service import LoadProfileService

        summary = BillService(loaded).annual_summary()
        assert summary.total_energy_kwh is None
        assert summary.total_amount_yuan is None
        assert summary.monthly == []
        assert LoadProfileService(loaded).datasets() == []
        assert LoadProfileService(loaded).active_dataset() is None

    def test_missing_only_some_fields(self, tmp_path: Path, golden_pv_storage):
        """只缺 bills（V2.0 项目）时，其余段不得被改动。"""
        envelope = _legacy_envelope(golden_pv_storage, schema="2.0", drop=("bills",))
        before = dict(envelope["project"])
        path = _write(tmp_path / "v20.nep", envelope)
        loaded = load_project(path)
        assert loaded.bills == []
        # timeseries / load_datasets 原本就在文件里，值必须一字不改
        assert loaded.load_datasets == []
        after = json.loads(loaded.model_dump_json())
        for key in ("pv", "storage", "tariff", "investment", "opex", "tax", "load", "financing"):
            assert after[key] == before[key], f"{key} 段被迁移改动"

    def test_v1_parameters_untouched(self, tmp_path: Path, golden_pv_storage):
        """§65 / §8.2 关键约束：迁移不得静默改变任何既有参数。"""
        envelope = _legacy_envelope(golden_pv_storage)
        before = json.loads(json.dumps(envelope["project"]))
        path = _write(tmp_path / "v1.nep", envelope)
        loaded = load_project(path)
        after = json.loads(loaded.model_dump_json())

        added = set(NEW_FIELDS) | {"schema_version", "migration_notes"} | {"timeseries"}
        changed = [
            key
            for key, value in before.items()
            if key not in added and after.get(key) != value
        ]
        assert changed == [], f"迁移改动了既有参数：{changed}"
        # timeseries 被补成显式默认段（默认关闭），且不影响 V1 年度模式
        assert after["timeseries"]["enabled"] is False

    def test_results_reproducible_after_full_chain(self, tmp_path: Path, golden_pv_storage):
        """V1 → V2.0 → V2.1/V2.2/V2.3 全链路后，计算结果与原来逐位一致（§1.1、§9.4）。"""
        baseline = calculation_engine.calculate(golden_pv_storage)

        for schema in ("1.0", "1.1", "2.0"):
            path = _write(tmp_path / f"legacy_{schema}.nep", _legacy_envelope(golden_pv_storage, schema=schema))
            migrated = calculation_engine.calculate(load_project(path))
            assert migrated.project_irr == pytest.approx(baseline.project_irr, rel=1e-12)
            assert migrated.equity_irr == pytest.approx(baseline.equity_irr, rel=1e-12)
            assert migrated.project_npv == pytest.approx(baseline.project_npv, rel=1e-12)
            assert migrated.total_capex == pytest.approx(baseline.total_capex, rel=1e-12)
            assert migrated.lcoe == pytest.approx(baseline.lcoe, rel=1e-12)
            assert migrated.lcos == pytest.approx(baseline.lcos, rel=1e-12)

    def test_migration_then_resave_then_reopen(self, tmp_path: Path, golden_pv_storage):
        """迁移 → 另存 → 再读的全链路稳定，且新增段版本号被写入信封。"""
        path = _write(tmp_path / "v1.nep", _legacy_envelope(golden_pv_storage))
        migrated = load_project(path)
        saved = save_project(migrated, tmp_path / "resaved")
        env = json.loads(saved.read_text(encoding="utf-8"))
        assert env["bills_schema_version"] == BILL_SECTION_SCHEMA_VERSION
        assert env["load_schema_version"] == LOAD_SECTION_SCHEMA_VERSION
        again = load_project(saved)
        assert again.bills == []
        assert again.load_datasets == []
        assert again.basic_info.project_name == golden_pv_storage.basic_info.project_name

    def test_unsupported_version_still_rejected(self, tmp_path: Path, golden_pv_storage):
        """§8.2：升级失败时提示用户并保留原文件（不得静默把 9.9 当 2.0）。"""
        from cenep.infrastructure.project_file import ProjectFileError

        path = _write(tmp_path / "bad.nep", _legacy_envelope(golden_pv_storage, schema="9.9"))
        with pytest.raises(ProjectFileError, match="版本不兼容"):
            load_project(path)
        assert path.exists(), "迁移失败不得删除或改写原文件"


class TestLegacyProjectWithoutTariffPlan:
    """§8.2：无电价计划的旧项目（版本库里没有计划时）也要能打开与导出。"""

    def test_no_tariff_plan_in_store_is_survivable(self, tmp_path: Path, golden_pv_storage):
        """空电价版本库时，报告层必须输出说明而不是抛异常。"""
        from cenep.reports.excel_exporter import ExcelExporter
        from cenep.reports.pdf_exporter import PdfExporter, _styles, register_cjk_font

        path = _write(tmp_path / "v1.nep", _legacy_envelope(golden_pv_storage))
        project = load_project(path)
        result = calculation_engine.calculate(project)

        # 不传 plan_ids：报告应列出内置计划摘要或说明，不得报错
        excel = ExcelExporter().export(project, result, tmp_path / "无电价计划.xlsx")
        assert excel.exists()
        pdf = PdfExporter().export(project, result, tmp_path / "无电价计划.pdf")
        assert pdf.exists() and pdf.stat().st_size > 5000
        story = PdfExporter().build_story(project, result, _styles(register_cjk_font()))
        assert story, "报告文档流不得为空"

    def test_unknown_plan_id_does_not_break_report(self, tmp_path: Path, golden_pv_storage):
        """指定了一个不存在的电价计划编号时，报告照常生成（只跳过该版本）。"""
        from cenep.reports.excel_exporter import ExcelExporter

        project = golden_pv_storage
        result = calculation_engine.calculate(project)
        path = ExcelExporter().export(
            project, result, tmp_path / "坏计划.xlsx", plan_ids=["NOT-A-REAL-PLAN-ID"]
        )
        assert path.exists()


class TestMigratePayloadContract:
    def test_current_version_passthrough_unchanged(self):
        outcome = migrate_project_payload({"schema_version": "2.0", "a": 1}, "2.0")
        assert outcome.migrated is False
        assert outcome.payload == {"schema_version": "2.0", "a": 1}

    def test_legacy_migration_only_adds_timeseries(self):
        """``migrate_project_payload`` 的既有契约不变：只补 timeseries 与版本留痕。"""
        payload = {"schema_version": "1.0", "bills": [{"bill_id": "B"}]}
        outcome = migrate_project_payload(payload, "1.0")
        assert outcome.payload["bills"] == [{"bill_id": "B"}]  # 不动既有段
        assert outcome.payload["timeseries"]["enabled"] is False
        assert outcome.payload["schema_version"] == CURRENT_SCHEMA_VERSION
