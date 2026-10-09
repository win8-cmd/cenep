"""SQLite 本地库（规范 §10）。

V1 用它保存四类数据：**政策模板、项目模板、参数字典、历史项目索引**。
项目本身的完整数据仍保存在 ``.nep`` 文件中，SQLite 只做索引与模板，避免单点损坏。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ..domain.models import PolicyProfile, Project
# V2.3 阶段 5：用户侧工商业电价计划（与 PolicyProfile 概念分离，见下方注释）
from ..domain.tariff_models import TariffPlan

DEFAULT_DB_NAME = "cenep.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_profiles (
    policy_id       TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    province        TEXT NOT NULL DEFAULT '',
    payload         TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (policy_id, policy_version)
);

CREATE TABLE IF NOT EXISTS policy_templates (
    policy_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_templates (
    name        TEXT PRIMARY KEY,
    project_type TEXT NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parameter_dictionary (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'SYSTEM_DEFAULT',
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL,
    project_name TEXT NOT NULL DEFAULT '',
    project_type TEXT NOT NULL DEFAULT '',
    opened_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tariff_plans (
    tariff_plan_id  TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    province        TEXT NOT NULL DEFAULT '',
    effective_from  TEXT NOT NULL DEFAULT '',
    effective_to    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'draft',
    payload         TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


class Database:
    """轻量 SQLite 封装。所有写入都是幂等 upsert，便于反复导入模板。"""

    def __init__(self, path: str | Path = DEFAULT_DB_NAME) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 政策模板（规范 §34–§36、§90）
    # ------------------------------------------------------------------ #
    def save_policy(self, policy: PolicyProfile) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO policy_profiles "
            "(policy_id, policy_version, province, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                policy.policy_id or policy.policy_name,
                policy.policy_version or "未标注版本",
                policy.province,
                policy.model_dump_json(),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    def list_policies(self, province: str | None = None) -> list[PolicyProfile]:
        sql = "SELECT payload FROM policy_profiles"
        params: tuple = ()
        if province:
            sql += " WHERE province = ?"
            params = (province,)
        sql += " ORDER BY updated_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [PolicyProfile.model_validate_json(r["payload"]) for r in rows]

    def get_policy(self, policy_id: str, policy_version: str) -> PolicyProfile | None:
        row = self._conn.execute(
            "SELECT payload FROM policy_profiles WHERE policy_id = ? AND policy_version = ?",
            (policy_id, policy_version),
        ).fetchone()
        return PolicyProfile.model_validate_json(row["payload"]) if row else None

    def list_policy_versions(self, policy_id: str) -> list[PolicyProfile]:
        """同一政策的全部版本（规范 §35：旧版本只新增、不覆盖）。"""
        rows = self._conn.execute(
            "SELECT payload FROM policy_profiles WHERE policy_id = ? ORDER BY updated_at, rowid",
            (policy_id,),
        ).fetchall()
        return [PolicyProfile.model_validate_json(r["payload"]) for r in rows]

    def latest_policy(self, policy_id: str) -> PolicyProfile | None:
        """同一政策最近一次保存的版本。"""
        row = self._conn.execute(
            "SELECT payload FROM policy_profiles WHERE policy_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (policy_id,),
        ).fetchone()
        return PolicyProfile.model_validate_json(row["payload"]) if row else None

    # ------------------------------------------------------------------ #
    # 政策模板（未填写数值的占位模板）
    # ------------------------------------------------------------------ #
    def save_policy_template(self, policy_id: str, name: str, payload_json: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO policy_templates (policy_id, name, payload, updated_at) VALUES (?, ?, ?, ?)",
            (policy_id, name, payload_json, datetime.now().isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def list_policy_templates(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT policy_id, name FROM policy_templates ORDER BY name"
        ).fetchall()
        return [(r["policy_id"], r["name"]) for r in rows]

    def load_policy_template(self, policy_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT payload FROM policy_templates WHERE policy_id = ?", (policy_id,)
        ).fetchone()
        return row["payload"] if row else None

    # ------------------------------------------------------------------ #
    # 项目模板
    # ------------------------------------------------------------------ #
    def save_template(self, name: str, project: Project) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO project_templates (name, project_type, payload, updated_at) VALUES (?, ?, ?, ?)",
            (
                name,
                project.basic_info.project_type.value,
                project.model_dump_json(),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    def list_templates(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT name, project_type FROM project_templates ORDER BY name"
        ).fetchall()
        return [(r["name"], r["project_type"]) for r in rows]

    def load_template(self, name: str) -> Project | None:
        row = self._conn.execute(
            "SELECT payload FROM project_templates WHERE name = ?", (name,)
        ).fetchone()
        return Project.model_validate_json(row["payload"]) if row else None

    # ------------------------------------------------------------------ #
    # 参数字典
    # ------------------------------------------------------------------ #
    def set_parameter(
        self, key: str, value: object, unit: str = "", source_type: str = "SYSTEM_DEFAULT", note: str = ""
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO parameter_dictionary "
            "(key, value, unit, source_type, note, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), unit, source_type, note, datetime.now().isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def get_parameter(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT key, value, unit, source_type, note FROM parameter_dictionary WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        return {
            "key": row["key"],
            "value": json.loads(row["value"]),
            "unit": row["unit"],
            "source_type": row["source_type"],
            "note": row["note"],
        }

    # ------------------------------------------------------------------ #
    # 历史项目索引
    # ------------------------------------------------------------------ #
    def touch_history(self, path: str | Path, project_name: str = "", project_type: str = "") -> None:
        self._conn.execute(
            "INSERT INTO project_history (path, project_name, project_type, opened_at) VALUES (?, ?, ?, ?)",
            (str(path), project_name, project_type, datetime.now().isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def recent_projects(self, limit: int = 10) -> list[dict]:
        """最近打开的项目，按**最近一次写入顺序**排序（用 MAX(id) 保证同秒内也稳定）。"""
        rows = self._conn.execute(
            "SELECT path, project_name, project_type, MAX(opened_at) AS opened_at, MAX(id) AS last_id "
            "FROM project_history GROUP BY path ORDER BY last_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "path": r["path"],
                "project_name": r["project_name"],
                "project_type": r["project_type"],
                "opened_at": r["opened_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # V2.3 §2.3、§7.3 用户侧工商业购电电价计划（阶段 5 新增，仅追加）
    # ------------------------------------------------------------------ #
    # 说明：与 policy_profiles（新能源上网电价政策）**概念分离**：
    # 前者是发电侧上网电价，后者是用电侧购电电价，不得共用同一张表。
    # ------------------------------------------------------------------ #
    def save_tariff_plan(self, plan: "TariffPlan") -> None:
        """写入/覆盖一份电价计划（按 ``tariff_plan_id`` 主键）。

        .. warning::
           与"政策模板只新增不覆盖"不同，电价计划**允许修订**（例如用户修正价格或补录来源），
           因此这里用 upsert。修订前后的差异应由服务层记录在 ``override_note`` / ``notes`` 中，
           历史版本的追溯依赖 ``tariff_plan_id`` 中带版本期（如 ``..._2026_01_...``）。
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO tariff_plans "
            "(tariff_plan_id, name, province, effective_from, effective_to, status, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.tariff_plan_id,
                plan.name,
                plan.province,
                plan.effective_from.isoformat(),
                plan.effective_to.isoformat() if plan.effective_to else "",
                plan.status.value,
                plan.model_dump_json(),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    def load_tariff_plan(self, tariff_plan_id: str) -> "TariffPlan | None":
        row = self._conn.execute(
            "SELECT payload FROM tariff_plans WHERE tariff_plan_id = ?", (tariff_plan_id,)
        ).fetchone()
        return TariffPlan.model_validate_json(row["payload"]) if row else None

    def list_tariff_plans(self, province: str | None = None) -> list["TariffPlan"]:
        sql = "SELECT payload FROM tariff_plans"
        params: tuple = ()
        if province:
            sql += " WHERE province = ?"
            params = (province,)
        sql += " ORDER BY effective_from, tariff_plan_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [TariffPlan.model_validate_json(row["payload"]) for row in rows]

    def delete_tariff_plan(self, tariff_plan_id: str) -> int:
        """删除一份电价计划，返回删除条数（0 = 原本不存在）。"""
        cursor = self._conn.execute(
            "DELETE FROM tariff_plans WHERE tariff_plan_id = ?", (tariff_plan_id,)
        )
        self._conn.commit()
        return int(cursor.rowcount)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
