"""项目服务：新建 / 打开 / 保存 / 另存为 / 自动保存（规范 §9、§134）。

GUI 只与本服务交互，不直接碰文件系统与数据库。
"""

from __future__ import annotations

from pathlib import Path

from ..domain.enums import ProjectType
from ..domain.models import BasicInfo, Project
from ..infrastructure.db import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.project_file import (
    autosave as _autosave,
    ensure_suffix,
    load_project,
    read_file_info,
    save_project,
)

logger = get_logger()


class ProjectService:
    """项目管理服务。"""

    def __init__(self, db: Database | None = None, autosave_enabled: bool = True) -> None:
        self.db = db
        self.autosave_enabled = autosave_enabled

    # ------------------------------------------------------------------ #
    # 新建
    # ------------------------------------------------------------------ #
    def new_project(
        self,
        project_type: ProjectType = ProjectType.COMMERCIAL_PV,
        name: str = "未命名项目",
        province: str = "湖北",
        city: str = "",
    ) -> Project:
        """按项目类型新建项目（规范 §2、§13、§15）。

        新建时会按类型清空不相关的容量参数，避免"储能项目里带着光伏容量"这类脏数据。
        """
        project = Project()
        project.basic_info = BasicInfo(
            project_name=name, province=province, city=city, project_type=project_type
        )
        if project_type.has_pv:
            # 系统默认起点值（规范 §84 的 SYSTEM_DEFAULT，用户可改）
            if project.pv.pv_capacity_kwp is None:
                project.pv.pv_capacity_kwp = 1000.0
        else:
            project.pv.pv_capacity_kwp = None
            project.pv.usable_roof_area_m2 = 0.0
            project.pv.equivalent_hours = 0.0
        if project_type.has_storage:
            if project.storage.storage_energy_kwh <= 0:
                project.storage.storage_power_kw = 500.0
                project.storage.storage_energy_kwh = 1000.0
        else:
            project.storage.storage_power_kw = 0.0
            project.storage.storage_energy_kwh = 0.0
        logger.info("新建项目：%s（%s）", name, project_type.label)
        return project

    # ------------------------------------------------------------------ #
    # 打开
    # ------------------------------------------------------------------ #
    def open_project(self, path: str | Path) -> Project:
        project = load_project(path)
        logger.info("打开项目：%s", path)
        if self.db is not None:
            self.db.touch_history(
                path,
                project.basic_info.project_name,
                project.basic_info.project_type.value,
            )
        return project

    def file_info(self, path: str | Path):
        return read_file_info(path)

    # ------------------------------------------------------------------ #
    # 保存
    # ------------------------------------------------------------------ #
    def save_project(self, project: Project, path: str | Path) -> Path:
        saved = save_project(project, path)
        logger.info("保存项目：%s", saved)
        if self.db is not None:
            self.db.touch_history(saved, project.basic_info.project_name, project.basic_info.project_type.value)
        return saved

    def save_project_as(self, project: Project, path: str | Path) -> Path:
        """另存为（规范 §125）：与保存等价，但语义上更换了当前文件。"""
        return self.save_project(project, ensure_suffix(path))

    # ------------------------------------------------------------------ #
    # 自动保存（规范 §134）
    # ------------------------------------------------------------------ #
    def autosave(self, project: Project, path: str | Path) -> Path | None:
        if not self.autosave_enabled:
            return None
        saved = _autosave(project, path)
        logger.info("自动保存：%s", saved)
        return saved

    # ------------------------------------------------------------------ #
    # 最近项目
    # ------------------------------------------------------------------ #
    def recent_projects(self, limit: int = 10) -> list[dict]:
        if self.db is None:
            return []
        return self.db.recent_projects(limit)
