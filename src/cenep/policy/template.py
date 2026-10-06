"""政策模板层（规范 §34、§36、§90、§159）。

**为什么不直接用 PolicyProfile？**
``PolicyProfile`` 的数值字段是 ``float``（不可为 ``None``）。如果模板把"未填写"写成 ``0.0``，
报告里就会出现"机制电价 0 元/kWh"这种**看起来像事实的假数据**，违反规范 §91
（不能把假设数据写成正式事实）。

因此本模块用 :class:`PolicyTemplate` 承载"可能未填写"的状态，只有用户确认填写完成后
才调用 :meth:`PolicyTemplate.to_profile` 生成 :class:`PolicyProfile`。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from ..domain.models import PolicyProfile


class PolicyTemplateError(Exception):
    """政策模板错误（中文说明，供界面直接提示）。"""

    def __init__(self, message: str, fields: list[str] | None = None) -> None:
        self.message = message
        self.fields = fields or []
        super().__init__(message)


#: 数值字段的中文标签，用于"哪些还没填"的提示
NUMERIC_FIELD_LABELS: dict[str, str] = {
    "market_price": "市场电价（元/kWh）",
    "mechanism_price": "机制电价（元/kWh）",
    "mechanism_volume_ratio": "机制电量比例（小数）",
    "green_energy_price": "绿电价格（元/kWh）",
    "green_environmental_value": "绿色环境价值（元/kWh）",
}

#: 文本字段的中文标签
TEXT_FIELD_LABELS: dict[str, str] = {
    "policy_version": "政策版本",
    "source": "来源",
    "pricing_mechanism": "价格机制说明",
}


class PolicyTemplate(BaseModel):
    """政策模板（允许"未填写"）。

    ``None`` 表示**尚未填写**，与 ``0.0``（用户确实填写了 0）严格区分。
    """

    model_config = ConfigDict(extra="forbid")

    policy_id: str = ""
    policy_name: str = ""
    policy_version: str | None = None
    effective_date: date | None = None
    expiry_date: date | None = None
    province: str = "湖北"

    pricing_mechanism: str | None = None
    market_price: float | None = Field(default=None, ge=0.0)
    mechanism_price: float | None = Field(default=None, ge=0.0)
    mechanism_volume_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    green_energy_price: float | None = Field(default=None, ge=0.0)
    green_environmental_value: float | None = Field(default=None, ge=0.0)

    source: str | None = None
    source_url: str | None = None
    notes: str = ""

    # ------------------------------------------------------------------ #
    # 完整性
    # ------------------------------------------------------------------ #
    def unfilled_numeric_fields(self) -> list[str]:
        """仍为 ``None`` 的数值字段（字段名）。"""
        return [name for name in NUMERIC_FIELD_LABELS if getattr(self, name) is None]

    def unfilled_text_fields(self) -> list[str]:
        """仍为空的文本字段（字段名）。"""
        return [name for name in TEXT_FIELD_LABELS if not (getattr(self, name) or "").strip()]

    def is_complete(self) -> bool:
        """数值字段与关键文本字段是否都已填写。"""
        return not self.unfilled_numeric_fields() and not self.unfilled_text_fields()

    def missing_description(self) -> str:
        """未填写项的中文描述，供界面提示。"""
        parts = [NUMERIC_FIELD_LABELS[f] for f in self.unfilled_numeric_fields()]
        parts += [TEXT_FIELD_LABELS[f] for f in self.unfilled_text_fields()]
        return "、".join(parts)

    # ------------------------------------------------------------------ #
    # 转换
    # ------------------------------------------------------------------ #
    def to_profile(self, *, allow_unfilled: bool = False) -> PolicyProfile:
        """转换为可用于计算的 :class:`PolicyProfile`（规范 §159）。

        未填写完整时**默认拒绝转换**，避免把空值当成 0 写进报告；
        传 ``allow_unfilled=True`` 可强制转换，但转换结果会在 ``notes`` 中自动加上未填写警示。
        """
        missing = self.missing_description()
        if missing and not allow_unfilled:
            raise PolicyTemplateError(
                f"政策模板尚未填写完整，缺少：{missing}。请按项目所在地现行政策填写后再用于测算。",
                fields=self.unfilled_numeric_fields() + self.unfilled_text_fields(),
            )

        notes = self.notes
        if missing:
            notes = f"{notes}\n【未填写警示】以下字段尚未填写，已在测算中按 0 处理，不得视为正式政策数值：{missing}。"

        return PolicyProfile(
            policy_id=self.policy_id,
            policy_name=self.policy_name,
            policy_version=self.policy_version or "",
            effective_date=self.effective_date,
            expiry_date=self.expiry_date,
            province=self.province,
            pricing_mechanism=self.pricing_mechanism or "",
            market_price=self.market_price or 0.0,
            mechanism_price=self.mechanism_price or 0.0,
            mechanism_volume_ratio=self.mechanism_volume_ratio or 0.0,
            green_energy_price=self.green_energy_price or 0.0,
            green_environmental_value=self.green_environmental_value or 0.0,
            source=self.source or "",
            source_url=self.source_url or "",
            notes=notes.strip(),
        )

    @classmethod
    def from_profile(cls, profile: PolicyProfile) -> PolicyTemplate:
        """把已填写的政策实例转回模板（用于编辑）。"""
        return cls(
            policy_id=profile.policy_id,
            policy_name=profile.policy_name,
            policy_version=profile.policy_version or None,
            effective_date=profile.effective_date,
            expiry_date=profile.expiry_date,
            province=profile.province,
            pricing_mechanism=profile.pricing_mechanism or None,
            market_price=profile.market_price,
            mechanism_price=profile.mechanism_price,
            mechanism_volume_ratio=profile.mechanism_volume_ratio,
            green_energy_price=profile.green_energy_price,
            green_environmental_value=profile.green_environmental_value,
            source=profile.source or None,
            source_url=profile.source_url or None,
            notes=profile.notes,
        )
