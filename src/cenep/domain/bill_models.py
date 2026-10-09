"""月电费账单事实模型与账单校验结果对象（V2.1 §2.1、§3.1、§5.5）。

分层边界（V2.1 §1、§0.2）
------------------------
* 本模块**只描述数据长什么样**，不含任何计算：所有求和、差异、覆盖率、
  平均电价公式都在 :mod:`cenep.calculation.bill_calculator`。
* :class:`ElectricityBill` 保存的是**账单事实**（用户手动录入或 Excel 导入的原始金额与电量），
  **绝不保存**任何模拟/复算结果。账单模拟结果（V2.3 的 ``BillSimulationResult``）必须另建模型，
  不得与账单事实混为一个模型（V2.1 §0.2）。
* 金额字段一律允许为 ``None``：``None`` = **账单未提供该分项**，与 ``0.0``（确实为 0）
  严格区分，禁止自动填 0 后误导用户（V2.1 §2.1）。

单位口径（V2.1 §0.2）
--------------------
金额 **人民币元**、电量 **kWh**、功率 **kW**、容量 **kVA**（合同容量）/ **kWh**（储能容量）。
字段名已带单位后缀；中文名见 :data:`BILL_FIELD_LABELS`。

取值范围策略
------------
**不在模型层使用 ``ge=0`` 硬约束**（与 :class:`~cenep.domain.timeseries.TimeSeriesPoint` 的既有取舍一致）：
负数、缺失、分项不一致等问题必须由 :func:`cenep.calculation.bill_calculator.validate_bill_fields`
统一产出**中文**问题清单（含字段名），供导入预览定位到"工作表 / 行号 / 字段 / 原因"，
而不是把 Pydantic 的英文异常直接抛给用户（V2.1 §0.2、§5.5）。
模型层只拦截**结构性问题**（空 ID、账期倒序、月份与账期不匹配），这些无法由用户"部分修正"。
"""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import Field, model_validator

from .base import _Model
from .enums import (
    BillEnergyPeriod,
    BillQualityStatus,
    BillSourceType,
    TariffStructure,
)

__all__ = [
    "BILL_ENERGY_FIELDS",
    "BILL_ENERGY_SUB_CHARGE_FIELDS",
    "BILL_LEVEL1_CHARGE_FIELDS",
    "BILL_PERIOD_CHARGE_FIELDS",
    "BILL_FIELD_LABELS",
    "ENERGY_PERIOD_FIELDS",
    "MONTH_FORMAT",
    "SIGNED_CHARGE_FIELDS",
    "BillAnnualSummary",
    "BillMonthlySummary",
    "BillQualityScore",
    "BillReconciliation",
    "BillTolerance",
    "BillValidationIssue",
    "ElectricityBill",
    "billing_month_of",
    "duplicate_key_of",
    "make_bill_id",
]

#: 账单月份格式（V2.1 §2.1：``YYYY-MM``）
MONTH_FORMAT = "%Y-%m"

#: 允许出现负值的金额字段（V2.1 §2.1：仅"调整 / 返还"类允许负值，其余不得为负）
SIGNED_CHARGE_FIELDS: tuple[str, ...] = (
    "power_factor_adjustment_yuan",
    "adjustment_charge_yuan",
)

#: **第一层**费用分项：直接相加应等于账单总额（V2.1 §2.1、§3.1）
#:
#: ``energy_charge_yuan`` 是"电度电费合计"，它的明细见
#: :data:`BILL_ENERGY_SUB_CHARGE_FIELDS`——两者**不可同时计入**总额，否则会重复计算。
BILL_LEVEL1_CHARGE_FIELDS: tuple[str, ...] = (
    "energy_charge_yuan",
    "basic_capacity_charge_yuan",
    "demand_charge_yuan",
    "power_factor_adjustment_yuan",
    "other_charge_yuan",
    "vat_yuan",
    "adjustment_charge_yuan",
)

#: **第二层**费用分项：``energy_charge_yuan``（电度电费合计）的明细。
#: 只用于校验一层合计，不重复计入账单总额（V2.1 §2.1、§3.1）。
BILL_ENERGY_SUB_CHARGE_FIELDS: tuple[str, ...] = (
    "market_purchase_charge_yuan",
    "transmission_distribution_charge_yuan",
    "line_loss_charge_yuan",
    "system_operation_charge_yuan",
    "government_fund_charge_yuan",
)

#: 账单中全部费用字段（一层 + 二层 + 增值税等，用于"是否提供了任何金额"的判断）
BILL_PERIOD_CHARGE_FIELDS: tuple[str, ...] = (
    *BILL_LEVEL1_CHARGE_FIELDS,
    *BILL_ENERGY_SUB_CHARGE_FIELDS,
)

#: 账单中全部电量字段（总电量 + 五个分时时段电量）
BILL_ENERGY_FIELDS: tuple[str, ...] = (
    "energy_total_kwh",
    "energy_sharp_kwh",
    "energy_peak_kwh",
    "energy_flat_kwh",
    "energy_valley_kwh",
    "energy_offpeak_kwh",
)

#: 分时时段枚举 → 账单字段名（§2.1：统一枚举定义，避免"深谷/低谷"混淆）
ENERGY_PERIOD_FIELDS: dict[BillEnergyPeriod, str] = {
    period: period.field_name for period in BillEnergyPeriod
}

#: 字段中文名（含单位）。GUI、Excel 模板、导入报错与报告都从这里取，避免各处硬编码。
BILL_FIELD_LABELS: dict[str, str] = {
    "bill_id": "账单编号",
    "project_id": "项目标识",
    "billing_period_start": "账期起（YYYY-MM-DD）",
    "billing_period_end": "账期止（YYYY-MM-DD）",
    "billing_month": "账单月份（YYYY-MM）",
    "meter_id": "计量点编号",
    "customer_name": "客户名称",
    "voltage_level": "电压等级",
    "tariff_structure": "计费方式",
    "contract_capacity_kva": "合同容量（kVA）",
    "billing_demand_kw": "账单计费需量（kW）",
    "energy_total_kwh": "总购电量（kWh）",
    "energy_sharp_kwh": "尖峰电量（kWh）",
    "energy_peak_kwh": "高峰电量（kWh）",
    "energy_flat_kwh": "平段电量（kWh）",
    "energy_valley_kwh": "低谷电量（kWh）",
    "energy_offpeak_kwh": "深谷电量（kWh）",
    "energy_charge_yuan": "电度电费合计（元）",
    "market_purchase_charge_yuan": "代理购电（电能量）电费（元）",
    "transmission_distribution_charge_yuan": "输配电费（元）",
    "line_loss_charge_yuan": "线损费（元）",
    "system_operation_charge_yuan": "系统运行费（元）",
    "government_fund_charge_yuan": "政府性基金及附加（元）",
    "basic_capacity_charge_yuan": "基本电费-按容量（元）",
    "demand_charge_yuan": "基本电费-按需量（元）",
    "power_factor_adjustment_yuan": "功率因数调整电费（元）",
    "other_charge_yuan": "其他费用（元）",
    "vat_yuan": "增值税（元）",
    "adjustment_charge_yuan": "调整 / 补退费（元）",
    "bill_total_yuan": "账单总额（元）",
    "source_type": "数据来源",
    "source_file_name": "来源文件名",
    "source_row_number": "来源行号",
    "notes": "备注",
    "quality_status": "数据质量状态",
}


def label_of(field: str) -> str:
    """字段中文名（未登记时回退为字段名本身，不抛异常，便于报告容错）。"""
    return BILL_FIELD_LABELS.get(field, field)


# --------------------------------------------------------------------------- #
# 账单标识与重复判定（V2.1 §5.5）
# --------------------------------------------------------------------------- #
def billing_month_of(period_start: date) -> str:
    """账单归属月份：取**账期起始日**所在月份（``YYYY-MM``）。

    跨月账单（如 1/26–2/25）按起始月归属，并由
    :attr:`ElectricityBill.is_cross_month` 明确标记，UI 与报告必须显示"跨月"，
    月度聚合不得把跨月账单当成完整自然月（V2.1 §2.1）。
    """
    return period_start.strftime(MONTH_FORMAT)


def _slug(text: str) -> str:
    """把计量点等用户文本压成可读的 ID 片段（保留中文、字母、数字、下划线、连字符）。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "", str(text or "").strip())
    return cleaned[:24]


def make_bill_id(period_start: date, period_end: date, meter_id: str | None = None) -> str:
    """按"账期 + 计量点"生成**确定性**账单编号（V2.1 §5.5）。

    为什么要确定性：重复导入同一份 Excel 时，同一条账单必须得到同一个 ``bill_id``，
    这样"跳过 / 替换"才是可靠的；随机 UUID 会让每次导入都产生新记录，
    从而绕过重复检测、把同一条账单写两遍。

    形如 ``BILL-20260101-20260131-METER001``；未提供计量点时用 ``MAIN`` 占位。
    """
    meter = _slug(meter_id) if meter_id else ""
    return f"BILL-{period_start:%Y%m%d}-{period_end:%Y%m%d}-{meter or 'MAIN'}"


def duplicate_key_of(
    project_id: str,
    billing_period_start: date,
    billing_period_end: date,
    meter_id: str | None,
) -> str:
    """重复判定键：**项目 + 账期（起止）+ 计量点**（V2.1 §5.5）。

    不含金额与电量：同项目、同账期、同计量点的两条记录视为同一条账单
    （即使金额不同，也是同一条账单的两次录入/两个版本）。
    """
    return (
        f"{str(project_id or '').strip()}|"
        f"{billing_period_start:%Y-%m-%d}|{billing_period_end:%Y-%m-%d}|"
        f"{str(meter_id or '').strip()}"
    )


# --------------------------------------------------------------------------- #
# V2.1 §2.1 ElectricityBill：月电费账单事实
# --------------------------------------------------------------------------- #
class ElectricityBill(_Model):
    """单个月电费账单的**事实**记录（V2.1 §2.1）。

    **本模型不含任何计算结果**：不保存"复算电费""模拟电费""节省额"等派生值，
    也不保存电价数值。``quality_status`` / ``quality_messages`` 是**数据质量标记**
    （§2.1 要求字段），只描述"这条账单本身是否自洽"，不是计算结果。

    关键区分（§2.1、§0.2）：

    * ``billing_demand_kw`` 是**账单计费需量**，由计量与计费规则确定，
      **不等于**负荷曲线的最大值；两者不得混用（V2.3 §7.5 才做曲线模拟需量）。
    * 金额字段 ``None`` 表示账单未提供该分项，**不得**自动填 0。
    """

    bill_id: str = Field(min_length=1, description="账单唯一 ID（同项目内唯一）")
    project_id: str = Field(default="", description="所属项目标识")

    billing_period_start: date = Field(description="账期起始日（含）")
    billing_period_end: date = Field(description="账期结束日（含）")
    billing_month: str = Field(
        default="", description=f"账单月份 {MONTH_FORMAT}（留空时按账期起始日推导）"
    )

    meter_id: str | None = Field(default=None, description="计量点编号")
    customer_name: str | None = Field(default=None, description="客户名称")
    voltage_level: str | None = Field(default=None, description="电压等级，如 10kV")

    tariff_structure: TariffStructure = Field(
        default=TariffStructure.UNKNOWN, description="单一制 / 两部制 / 未知"
    )
    contract_capacity_kva: float | None = Field(default=None, description="合同容量 kVA")
    billing_demand_kw: float | None = Field(
        default=None,
        description="账单计费需量 kW（**账单事实**，与负荷曲线最大值不可混淆）",
    )

    # ---- 电量（kWh；None = 账单未提供，不是 0）----
    energy_total_kwh: float | None = Field(default=None, description="账单总购电量 kWh")
    energy_peak_kwh: float | None = Field(default=None, description="高峰电量 kWh")
    energy_sharp_kwh: float | None = Field(default=None, description="尖峰电量 kWh")
    energy_flat_kwh: float | None = Field(default=None, description="平段电量 kWh")
    energy_valley_kwh: float | None = Field(default=None, description="低谷电量 kWh")
    energy_offpeak_kwh: float | None = Field(
        default=None, description="深谷电量 kWh（**不是**低谷：低谷见 energy_valley_kwh）"
    )

    # ---- 费用（元；None = 账单未提供，不是 0）----
    energy_charge_yuan: float | None = Field(default=None, description="电度电费合计 元")
    market_purchase_charge_yuan: float | None = Field(default=None, description="代理购电电费 元")
    transmission_distribution_charge_yuan: float | None = Field(default=None, description="输配电费 元")
    line_loss_charge_yuan: float | None = Field(default=None, description="线损费 元")
    system_operation_charge_yuan: float | None = Field(default=None, description="系统运行费 元")
    government_fund_charge_yuan: float | None = Field(default=None, description="政府性基金及附加 元")
    basic_capacity_charge_yuan: float | None = Field(default=None, description="基本电费（按容量）元")
    demand_charge_yuan: float | None = Field(default=None, description="基本电费（按需量）元")
    power_factor_adjustment_yuan: float | None = Field(
        default=None, description="功率因数调整电费 元（可为负：返还 / 奖励）"
    )
    other_charge_yuan: float | None = Field(default=None, description="其他费用 元")
    vat_yuan: float | None = Field(default=None, description="增值税 元")
    adjustment_charge_yuan: float | None = Field(
        default=None, description="调整 / 补退费 元（可为负，V2.1 §2.1）"
    )
    bill_total_yuan: float | None = Field(default=None, description="账单总额 元")

    # ---- 来源与备注 ----
    source_type: BillSourceType = Field(default=BillSourceType.MANUAL, description="数据来源")
    source_file_name: str | None = Field(default=None, description="来源文件名（Excel 导入）")
    source_row_number: int | None = Field(default=None, ge=1, description="来源行号（Excel 导入）")
    notes: str | None = Field(default=None, description="备注")

    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.now, description="最后修改时间")

    quality_status: BillQualityStatus = Field(
        default=BillQualityStatus.VALID, description="数据质量状态（由校验器写入）"
    )
    quality_messages: list[str] = Field(default_factory=list, description="数据质量中文说明")

    # ------------------------------------------------------------------ #
    # 结构校验（只拦"用户无法部分修正"的问题）
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _check_identity_and_period(self) -> ElectricityBill:
        """账期方向、账单月份与账期一致性（§2.1）。"""
        if self.billing_period_end < self.billing_period_start:
            raise ValueError(
                f"账期结束日期（{self.billing_period_end:%Y-%m-%d}）不能早于起始日期"
                f"（{self.billing_period_start:%Y-%m-%d}），请检查账单账期"
            )
        expected = billing_month_of(self.billing_period_start)
        if not str(self.billing_month or "").strip():
            # 直接写 __dict__：避免 validate_assignment 递归触发本校验器
            self.__dict__["billing_month"] = expected
        elif self.billing_month != expected:
            raise ValueError(
                f"账单月份（{self.billing_month}）与账期不一致：账期"
                f"{self.billing_period_start:%Y-%m-%d} ~ {self.billing_period_end:%Y-%m-%d} "
                f"的账单月份应为 {expected}（账单月份取账期起始日所在月，跨月账单同样如此）"
            )
        return self

    # ------------------------------------------------------------------ #
    # 派生描述（不是计算，只是把已有字段读出来）
    # ------------------------------------------------------------------ #
    @property
    def is_cross_month(self) -> bool:
        """是否跨月账期（§2.1：跨月账单必须在 UI 中明确标注）。"""
        return self.billing_period_start.month != self.billing_period_end.month

    @property
    def period_days(self) -> int:
        """账期天数（含首尾，``end - start + 1``）。"""
        return (self.billing_period_end - self.billing_period_start).days + 1

    @property
    def duplicate_key(self) -> str:
        """重复判定键（§5.5）：项目 + 账期 + 计量点。"""
        return duplicate_key_of(
            self.project_id, self.billing_period_start, self.billing_period_end, self.meter_id
        )

    def touch(self, *, now: datetime | None = None) -> None:
        """更新 ``updated_at``（就地修改，由服务层在编辑后调用）。"""
        self.updated_at = now or datetime.now()

    def describe(self) -> str:
        """一行中文摘要，用于列表与日志。"""
        energy = "未提供" if self.energy_total_kwh is None else f"{self.energy_total_kwh:,.1f} kWh"
        amount = "未提供" if self.bill_total_yuan is None else f"{self.bill_total_yuan:,.2f} 元"
        cross = "（跨月账期）" if self.is_cross_month else ""
        return (
            f"{self.billing_month} 账单 {self.bill_id}：账期 "
            f"{self.billing_period_start:%Y-%m-%d} ~ {self.billing_period_end:%Y-%m-%d}{cross}，"
            f"总电量 {energy}，账单总额 {amount}，来源 {self.source_type.label}"
        )


# --------------------------------------------------------------------------- #
# 校验容差配置（V2.1 §2.1：容差可配置，但不得隐藏差异）
# --------------------------------------------------------------------------- #
class BillTolerance(_Model):
    """账单自洽性校验容差（V2.1 §2.1）。

    默认值与规格书一致：

    * 电量：``max(1 kWh, 0.5% × 总电量)``
    * 金额：``max(1 元, 0.5% × 账单总额)``

    容差**只决定是否升级为警告**；无论是否超出容差，差异数值都会出现在
    :class:`BillReconciliation` 与消息中，不允许"因为没超容差就不显示差异"。
    """

    energy_absolute_kwh: float = Field(default=1.0, ge=0.0, description="电量绝对容差 kWh")
    energy_relative: float = Field(default=0.005, ge=0.0, le=1.0, description="电量相对容差（占总电量）")
    amount_absolute_yuan: float = Field(default=1.0, ge=0.0, description="金额绝对容差 元")
    amount_relative: float = Field(default=0.005, ge=0.0, le=1.0, description="金额相对容差（占总额）")

    def energy_tolerance(self, total_kwh: float | None) -> float:
        """电量容差 = ``max(绝对值, 相对值 × 总电量)``；总电量未知时只取绝对值。"""
        base = abs(float(total_kwh)) if total_kwh is not None else 0.0
        return max(self.energy_absolute_kwh, self.energy_relative * base)

    def amount_tolerance(self, total_yuan: float | None) -> float:
        """金额容差 = ``max(绝对值, 相对值 × 账单总额)``；总额未知时只取绝对值。"""
        base = abs(float(total_yuan)) if total_yuan is not None else 0.0
        return max(self.amount_absolute_yuan, self.amount_relative * base)


# --------------------------------------------------------------------------- #
# 校验与汇总结果对象（V2.1 §2.1、§3.1）
# --------------------------------------------------------------------------- #
class BillValidationIssue(_Model):
    """一条账单校验问题（V2.1 §2.1、§5.5）。

    ``level`` 与既有 :class:`~cenep.domain.timeseries_results.DataQualityIssue`
    保持一致：``ERROR`` 拒收 / ``WARNING`` 告警 / ``INFO`` 提示。
    ``row_number`` 用于让导入预览能定位到"工作表 → 行号 → 字段 → 原因"。
    """

    level: str = Field(default="WARNING", description="ERROR / WARNING / INFO")
    code: str = Field(default="", description="规则编号，如 B04（便于测试断言与报告检索）")
    field: str = Field(default="", description="出问题的字段名")
    message: str = Field(default="", description="面向用户的中文说明")
    source_row_number: int | None = Field(default=None, ge=1, description="Excel 来源行号")


class BillReconciliation(_Model):
    """单条账单的自洽性核对结果（V2.1 §2.1、§3.1）。

    这是**校验结果**，不是账单事实：不写回 :class:`ElectricityBill` 的任何金额/电量字段，
    只把质量状态与说明写进 ``quality_status`` / ``quality_messages``。
    """

    bill_id: str
    billing_month: str = ""

    # ---- 分时电量合计差 ΔE = E_total − ΣE_period（§3.1）----
    energy_total_kwh: float | None = None
    energy_period_sum_kwh: float | None = None
    energy_difference_kwh: float | None = None
    energy_tolerance_kwh: float = 0.0
    energy_consistent: bool | None = Field(
        default=None, description="True 一致 / False 超容差 / None 无法判断（缺数据）"
    )
    energy_partial: bool = Field(default=False, description="是否只比较了部分时段（其余时段未提供）")
    energy_missing_periods: list[str] = Field(default_factory=list, description="未提供的时段字段")

    # ---- 费用分项合计差 ΔC = C_total − ΣC_i（§3.1，第一层）----
    amount_total_yuan: float | None = None
    amount_component_sum_yuan: float | None = None
    amount_difference_yuan: float | None = None
    amount_tolerance_yuan: float = 0.0
    amount_consistent: bool | None = None
    amount_components_used: list[str] = Field(default_factory=list)

    # ---- 电度电费二层分项核对（§2.1：分项与一层合计的差）----
    energy_charge_declared_yuan: float | None = None
    energy_sub_sum_yuan: float | None = None
    energy_sub_difference_yuan: float | None = None
    energy_sub_consistent: bool | None = None
    energy_sub_components_used: list[str] = Field(default_factory=list)

    quality_status: BillQualityStatus = BillQualityStatus.VALID
    issues: list[BillValidationIssue] = Field(
        default_factory=list, description="按规则编号排列的问题清单（ERROR/WARNING/INFO）"
    )
    messages: list[str] = Field(default_factory=list, description="中文说明（含全部差异数值）")
    assumptions: list[str] = Field(default_factory=list, description="口径说明（供报告披露）")

    @property
    def passed(self) -> bool:
        """是否存在 ERROR 级问题（``quality_status != invalid`` 即通过）。"""
        return self.quality_status is not BillQualityStatus.INVALID


class BillMonthlySummary(_Model):
    """某个月的账单汇总（V2.1 §5.1、§3.1：月度趋势与基准电费）。"""

    billing_month: str
    bill_count: int = 0
    bill_ids: list[str] = Field(default_factory=list)
    energy_total_kwh: float | None = Field(default=None, description="该月已提供总电量的账单之和")
    amount_total_yuan: float | None = None
    average_price_yuan_per_kwh: float | None = Field(
        default=None, description="账单平均综合电价 P_avg = C_bill / E_grid；电量为 0 或未知时为 None"
    )
    energy_missing_bills: int = Field(default=0, description="未提供总电量的账单条数")
    amount_missing_bills: int = Field(default=0, description="未提供账单总额的账单条数")
    has_cross_month: bool = False
    has_overlap: bool = False
    quality_status: BillQualityStatus = BillQualityStatus.VALID
    messages: list[str] = Field(default_factory=list)


class BillAnnualSummary(_Model):
    """年度账单基准数据（V2.1 §3.1）。

    年总用电量只在**每月账单周期完整且不重叠**时才允许直接相加；
    缺月、跨月、重叠、计费周期非自然月都会降低可信度并输出警告与覆盖率。
    """

    year: int
    months_covered: list[str] = Field(default_factory=list)
    missing_months: list[str] = Field(default_factory=list)
    duplicate_months: list[str] = Field(default_factory=list, description="同月出现多条账单")
    cross_month_bills: list[str] = Field(default_factory=list, description="跨月账单 ID")
    non_natural_month_bills: list[str] = Field(default_factory=list, description="计费周期非自然月的账单 ID")
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0, description="月份覆盖率（已覆盖月 / 12）")
    total_energy_kwh: float | None = None
    total_amount_yuan: float | None = None
    average_price_yuan_per_kwh: float | None = None
    can_sum_directly: bool = Field(
        default=False, description="是否满足「每月账单周期完整且不重叠」的直接相加条件"
    )
    monthly: list[BillMonthlySummary] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class BillQualityScore(_Model):
    """账单数据质量评分（V2.1 §5.5；沿用 V2 §55、§56 的四维评分思路）。

    计分（满分 100）::

        完整性 = 50 × 已提供关键字段数 / 关键字段总数
        一致性 = 35 × 通过的核对项 / 可判断的核对项   （无可判断项时按 35 计，
                 避免"没有数据"被当成"数据很差"）
        来源   = 15 × 来源可信度 / 15                 （复用 V2 §8.2 分值表）

    评分**只作展示**：账单的 ``quality_status`` 由问题级别决定（有 ERROR → 无效），
    不允许用高分掩盖"分项合计不一致"这类硬问题。
    """

    score: float = Field(default=0.0, ge=0.0, le=100.0, description="总分")
    completeness: float = 0.0
    consistency: float = 0.0
    source_credibility: float = 0.0
    status: BillQualityStatus = BillQualityStatus.VALID
    issues: list[BillValidationIssue] = Field(default_factory=list)
