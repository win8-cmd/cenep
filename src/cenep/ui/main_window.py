"""主窗口：七个页面 + 应用服务编排（规范 §97、§125、§148）。

主窗口是**唯一**知道"如何把界面动作变成一次计算"的地方：
它只调用 :class:`ProjectService` 与 :class:`CalculationService`，自身不含任何公式。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
)
from PySide6.QtGui import QCloseEvent

from .. import __version__
from ..application.calculation_service import CalculationService
from ..application.project_service import ProjectService
from ..calculation.errors import CalculationError
from ..domain.enums import ProjectType
from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.db import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.project_file import NEP_SUFFIX, ProjectFileError, has_autosave, recover_autosave
from ..policy.store import PolicyStore
from ..reports.excel_exporter import ExcelExporter
from ..reports.pdf_exporter import PdfExporter
from .pages import (
    CalculatePage,
    ParametersPage,
    ProjectPage,
    ReportPage,
    ResultPage,
    SensitivityPage,
    SettingsPage,
    TimeSeriesPage,
)

logger = get_logger()


class MainWindow(QMainWindow):
    """CENEP V1 主窗口。"""

    def __init__(
        self,
        project: Project | None = None,
        db_path: str | Path = "cenep.db",
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"CENEP V1 — 工商业新能源项目经济评价软件（v{__version__}）")
        self.resize(1280, 860)

        self.database = Database(db_path)
        self.project_service = ProjectService(db=self.database, autosave_enabled=True)
        self.calculation_service = CalculationService(project_service=self.project_service)
        self.policy_store = PolicyStore(self.database)
        self.excel_exporter = ExcelExporter()
        self.pdf_exporter = PdfExporter()

        self.project: Project = project or self.project_service.new_project(ProjectType.COMMERCIAL_PV)
        self.current_path: Path | None = None
        self.last_result: CalculationResult | None = None
        self._dirty = False

        self._build_ui()
        self._connect()
        self._reload_all()

        if self.current_path is not None and has_autosave(self.current_path):
            answer = QMessageBox.question(
                self,
                "发现自动保存文件",
                "上次计算前的自动保存文件存在，是否恢复？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                try:
                    self.project = recover_autosave(self.current_path)
                    self._reload_all()
                except ProjectFileError as exc:
                    self._show_error("恢复自动保存失败", str(exc))

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self.tabs = QTabWidget(self)
        self.project_page = ProjectPage(self.tabs)
        self.parameters_page = ParametersPage(self.tabs)
        self.calculate_page = CalculatePage(self.tabs)
        self.result_page = ResultPage(self.tabs)
        self.timeseries_page = TimeSeriesPage(self.tabs)
        self.sensitivity_page = SensitivityPage(self.tabs)
        self.report_page = ReportPage(self.tabs)
        self.settings_page = SettingsPage(self.tabs)

        for widget, title in (
            (self.project_page, "项目"),
            (self.parameters_page, "参数"),
            (self.calculate_page, "计算"),
            (self.result_page, "结果"),
            (self.timeseries_page, "时序仿真"),
            (self.sensitivity_page, "敏感性"),
            (self.report_page, "报告"),
            (self.settings_page, "设置"),
        ):
            self.tabs.addTab(widget, title)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage(self.policy_store.startup_notice(self.project))

    def _connect(self) -> None:
        self.project_page.new_project_requested.connect(self.on_new_project)
        self.project_page.open_project_requested.connect(self.on_open_project)
        self.project_page.save_project_requested.connect(self.on_save_project)
        self.project_page.save_as_project_requested.connect(self.on_save_as_project)
        self.project_page.project_type_changed.connect(self.on_project_type_changed)
        self.calculate_page.calculate_requested.connect(self.on_calculate)
        self.report_page.export_excel_requested.connect(self.on_export_excel)
        self.report_page.export_pdf_requested.connect(self.on_export_pdf)

    # ------------------------------------------------------------------ #
    # 数据搬运
    # ------------------------------------------------------------------ #
    def _reload_all(self) -> None:
        self.project_page.load(self.project)
        self.parameters_page.load(self.project)
        self.settings_page.load(self.project)
        self.project_page.set_current_path(self.current_path)
        self.project_page.set_recent(self.project_service.recent_projects())
        self.calculate_page.status_label.setText("尚未计算")
        self.result_page.clear()
        self.statusBar().showMessage(self.policy_store.startup_notice(self.project))

    def collect_project(self) -> tuple[Project, list[str]]:
        """把界面上的值写回项目（返回项目与错误列表）。"""
        errors: list[str] = []
        errors.extend(self.project_page.apply(self.project))
        errors.extend(self.parameters_page.apply(self.project))
        errors.extend(self.settings_page.apply(self.project))
        return self.project, errors

    # ------------------------------------------------------------------ #
    # 文件操作
    # ------------------------------------------------------------------ #
    def on_new_project(self) -> None:
        self.project = self.project_service.new_project(
            self.project_page.project_type(), name="未命名项目", province="湖北"
        )
        self.current_path = None
        self.last_result = None
        self._reload_all()
        self.tabs.setCurrentWidget(self.project_page)

    def on_open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开项目", "", f"CENEP 项目 (*{NEP_SUFFIX});;所有文件 (*)"
        )
        if not path:
            return
        try:
            self.project = self.project_service.open_project(path)
        except ProjectFileError as exc:
            self._show_error("打开项目失败", str(exc))
            return
        self.current_path = Path(path)
        self.last_result = None
        self._reload_all()

    def on_save_project(self) -> None:
        if self.current_path is None:
            self.on_save_as_project()
            return
        project, errors = self.collect_project()
        if errors:
            self._show_error("保存前校验未通过", "\n".join(errors))
            return
        try:
            self.project_service.save_project(project, self.current_path)
        except ProjectFileError as exc:
            self._show_error("保存项目失败", str(exc))
            return
        self.statusBar().showMessage(f"已保存：{self.current_path}")

    def on_save_as_project(self) -> None:
        project, errors = self.collect_project()
        if errors:
            self._show_error("保存前校验未通过", "\n".join(errors))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为", project.basic_info.project_name, f"CENEP 项目 (*{NEP_SUFFIX})"
        )
        if not path:
            return
        try:
            self.current_path = self.project_service.save_project_as(project, path)
        except ProjectFileError as exc:
            self._show_error("保存项目失败", str(exc))
            return
        self.project_page.set_current_path(self.current_path)
        self.statusBar().showMessage(f"已保存：{self.current_path}")

    def on_project_type_changed(self, project_type: ProjectType) -> None:
        """切换项目类型：补齐该类项目必需的系统默认起点值（可修改）。"""
        try:
            self.project.basic_info.project_type = project_type
            if project_type.has_pv and not self.project.pv.pv_capacity_kwp:
                self.project.pv.pv_capacity_kwp = 1000.0
            if project_type.has_storage and self.project.storage.storage_energy_kwh <= 0:
                self.project.storage.storage_power_kw = 500.0
                self.project.storage.storage_energy_kwh = 1000.0
                self.parameters_page.forms["储能参数"].load(self.project)
            self.parameters_page.forms["光伏参数"].load(self.project)
        except Exception as exc:  # pragma: no cover
            self._show_error("切换项目类型失败", str(exc))

    # ------------------------------------------------------------------ #
    # 计算
    # ------------------------------------------------------------------ #
    def on_calculate(self) -> None:
        project, errors = self.collect_project()
        if errors:
            self.calculate_page.error_box.setPlainText("参数校验未通过：\n" + "\n".join(errors))
            self._show_error("参数校验未通过", "\n".join(errors))
            return

        with_scenario, with_sensitivity = self.calculate_page.options()
        try:
            outcome = self.calculation_service.calculate_with_outcome(
                project,
                self.current_path,
                include_scenario=with_scenario,
                include_sensitivity=with_sensitivity,
            )
        except CalculationError as exc:
            self.calculate_page.error_box.setPlainText(f"计算失败：{exc}")
            self.calculate_page.status_label.setText("计算失败")
            self._show_error("计算失败", str(exc))
            return

        self.last_result = outcome.result
        self.calculate_page.status_label.setText(
            f"计算完成，耗时 {outcome.elapsed_seconds:.3f} 秒"
            + (f"；已自动保存至 {outcome.autosaved_to.name}" if outcome.autosaved_to else "")
        )
        self.calculate_page.error_box.setPlainText(
            "计算成功。\n\n口径说明：\n" + "\n".join(f"• {n}" for n in outcome.result.notes)
        )
        self.result_page.show_result(outcome.result)
        self.timeseries_page.show_result(project, outcome.result)
        self.sensitivity_page.show_result(outcome.result)
        self.report_page.show_result(project, outcome.result)
        self.tabs.setCurrentWidget(self.result_page)
        self.statusBar().showMessage(
            f"计算完成：项目 IRR {outcome.result.project_irr if outcome.result.project_irr is None else format(outcome.result.project_irr, '.2%')}"
        )

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #
    def _require_result(self) -> bool:
        if self.last_result is None:
            QMessageBox.information(self, "提示", "请先执行计算，再导出报告。")
            return False
        return True

    def on_export_excel(self) -> None:
        if not self._require_result():
            return
        project, _ = self.collect_project()
        default_name = f"{project.basic_info.project_name}_经济评价.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "导出 Excel", default_name, "Excel (*.xlsx)")
        if not path:
            return
        try:
            target = self.excel_exporter.export(project, self.last_result, path)
        except Exception as exc:  # pragma: no cover - 文件占用等
            self._show_error("导出 Excel 失败", str(exc))
            return
        self.statusBar().showMessage(f"已导出 Excel：{target}")
        QMessageBox.information(self, "导出成功", f"Excel 已导出：\n{target}")

    def on_export_pdf(self) -> None:
        if not self._require_result():
            return
        project, _ = self.collect_project()
        default_name = f"{project.basic_info.project_name}_经济评价报告.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "导出 PDF", default_name, "PDF (*.pdf)")
        if not path:
            return
        try:
            target = self.pdf_exporter.export(project, self.last_result, path)
        except Exception as exc:  # pragma: no cover
            self._show_error("导出 PDF 失败", str(exc))
            return
        self.statusBar().showMessage(f"已导出 PDF：{target}")
        QMessageBox.information(self, "导出成功", f"PDF 已导出：\n{target}")

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #
    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        try:
            self.database.close()
        except Exception:  # pragma: no cover
            pass
        super().closeEvent(event)

    # 供自动化测试调用（不经过对话框）
    def calculate_now(self) -> CalculationResult | None:
        """无对话框地执行一次计算：成功返回结果，校验失败返回 ``None`` 并把原因写到计算页。"""
        project, errors = self.collect_project()
        if errors:
            self.calculate_page.error_box.setPlainText("参数校验未通过：\n" + "\n".join(errors))
            return None
        try:
            outcome = self.calculation_service.calculate_with_outcome(
                project,
                self.current_path,
                include_scenario=self.calculate_page.scenario_check.isChecked(),
                include_sensitivity=self.calculate_page.sensitivity_check.isChecked(),
            )
        except CalculationError as exc:
            self.calculate_page.error_box.setPlainText(f"计算失败：{exc}")
            self.calculate_page.status_label.setText("计算失败")
            return None
        self.last_result = outcome.result
        self.calculate_page.status_label.setText(f"计算完成，耗时 {outcome.elapsed_seconds:.3f} 秒")
        self.result_page.show_result(outcome.result)
        self.timeseries_page.show_result(project, outcome.result)
        self.sensitivity_page.show_result(outcome.result)
        self.report_page.show_result(project, outcome.result)
        return outcome.result


__all__ = ["MainWindow", "Qt"]
