"""政策模块测试（规范 §34–§36、§89、§90、§91、§159）。

核心断言：**内置模板不得预填任何具体数值**，未填写完整时不得生成可用于计算的 PolicyProfile。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cenep.calculation.engine import calculation_engine
from cenep.infrastructure.db import Database
from cenep.policy import HUBEI_TEMPLATE, PolicyStore, PolicyTemplate, PolicyTemplateError
from cenep.policy.hubei import describe_startup_notice
from cenep.policy.template import NUMERIC_FIELD_LABELS


class TestBuiltinTemplate:
    def test_no_prefilled_numeric_values(self):
        """§89、§159：模板不预填任何具体数值。"""
        for field in NUMERIC_FIELD_LABELS:
            assert getattr(HUBEI_TEMPLATE, field) is None, f"{field} 不应预填数值"

    def test_province_is_hubei(self):
        assert HUBEI_TEMPLATE.province == "湖北"
        assert HUBEI_TEMPLATE.policy_id == "HUBEI_NEW_ENERGY_TARIFF"

    def test_not_complete_initially(self):
        assert HUBEI_TEMPLATE.is_complete() is False
        assert len(HUBEI_TEMPLATE.unfilled_numeric_fields()) == len(NUMERIC_FIELD_LABELS)

    def test_missing_description_is_chinese(self):
        text = HUBEI_TEMPLATE.missing_description()
        assert "市场电价" in text
        assert "机制电价" in text


class TestToProfile:
    def test_refuses_when_incomplete(self):
        """§159：政策不确定时不得硬编码，必须先要求用户输入。"""
        with pytest.raises(PolicyTemplateError) as exc:
            HUBEI_TEMPLATE.to_profile()
        assert "尚未填写完整" in str(exc.value)
        assert "market_price" in exc.value.fields

    def test_allow_unfilled_marks_warning(self):
        profile = HUBEI_TEMPLATE.to_profile(allow_unfilled=True)
        assert profile.market_price == 0.0
        assert "未填写警示" in profile.notes
        assert "不得视为正式政策数值" in profile.notes

    def test_converts_when_filled(self):
        filled = HUBEI_TEMPLATE.model_copy(
            update={
                "policy_version": "2025-10-01",
                "effective_date": date(2025, 10, 1),
                "market_price": 0.42,
                "mechanism_price": 0.38,
                "mechanism_volume_ratio": 0.5,
                "green_energy_price": 0.02,
                "green_environmental_value": 0.01,
                "source": "用户按现行政策填写",
                "source_url": "https://www.ndrc.gov.cn/",
            }
        )
        assert filled.is_complete()
        profile = filled.to_profile()
        assert profile.market_price == pytest.approx(0.42)
        assert profile.mechanism_volume_ratio == pytest.approx(0.5)
        assert profile.display_version == f"{filled.policy_name}（版本：2025-10-01）"

    def test_zero_is_distinguishable_from_unfilled(self):
        """0.0（用户确实填了 0）与 None（未填写）必须区分。"""
        filled_zero = HUBEI_TEMPLATE.model_copy(update={"market_price": 0.0})
        assert "market_price" not in filled_zero.unfilled_numeric_fields()

    def test_round_trip_from_profile(self, hubei_policy):
        template = PolicyTemplate.from_profile(hubei_policy)
        assert template.market_price == pytest.approx(0.42)
        back = template.to_profile()
        assert back.policy_version == hubei_policy.policy_version


class TestPolicyStore:
    def test_seeds_builtin_template(self, tmp_path: Path):
        with Database(tmp_path / "t.db") as db:
            store = PolicyStore(db)
            ids = [pid for pid, _ in store.list_templates()]
            assert "HUBEI_NEW_ENERGY_TARIFF" in ids
            loaded = store.load_template("HUBEI_NEW_ENERGY_TARIFF")
            assert loaded.is_complete() is False

    def test_seeding_is_idempotent_and_keeps_user_edits(self, tmp_path: Path):
        path = tmp_path / "t.db"
        with Database(path) as db:
            store = PolicyStore(db)
            edited = store.load_template("HUBEI_NEW_ENERGY_TARIFF").model_copy(
                update={"market_price": 0.5}
            )
            store.save_template(edited)
            PolicyStore(db)  # 再次 seed
            assert store.load_template("HUBEI_NEW_ENERGY_TARIFF").market_price == pytest.approx(0.5)

    def test_publish_requires_version(self, tmp_path: Path):
        with Database(tmp_path / "t.db") as db:
            store = PolicyStore(db)
            filled = store.load_template("HUBEI_NEW_ENERGY_TARIFF").model_copy(
                update={
                    "market_price": 0.42,
                    "mechanism_price": 0.38,
                    "mechanism_volume_ratio": 0.5,
                    "green_energy_price": 0.02,
                    "green_environmental_value": 0.01,
                    "pricing_mechanism": "市场化交易",
                    "source": "测试",
                }
            )
            with pytest.raises(PolicyTemplateError) as exc:
                store.publish(filled)
            # 未填写版本时被拒绝（to_profile 的完整性检查先触发，publish 另有兜底检查）
            assert "政策版本" in str(exc.value)
            assert "policy_version" in exc.value.fields

    def test_versions_accumulate_without_overwriting(self, tmp_path: Path):
        """§35：旧版本只新增、不覆盖。"""
        with Database(tmp_path / "t.db") as db:
            store = PolicyStore(db)
            base = store.load_template("HUBEI_NEW_ENERGY_TARIFF").model_copy(
                update={
                    "market_price": 0.42,
                    "mechanism_price": 0.38,
                    "mechanism_volume_ratio": 0.5,
                    "green_energy_price": 0.02,
                    "green_environmental_value": 0.01,
                    "pricing_mechanism": "市场化交易",
                    "source": "测试来源",
                }
            )
            v1 = store.publish(base.model_copy(update={"policy_version": "2025-10-01"}))
            v2 = store.publish(base.model_copy(update={"policy_version": "2027-01-01", "market_price": 0.36}))

            versions = store.versions("HUBEI_NEW_ENERGY_TARIFF")
            assert [v.policy_version for v in versions] == ["2025-10-01", "2027-01-01"]
            assert versions[0].market_price == pytest.approx(0.42)
            assert store.latest("HUBEI_NEW_ENERGY_TARIFF").policy_version == v2.policy_version
            assert store.db.get_policy("HUBEI_NEW_ENERGY_TARIFF", v1.policy_version) is not None

    def test_attach_does_not_mutate(self, golden_pv_storage, hubei_policy):
        updated = PolicyStore.attach(golden_pv_storage, hubei_policy)
        assert golden_pv_storage.policy is None
        assert updated.policy is not None
        assert updated.basic_info.project_name == golden_pv_storage.basic_info.project_name


class TestStartupNotice:
    def test_without_policy(self, golden_pv_storage):
        notice = describe_startup_notice(golden_pv_storage)
        assert "未关联政策模板" in notice

    def test_with_policy(self, golden_pv_storage, hubei_policy):
        project = golden_pv_storage.model_copy(deep=True)
        project.policy = hubei_policy
        notice = describe_startup_notice(project)
        assert "2025-10-01" in notice
        assert "请确认是否为项目所在地现行政策" in notice


class TestEngineIntegration:
    def test_policy_recorded_in_result(self, golden_pv_storage, hubei_policy):
        project = golden_pv_storage.model_copy(deep=True)
        project.policy = hubei_policy
        result = calculation_engine.calculate(project)
        assert any("本测算采用政策" in n for n in result.notes)
        policy_keys = [k for k, v in result.parameter_sources.items() if v["source_type"] == "POLICY"]
        assert "policy.market_price" in policy_keys
        assert result.parameter_sources["policy.market_price"]["source_name"] == hubei_policy.source
