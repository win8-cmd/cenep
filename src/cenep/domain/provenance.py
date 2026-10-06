"""参数来源与可追溯性（规范 §36、§83–§85、§91）。

每个重要参数都可以挂一份 :class:`ParameterMeta`，记录它的值、单位、来源类型、来源名称、
来源日期、来源链接、是否为假设值、备注。报告必须能区分"实际数据/用户输入/政策参数/合同参数/
历史数据/行业经验/假设值"（规范 §91），**不能把假设数据写成正式事实**。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from .enums import SourceType

#: 参数优先级（规范 §85）：数字越大优先级越高（合同/正式文件 > 政策 > 实测 > 历史 >
#: 用户明确输入 > 行业经验 > 系统默认）。用户明确修改的值仍然优先于系统默认值。
SOURCE_PRIORITY: dict[SourceType, int] = {
    SourceType.SYSTEM_DEFAULT: 0,
    SourceType.EXPERIENCE: 1,
    SourceType.USER_INPUT: 2,
    SourceType.HISTORICAL: 3,
    SourceType.ASSUMPTION: 3,
    SourceType.POLICY: 4,
    SourceType.CONTRACT: 5,
    SourceType.CALCULATED: 6,
}


class ParameterMeta(BaseModel):
    """单个参数的来源记录（规范 §83、§84）。"""

    model_config = ConfigDict(extra="forbid")

    value: float | str | bool | None = None
    unit: str = ""
    source_type: SourceType = SourceType.SYSTEM_DEFAULT
    source_name: str = ""
    source_date: date | None = None
    source_url: str = ""
    is_assumption: bool = False
    note: str = ""
    override_reason: str = ""
    """用户覆盖系统默认值的原因（规范 §85 要求记录）。"""

    @property
    def priority(self) -> int:
        return SOURCE_PRIORITY.get(self.source_type, 0)

    @property
    def color(self) -> str:
        """界面配色（规范 §144）：蓝=用户输入，绿=系统计算，黄=假设，灰=政策模板。"""
        return self.source_type.ui_color


class ParameterRegistry:
    """参数来源登记表。

    计算引擎在开始时按规范 §83 登记所有重要参数；报告层据此输出"参数来源"表（规范 §108）。
    """

    def __init__(self) -> None:
        self._items: dict[str, ParameterMeta] = {}

    def register(self, key: str, meta: ParameterMeta) -> None:
        self._items[key] = meta

    def register_value(
        self,
        key: str,
        value: float | str | bool | None,
        unit: str = "",
        source_type: SourceType = SourceType.SYSTEM_DEFAULT,
        source_name: str = "",
        source_url: str = "",
        is_assumption: bool = False,
        note: str = "",
        source_date: date | None = None,
    ) -> None:
        self.register(
            key,
            ParameterMeta(
                value=value,
                unit=unit,
                source_type=source_type,
                source_name=source_name,
                source_url=source_url,
                is_assumption=is_assumption,
                note=note,
                source_date=source_date,
            ),
        )

    def get(self, key: str) -> ParameterMeta | None:
        return self._items.get(key)

    def items(self) -> dict[str, ParameterMeta]:
        return dict(self._items)

    def assumptions(self) -> dict[str, ParameterMeta]:
        """所有被标记为假设值/系统默认/行业经验的参数（报告需要单独列出）。"""
        return {
            k: v
            for k, v in self._items.items()
            if v.is_assumption
            or v.source_type in (SourceType.ASSUMPTION, SourceType.SYSTEM_DEFAULT, SourceType.EXPERIENCE)
        }

    def policy_items(self) -> dict[str, ParameterMeta]:
        return {k: v for k, v in self._items.items() if v.source_type == SourceType.POLICY}

    def to_dict(self) -> dict[str, dict]:
        return {
            k: {
                "value": v.value,
                "unit": v.unit,
                "source_type": v.source_type.value,
                "source_type_label": v.source_type.label,
                "source_name": v.source_name,
                "source_date": v.source_date.isoformat() if v.source_date else "",
                "source_url": v.source_url,
                "is_assumption": v.is_assumption,
                "note": v.note,
                "override_reason": v.override_reason,
            }
            for k, v in self._items.items()
        }


#: 参数单位字典（规范 §14）。GUI 与报告均从此处取单位，避免各处硬编码。
UNITS: dict[str, str] = {
    "power": "kW",
    "pv_capacity": "kWp",
    "storage_energy": "kWh",
    "energy": "kWh",
    "price": "元/kWh",
    "money": "元",
    "area": "m²",
    "time": "年",
    "rate": "小数",
    "ratio": "小数",
}


def unit_of(kind: str) -> str:
    """取单位字符串；未知类型返回空串（不抛异常，便于报表容错）。"""
    return UNITS.get(kind, "")


__all__ = ["ParameterMeta", "ParameterRegistry", "SOURCE_PRIORITY", "UNITS", "unit_of"]


# Field 占位以保持导入一致（pydantic 在部分版本下需要显式使用才不被 lint 标记）
_ = Field
