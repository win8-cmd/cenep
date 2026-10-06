"""``.nep`` 项目文件读写（规范 §9、§134）。

设计
----
* 每个项目一个独立文件，扩展名 ``.nep``；
* 文件内容是 UTF-8 的 JSON，首层为文件信封，``project`` 字段是 :class:`Project` 的完整序列化；
* 写入采用"临时文件 + 原子替换"，避免崩溃导致文件损坏；
* 计算前可自动保存（§134）。

信封结构::

    {
      "format": "cenep-project",
      "schema_version": "1.0",
      "saved_at": "2026-10-06T12:00:00",
      "app_version": "1.0.0",
      "project": { ... }
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..domain.models import Project

#: 项目文件扩展名（规范 §9）
NEP_SUFFIX = ".nep"
#: 文件格式标识
FILE_FORMAT = "cenep-project"
#: 当前 schema 版本
SCHEMA_VERSION = "1.0"


class ProjectFileError(Exception):
    """项目文件读写错误，携带面向用户的中文说明。"""

    def __init__(self, message: str, path: str | Path | None = None) -> None:
        self.message = message
        self.path = str(path) if path is not None else ""
        super().__init__(message)


@dataclass(frozen=True)
class ProjectFileInfo:
    """项目文件摘要信息，用于"最近打开"列表与窗口标题。"""

    path: Path
    project_name: str
    project_type: str
    saved_at: str
    schema_version: str


def ensure_suffix(path: str | Path) -> Path:
    """确保路径带 ``.nep`` 后缀。

    注意：**不能用** ``Path.with_suffix()``——它会把文件名里最后一个点之后的内容当后缀删掉，
    例如 ``项目V1.2名称`` → ``项目V1.nep``、``全屋面2061.8kWp`` → ``全屋面2061.nep``。
    项目名里带 ``V1.2`` / ``2061.8kWp`` / ``10.5MW`` 是常态，因此这里改为直接拼接。
    """
    p = Path(path)
    if p.suffix.lower() != NEP_SUFFIX:
        p = p.with_name(p.name + NEP_SUFFIX)
    return p


def _atomic_write(path: Path, text: str) -> None:
    """原子写入：先写临时文件，再替换目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - 极端情况下清理失败不影响主流程
                pass


def save_project(project: Project, path: str | Path) -> Path:
    """保存项目到 ``.nep`` 文件，返回实际写入路径（规范 §9）。"""
    target = ensure_suffix(path)
    payload = {
        "format": FILE_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "app_version": __version__,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "project": project.model_dump(mode="json"),
    }
    try:
        _atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError as exc:
        raise ProjectFileError(f"保存项目文件失败：{exc}", target) from exc
    return target


def load_project(path: str | Path) -> Project:
    """从 ``.nep`` 文件读取项目。"""
    source = Path(path)
    if not source.exists():
        raise ProjectFileError("项目文件不存在", source)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProjectFileError(f"项目文件不是合法的 JSON：{exc}", source) from exc
    except OSError as exc:
        raise ProjectFileError(f"读取项目文件失败：{exc}", source) from exc

    if not isinstance(raw, dict) or raw.get("format") != FILE_FORMAT:
        raise ProjectFileError("不是本软件的项目文件（缺少 format 标识）", source)

    schema = str(raw.get("schema_version", ""))
    if schema != SCHEMA_VERSION:
        raise ProjectFileError(
            f"项目文件版本不兼容：文件为 {schema or '未知'}，当前软件支持 {SCHEMA_VERSION}", source
        )

    project_payload = raw.get("project")
    if not isinstance(project_payload, dict):
        raise ProjectFileError("项目文件缺少 project 内容", source)

    try:
        return Project.model_validate(project_payload)
    except Exception as exc:  # pydantic ValidationError 等
        raise ProjectFileError(f"项目内容校验失败：{exc}", source) from exc


def read_file_info(path: str | Path) -> ProjectFileInfo:
    """只读取信封信息，不构造完整 Project（用于最近文件列表）。"""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectFileError(f"读取项目信息失败：{exc}", source) from exc
    project = raw.get("project", {}) if isinstance(raw, dict) else {}
    basic = project.get("basic_info", {}) if isinstance(project, dict) else {}
    return ProjectFileInfo(
        path=source,
        project_name=str(basic.get("project_name", "")),
        project_type=str(basic.get("project_type", "")),
        saved_at=str(raw.get("saved_at", "")) if isinstance(raw, dict) else "",
        schema_version=str(raw.get("schema_version", "")) if isinstance(raw, dict) else "",
    )


def autosave_path(path: str | Path) -> Path:
    """计算前自动保存的目标文件（§134）：在项目文件旁生成 ``.autosave.nep``。"""
    target = ensure_suffix(path)
    return target.with_name(target.stem + ".autosave" + NEP_SUFFIX)


def autosave(project: Project, path: str | Path) -> Path:
    """计算前自动保存（§134），防止软件崩溃导致数据丢失。"""
    return save_project(project, autosave_path(path))


def has_autosave(path: str | Path) -> bool:
    return autosave_path(path).exists()


def recover_autosave(path: str | Path) -> Project:
    """从自动保存文件恢复项目。"""
    return load_project(autosave_path(path))
