"""``.nep`` 项目文件版本迁移（V2 §64、§65、§94、§95）。

V1 项目文件（``schema_version = 1.x``）在 V2 中必须**照常打开**，且
「不得静默改变原有参数」（V2 §65）。因此迁移只做三件事：

1. **补默认值**：为 V2 新增的配置段（``timeseries``）写入**显式**默认值，
   而不是留给 Pydantic 隐式默认，使文件本身自解释；
2. **记录版本**：写入 ``schema_version`` / ``calculation_version`` /
   ``policy_version`` / ``tariff_version``（V2 §95），保证历史结果可追溯（V2 §94）；
3. **留痕**：把迁移过程写进 ``migration_notes``，用户与报告都能看到
   "这个项目是从哪个版本迁过来的"。

绝不做的事：修改任何既有参数值、重算任何结果、删除任何字段。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 当前项目文件版本（V2 §64：``schema_version = "2.0"``）
CURRENT_SCHEMA_VERSION = "2.0"

#: 计算引擎版本（V2 §94：每次结果都要能追溯到引擎版本）
CALCULATION_ENGINE_VERSION = "2.0.0"

#: 能够被迁移到当前版本的旧版本号
LEGACY_SCHEMA_VERSIONS: tuple[str, ...] = ("1.0", "1.1")

#: 允许打开的全部版本
SUPPORTED_SCHEMA_VERSIONS: tuple[str, ...] = (CURRENT_SCHEMA_VERSION, *LEGACY_SCHEMA_VERSIONS)


class MigrationError(Exception):
    """项目文件迁移失败。"""

    def __init__(self, message: str, *, from_version: str = "", to_version: str = "") -> None:
        super().__init__(message)
        self.from_version = from_version
        self.to_version = to_version


@dataclass
class MigrationOutcome:
    """迁移结果。"""

    payload: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    from_version: str = ""
    to_version: str = CURRENT_SCHEMA_VERSION

    @property
    def migrated(self) -> bool:
        return self.from_version != self.to_version


# --------------------------------------------------------------------------- #
# 版本元数据
# --------------------------------------------------------------------------- #
def _record_version_metadata(notes: list[str]) -> None:
    """记录 V2 §95 要求的版本号到迁移说明。

    注意：``schema_version`` / ``calculation_version`` / ``policy_version`` /
    ``tariff_version`` 是 **``.nep`` 信封级**字段，由 :func:`save_project` 写入，
    **不能**塞进项目载荷 —— 项目载荷按 ``Project`` 校验且 ``extra="forbid"``。
    """
    notes.append(
        f"版本元数据将在保存时写入信封：calculation_version={CALCULATION_ENGINE_VERSION}"
    )


# --------------------------------------------------------------------------- #
# V1 (1.x) → V2 (2.0)
# --------------------------------------------------------------------------- #
def migrate_v1_to_v2(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """V1 项目载荷 → V2 载荷（V2 §65）。

    只补 ``timeseries`` 段的显式默认值并记录版本，**不改动任何既有参数**。
    """
    out = dict(payload)
    notes: list[str] = []

    if "timeseries" not in out or not isinstance(out.get("timeseries"), dict):
        # 显式写出 V2 新增段的默认值：默认关闭时序仿真，V1 项目继续走年度模式，
        # 结果与 V1 完全一致（V2 §1.1 兼容性承诺）。
        out["timeseries"] = {
            "enabled": False,
            "resolution": "HOURLY",
            "base_year": 2025,
            "load": {"mode": "ANNUAL_SIMPLE"},
            "pv": {"mode": "EQUIVALENT_HOURS"},
            "tariff": {"profile": {}, "annual_growth_rate": 0.0},
            "dispatch": {},
            "holidays": [],
            "balance_tolerance": 1e-06,
        }
        notes.append("补充 V2 新增段 timeseries（默认 enabled=false，保持 V1 年度模式）")
    else:
        out["timeseries"].setdefault("enabled", False)
        notes.append("项目已含 timeseries 段，仅补齐缺失的开关默认值")

    previous = str(out.get("schema_version", ""))
    out["schema_version"] = CURRENT_SCHEMA_VERSION
    notes.append(f"项目文件版本 {previous or '1.0'} → {CURRENT_SCHEMA_VERSION}")

    existing_notes = out.get("migration_notes")
    if not isinstance(existing_notes, list):
        existing_notes = []
    out["migration_notes"] = [
        *existing_notes,
        f"已从 schema_version {previous or '1.0'} 迁移到 {CURRENT_SCHEMA_VERSION}；"
        "既有参数未被修改，计算结果口径不变。",
    ]
    return out, notes


#: 迁移链：旧版本 → 迁移函数
_MIGRATION_STEPS: dict[str, Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]]] = {
    "1.0": migrate_v1_to_v2,
    "1.1": migrate_v1_to_v2,
}


def migrate_project_payload(payload: dict[str, Any], from_version: str) -> MigrationOutcome:
    """把 ``from_version`` 的项目载荷迁移到当前版本。

    :raises MigrationError: 版本号未知或不受支持（例如 9.9）。
    """
    if from_version == CURRENT_SCHEMA_VERSION:
        return MigrationOutcome(payload=dict(payload), from_version=from_version)

    step = _MIGRATION_STEPS.get(from_version)
    if step is None:
        raise MigrationError(
            f"项目文件版本不受支持：文件为 {from_version or '未知'}，"
            f"当前软件支持 {CURRENT_SCHEMA_VERSION}，可迁移版本 "
            f"{'、'.join(LEGACY_SCHEMA_VERSIONS)}",
            from_version=from_version,
            to_version=CURRENT_SCHEMA_VERSION,
        )

    migrated, notes = step(payload)
    _record_version_metadata(notes)
    return MigrationOutcome(
        payload=migrated, notes=notes, from_version=from_version, to_version=CURRENT_SCHEMA_VERSION
    )


__all__ = [
    "CALCULATION_ENGINE_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "LEGACY_SCHEMA_VERSIONS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "MigrationError",
    "MigrationOutcome",
    "migrate_project_payload",
    "migrate_v1_to_v2",
]
