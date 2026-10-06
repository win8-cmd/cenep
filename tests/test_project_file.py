"""``.nep`` 项目文件与 SQLite 库测试（规范 §9、§10、§134）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cenep.domain.enums import ProjectType
from cenep.infrastructure.db import Database
from cenep.infrastructure.project_file import (
    FILE_FORMAT,
    NEP_SUFFIX,
    SCHEMA_VERSION,
    ProjectFileError,
    autosave,
    autosave_path,
    ensure_suffix,
    has_autosave,
    load_project,
    read_file_info,
    recover_autosave,
    save_project,
)


class TestNepRoundTrip:
    def test_save_then_load_preserves_everything(self, golden_pv_storage, tmp_path: Path):
        target = tmp_path / "湖北某工厂屋顶光储"
        saved = save_project(golden_pv_storage, target)
        assert saved.suffix == NEP_SUFFIX
        assert saved.exists()

        loaded = load_project(saved)
        assert loaded.model_dump() == golden_pv_storage.model_dump()

    def test_envelope_fields(self, golden_pv, tmp_path: Path):
        saved = save_project(golden_pv, tmp_path / "p.nep")
        raw = json.loads(saved.read_text(encoding="utf-8"))
        assert raw["format"] == FILE_FORMAT
        assert raw["schema_version"] == SCHEMA_VERSION
        assert "saved_at" in raw and "app_version" in raw
        assert raw["project"]["basic_info"]["project_type"] == "COMMERCIAL_PV"

    def test_ensure_suffix(self, tmp_path: Path):
        assert ensure_suffix(tmp_path / "abc").name == "abc.nep"
        assert ensure_suffix(tmp_path / "abc.nep").name == "abc.nep"
        assert ensure_suffix(tmp_path / "abc.NEP").name == "abc.NEP"

    def test_ensure_suffix_keeps_dots_in_name(self, tmp_path: Path):
        """回归：项目名含小数点的片段（2061.8kWp / V1.2 / 10.5MW）不得被当后缀截掉。

        这类命名在本行业是常态，早期用 ``Path.with_suffix()`` 会把
        ``全屋面2061.8kWp`` 截成 ``全屋面2061.nep``。
        """
        assert ensure_suffix(tmp_path / "全屋面2061.8kWp").name == "全屋面2061.8kWp.nep"
        assert ensure_suffix(tmp_path / "项目V1.2名称").name == "项目V1.2名称.nep"
        assert ensure_suffix(tmp_path / "储能10.5MW").name == "储能10.5MW.nep"

    def test_autosave_path_keeps_dots(self, tmp_path: Path):
        assert autosave_path(tmp_path / "全屋面2061.8kWp.nep").name == "全屋面2061.8kWp.autosave.nep"

    def test_round_trip_with_dotted_name(self, golden_pv, tmp_path: Path):
        saved = save_project(golden_pv, tmp_path / "全屋面2061.8kWp")
        assert saved.name == "全屋面2061.8kWp.nep"
        loaded = load_project(saved)
        assert loaded.basic_info.project_name == golden_pv.basic_info.project_name

    def test_atomic_write_leaves_no_temp_file(self, golden_pv, tmp_path: Path):
        saved = save_project(golden_pv, tmp_path / "p.nep")
        leftovers = list(saved.parent.glob("*.tmp"))
        assert leftovers == []

    def test_overwrite_existing_file(self, golden_pv, tmp_path: Path):
        path = tmp_path / "p.nep"
        save_project(golden_pv, path)
        golden_pv.basic_info.project_name = "改名后的项目"
        save_project(golden_pv, path)
        assert load_project(path).basic_info.project_name == "改名后的项目"


class TestNepErrors:
    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(ProjectFileError) as exc:
            load_project(tmp_path / "not-exists.nep")
        assert "项目文件不存在" in str(exc.value)

    def test_not_json(self, tmp_path: Path):
        path = tmp_path / "bad.nep"
        path.write_text("这不是 JSON", encoding="utf-8")
        with pytest.raises(ProjectFileError) as exc:
            load_project(path)
        assert "合法的 JSON" in str(exc.value)

    def test_wrong_format(self, tmp_path: Path):
        path = tmp_path / "other.nep"
        path.write_text(json.dumps({"foo": 1}), encoding="utf-8")
        with pytest.raises(ProjectFileError) as exc:
            load_project(path)
        assert "不是本软件的项目文件" in str(exc.value)

    def test_schema_version_mismatch(self, golden_pv, tmp_path: Path):
        path = save_project(golden_pv, tmp_path / "p.nep")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["schema_version"] = "9.9"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ProjectFileError) as exc:
            load_project(path)
        assert "版本不兼容" in str(exc.value)

    def test_corrupted_project_payload(self, tmp_path: Path):
        path = tmp_path / "p.nep"
        path.write_text(
            json.dumps({"format": FILE_FORMAT, "schema_version": SCHEMA_VERSION, "project": {"basic_info": 1}}),
            encoding="utf-8",
        )
        with pytest.raises(ProjectFileError) as exc:
            load_project(path)
        assert "校验失败" in str(exc.value)


class TestAutosave:
    def test_autosave_path_convention(self, tmp_path: Path):
        assert autosave_path(tmp_path / "p.nep").name == "p.autosave.nep"

    def test_autosave_and_recover(self, golden_pv, tmp_path: Path):
        """§134：计算前自动保存，可恢复。"""
        path = tmp_path / "p.nep"
        save_project(golden_pv, path)
        golden_pv.basic_info.project_name = "崩溃前改名"
        autosave(golden_pv, path)
        assert has_autosave(path)
        recovered = recover_autosave(path)
        assert recovered.basic_info.project_name == "崩溃前改名"
        # 原文件未被覆盖
        assert load_project(path).basic_info.project_name != "崩溃前改名"


class TestFileInfo:
    def test_read_info(self, golden_pv_storage, tmp_path: Path):
        saved = save_project(golden_pv_storage, tmp_path / "p.nep")
        info = read_file_info(saved)
        assert info.project_name == "湖北某工商业厂房"
        assert info.project_type == "PV_STORAGE"
        assert info.schema_version == SCHEMA_VERSION
        assert info.saved_at


class TestDatabase:
    def test_policy_round_trip(self, tmp_path: Path, hubei_policy):
        with Database(tmp_path / "t.db") as db:
            db.save_policy(hubei_policy)
            items = db.list_policies("湖北")
            assert len(items) == 1
            assert items[0].policy_version == "2025-10-01"
            assert db.get_policy("HUBEI-NEM-2025", "2025-10-01") is not None
            assert db.list_policies("广东") == []

    def test_old_policy_version_not_overwritten(self, tmp_path: Path, hubei_policy):
        """§35：不能直接覆盖旧政策，必须新增版本。"""
        with Database(tmp_path / "t.db") as db:
            db.save_policy(hubei_policy)
            newer = hubei_policy.model_copy(update={"policy_version": "2027-01-01"})
            db.save_policy(newer)
            versions = {p.policy_version for p in db.list_policies()}
            assert versions == {"2025-10-01", "2027-01-01"}

    def test_template_round_trip(self, tmp_path: Path, golden_storage):
        with Database(tmp_path / "t.db") as db:
            db.save_template("储能标准模板", golden_storage)
            assert ("储能标准模板", "COMMERCIAL_STORAGE") in db.list_templates()
            loaded = db.load_template("储能标准模板")
            assert loaded is not None
            assert loaded.storage.storage_energy_kwh == pytest.approx(1000.0)
            assert db.load_template("不存在") is None

    def test_parameter_dictionary(self, tmp_path: Path):
        with Database(tmp_path / "t.db") as db:
            db.set_parameter("pv_capex_per_kw", 3000.0, unit="元/kWp", source_type="EXPERIENCE", note="行业经验值")
            item = db.get_parameter("pv_capex_per_kw")
            assert item is not None
            assert item["value"] == 3000.0
            assert item["unit"] == "元/kWp"
            assert item["source_type"] == "EXPERIENCE"
            assert db.get_parameter("missing") is None

    def test_history_and_recent(self, tmp_path: Path):
        with Database(tmp_path / "t.db") as db:
            db.touch_history("a.nep", "项目A", "COMMERCIAL_PV")
            db.touch_history("b.nep", "项目B", "PV_STORAGE")
            db.touch_history("a.nep", "项目A", "COMMERCIAL_PV")
            recent = db.recent_projects(limit=5)
            assert len(recent) == 2  # 同一路径只保留一条
            assert recent[0]["path"] == "a.nep"

    def test_survives_reopen(self, tmp_path: Path, hubei_policy):
        path = tmp_path / "t.db"
        with Database(path) as db:
            db.save_policy(hubei_policy)
        with Database(path) as db:
            assert len(db.list_policies()) == 1


class TestProjectTypeScoping:
    def test_new_project_of_pv_has_no_storage_capacity(self):
        from cenep.application.project_service import ProjectService

        service = ProjectService(autosave_enabled=False)
        project = service.new_project(ProjectType.COMMERCIAL_PV, name="纯光伏")
        assert project.storage.storage_power_kw == 0.0
        assert project.storage.storage_energy_kwh == 0.0
        assert project.pv.pv_capacity_kwp == pytest.approx(1000.0)

    def test_new_project_of_storage_has_no_pv_capacity(self):
        from cenep.application.project_service import ProjectService

        service = ProjectService(autosave_enabled=False)
        project = service.new_project(ProjectType.COMMERCIAL_STORAGE, name="纯储能")
        assert project.pv.pv_capacity_kwp is None
        assert project.storage.storage_energy_kwh == pytest.approx(1000.0)

    def test_new_project_pv_storage_keeps_both(self):
        from cenep.application.project_service import ProjectService

        service = ProjectService(autosave_enabled=False)
        project = service.new_project(ProjectType.PV_STORAGE, name="光储")
        assert project.pv.pv_capacity_kwp is not None
        assert project.storage.storage_energy_kwh > 0
