"""声明式字段绑定（规范 §97–§104、§144）。

思路：用一个"字段规格（:class:`FieldSpec`）"描述每个输入项，自动生成控件并双向绑定到
``Project`` 的某个字段路径（如 ``pv.equivalent_hours``）。这样：

* 新增参数只需加一行规格，不必手写控件代码；
* 比例类字段统一按百分数显示、按小数存储（规范 §14）；
* 可按参数来源着色（§144：蓝=用户输入、绿=计算、黄=假设、灰=政策）。

**本模块不含任何计算公式。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from ..domain.enums import SourceType

#: 参数来源配色（规范 §144）
SOURCE_COLORS: dict[SourceType, str] = {
    SourceType.USER_INPUT: "#DDEBF7",
    SourceType.CALCULATED: "#E2EFDA",
    SourceType.POLICY: "#EDEDED",
    SourceType.CONTRACT: "#DDEBF7",
    SourceType.HISTORICAL: "#DDEBF7",
    SourceType.EXPERIENCE: "#FFF2CC",
    SourceType.ASSUMPTION: "#FFF2CC",
    SourceType.SYSTEM_DEFAULT: "#FFF2CC",
}


class Kind(StrEnum):
    """字段类型。"""

    FLOAT = "float"
    INT = "int"
    PERCENT = "percent"
    """界面按百分数显示，内部存小数（规范 §14）。"""

    TEXT = "text"
    CHOICE = "choice"
    BOOL = "bool"
    OPTIONAL_FLOAT = "optional_float"
    """可留空：勾选后填写，不勾选则为 ``None``（表示"由系统推导/不适用"）。"""

    OPTIONAL_INT = "optional_int"


@dataclass(frozen=True)
class FieldSpec:
    """一个输入字段的规格。"""

    path: str
    label: str
    kind: Kind = Kind.FLOAT
    unit: str = ""
    minimum: float = 0.0
    maximum: float = 1e12
    decimals: int = 4
    step: float = 1.0
    choices: tuple[tuple[str, str], ...] = ()
    source: SourceType = SourceType.USER_INPUT
    tooltip: str = ""
    optional_label: str = "填写"

    @property
    def is_nullable(self) -> bool:
        return self.kind in (Kind.OPTIONAL_FLOAT, Kind.OPTIONAL_INT)


@dataclass
class SectionSpec:
    """一组字段（对应界面上的一个分组/页签）。"""

    title: str
    fields: list[FieldSpec] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 路径访问
# --------------------------------------------------------------------------- #
def get_path(obj: Any, path: str) -> Any:
    current = obj
    for part in path.split("."):
        current = getattr(current, part)
    return current


def set_path(obj: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        current = getattr(current, part)
    setattr(current, parts[-1], value)


# --------------------------------------------------------------------------- #
# 控件构造
# --------------------------------------------------------------------------- #
class FieldRow:
    """一个字段的控件组合，负责取值/赋值/着色。"""

    def __init__(self, spec: FieldSpec, parent: QWidget | None = None) -> None:
        self.spec = spec
        self.container = QWidget(parent)
        layout = QHBoxLayout(self.container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.checkbox: QCheckBox | None = None
        self.editor: QWidget
        self.unit_label: QLabel | None = None

        if spec.is_nullable:
            self.checkbox = QCheckBox(spec.optional_label, self.container)
            self.checkbox.setToolTip("不勾选表示留空（由系统按电价参数推导或本项目不适用）")
            self.checkbox.toggled.connect(self._on_toggle)
            layout.addWidget(self.checkbox)

        self.editor = self._build_editor(spec, self.container)
        layout.addWidget(self.editor, 1)

        if spec.unit:
            self.unit_label = QLabel(spec.unit, self.container)
            self.unit_label.setMinimumWidth(60)
            layout.addWidget(self.unit_label)

        if spec.tooltip:
            self.container.setToolTip(spec.tooltip)
            self.editor.setToolTip(spec.tooltip)

        self.colorize(spec.source)
        if self.checkbox is not None:
            self._on_toggle(False)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_editor(spec: FieldSpec, parent: QWidget) -> QWidget:
        if spec.kind in (Kind.FLOAT, Kind.PERCENT, Kind.OPTIONAL_FLOAT):
            spin = QDoubleSpinBox(parent)
            spin.setDecimals(spec.decimals if spec.kind != Kind.PERCENT else 2)
            spin.setRange(spec.minimum, spec.maximum)
            spin.setSingleStep(spec.step)
            spin.setGroupSeparatorShown(spec.kind == Kind.FLOAT and spec.maximum > 10000)
            return spin
        if spec.kind in (Kind.INT, Kind.OPTIONAL_INT):
            spin_int = QSpinBox(parent)
            spin_int.setRange(int(spec.minimum), int(min(spec.maximum, 2_000_000_000)))
            spin_int.setSingleStep(max(int(spec.step), 1))
            return spin_int
        if spec.kind == Kind.TEXT:
            return QLineEdit(parent)
        if spec.kind == Kind.BOOL:
            box = QCheckBox(parent)
            return box
        if spec.kind == Kind.CHOICE:
            combo = QComboBox(parent)
            for value, label in spec.choices:
                combo.addItem(label, value)
            return combo
        raise ValueError(f"不支持的字段类型：{spec.kind}")

    def _on_toggle(self, checked: bool) -> None:
        self.editor.setEnabled(checked)

    # ------------------------------------------------------------------ #
    def value(self) -> Any:
        """从控件读取值（已换算为内部单位）。"""
        spec = self.spec
        if spec.is_nullable and self.checkbox is not None and not self.checkbox.isChecked():
            return None
        if spec.kind in (Kind.FLOAT, Kind.OPTIONAL_FLOAT):
            return float(self.editor.value())  # type: ignore[attr-defined]
        if spec.kind == Kind.PERCENT:
            return float(self.editor.value()) / 100.0  # type: ignore[attr-defined]
        if spec.kind in (Kind.INT, Kind.OPTIONAL_INT):
            return int(self.editor.value())  # type: ignore[attr-defined]
        if spec.kind == Kind.TEXT:
            return self.editor.text()  # type: ignore[attr-defined]
        if spec.kind == Kind.BOOL:
            return bool(self.editor.isChecked())  # type: ignore[attr-defined]
        if spec.kind == Kind.CHOICE:
            return self.editor.currentData()  # type: ignore[attr-defined]
        raise ValueError(spec.kind)

    def set_value(self, value: Any) -> None:
        spec = self.spec
        if spec.is_nullable and self.checkbox is not None:
            self.checkbox.setChecked(value is not None)
            if value is None:
                return
        if spec.kind in (Kind.FLOAT, Kind.OPTIONAL_FLOAT):
            self.editor.setValue(float(value or 0.0))  # type: ignore[attr-defined]
        elif spec.kind == Kind.PERCENT:
            self.editor.setValue(float(value or 0.0) * 100.0)  # type: ignore[attr-defined]
        elif spec.kind in (Kind.INT, Kind.OPTIONAL_INT):
            self.editor.setValue(int(value or 0))  # type: ignore[attr-defined]
        elif spec.kind == Kind.TEXT:
            self.editor.setText("" if value is None else str(value))  # type: ignore[attr-defined]
        elif spec.kind == Kind.BOOL:
            self.editor.setChecked(bool(value))  # type: ignore[attr-defined]
        elif spec.kind == Kind.CHOICE:
            data = value.value if hasattr(value, "value") else str(value)
            index = self.editor.findData(data)  # type: ignore[attr-defined]
            if index >= 0:
                self.editor.setCurrentIndex(index)  # type: ignore[attr-defined]

    def colorize(self, source: SourceType) -> None:
        """按参数来源着色（规范 §144）。"""
        color = SOURCE_COLORS.get(source, "#FFFFFF")
        self.editor.setStyleSheet(f"background-color: {color};")
        if self.unit_label is not None:
            self.unit_label.setStyleSheet(f"color: {QColor(color).darker(300).name()};")


class SectionForm(QWidget):
    """由 :class:`FieldSpec` 列表自动生成的表单。"""

    def __init__(self, section: SectionSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QFormLayout

        self.section = section
        self.rows: dict[str, FieldRow] = {}
        layout = QFormLayout(self)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for spec in section.fields:
            row = FieldRow(spec, self)
            self.rows[spec.path] = row
            layout.addRow(f"{spec.label}：", row.container)

    def load(self, project: Any) -> None:
        """把项目字段读入控件。"""
        for path, row in self.rows.items():
            row.set_value(get_path(project, path))

    def apply(self, project: Any) -> list[str]:
        """把控件值写回项目；返回失败字段的提示信息列表。"""
        errors: list[str] = []
        for path, row in self.rows.items():
            try:
                set_path(project, path, row.value())
            except Exception as exc:  # pydantic 校验失败等
                errors.append(f"{row.spec.label}：{exc}")
        return errors
