"""版本化用户侧工商业购电电价计划（V2.3 §2.3、§4.1、§4.2、§7.3）。

本模块**只描述数据长什么样**，不含任何电价计算（V2.1 §1、§0.2：``domain/`` 不放计算逻辑）。
所有"时段匹配、覆盖/冲突检测、价格推导、账单复算"都在
:mod:`cenep.calculation.tariff_plan_engine` 与 :mod:`cenep.calculation.bill_recalculator`。

概念分离（§2.3 明确要求，不得违反）
--------------------------------
* :class:`~cenep.policy.hubei.HUBEI_TEMPLATE` / ``PolicyProfile`` 是**新能源上网电价**政策模板；
* :class:`TariffPlan` 是**用户侧工商业购电电价计划**（含分时时段规则、分项电价、容需量电价）。
  两者概念不同、字段不同、来源不同，**不得互相复用**，也不得把上网电价模板当成购电电价模板。

三条硬性边界（§1 设计边界、§4.1、§4.2）
------------------------------------
1. **政策时段规则与具体电价数值分离**：:class:`TariffTimePeriodRule` 既能表达"时段划分"
   （``months`` / ``start_time`` / ``end_time``），也能承载价格；但价格来源必须由
   :class:`~cenep.domain.enums.PriceBasis` 显式声明——直接单价（权威）或"基础电价 × 浮动系数"。
   同一个时段**不允许**把两者都当成最终价格来源，另一个只能作参考值并在报告中披露差异。
2. **没有来源的电价一律标"待确认"**：所有价格字段都是 ``float | None``；``None`` 表示
   **未填写**，与用户确实填写 ``0.0`` 严格区分（沿用 ``PolicyTemplate`` 既有做法，规范 §91）。
   ``status=VERIFIED`` 的计划必须同时具备适用范围、生效日期、来源与必要价格参数（§2.3）。
3. **不同政策版本必须能共存**：同一电价族可有多个 :class:`TariffPlan`，按"计费日期"选取版本；
   跨政策版本的模拟年度必须按日期取版本，或由用户显式固定一个版本并在报告中披露（§2.3）。
   本项目尤其要求**官方 2026-01 版与项目资料版并存**，不得只留一套（用户明确要求）。

单位口径（§0.2）
--------------
金额 **人民币元**、电量 **kWh**、功率 **kW**、容量 **kVA**；时间用 ``HH:MM`` 字符串表达，
``24:00`` 表示当日 24 时（用于表达 22:00-24:00 这类时段）。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, model_validator

from .base import _Model
from .enums import (
    DayType,
    DemandBillingMode,
    MarketMode,
    PriceBasis,
    TariffComponentType,
    TariffComponentUnit,
    TariffPeriod,
    TariffPlanStatus,
)

__all__ = [
    "DEFAULT_SLOT_MINUTES",
    "MINUTES_PER_DAY",
    "SLOTS_PER_DAY",
    "TARIFF_FIELD_LABELS",
    "TariffPlan",
    "TariffPlanComparison",
    "TariffPlanDiffItem",
    "TariffPlanIssue",
    "TariffPlanValidation",
    "TariffPriceComponent",
    "TariffTimePeriodRule",
    "TimePeriodCoverage",
    "format_hhmm",
    "label_of",
    "parse_hhmm",
]

#: 一天的总分钟数
MINUTES_PER_DAY = 24 * 60
#: 时段覆盖检测的默认步长（分钟）。政策时段以小时为界，账单/合同可能出现半小时，
#: 因此默认按 **30 分钟** 粒度检测"是否覆盖 24 小时"（48 个槽位）。
DEFAULT_SLOT_MINUTES = 30
#: 30 分钟粒度下一天的槽位数
SLOTS_PER_DAY = MINUTES_PER_DAY // DEFAULT_SLOT_MINUTES


def parse_hhmm(text: str) -> int:
    """把 ``HH:MM`` 解析为"当日第几分钟"（0~1440）。

    支持 ``24:00``（= 1440）以表达"当日 24 时"（如官方政策的 22:00-24:00）。

    :raises ValueError: 格式非法或超出范围（**中文**报错，供模型层直接展示）
    """
    raw = str(text or "").strip().replace("：", ":")
    parts = raw.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"时段时刻『{text}』格式不正确，请使用 HH:MM（如 08:30、24:00）")
    hour, minute = int(parts[0]), int(parts[1])
    if not 0 <= hour <= 24 or not 0 <= minute <= 59:
        raise ValueError(f"时段时刻『{text}』超出范围，小时必须为 0~24、分钟必须为 0~59")
    total = hour * 60 + minute
    if total > MINUTES_PER_DAY:
        raise ValueError(f"时段时刻『{text}』超出一天范围，最大为 24:00")
    if total == MINUTES_PER_DAY and minute != 0:
        raise ValueError(f"时段时刻『{text}』不合法，24 时只能写作 24:00")
    return total


def format_hhmm(minutes: int) -> str:
    """把"当日第几分钟"格式化为 ``HH:MM``（1440 → ``24:00``）。"""
    value = int(minutes)
    return f"{value // 60:02d}:{value % 60:02d}"


# --------------------------------------------------------------------------- #
# §2.3 TariffComponent：电价组成分项
# --------------------------------------------------------------------------- #
class TariffPriceComponent(_Model):
    """电价的一个组成分项（V2.3 §2.3、§4.1、§3.4）。

    ``included_in_tou_price`` 与 ``adjustable_by_tou`` 是**两个不同的开关**，不能混为一谈
    （§2.3：不同电价组成项目是否参与峰谷浮动，必须由规则配置明确控制，不能默认对全部分项一起乘倍率）：

    * ``included_in_tou_price``：该分项是否已经包含在分时电度电价里（用于"基础电价"反算与账单核对）；
    * ``adjustable_by_tou``：该分项是否**参与峰谷浮动**。按湖北官方口径，只有
      "代理购电价 + 上网环节线损费用折价"参与浮动，输配电价、系统运行费折价、政府性基金及附加
      不参与浮动（见 :mod:`cenep.policy.hubei_commercial` 的官方口径说明）。
    """

    component_type: TariffComponentType
    name: str = Field(min_length=1, description="分项中文名称（如『电度输配电价』）")
    unit: TariffComponentUnit = TariffComponentUnit.YUAN_PER_KWH
    value: float | None = Field(
        default=None, description="数值；None = **未填写**（不是 0），不得预填无来源数值"
    )
    included_in_tou_price: bool = Field(default=True, description="是否已包含在分时电度电价中")
    adjustable_by_tou: bool = Field(default=False, description="是否参与峰谷浮动")
    source_note: str | None = Field(default=None, description="该分项的出处（文件、页、口径）")


# --------------------------------------------------------------------------- #
# §2.3、§4.2 TimePeriodRule：一条分时时段规则
# --------------------------------------------------------------------------- #
class TariffTimePeriodRule(_Model):
    """一条分时时段规则（V2.3 §2.3、§4.2）。

    支持的能力（逐条对应 §4.2 的要求）：

    ================================  ==================================================
    §4.2 要求                         实现方式
    ================================  ==================================================
    月份范围                          ``months``（空列表 = 全年）
    工作日 / 周末（若政策或合同需要）  ``day_types``（空列表 = 全部日类型）
    跨午夜时段                        ``end_time <= start_time`` 即视为跨午夜（如 22:00-24:00、23:00-07:00）
    尖峰 / 高峰 / 平段 / 低谷（深谷）  ``period`` 取 :class:`~cenep.domain.enums.TariffPeriod`
    直接单价或基础单价 × 浮动倍率      ``price_basis`` + ``direct_price_yuan_per_kwh`` / ``price_multiplier``
    同时段冲突检测                    由 :func:`cenep.calculation.tariff_plan_engine.check_time_period_coverage` 给出中文报告
    缺少时段覆盖检测                   同上
    ================================  ==================================================

    **优先级口径**：``priority`` 大者优先；相同优先级时**后声明的规则覆盖先声明的**
    （与既有 :mod:`cenep.calculation.tariff_series` 的既定语义一致，避免两套引擎行为不一致）。
    """

    period: TariffPeriod
    months: list[int] = Field(default_factory=list, description="生效月份 1~12；空 = 全年")
    day_types: list[DayType] = Field(default_factory=list, description="生效日类型；空 = 全部")
    start_time: str = Field(default="00:00", description="起始时刻 HH:MM（含）")
    end_time: str = Field(default="00:00", description="结束时刻 HH:MM（不含）；24:00 表示当日 24 时")
    price_multiplier: float | None = Field(
        default=None, description="相对『基础电价』的浮动系数（如尖峰 2.0、低谷 0.45）；仅参考或价格来源"
    )
    direct_price_yuan_per_kwh: float | None = Field(
        default=None, description="该时段最终电度电价 元/千瓦时（官方价格表的绝对值）"
    )
    price_basis: PriceBasis = Field(
        default=PriceBasis.DIRECT_PRICE, description="本时段最终价格的来源：直接单价 或 基础电价×浮动系数"
    )
    priority: int = Field(default=0, description="优先级，大者优先；相同优先级时后声明者覆盖")
    note: str = Field(default="", description="口径说明（如『7、8 月执行』）")

    # ------------------------------------------------------------------ #
    # 校验（只拦"结构性错误"，价格缺失交由 validate_tariff_plan 判定）
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _check_shape(self) -> TariffTimePeriodRule:
        bad_months = [m for m in self.months if not 1 <= int(m) <= 12]
        if bad_months:
            raise ValueError(f"时段规则的月份必须在 1~12，实际出现：{bad_months}")
        start = parse_hhmm(self.start_time)
        end = parse_hhmm(self.end_time)
        if start == end:
            raise ValueError(
                f"时段规则『{self.period.label} {self.start_time}-{self.end_time}』时长为 0；"
                "请填写不同的起止时刻（跨午夜时段请把结束时刻写小，如 23:00-07:00）"
            )
        if self.price_basis is PriceBasis.DIRECT_PRICE and self.direct_price_yuan_per_kwh is None:
            # 允许保存草稿（§4.2：价格为空时阻止"正式账单模拟"，允许保存草稿）；
            # "缺价格"本身是**业务校验**，由 calculation 层的 validate_tariff_plan 判定为 ERROR，
            # 不在模型层抛异常，否则用户无法先存草稿再补价。
            pass
        if (
            self.direct_price_yuan_per_kwh is not None
            and float(self.direct_price_yuan_per_kwh) < 0.0
        ):
            raise ValueError(
                f"时段规则『{self.period.label}』的直接单价不能为负（当前 "
                f"{self.direct_price_yuan_per_kwh} 元/千瓦时），请核对官方价格表"
            )
        return self

    # ------------------------------------------------------------------ #
    # 派生（只读，供计算模块使用）
    # ------------------------------------------------------------------ #
    @property
    def start_minute(self) -> int:
        """起始时刻（当日第几分钟）。"""
        return parse_hhmm(self.start_time)

    @property
    def end_minute(self) -> int:
        """结束时刻（当日第几分钟，可为 1440）。"""
        return parse_hhmm(self.end_time)

    @property
    def crosses_midnight(self) -> bool:
        """是否跨午夜（结束时刻不晚于起始时刻）。"""
        return self.end_minute <= self.start_minute

    @property
    def duration_minutes(self) -> int:
        """时段长度（分钟）。跨午夜时按"到次日"计算。"""
        end = self.end_minute
        if self.crosses_midnight:
            end += MINUTES_PER_DAY
        return end - self.start_minute

    @property
    def duration_hours(self) -> float:
        """时段长度（小时），用于报告展示（如官方政策注 2 的"共 7 小时"）。"""
        return self.duration_minutes / 60.0

    def covers_minute(self, minute_of_day: int) -> bool:
        """给定"当日第几分钟"是否落在本时段内（左闭右开，支持跨午夜）。"""
        point = int(minute_of_day) % MINUTES_PER_DAY
        start, end = self.start_minute, self.end_minute
        if end > start:
            return start <= point < end
        return point >= start or point < end

    def applies_on(self, month: int, day_type: DayType | None = None) -> bool:
        """给定月份（可选日类型）是否适用本规则。"""
        if self.months and int(month) not in {int(m) for m in self.months}:
            return False
        if self.day_types and day_type is not None and day_type not in self.day_types:
            return False
        if self.day_types and day_type is None:
            return False
        return True

    def time_range_text(self) -> str:
        """``HH:MM-HH:MM（共 X 小时）``，其中跨午夜标注"次日"。"""
        cross = "（次日）" if self.crosses_midnight else ""
        return f"{self.start_time}-{self.end_time}{cross}（共 {self.duration_hours:g} 小时）"

    def months_text(self) -> str:
        """月份中文描述（空 = 全年；连续月份压缩为区间）。"""
        if not self.months:
            return "全年"
        ordered = sorted({int(m) for m in self.months})
        groups: list[str] = []
        start = previous = ordered[0]
        for month in ordered[1:]:
            if month == previous + 1:
                previous = month
                continue
            groups.append(_month_group_text(start, previous))
            start = previous = month
        groups.append(_month_group_text(start, previous))
        return "、".join(groups)


def _month_group_text(start: int, end: int) -> str:
    if start == end:
        return f"{start} 月"
    return f"{start}-{end} 月"


# --------------------------------------------------------------------------- #
# §2.3 TariffPlan：版本化电价计划
# --------------------------------------------------------------------------- #
class TariffPlan(_Model):
    """一份**版本化**的用户侧工商业购电电价计划（V2.3 §2.3、§4.1、§7.3）。

    **本模型不含任何计算**：不计算覆盖、不推导分时电价、不复算账单。它只承载
    "适用范围 + 生效期间 + 来源与文号 + 时段规则 + 分项电价 + 容需量电价"。

    ``status`` 与价格完整性共同决定"能否用于正式账单复算"（§4.2、§7.4）：
    价格为空、来源缺失、未核验或已过期的计划只能保存为草稿，正式模拟必须由
    :func:`cenep.calculation.tariff_plan_engine.validate_tariff_plan` 拦截。
    """

    tariff_plan_id: str = Field(min_length=1, description="电价计划唯一 ID")
    name: str = Field(min_length=1, description="电价计划名称（含版本，如『湖北 2026-01 代理购电』）")

    province: str = Field(default="湖北", description="适用省份")
    city: str | None = Field(default=None, description="适用城市；None = 全省")

    effective_from: date = Field(description="生效起始日（含）")
    effective_to: date | None = Field(default=None, description="生效结束日（含）；None = 未标注失效")

    source_name: str | None = Field(default=None, description="来源名称（文件名 / 表名 / 文件标题）")
    source_url: str | None = Field(default=None, description="来源链接（**不得编造**）")
    source_document_number: str | None = Field(
        default=None, description="政策文号（如 鄂发改价管〔2024〕77 号）"
    )
    source_fetched_at: date | None = Field(default=None, description="数据抓取日期")
    verified_at: datetime | None = Field(default=None, description="核验时间")
    verified_by: str | None = Field(default=None, description="核验人 / 核验方式")
    status: TariffPlanStatus = Field(
        default=TariffPlanStatus.DRAFT, description="核验状态：草稿 / 已核验 / 已过期"
    )

    applicable_voltage_levels: list[str] = Field(
        default_factory=list, description="适用电压等级（如『110千伏』），空 = 未限定"
    )
    applicable_tariff_structures: list[str] = Field(
        default_factory=list, description="适用计费方式（single_part / two_part），空 = 未限定"
    )
    applicable_customer_types: list[str] = Field(
        default_factory=list, description="适用用户类别（如『工商业两部制』），空 = 未限定"
    )
    market_mode: MarketMode = Field(default=MarketMode.MANUAL, description="购电模式")

    time_period_rules: list[TariffTimePeriodRule] = Field(
        default_factory=list, description="分时时段规则（§4.2）"
    )
    price_components: list[TariffPriceComponent] = Field(
        default_factory=list, description="电价组成分项（§2.3）"
    )

    capacity_charge_yuan_per_kva_month: float | None = Field(
        default=None, description="变压器容量电价 元/千伏安·月"
    )
    demand_charge_yuan_per_kw_month: float | None = Field(
        default=None, description="最大需量电价 元/千瓦·月"
    )
    demand_billing_mode: DemandBillingMode = Field(
        default=DemandBillingMode.DEMAND, description="两部制基本电费计费方式（容量/需量互斥）"
    )

    power_factor_rule_config: dict | None = Field(
        default=None, description="功率因数调整规则（本阶段支持『保留账单实际值』口径）"
    )
    base_price_definition: str = Field(
        default="", description="『基础电价』的口径定义（参与浮动/不参与浮动的分项说明）"
    )
    notes: str = Field(default="", description="口径说明、适用范围限制、待确认事项")
    is_example: bool = Field(
        default=False, description="是否为**示例**数据（示例数据不得当作真实电价，§0.2 红线）"
    )
    user_overridden: bool = Field(
        default=False, description="是否被用户覆盖过（§4.1、§4.2：用户可覆盖政策默认值，必须留痕）"
    )
    override_note: str | None = Field(default=None, description="用户覆盖说明（改了什么、为什么）")

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _check_period_and_scope(self) -> TariffPlan:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(
                f"电价计划『{self.tariff_plan_id}』的失效日期（{self.effective_to}）不能早于"
                f"生效日期（{self.effective_from}），请核对执行时间"
            )
        if self.status is TariffPlanStatus.VERIFIED:
            missing: list[str] = []
            if not (self.source_name or self.source_url or self.source_document_number):
                missing.append("来源（文件名 / 链接 / 文号至少一项）")
            if not self.applicable_voltage_levels:
                missing.append("适用电压等级")
            if not self.applicable_tariff_structures:
                missing.append("适用计费方式（单一制 / 两部制）")
            if missing:
                raise ValueError(
                    "已核验（verified）的电价计划必须具备适用范围与来源，当前缺少："
                    + "、".join(missing)
                    + "。请补齐后重新标记为已核验，或先保存为草稿（draft）"
                )
        return self

    # ------------------------------------------------------------------ #
    # 派生（只读）
    # ------------------------------------------------------------------ #
    @property
    def display_name(self) -> str:
        """报告与界面统一展示名称：``名称（生效期）``。"""
        end = self.effective_to.isoformat() if self.effective_to else "未标注失效"
        return f"{self.name}（{self.effective_from.isoformat()} ~ {end}）"

    @property
    def source_text(self) -> str:
        """来源一行摘要（报告披露用；缺失时明确写"未提供"而不是留空）。"""
        parts = [self.source_name or "未提供来源名称"]
        if self.source_document_number:
            parts.append(f"文号：{self.source_document_number}")
        if self.source_url:
            parts.append(f"链接：{self.source_url}")
        if self.source_fetched_at:
            parts.append(f"抓取日期：{self.source_fetched_at.isoformat()}")
        return "；".join(parts)

    def covers_date(self, day: date) -> bool:
        """给定日期是否落在本计划的生效期间内（含首尾）。"""
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to

    def is_expired_on(self, day: date) -> bool:
        """给定日期是否已超出本计划失效日期（§7.4：过期计划阻断正式模拟）。"""
        return self.effective_to is not None and day > self.effective_to

    @property
    def has_complete_prices(self) -> bool:
        """是否每个已声明时段都拿到了价格（直接单价或浮动系数）。"""
        if not self.time_period_rules:
            return False
        for rule in self.time_period_rules:
            if rule.price_basis is PriceBasis.DIRECT_PRICE:
                if rule.direct_price_yuan_per_kwh is None:
                    return False
            elif rule.price_multiplier is None:
                return False
        return True

    @property
    def declares_voltage_level(self) -> bool:
        """是否声明了适用电压等级（§4.1 第 6 条：不允许只凭"湖北省"就断定适用某电价）。"""
        return bool(self.applicable_voltage_levels)

    def rule_summary(self) -> list[str]:
        """时段规则的中文逐条摘要，供报告与界面展示。"""
        return [
            f"{rule.months_text()} {rule.period.label}：{rule.time_range_text()}"
            + (f"；{rule.note}" if rule.note else "")
            for rule in self.time_period_rules
        ]


# --------------------------------------------------------------------------- #
# 校验 / 覆盖检测 / 对比 的结果对象（V2.3 §4.2、§7.4）
# --------------------------------------------------------------------------- #
class TariffPlanIssue(_Model):
    """一条电价计划校验问题（V2.3 §4.2、§7.4）。

    级别口径与项目既有对象一致：``ERROR`` 阻断正式模拟 / ``WARNING`` 告警 / ``INFO`` 提示。
    """

    level: str = Field(default="WARNING", description="ERROR / WARNING / INFO")
    code: str = Field(default="", description="规则编号，如 T05（便于测试断言与报告检索）")
    field: str = Field(default="", description="出问题的字段名")
    message: str = Field(default="", description="面向用户的**中文**说明")
    blocked: bool = Field(default=False, description="是否因此阻断正式账单复算")


class TimePeriodCoverage(_Model):
    """某个月的分时时段覆盖检查结果（V2.3 §4.2：冲突检测 + 缺少时段覆盖检测）。

    ``gaps`` / ``overlaps`` 里的时刻一律用 ``HH:MM-HH:MM`` **中文可读**形式描述，
    并且总是给出"共几小时、缺哪几段"，方便用户直接对照官方文件修改。
    """

    month: int = Field(ge=1, le=12, description="月份")
    slot_minutes: int = Field(default=DEFAULT_SLOT_MINUTES, description="检测粒度（分钟）")
    period_hours: dict[str, float] = Field(
        default_factory=dict, description="各时段合计小时数（键为 TariffPeriod.value）"
    )
    covered_hours: float = Field(default=0.0, description="已被任一规则覆盖的小时数")
    gaps: list[str] = Field(default_factory=list, description="未覆盖时段（HH:MM-HH:MM）")
    gap_hours: float = Field(default=0.0, description="未覆盖小时数")
    overlaps: list[str] = Field(default_factory=list, description="重叠时段及涉及规则")
    conflict_slots: int = Field(default=0, description="落在多条规则上的槽位数")
    messages: list[str] = Field(default_factory=list, description="中文说明")

    @property
    def is_complete(self) -> bool:
        """是否无缺口且无冲突（§7.7 验收：所有分时电价时间段覆盖完整，无重叠）。"""
        return not self.gaps and not self.overlaps and self.gap_hours <= 1e-9


class TariffPlanValidation(_Model):
    """电价计划的整体校验结论（V2.3 §4.2、§7.4、§7.7）。"""

    tariff_plan_id: str = ""
    plan_name: str = ""
    status: TariffPlanStatus = TariffPlanStatus.DRAFT
    usable_for_formal_use: bool = Field(
        default=False, description="是否可用于**正式**账单复算（价格缺失/未核验/过期时为 False）"
    )
    as_of: date | None = Field(default=None, description="校验基准日（默认取计划生效起始日）")
    coverage: list[TimePeriodCoverage] = Field(default_factory=list)
    issues: list[TariffPlanIssue] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list, description="中文说明（含全部结论）")
    assumptions: list[str] = Field(default_factory=list, description="口径披露（供报告引用）")

    @property
    def has_error(self) -> bool:
        """是否存在 ERROR 级问题。"""
        return any(issue.level == "ERROR" for issue in self.issues)

    def issues_of(self, level: str) -> list[TariffPlanIssue]:
        """按级别筛选问题（大小写不敏感）。"""
        wanted = str(level).upper()
        return [issue for issue in self.issues if issue.level.upper() == wanted]


class TariffPlanDiffItem(_Model):
    """两套电价计划的**一项**逐项差异（V2.3 §4.2、§7.3）。

    ``category`` 取值：``时段`` / ``浮动系数`` / ``电度电价`` / ``容需量电价`` /
    ``适用范围`` / ``来源`` / ``计费方式``。``conclusion`` 是中文结论，直接可用于报告。
    """

    category: str
    item: str = Field(description="差异项名称，如『尖峰时段』『低谷浮动系数』")
    value_a: str = Field(default="", description="计划 A 的取值（中文可读）")
    value_b: str = Field(default="", description="计划 B 的取值（中文可读）")
    difference: str = Field(default="", description="差异说明（含数值差）")


class TariffPlanComparison(_Model):
    """两套电价计划的逐项差异汇总（V2.3 §4.2、§7.3）。

    本项目明确要求"官方版"与"项目资料版"**并存**：本对象用于把两者的差异逐条列出，
    由用户选择、核对、覆盖，**不得只留一套**。
    """

    plan_a_id: str = ""
    plan_a_name: str = ""
    plan_b_id: str = ""
    plan_b_name: str = ""
    items: list[TariffPlanDiffItem] = Field(default_factory=list)
    same_time_periods: bool = Field(default=False, description="时段划分是否完全一致")
    same_float_multipliers: bool = Field(default=False, description="浮动系数是否完全一致")
    messages: list[str] = Field(default_factory=list, description="中文结论")
    assumptions: list[str] = Field(default_factory=list)

    @property
    def has_difference(self) -> bool:
        """是否存在任何差异项。"""
        return bool(self.items)

    def items_of(self, category: str) -> list[TariffPlanDiffItem]:
        """按类别筛选差异项。"""
        return [item for item in self.items if item.category == category]


#: 电价计划字段的中文名（界面、报告、报错统一从这里取，避免各处硬编码）
TARIFF_FIELD_LABELS: dict[str, str] = {
    "tariff_plan_id": "电价计划编号",
    "name": "电价计划名称",
    "province": "适用省份",
    "city": "适用城市",
    "effective_from": "生效起始日",
    "effective_to": "生效结束日",
    "source_name": "来源名称",
    "source_url": "来源链接",
    "source_document_number": "政策文号",
    "source_fetched_at": "数据抓取日期",
    "verified_at": "核验时间",
    "verified_by": "核验人",
    "status": "核验状态",
    "applicable_voltage_levels": "适用电压等级",
    "applicable_tariff_structures": "适用计费方式",
    "applicable_customer_types": "适用用户类别",
    "market_mode": "购电模式",
    "time_period_rules": "分时时段规则",
    "price_components": "电价组成分项",
    "capacity_charge_yuan_per_kva_month": "变压器容量电价（元/千伏安·月）",
    "demand_charge_yuan_per_kw_month": "最大需量电价（元/千瓦·月）",
    "demand_billing_mode": "基本电费计费方式",
    "power_factor_rule_config": "功率因数调整规则",
    "base_price_definition": "基础电价口径",
    "notes": "备注",
    "is_example": "是否示例数据",
    "user_overridden": "是否被用户覆盖",
    "override_note": "覆盖说明",
}


def label_of(field: str) -> str:
    """电价计划字段的中文名（未登记时回退为字段名本身，不抛异常）。"""
    return TARIFF_FIELD_LABELS.get(field, field)
