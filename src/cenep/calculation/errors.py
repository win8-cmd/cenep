"""计算层异常。

规范 §132：任何计算错误都必须告诉用户"哪个参数有问题"，而不是抛出裸 ``ValueError``。
"""

from __future__ import annotations


class CalculationError(Exception):
    """计算/校验错误的基类。

    参数
    ----
    message:
        面向用户的中文说明，例如"储能充电效率不能大于 100%"。
    field:
        出问题的参数名（对应数据模型字段名），便于 GUI 定位到具体控件。
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        self.message = message
        self.field = field
        super().__init__(message)

    def __str__(self) -> str:  # pragma: no cover - 简单透传
        return f"[{self.field}] {self.message}" if self.field else self.message


class ValidationError(CalculationError):
    """用户输入校验失败（规范 §112、§114）。"""


class EnergyBalanceError(CalculationError):
    """能量守恒校验失败（规范 §113），允许误差 1e-6。"""


class ScenarioError(CalculationError):
    """情景分析配置错误（规范 §93：所有情景必须从 BASE 复制）。"""
