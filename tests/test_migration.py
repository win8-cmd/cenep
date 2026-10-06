"""``.nep`` 版本迁移测试（V2 §64、§65、§94、§95、§106）。

核心承诺：**V1 项目必须能打开，且既有参数一个都不能被改动。**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cenep.domain.models import Project
from cenep.infrastructure.migration import (
    CALCULATION_ENGINE_VERSION,
    CURRENT_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSIONS,
    MigrationError,
    migrate_project_payload,
    migrate_v1_to_v2,
)
from cenep.infrastructure.project_file import (
    FILE_FORMAT,
    SCHEMA_VERSION,
    ProjectFileError,
    load_project,
    save_project,
)


def _stable_text(path: Path) -> str:
    """规范化文件内容以便比较：剔除每次保存都会变的 ``saved_at``。

    直接比较原始文本会**偶发失败**——两次存盘若跨过 1 秒整，``saved_at`` 就不同
    （该 flaky 已由并行会话复现）。存盘幂等性要断言的是**内容**稳定，不是时间戳。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("saved_at", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _v1_envelope(project: Project, schema: str = "1.0") -> dict:
    """构造一个 V1 风格的 .nep 信封：无 timeseries / migration_notes，schema 为 1.x。"""
    payload = json.loads(project.model_dump_json())
    payload.pop("timeseries", None)
    payload.pop("migration_notes", None)
    payload["schema_version"] = schema
    return {
        "format": FILE_FORMAT,
        "schema_version": schema,
        "app_version": "1.0.0",
        "saved_at": "2026-01-01T00:00:00",
        "project": payload,
    }


class TestSchemaVersionConstants:
    def test_current_is_v2(self):
        """V2 §64：升级为 schema_version = 2.0。"""
        assert CURRENT_SCHEMA_VERSION == "2.0"
        assert SCHEMA_VERSION == CURRENT_SCHEMA_VERSION

    def test_legacy_versions_listed(self):
        assert "1.0" in LEGACY_SCHEMA_VERSIONS

    def test_engine_version_recorded(self):
        """V2 §94：结果必须能追溯到计算引擎版本。"""
        assert CALCULATION_ENGINE_VERSION == "2.0.0"


class TestMigrateV1ToV2:
    def test_adds_timeseries_with_explicit_defaults(self):
        """§65：缺失数据用明确默认值，且时序默认关闭以保持 V1 行为。"""
        migrated, notes = migrate_v1_to_v2({"schema_version": "1.0"})
        ts = migrated["timeseries"]
        assert ts["enabled"] is False
        assert ts["resolution"] == "HOURLY"
        assert ts["balance_tolerance"] == pytest.approx(1e-6)
        assert notes

    def test_does_not_touch_existing_parameters(self, golden_pv):
        """§65 关键约束：不得静默改变原有参数。"""
        original = json.loads(golden_pv.model_dump_json())
        original.pop("timeseries", None)
        original.pop("migration_notes", None)

        migrated, _ = migrate_v1_to_v2(dict(original))

        added = {"timeseries", "migration_notes", "schema_version"}
        for key, value in original.items():
            if key in added:
                continue
            assert migrated[key] == value, f"迁移改动了既有参数：{key}"

    def test_numeric_parameters_identical(self, golden_pv):
        original = json.loads(golden_pv.model_dump_json())
        migrated, _ = migrate_v1_to_v2(json.loads(golden_pv.model_dump_json()))
        for section in ("pv", "storage", "tariff", "investment", "opex", "tax", "load"):
            if section in original:
                assert migrated[section] == original[section], f"{section} 段被改动"

    def test_records_migration_note(self):
        migrated, _ = migrate_v1_to_v2({"schema_version": "1.0"})
        assert migrated["schema_version"] == "2.0"
        assert any("迁移" in n for n in migrated["migration_notes"])

    def test_existing_timeseries_only_gets_default_switch(self):
        payload = {"schema_version": "1.0", "timeseries": {"base_year": 2030}}
        migrated, _ = migrate_v1_to_v2(payload)
        assert migrated["timeseries"]["base_year"] == 2030  # 既有值保留
        assert migrated["timeseries"]["enabled"] is False  # 只补开关


class TestMigrateProjectPayload:
    def test_current_version_passthrough(self):
        payload = {"schema_version": "2.0", "a": 1}
        outcome = migrate_project_payload(payload, "2.0")
        assert outcome.migrated is False
        assert outcome.payload == payload

    @pytest.mark.parametrize("version", ["1.0", "1.1"])
    def test_legacy_versions_migrate(self, version):
        outcome = migrate_project_payload({"schema_version": version}, version)
        assert outcome.migrated is True
        assert outcome.payload["schema_version"] == "2.0"

    def test_unknown_version_raises(self):
        with pytest.raises(MigrationError) as exc:
            migrate_project_payload({"schema_version": "9.9"}, "9.9")
        assert "不受支持" in str(exc.value)

    def test_error_carries_versions(self):
        with pytest.raises(MigrationError) as exc:
            migrate_project_payload({}, "3.1")
        assert exc.value.from_version == "3.1"
        assert exc.value.to_version == "2.0"


class TestLoadProjectMigration:
    def test_v1_file_opens(self, tmp_path, golden_pv):
        """§106 验收项：V1 项目可以打开。"""
        path = tmp_path / "v1.nep"
        path.write_text(
            json.dumps(_v1_envelope(golden_pv), ensure_ascii=False), encoding="utf-8"
        )
        loaded = load_project(path)
        assert loaded.basic_info.project_name == golden_pv.basic_info.project_name
        assert loaded.schema_version == "2.0"
        assert loaded.timeseries.enabled is False
        assert loaded.migration_notes

    def test_v1_project_results_reproducible(self, tmp_path, golden_pv):
        """§1.1 / §106：迁移后的 V1 项目必须复现原结果。"""
        from cenep.calculation.engine import calculation_engine

        baseline = calculation_engine.calculate(golden_pv)

        path = tmp_path / "v1.nep"
        path.write_text(
            json.dumps(_v1_envelope(golden_pv), ensure_ascii=False), encoding="utf-8"
        )
        migrated = calculation_engine.calculate(load_project(path))

        assert migrated.project_irr == pytest.approx(baseline.project_irr, rel=1e-12)
        assert migrated.project_npv == pytest.approx(baseline.project_npv, rel=1e-12)
        assert migrated.lcoe == pytest.approx(baseline.lcoe, rel=1e-12)
        assert migrated.total_capex == pytest.approx(baseline.total_capex, rel=1e-12)

    def test_v1_1_file_opens(self, tmp_path, golden_pv):
        path = tmp_path / "v1_1.nep"
        path.write_text(
            json.dumps(_v1_envelope(golden_pv, schema="1.1"), ensure_ascii=False), encoding="utf-8"
        )
        assert load_project(path).schema_version == "2.0"

    def test_unknown_version_rejected(self, tmp_path, golden_pv):
        envelope = _v1_envelope(golden_pv, schema="9.9")
        path = tmp_path / "bad.nep"
        path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ProjectFileError, match="版本不兼容"):
            load_project(path)


class TestSaveMetadata:
    def test_envelope_carries_all_versions(self, tmp_path, golden_pv):
        """§95：项目保存必须记录四个版本号。"""
        path = save_project(golden_pv, tmp_path / "项目")
        env = json.loads(path.read_text(encoding="utf-8"))
        for key in ("schema_version", "calculation_version", "policy_version", "tariff_version"):
            assert key in env, f"信封缺少版本字段 {key}"
        assert env["schema_version"] == "2.0"
        assert env["calculation_version"] == CALCULATION_ENGINE_VERSION

    def test_migration_then_save_round_trip(self, tmp_path, golden_pv):
        """V1 → 迁移 → 另存 V2 → 再读，全链路稳定。"""
        v1 = tmp_path / "v1.nep"
        v1.write_text(json.dumps(_v1_envelope(golden_pv), ensure_ascii=False), encoding="utf-8")

        migrated = load_project(v1)
        saved = save_project(migrated, tmp_path / "v2")
        again = load_project(saved)

        assert again.schema_version == "2.0"
        assert again.basic_info.project_name == golden_pv.basic_info.project_name
        assert again.timeseries.enabled is False
        # 二次存盘应幂等（比较内容，剔除会随时间变化的 saved_at）
        resaved = save_project(again, tmp_path / "v2b")
        assert _stable_text(resaved) == _stable_text(saved)

    def test_project_file_info_reports_v2(self, tmp_path, golden_pv):
        from cenep.infrastructure.project_file import read_file_info

        path = save_project(golden_pv, tmp_path / "项目")
        assert read_file_info(path).schema_version == "2.0"
