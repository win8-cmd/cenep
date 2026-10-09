"""基准账单复算与差异分析的结果对象（V2.3 §3.4、§7.4、§7.5）。

**边界（§0.2、§1）：**
* 本模块**只描述结果长什么样**，不含任何计算；全部公式都在
  :mod:`cenep.calculation.bill_recalculator`（核心公式必须在 ``calculation/``）。
* 复算结果**绝不写回** :class:`~cenep.domain.bill_models.ElectricityBill` 的任何金额/电量字段：
  账单事实与"软件复算值"必须分开保存（§2.5 明确要求）。
* :class:`BillSimulationResult`（含光伏/储能方案对比）属 V2.3 阶段 6，**本阶段不实现**；
  本模块只承载"基准（无光伏无储能）"这一侧的复算与校准结果。

必须区分的三类数值（§2.5）
------------------------
:data:`RECOMPUTATION_BASIS_LABELS` 里的三种口径互不替代：

* ``BILL_ACTUAL``：**账单实录值**（用户 PDF / 手工录入的原始事实）；
* ``TOU_ENERGY_RECOMPUTE``：**按分时电量复算值**（本阶段主口径，§7.4 第 2 条）；
* ``LOAD_CURVE_SIMULATION``：**基于负荷曲线模拟值**（需要高频负荷，属阶段 6）。

单位：金额 **元**、电量 **kWh**、功率 **kW**、容量 **kVA**、电价 **元/kWh**
（需量电价 元/kW·月、容量电价 元/kVA·月）。
"""

from __future__ import annotations

from pydantic import Field

from .base import _Model
from .enums import DemandBillingMode, TariffPlanStatus, TariffStructure

__all__ = [
    "RECOMPUTATION_BASIS_LABELS",
    "BillCalibrationSummary",
    "BillDifferenceItem",
    "BillRecomputation",
    "PeriodPriceDetail",
    "RecomputationBasis",
]

#: 复算口径的中文标签（报告与界面必须显示，避免"账单实录"与"软件复算"混为一谈，§8.1）
RECOMPUTATION_BASIS_LABELS: dict[str, str] = {
    "bill_actual": "账单实录值",
    "tou_energy_recompute": "按分时电量复算值",
    "load_curve_simulation": "基于负荷曲线模拟值",
}

#: 复算口径取值（str 常量；不使用新枚举，避免与既有枚举口径重叠）
RecomputationBasis = str


class PeriodPriceDetail(_Model):
    """某一分时时段的分项明细（V2.3 §3.4：``C_energy = Σ_i E_grid,i × P_i``）。"""

    period: str = Field(description="TariffPeriod.value")
    period_label: str = Field(default="", description="时段中文名（尖峰 / 高峰 / 平段 / 低谷 / 深谷）")
    energy_kwh: float | None = Field(default=None, description="该时段购电量 kWh；None = 账单未提供")
    unit_price_yuan_per_kwh: float | None = Field(
        default=None, description="电价计划中该时段的电度电价 元/kWh"
    )
    amount_yuan: float | None = Field(default=None, description="该时段电度电费 元")
    price_source: str = Field(default="", description="价格来源说明（计划名称 + 直接单价 / 浮动系数口径）")
    energy_source: str = Field(default="", description="电量来源说明（账单分时电量 / 分时电量估算）")
    note: str = Field(default="", description="口径补充")


class BillDifferenceItem(_Model):
    """差异分解中的一项因素（V2.3 §7.4 第 4、5 条、§7.6）。

    差异**必须被解释**，不允许为了匹配总金额而无解释地修改各时段电价（§7.4 第 4 条）。
    因此每一项都带 ``factor``（因素）、金额与口径说明，且全部分项之和**严格等于**
    :attr:`BillRecomputation.difference_yuan`（最后一项为残差项，永远列出）。
    """

    factor: str = Field(description="差异因素中文名，如『电能量（市场化直购 vs 代理购电）价差』")
    category: str = Field(default="", description="归类：energy / basic / unmodeled / caliber / residual")
    amount_yuan: float = Field(description="该因素贡献的差异金额 元（正 = 复算低于账单）")
    unit_amount_yuan_per_kwh: float | None = Field(
        default=None, description="折算到每千瓦时的单价差 元/kWh（无法折算时为 None）"
    )
    explanation: str = Field(default="", description="中文解释（口径、来源、方向）")


class BillRecomputation(_Model):
    """**单张账单**的基准复算与差异分析结果（V2.3 §3.4、§7.4）。

    记账口径（保证恒等，便于核对）::

        复算合计 = 复算电度电费 + 复算基本电费（容量或需量，互斥）
        账单合计 = 账单电度电费 + 账单基本电费 + 未建模费用 + 调整/返还
        差异     = 账单合计 − 复算合计
        差异     = 电度电费差 + 基本电费差 + 未建模费用 + 调整/返还 + 口径残差

    其中"未建模费用"包括功率因数调整电费、其他费用、增值税等——§3.4 明确要求
    **不得凭空估算**，因此这些项一律按账单实际值作为固定基准保留，并在结果中披露处理方式。
    """

    bill_id: str
    billing_month: str = ""
    tariff_plan_id: str = ""
    tariff_plan_name: str = ""
    plan_status: TariffPlanStatus = TariffPlanStatus.DRAFT
    voltage_level: str | None = None
    tariff_structure: TariffStructure = TariffStructure.UNKNOWN

    basis: str = Field(default="tou_energy_recompute", description="复算口径（见 RECOMPUTATION_BASIS_LABELS）")
    basis_label: str = Field(default="按分时电量复算值", description="复算口径中文标签")

    # ---- 电量 ----
    energy_total_kwh: float | None = None
    energy_period_sum_kwh: float | None = None
    energy_difference_kwh: float | None = Field(
        default=None, description="ΔE = 总电量 − 分时电量合计（None = 无法判断）"
    )
    energy_missing_periods: list[str] = Field(default_factory=list, description="账单未提供的时段")

    # ---- 逐时段明细 ----
    period_details: list[PeriodPriceDetail] = Field(default_factory=list)

    # ---- 复算值（软件口径）----
    recomputed_energy_charge_yuan: float | None = None
    recomputed_capacity_charge_yuan: float | None = None
    recomputed_demand_charge_yuan: float | None = None
    recomputed_subtotal_yuan: float | None = Field(
        default=None, description="复算合计（**仅软件建模部分**：电度电费 + 基本电费）"
    )
    recomputed_unmodeled_baseline_yuan: float | None = Field(
        default=None,
        description="未建模费用基准（功率因数调整 / 其他 / 增值税，取账单实际值）；与复算合计相加才是可比的复算总额",
    )
    recomputed_total_with_baseline_yuan: float | None = Field(
        default=None, description="复算总额（含未建模费用基准与调整项），与账单总额同口径可比"
    )
    recomputed_average_price_yuan_per_kwh: float | None = Field(
        default=None, description="复算合计 / 账单总电量（元/kWh）"
    )

    # ---- 账单实录值 ----
    bill_energy_charge_yuan: float | None = None
    bill_capacity_charge_yuan: float | None = None
    bill_demand_charge_yuan: float | None = None
    bill_unmodeled_charge_yuan: float | None = Field(
        default=None, description="账单已列但本阶段未建模的费用合计（功率因数调整 / 其他 / 增值税）"
    )
    bill_unmodeled_fields: list[str] = Field(default_factory=list, description="上述未建模费用对应的字段")
    bill_adjustment_yuan: float | None = Field(default=None, description="账单调整 / 补退费（可为负）")
    bill_total_yuan: float | None = None

    # ---- 差异 ----
    difference_yuan: float | None = Field(
        default=None, description="**毛差异** = 账单合计 − 复算合计（仅软件建模部分；正 = 账单更高）"
    )
    difference_rate: float | None = Field(default=None, description="毛差异率 = 毛差异 / 复算合计")
    difference_after_baseline_yuan: float | None = Field(
        default=None,
        description=(
            "**净差异** = 账单合计 −（复算合计 + 未建模费用基准 + 调整项）；"
            "即剔除『账单已列但本阶段未建模的费用』后的差异，是容差判定的依据"
        ),
    )
    tolerance_yuan: float = 0.0
    passed_tolerance: bool | None = Field(
        default=None, description="净差异是否在容差内（None = 无法判断）"
    )
    gross_passed_tolerance: bool | None = Field(
        default=None, description="毛差异是否在容差内（供报告并行披露）"
    )
    difference_items: list[BillDifferenceItem] = Field(default_factory=list)

    # ---- 需量（§7.5）----
    demand_billing_mode: DemandBillingMode = DemandBillingMode.DEMAND
    billing_demand_kw: float | None = Field(default=None, description="账单计费需量 kW（账单事实）")
    contract_capacity_kva: float | None = None
    demand_price_yuan_per_kw_month: float | None = None
    capacity_price_yuan_per_kva_month: float | None = None
    demand_method: str = Field(default="", description="需量取值方法（账单计费需量 / 曲线模拟需量）")

    # ---- 质量与披露 ----
    quality_status: str = Field(default="valid", description="valid / warning / invalid")
    messages: list[str] = Field(default_factory=list, description="中文说明（含全部差异数值）")
    assumptions: list[str] = Field(default_factory=list, description="口径与假设（供报告披露）")

    @property
    def passed(self) -> bool:
        """是否通过账单校准（§7.4 第 6 条：**净差异**超容差即"未通过账单校准"）。"""
        return self.passed_tolerance is True

    def describe(self) -> str:
        """一行中文摘要。"""
        return (
            f"{self.billing_month} 账单 {self.bill_id}："
            f"账单 {_fmt(self.bill_total_yuan)} − 复算 {_fmt(self.recomputed_subtotal_yuan)} = "
            f"毛差异 {_fmt(self.difference_yuan)}，净差异 {_fmt(self.difference_after_baseline_yuan)}"
            f"（{self.basis_label}）"
        )


class BillCalibrationSummary(_Model):
    """多张账单的基准复算与校准汇总（V2.3 §7.4、§3.1）。

    年度电量只在**每月账单周期完整且不重叠**时才允许直接相加（§3.1）；
    覆盖率与缺月信息随结果一起返回，报告不得把不完整年度写成"完整年度实测"。
    """

    year: int
    tariff_plan_id: str = ""
    tariff_plan_name: str = ""
    basis: str = "tou_energy_recompute"
    basis_label: str = "按分时电量复算值"

    bill_count: int = 0
    months_covered: list[str] = Field(default_factory=list)
    missing_months: list[str] = Field(default_factory=list)
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    can_sum_directly: bool = Field(default=False, description="是否满足月度完整且不重叠")

    total_energy_kwh: float | None = None
    total_bill_amount_yuan: float | None = None
    total_recomputed_amount_yuan: float | None = Field(
        default=None, description="复算合计（仅软件建模部分）之和"
    )
    total_recomputed_with_baseline_yuan: float | None = Field(
        default=None, description="复算总额（含未建模费用基准）之和，与账单总额同口径"
    )
    total_difference_yuan: float | None = Field(default=None, description="**毛差异**合计（账单 − 复算合计）")
    total_difference_rate: float | None = None
    total_net_difference_yuan: float | None = Field(
        default=None, description="**净差异**合计（账单 − 复算总额），容差判定依据"
    )

    bill_average_price_yuan_per_kwh: float | None = None
    recomputed_average_price_yuan_per_kwh: float | None = None
    unit_price_difference_yuan_per_kwh: float | None = Field(
        default=None, description="复算均价 − 账单均价（元/kWh）"
    )

    factor_totals: dict[str, float] = Field(default_factory=dict, description="各差异因素的年度合计金额 元")
    passed_bills: int = 0
    failed_bills: int = 0
    monthly: list[BillRecomputation] = Field(default_factory=list)

    tolerance_yuan: float = 0.0
    calibration_passed: bool | None = Field(
        default=None, description="整体是否通过账单校准（§7.4 第 6 条）"
    )
    messages: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def _fmt(value: float | None) -> str:
    return "未提供" if value is None else f"{float(value):,.2f} 元"
