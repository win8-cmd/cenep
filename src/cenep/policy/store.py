"""政策模板库（规范 §10、§34–§36、§90）。

职责：把"未填写的模板"与"已填写可用的政策版本"分开存放与管理。

* 模板 → SQLite ``policy_templates``（每个 ``policy_id`` 一条，可反复编辑）
* 已填写版本 → SQLite ``policy_profiles``（同一 ``policy_id`` 多个版本，**只新增不覆盖**）
"""

from __future__ import annotations

from ..domain.models import PolicyProfile, Project
from ..infrastructure.db import Database
from ..infrastructure.logging_setup import get_logger
from .hubei import HUBEI_TEMPLATE, describe_startup_notice
from .template import PolicyTemplate, PolicyTemplateError

logger = get_logger()


class PolicyStore:
    """政策模板与版本管理。"""

    def __init__(self, db: Database, *, seed_builtin: bool = True) -> None:
        self.db = db
        if seed_builtin:
            self.seed_builtin_templates()

    # ------------------------------------------------------------------ #
    # 模板
    # ------------------------------------------------------------------ #
    def seed_builtin_templates(self) -> None:
        """写入内置模板（幂等）。已有同名记录时不覆盖用户修改。"""
        for template in (HUBEI_TEMPLATE,):
            if self.db.load_policy_template(template.policy_id) is None:
                self.save_template(template)

    def save_template(self, template: PolicyTemplate) -> None:
        if not template.policy_id:
            raise PolicyTemplateError("政策模板必须提供 policy_id", fields=["policy_id"])
        self.db.save_policy_template(template.policy_id, template.policy_name, template.model_dump_json())
        logger.info("保存政策模板：%s（%s）", template.policy_id, template.policy_name)

    def load_template(self, policy_id: str) -> PolicyTemplate:
        payload = self.db.load_policy_template(policy_id)
        if payload is None:
            raise PolicyTemplateError(f"未找到政策模板：{policy_id}", fields=["policy_id"])
        return PolicyTemplate.model_validate_json(payload)

    def list_templates(self) -> list[tuple[str, str]]:
        return self.db.list_policy_templates()

    # ------------------------------------------------------------------ #
    # 版本
    # ------------------------------------------------------------------ #
    def publish(self, template: PolicyTemplate) -> PolicyProfile:
        """把填写完成的模板发布为一个政策版本（规范 §35、§90）。

        未填写完整时拒绝发布，避免把空值当成 0 写进报告（规范 §91）。
        """
        profile = template.to_profile()
        if not profile.policy_version:
            raise PolicyTemplateError("发布政策版本前必须填写政策版本号（如 2025-10-01）", fields=["policy_version"])
        self.db.save_policy(profile)
        logger.info("发布政策版本：%s / %s", profile.policy_id, profile.policy_version)
        return profile

    def versions(self, policy_id: str) -> list[PolicyProfile]:
        return self.db.list_policy_versions(policy_id)

    def latest(self, policy_id: str) -> PolicyProfile | None:
        return self.db.latest_policy(policy_id)

    # ------------------------------------------------------------------ #
    # 与项目配合
    # ------------------------------------------------------------------ #
    @staticmethod
    def attach(project: Project, policy: PolicyProfile) -> Project:
        """把政策版本挂到项目上（返回副本，不改原对象）。"""
        updated = project.model_copy(deep=True)
        updated.policy = policy
        return updated

    @staticmethod
    def startup_notice(project: Project) -> str:
        """启动提示文案（规范 §90）。"""
        return describe_startup_notice(project)
