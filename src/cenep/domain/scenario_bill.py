"""光储四场景账单对比的**结果模型**（V2.3 §7.1、§7.5、§7.6、§3.5、§10 阶段 6）。

本模块**只描述结果长什么样**，不含任何计算（V2.1 §1、§0.2：``domain/`` 不放计算逻辑）。
全部公式都在 :mod:`cenep.calculation.scenario_bill_engine`
（核心公式必须在 ``calculation/``），界面只允许调用
:class:`cenep.application.scenario_service.ScenarioBillService`。

为什么新增本模块而不是扩展现有模型（§0.2 追加式扩展）
----------------------------------------------------
* :class:`~cenep.domain.bill_recomputation.BillRecomputation` 只承载
  "**基准（无光伏无储能）**"一侧的复算与校准（阶段 5 交付），
  其模块文档明确声明"含光伏/储能方案对比的 ``BillSimulationResult`` 属阶段 6"；
* :class:`~cenep.domain.timeseries_results.ScenarioResult` 是 V2 既有的"候选方案摘要"，
  其 ``annual_saving`` 是**单一标量**，无法承载规格书 §7.1／§7.5 要求的
  "分项费用 + 需量变化 + 逐月明细"，也**没有**任何去重字段；
* 既有两个模型都不改：本模块只**新增**，既有公开接口、字段语义、报告字段一律不动。

单位口径（§0.2）
--------------
金额 **人民币元**、电量 **kWh**、功率 **kW**、容量 **kVA**、
电价 元/kWh、需量电价 元/kW·月、容量电价 元/kVA·月。

四场景与"唯一去重"的关系（§7.6）
------------------------------
四场景共享同一负荷、同一电价计划与同一套需求/其他费用假设，**只有设备配置不同**。
因此场景之间的差额有且只有一个来源：`:attr:`ScenarioBillLine.total_cost_yuan``
（= 电度电费 + 基本电费 + 力调 + 政府性基金及附加 + 其他未建模费用）。
收益只在 :class:`ScenarioDedupManifest` 中被**唯一登记**一次，
财务引擎也只接收 :attr:`ScenarioComparison.unique_annual_benefit_yuan` 这一个数值。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import _Model

__all__ = [
    "BENEFIT_KEY_LABELS",
    "COST_COMPONENT_LABELS",
    "DEDUP_EXCLUDED_REASON",
    "DEDUP_UNIQUE_SOURCE",
    "DemandMeasureMethod",
    "PowerFactorAdjustMode",
    "SCENARIO_ORDER",
    "SUPPORTED_SCENARIO_KINDS",
    "ScenarioBenefitEntry",
    "ScenarioBillComparison",
    "ScenarioBillLine",
    "ScenarioBillSet",
    "ScenarioCostComponent",
    "ScenarioDedupManifest",
    "ScenarioKind",
    "ScenarioMonthlyRow",
    "label_of_benefit",
    "label_of_component",
]


# --------------------------------------------------------------------------- #
# 枚举（追加式；不复用既有 ScenarioType —— 那是"保守/基准/乐观"的敏感性情景，
# 与"无光伏/仅光伏/仅储能/光储"的设备配置场景是两件事，不得混为一谈）
# --------------------------------------------------------------------------- #
class ScenarioKind(StrEnum):
    """规格书 §7.1／§10 阶段 6 要求的四类设备配置场景。

    ==================================  ====================================================
    成员                                 含义
    ==================================  ====================================================
    ``NO_PV_NO_STORAGE``                基准：无光伏、无储能，全部负荷由电网供应
    ``PV_ONLY``                         仅光伏：有光伏、无储能
    ``STORAGE_ONLY``                    仅储能：无光伏、有储能
    ``PV_STORAGE``                      光储：光伏 + 储能
    ==================================  ====================================================

    取值一律用**字符串常量**（与既有 ``RecomputationBasis`` 的做法一致），
    避免与 V2 既有 ``ScenarioType`` 的枚举成员重叠。
    """

    NO_PV_NO_STORAGE = "no_pv_no_storage"
    PV_ONLY = "pv_only"
    STORAGE_ONLY = "storage_only"
    PV_STORAGE = "pv_storage"

    @property
    def label(self) -> str:
        """中文名（界面、报告、Excel 表头三处共用，避免各处硬编码）。"""
        return {
            ScenarioKind.NO_PV_NO_STORAGE: "基准（无光伏、无储能）",
            ScenarioKind.PV_ONLY: "方案 A：仅光伏",
            ScenarioKind.STORAGE_ONLY: "方案 B：仅储能",
            ScenarioKind.PV_STORAGE: "方案 C：光伏 + 储能",
        }[self]

    @property
    def short_label(self) -> str:
        """短中文名（表头与图例用）。"""
        return {
            ScenarioKind.NO_PV_NO_STORAGE: "基准",
            ScenarioKind.PV_ONLY: "仅光伏",
            ScenarioKind.STORAGE_ONLY: "仅储能",
            ScenarioKind.PV_STORAGE: "光储",
        }[self]

    @property
    def has_pv(self) -> bool:
        """本场景是否投入光伏容量。"""
        return self in (ScenarioKind.PV_ONLY, ScenarioKind.PV_STORAGE)

    @property
    def has_storage(self) -> bool:
        """本场景是否投入储能容量。"""
        return self in (ScenarioKind.STORAGE_ONLY, ScenarioKind.PV_STORAGE)

    @property
    def is_baseline(self) -> bool:
        """是否基准场景（差额的另一端）。"""
        return self is ScenarioKind.NO_PV_NO_STORAGE


#: 四场景的固定展示顺序（§7.1：基准 → 仅光伏 → 仅储能 → 光储）
SCENARIO_ORDER: tuple[ScenarioKind, ...] = (
    ScenarioKind.NO_PV_NO_STORAGE,
    ScenarioKind.PV_ONLY,
    ScenarioKind.STORAGE_ONLY,
    ScenarioKind.PV_STORAGE,
)

#: 本阶段支持的全部场景（导出给界面做下拉/勾选，避免界面自己拼枚举）
SUPPORTED_SCENARIO_KINDS: tuple[ScenarioKind, ...] = SCENARIO_ORDER


class DemandMeasureMethod(StrEnum):
    """需量的取值方法（V2.3 §7.5、§3.4）。

    §7.5 明确：``D_billable`` **不能**直接等同于曲线最大功率；V2.3 只支持两种来源
    （本阶段新增第三种"用户固定假设"以覆盖非实测项目的显式假设场景）：

    * :attr:`BILL`：**账单计费需量**（阶段 5 已核验的真实账单值，基准侧默认）；
    * :attr:`CURVE_WINDOW`：按**配置的计量窗口**从高频购电曲线计算模拟需量（方案侧默认）；
    * :attr:`FIXED`：用户显式给定的固定需量假设（无高频曲线时的显式假设，必须披露）。

    结果中的 :attr:`ScenarioBillLine.demand_method` 永远标明用了哪一种（§7.4、§7.5）。
    """

    BILL = "bill"
    CURVE_WINDOW = "curve_window"
    FIXED = "fixed"

    @property
    def label(self) -> str:
        return {
            DemandMeasureMethod.BILL: "账单计费需量（账单事实，§7.5 默认基准口径）",
            DemandMeasureMethod.CURVE_WINDOW: "按配置计量窗口从高频购电曲线计算的模拟需量（§7.5）",
            DemandMeasureMethod.FIXED: "用户给定的固定需量假设（无高频曲线，显式假设）",
        }[self]


class PowerFactorAdjustMode(StrEnum):
    """功率因数调整电费的处理方式（V2.3 §7.5、§3.4）。

    §3.4 明确："若缺乏适用规则，**不得凭空估算**，应允许用户输入实际账单金额，
    或将未建模费用按明确的基准固定/比例假设保留，并在结果中披露"。
    因此本枚举只提供"固定值"与"按电量比例分摊"两种**显式**口径，没有隐含默认。
    """

    FIXED = "fixed"
    RATIO_OF_ENERGY_CHARGE = "ratio_of_energy_charge"
    EXCLUDED = "excluded"

    @property
    def label(self) -> str:
        return {
            PowerFactorAdjustMode.FIXED: "固定金额（取账单实际值，不随方案变化）",
            PowerFactorAdjustMode.RATIO_OF_ENERGY_CHARGE: "按电度电费的比例分摊（显式比例假设）",
            PowerFactorAdjustMode.EXCLUDED: "不计入（仅作口径对照，不得用于正式结论）",
        }[self]


# --------------------------------------------------------------------------- #
# 费用分项（§7.1、§7.5：**必须与电度电费节省分开列示**）
# --------------------------------------------------------------------------- #
#: 费用分项的固定分类与中文名（顺序即报告/Excel 中的展示顺序）
COST_COMPONENT_LABELS: dict[str, str] = {
    "energy_charge": "电度电费（Σ 分时购电量 × 分时电价）",
    "capacity_charge": "基本电费·变压器容量计费（kVA × 元/kVA·月）",
    "demand_charge": "基本电费·最大需量计费（kW × 元/kW·月）",
    "power_factor_adjustment": "功率因数调整电费",
    "government_fund": "政府性基金及附加",
    "other_unmodeled": "其他未建模费用",
    "export_revenue": "上网收入（**收益**，不计入电费成本）",
}

#: 各费用分项的计价口径（报告必须逐项披露，§3.4、§7.4）
COST_COMPONENT_CALIBERS: dict[str, str] = {
    "energy_charge": "Σ_t 逐周期购电量 E_grid,t × 该周期电价 P_t；P_t 由电价计划的月份时段规则确定（§3.4）",
    "capacity_charge": "合同容量 K_contract,kVA × 容量电价 P_capacity；与需量计费**默认互斥**（§7.5）",
    "demand_charge": "计费需量 D_billable,kW × 需量电价 P_demand；D 的取值方法见 demand_method（§7.5）",
    "power_factor_adjustment": "本阶段不重建力调规则，按用户给定的固定值或显式比例假设保留（§3.4、§7.5）",
    "government_fund": "电价计划的分项电价中 extracted 的政府性基金及附加单价 × 购电量；已含在分时电度电价内，此处为**分类列示**，不是二次加价（§2.3）",
    "other_unmodeled": "用户显式给定的其他未建模费用假设（固定值或按电量比例分摊），必须披露（§3.4）",
    "export_revenue": "Σ_t 上网电量 E_export,t × 上网电价；属**收入**，单列且不与电费节省相加两次（§7.6）",
}


def label_of_component(key: str) -> str:
    """费用分项的中文名（未登记时回退为键名，不抛异常）。"""
    return COST_COMPONENT_LABELS.get(key, key)


class ScenarioCostComponent(_Model):
    """一个场景的**单项费用**构成（V2.3 §7.1、§7.5）。

    ``amount_yuan`` 一律为**正数表示支出**；``export_revenue`` 是收入，
    在 :class:`ScenarioBillLine` 中通过 :attr:`is_revenue` 显式区分，
    **不参与** :attr:`ScenarioBillLine.total_cost_yuan` 的求和（§7.6 红线）。
    """

    component: str = Field(description="分项键，见 COST_COMPONENT_LABELS")
    label: str = Field(default="", description="分项中文名")
    amount_yuan: float = Field(default=0.0, description="金额 元；支出为正、返还/奖励可为负")
    unit: str = Field(default="元", description="金额单位（恒为元）")
    quantity: float | None = Field(
        default=None, description="对应的实物量（kWh / kW / kVA）；不适用时为 None"
    )
    quantity_unit: str = Field(default="", description="实物量单位，如 kWh / kW / kVA")
    unit_price: float | None = Field(
        default=None, description="对应单价（元/kWh、元/kW·月、元/kVA·月）；不适用时为 None"
    )
    is_revenue: bool = Field(default=False, description="是否为收入项（True 时不计入电费成本）")
    included_in_energy_charge: bool = Field(
        default=False,
        description=(
            "该分项是否**已包含在同行的电度电费里**（湖北官方电价表的政府性基金及附加即如此，§2.3）。"
            "为 True 时分项金额是**分类列示**：它不是一笔额外成本，也不是收入，"
            "因此既不计入 ``total_cost_yuan``，也不应被剔除——电度电费本身已经含了它"
        ),
    )
    caliber: str = Field(default="", description="计价口径与公式（§3.4、§7.4 要求可追溯）")
    source_note: str = Field(default="", description="数值来源说明（电价计划 / 账单实际值 / 用户假设）")
    included_in_total_cost: bool = Field(
        default=True, description="是否已计入 total_cost_yuan；已含在电度电费内的分项与收入项为 False"
    )

    @property
    def is_transfer_note(self) -> bool:
        """是否为"已在电度电费内"的分类列示项（不计成本、也不是收入）。"""
        return bool(self.included_in_energy_charge) and not self.is_revenue


# --------------------------------------------------------------------------- #
# 逐月明细（§7.7：输出每月费用拆分）
# --------------------------------------------------------------------------- #
class ScenarioMonthlyRow(_Model):
    """某场景某个月的费用与需量明细（V2.3 §7.1、§7.5、§7.7）。

    需量电费逐月计算（每月 1 次最大需量），电量电费与政府性基金逐月汇总，
    因此逐月行的合计**严格等于**年度值（差额只在浮点末位）。
    """

    month: int = Field(ge=1, le=12, description="月份 1~12")
    billing_month: str = Field(default="", description="YYYY-MM，便于与账单对齐")
    grid_import_kwh: float = Field(default=0.0, description="该月购电量 kWh")
    grid_export_kwh: float = Field(default=0.0, description="该月上网电量 kWh")
    pv_generation_kwh: float = Field(default=0.0, description="该月光伏发电量 kWh")
    pv_to_load_kwh: float = Field(default=0.0, description="该月光伏自发自用电量 kWh")
    load_kwh: float = Field(default=0.0, description="该月负荷电量 kWh")
    storage_charge_kwh: float = Field(default=0.0, description="该月储能充电量（交流侧）kWh")
    storage_discharge_kwh: float = Field(default=0.0, description="该月储能放电量（交流侧）kWh")

    energy_charge_yuan: float = Field(default=0.0, description="电度电费 元")
    demand_charge_yuan: float = Field(default=0.0, description="需量电费 元")
    government_fund_yuan: float = Field(default=0.0, description="政府性基金及附加 元")
    export_revenue_yuan: float = Field(default=0.0, description="上网收入 元（收入，不计入成本）")

    billable_demand_kw: float = Field(default=0.0, description="该月计费需量 kW（口径见 demand_method）")
    demand_measured_kw: float = Field(default=0.0, description="该月按计量窗口测得的需量 kW")


# --------------------------------------------------------------------------- #
# 单场景结果行（§7.1、§7.5）
# --------------------------------------------------------------------------- #
class ScenarioBillLine(_Model):
    """**一个场景**在给定负荷、电价与配置下的年度账单模拟结果（V2.3 §7.1、§7.5、§3.4）。

    金额恒等式（保证可核对，且是场景差额的唯一来源）::

        total_cost_yuan = energy_charge + capacity_charge + demand_charge
                          + power_factor_adjustment + government_fund + other_unmodeled
        net_cost_yuan   = total_cost_yuan − export_revenue        （上网收入单列为收入）

    **总成本里不含任何"收益"**：光伏自用、储能套利的价值**只**通过
    "少买电"体现在 ``energy_charge_yuan`` 的下降上，绝不在这里再记一笔收入（§7.6）。
    """

    kind: ScenarioKind
    label: str = Field(default="", description="场景中文名")
    is_baseline: bool = Field(default=False, description="是否基准场景")

    # ---- 设备配置（四场景共享负荷与电价，只有这里不同，§7.1）----
    pv_capacity_kwp: float = Field(default=0.0, description="本场景投入的光伏容量 kWp")
    storage_power_kw: float = Field(default=0.0, description="本场景投入的储能功率 kW")
    storage_energy_kwh: float = Field(default=0.0, description="本场景投入的储能容量 kWh")

    # ---- 电量（kWh）----
    load_kwh: float = Field(default=0.0, description="负荷电量 kWh（四场景应完全一致）")
    pv_generation_kwh: float = Field(default=0.0, description="光伏发电量 kWh")
    pv_to_load_kwh: float = Field(default=0.0, description="光伏自发自用电量 kWh")
    pv_to_storage_kwh: float = Field(default=0.0, description="光伏充电量 kWh")
    pv_curtailed_kwh: float = Field(default=0.0, description="弃光电量 kWh")
    grid_import_kwh: float = Field(default=0.0, description="购电量 kWh（含电网→储能）")
    grid_export_kwh: float = Field(default=0.0, description="上网电量 kWh（光伏 + 储能放电上网）")
    storage_charge_kwh: float = Field(default=0.0, description="储能充电量（交流侧）kWh")
    storage_discharge_kwh: float = Field(default=0.0, description="储能放电量（交流侧）kWh")
    storage_grid_charge_kwh: float = Field(default=0.0, description="其中电网→储能充电量 kWh")

    self_consumption_rate: float | None = Field(
        default=None, description="光伏自用率 = 自用电量 ÷ 光伏发电量；无光伏时为 None（不适用）"
    )
    load_coverage_rate: float | None = Field(
        default=None, description="负荷覆盖率 = 自用电量 ÷ 负荷电量；负荷为 0 时为 None"
    )
    export_rate: float | None = Field(
        default=None, description="光伏上网率 = 上网电量 ÷ 光伏发电量；无光伏时为 None"
    )
    grid_dependency_rate: float | None = Field(
        default=None, description="电网依赖率 = 购电量 ÷ 负荷电量；负荷为 0 时为 None"
    )

    # ---- 需量（§7.5）----
    demand_method: DemandMeasureMethod = DemandMeasureMethod.CURVE_WINDOW
    demand_method_label: str = Field(default="", description="需量取值方法中文说明")
    demand_window_minutes: int = Field(default=15, description="计量窗口（分钟）")
    peak_demand_kw: float = Field(default=0.0, description="全年计费需量最大值 kW")
    monthly_billable_demand_kw: list[float] = Field(
        default_factory=list, description="逐月计费需量 kW（12 项）"
    )

    # ---- 费用（§7.1、§7.5；分项必须分开列示）----
    energy_charge_yuan: float = Field(default=0.0, description="电度电费 元")
    capacity_charge_yuan: float = Field(default=0.0, description="基本电费·容量计费 元")
    demand_charge_yuan: float = Field(default=0.0, description="基本电费·需量计费 元")
    power_factor_adjustment_yuan: float = Field(default=0.0, description="功率因数调整电费 元")
    government_fund_yuan: float = Field(default=0.0, description="政府性基金及附加 元（含在分时电价内，分类列示）")
    other_unmodeled_yuan: float = Field(default=0.0, description="其他未建模费用 元")
    export_revenue_yuan: float = Field(default=0.0, description="上网收入 元（收入，不计入成本）")

    total_cost_yuan: float = Field(default=0.0, description="电费成本合计 元（不含上网收入）")
    net_cost_yuan: float = Field(default=0.0, description="净成本 = 电费成本合计 − 上网收入 元")
    average_price_yuan_per_kwh: float | None = Field(
        default=None, description="综合单价 = 电费成本合计 ÷ 购电量（元/kWh）"
    )

    # ---- 成本分项清单（报告/Excel 直接遍历，§8.1）----
    cost_components: list[ScenarioCostComponent] = Field(
        default_factory=list, description="分项费用明细（含口径与来源）"
    )
    monthly: list[ScenarioMonthlyRow] = Field(default_factory=list, description="逐月明细")

    # ---- 收益分解（供报告分析与去重清单**引用**，禁止单独填入现金流，§7.6）----
    pv_self_consumption_saving_yuan: float = Field(
        default=0.0, description="光伏自用节省（分析口径，已含在电费节省中，**不得重复计入现金流**）"
    )
    storage_arbitrage_revenue_yuan: float = Field(
        default=0.0, description="储能套利收益（分析口径，已含在电费节省中，**不得重复计入现金流**）"
    )
    demand_saving_kw: float = Field(default=0.0, description="需量削减量 kW（分析口径）")
    demand_cost_saving_yuan: float = Field(
        default=0.0, description="需量电费节省（分析口径，已含在电费节省中，**不得重复计入现金流**）"
    )

    # ---- 质量与披露（§0.2、§7.7）----
    energy_balance_error_kwh: float = Field(default=0.0, description="逐周期最大能量平衡误差 kWh")
    balance_is_balanced: bool = Field(default=True, description="能量平衡是否在容差内")
    data_quality_status: str = Field(default="valid", description="valid / warning / invalid")
    calculation_warnings: list[str] = Field(default_factory=list, description="中文警告")
    assumptions: list[str] = Field(default_factory=list, description="口径与假设（供报告披露）")

    def component(self, key: str) -> ScenarioCostComponent | None:
        """按分项键取费用分项（不存在时返回 ``None``，不抛异常）。"""
        for item in self.cost_components:
            if item.component == key:
                return item
        return None


# --------------------------------------------------------------------------- #
# 收益去重清单（§7.6 —— 本阶段的核心）
# --------------------------------------------------------------------------- #
#: 收益项在现金流中的**唯一来源**登记值（§7.6）
DEDUP_UNIQUE_SOURCE = "unique"

#: 收益项被排除的理由之一：与"电费节省"是同一笔钱的另一种说法（§7.6 第 2、4 条）
DEDUP_EXCLUDED_REASON = (
    "该数值是『电费节省』的分解口径，同一笔钱已在 unique_annual_benefit_yuan 中以"
    "『电费节省』计入一次；再单独相加即为重复计算（V2.3 §7.6 第 2、4 条）"
)

#: 收益分项键 → 中文名（去重清单与报告共用）
BENEFIT_KEY_LABELS: dict[str, str] = {
    "electricity_cost_saving": "电费节省（购电成本下降）",
    "export_revenue": "上网收入",
    "pv_self_consumption_saving": "其中：光伏自用节省（分解口径）",
    "storage_arbitrage_revenue": "其中：储能套利收益（分解口径）",
    "demand_cost_saving": "其中：需量电费节省（分解口径）",
    "storage_capacity_revenue": "储能容量收益（外部收入参数）",
    "storage_ancillary_revenue": "储能辅助服务收益（外部收入参数）",
    "storage_other_revenue": "储能其他收益（外部收入参数）",
    "total_benefit_analysis": "分析口径合计（**不得**填入现金流）",
}


def label_of_benefit(key: str) -> str:
    """收益分项的中文名（未登记时回退为键名，不抛异常）。"""
    return BENEFIT_KEY_LABELS.get(key, key)


class ScenarioBenefitEntry(_Model):
    """收益去重清单中的**一项**（V2.3 §7.6）。

    每一项必须回答三个问题：
    "这笔钱是多少"（``amount_yuan``）、"它是不是现金流里的唯一来源"
    （``counted_in_unique_benefit``）、"如果不是，为什么"（``excluded_reason``）。
    """

    key: str = Field(description="收益分项键，见 BENEFIT_KEY_LABELS")
    label: str = Field(default="", description="中文名")
    amount_yuan: float = Field(default=0.0, description="该分项金额 元")
    counted_in_unique_benefit: bool = Field(
        default=False, description="是否已（且仅一次）计入 unique_annual_benefit_yuan"
    )
    entry_role: str = Field(
        default=DEDUP_UNIQUE_SOURCE,
        description="unique = 现金流唯一来源；decomposition = 分解口径（仅供展示）；external = 外部收入参数",
    )
    excluded_reason: str = Field(default="", description="不计入时的**中文**理由（必须能追溯）")
    caliber: str = Field(default="", description="该分项的计价口径")


class ScenarioDedupManifest(_Model):
    """四场景收益**去重清单**（V2.3 §7.6 —— 本阶段最关键的交付物）。

    规格书 §7.6 的六条要求逐条落到本对象：

    ==============================================================  ==========================================
    §7.6 要求                                                        实现
    ==============================================================  ==========================================
    "将『基准账单 − 方案账单』作为账单节省额唯一来源"                  :attr:`bill_saving_yuan`，唯一来源
    "不得再把同一份光伏自用电量乘平均电价作为另一笔独立收益"           光伏自用节省标记 ``decomposition``，排除
    "上网电量收益可以独立计算，但必须确保未包含在账单节省额中"          :attr:`export_revenue_yuan` 唯一来源，且
                                                                      数学上不在 :attr:`bill_saving_yuan` 内
    "储能套利收益应由方案账单差额体现；额外列示只做分析展示，不再次累加" 储能套利标记 ``decomposition``，排除
    "财务现金流只接收一个去重后的年度运营收益对象"                     :attr:`unique_annual_benefit_yuan`
    "所有差异项应有可追溯明细"                                       :attr:`entries` 逐项带口径与理由
    ==============================================================  ==========================================
    """

    strategy: str = Field(
        default="基准账单 − 方案账单（现金口径）为唯一来源；分解口径仅展示不累加",
        description="去重策略的中文声明（报告必须原样披露）",
    )
    entries: list[ScenarioBenefitEntry] = Field(
        default_factory=list, description="逐项登记（唯一来源 / 分解口径 / 外部参数）"
    )

    baseline_cost_yuan: float = Field(default=0.0, description="基准场景电费成本合计 元")
    baseline_net_cost_yuan: float = Field(default=0.0, description="基准场景净成本 元")

    bill_saving_yuan: float = Field(default=0.0, description="**账单节省** = 基准成本 − 方案成本（唯一来源）")
    export_revenue_yuan: float = Field(default=0.0, description="**上网收入**（独立来源，不在账单节省内）")
    storage_capacity_revenue_yuan: float = Field(default=0.0, description="储能容量收益（外部参数）")
    storage_ancillary_revenue_yuan: float = Field(default=0.0, description="储能辅助服务收益（外部参数）")
    storage_other_revenue_yuan: float = Field(default=0.0, description="储能其他收益（外部参数）")

    unique_annual_benefit_yuan: float = Field(
        default=0.0,
        description="**唯一去重后的年度运营收益**（送财务引擎的**唯一**数值，§3.5、§7.6）",
    )
    analysis_total_yuan: float = Field(
        default=0.0, description="分析口径合计（含分解口径，**仅展示**，绝不可填入现金流）"
    )

    identity_deviation_yuan: float = Field(
        default=0.0, description="恒等式校验偏差（元）；超过容差即判定计算失败（§7.6、V2 §19 精神）"
    )
    identity_tolerance_yuan: float = Field(default=0.0, description="本次允许的偏差容差 元")
    identity_passed: bool = Field(default=False, description="恒等式是否通过")
    identity_equations: list[str] = Field(
        default_factory=list, description="本次校验的恒等式（中文，可直接写入报告）"
    )
    duplicate_risk_notes: list[str] = Field(
        default_factory=list, description="边界条件下的重复计算风险说明（含成立/不成立结论）"
    )
    messages: list[str] = Field(default_factory=list, description="中文结论（含全部数值）")
    assumptions: list[str] = Field(default_factory=list, description="口径假设（供报告披露）")

    def entry(self, key: str) -> ScenarioBenefitEntry | None:
        """按分项键取登记项（不存在时返回 ``None``）。"""
        for item in self.entries:
            if item.key == key:
                return item
        return None

    def unique_entries(self) -> list[ScenarioBenefitEntry]:
        """全部**唯一来源**登记项（现金流里应当且仅当出现这些项）。"""
        return [item for item in self.entries if item.counted_in_unique_benefit]

    def excluded_entries(self) -> list[ScenarioBenefitEntry]:
        """全部**未计入**登记项（分解口径与外部参数），用于报告展示"哪些没有重复计"。"""
        return [item for item in self.entries if not item.counted_in_unique_benefit]


# --------------------------------------------------------------------------- #
# 单方案对比（§7.1、§7.6）
# --------------------------------------------------------------------------- #
class ScenarioBillComparison(_Model):
    """**一个方案 vs 基准**的完整对比结果（V2.3 §7.1、§7.6、§3.4）。

    记账口径::

        账单节省 S_bill = 基准 total_cost − 方案 total_cost          （§3.4、§7.6 唯一来源）
        净节省   S_net  = 基准 net_cost   − 方案 net_cost
                        = S_bill + (方案上网收入 − 基准上网收入)
        需量削减 ΔD     = 基准 peak_demand − 方案 peak_demand         （§7.5，**与电度电费分开列示**）
    """

    scenario: ScenarioBillLine
    baseline: ScenarioBillLine

    # ---- 电费差额（§7.1、§3.4）----
    bill_saving_yuan: float = Field(default=0.0, description="账单节省 = 基准成本 − 方案成本 元")
    bill_saving_rate: float | None = Field(
        default=None, description="节省率 = 节省 ÷ 基准成本；基准成本为 0 时为 None（不适用）"
    )
    net_saving_yuan: float = Field(default=0.0, description="净节省（含上网收入变化）元")
    export_revenue_delta_yuan: float = Field(default=0.0, description="上网收入变化 元（独立列示）")

    # ---- 分项费用差额（§7.1、§7.5：必须分开列示）----
    energy_charge_delta_yuan: float = Field(default=0.0, description="电度电费差额 元")
    capacity_charge_delta_yuan: float = Field(default=0.0, description="容量电费差额 元")
    demand_charge_delta_yuan: float = Field(default=0.0, description="需量电费差额 元（**与电度电费分开**）")
    power_factor_delta_yuan: float = Field(default=0.0, description="功率因数调整差额 元")
    government_fund_delta_yuan: float = Field(default=0.0, description="政府性基金及附加差额 元")
    other_unmodeled_delta_yuan: float = Field(default=0.0, description="其他未建模费用差额 元")

    # ---- 需量变化（§7.1、§7.5）----
    baseline_peak_demand_kw: float = Field(default=0.0, description="基准计费需量 kW")
    scenario_peak_demand_kw: float = Field(default=0.0, description="方案计费需量 kW")
    demand_reduction_kw: float = Field(default=0.0, description="需量削减 kW（正 = 下降）")
    monthly_demand_reduction_kw: list[float] = Field(
        default_factory=list, description="逐月需量削减 kW（12 项）"
    )

    # ---- 经济指标（§7.1）----
    baseline_average_price_yuan_per_kwh: float | None = Field(
        default=None, description="基准综合单价 元/kWh"
    )
    scenario_average_price_yuan_per_kwh: float | None = Field(
        default=None, description="方案综合单价 元/kWh"
    )

    # ---- 去重（§7.6）----
    dedup: ScenarioDedupManifest = Field(
        default_factory=ScenarioDedupManifest, description="收益去重清单（唯一来源登记）"
    )

    messages: list[str] = Field(default_factory=list, description="中文说明（含全部差额数值）")
    assumptions: list[str] = Field(default_factory=list, description="口径与假设")


# --------------------------------------------------------------------------- #
# 四场景总结果（§7.1、§7.6、§7.7）
# --------------------------------------------------------------------------- #
class ScenarioBillSet(_Model):
    """**四场景**在同一负荷、同一电价计划下的完整对比结果（V2.3 §7.1、§7.6、§7.7）。

    这是阶段 6 对外的**唯一**总结果对象：界面与报告只读它，
    财务引擎只接收 :meth:`unique_annual_benefit_yuan`。

    必须同时成立的三条不变式（由 :mod:`cenep.calculation.scenario_bill_engine`
    强制校验，违反即抛中文异常）：

    1. **负荷一致**：四个场景的 ``load_kwh`` 完全相同（同一负荷，§7.1）；
    2. **电价一致**：四个场景的 ``average_price`` 基于同一电价序列（同一电价计划，§7.1）；
    3. **收益唯一**：每个场景的节省额只由一种口径（账单差额）计算一次（§7.6）。
    """

    tariff_plan_id: str = Field(default="", description="本项目采用的电价计划编号")
    tariff_plan_name: str = Field(default="", description="电价计划名称（含版本）")
    tariff_plan_source: str = Field(default="", description="电价来源与文号（§4.1 可追溯）")
    tariff_plan_status: str = Field(default="", description="核验状态（draft / verified / expired）")

    load_source: str = Field(default="", description="负荷数据来源说明（实测 / 估算，§0.2）")
    load_is_measured: bool = Field(default=False, description="是否为实测高频负荷（决定结论置信度）")
    interval_minutes: int = Field(default=60, description="计算时间间隔（分钟）")
    base_year: int = Field(default=2025, description="仿真基准年")
    point_count: int = Field(default=0, description="时间点数量")

    baseline: ScenarioBillLine | None = Field(default=None, description="基准场景结果行")
    scenarios: list[ScenarioBillLine] = Field(default_factory=list, description="四场景结果行（含基准）")
    comparisons: list[ScenarioBillComparison] = Field(
        default_factory=list, description="方案 vs 基准的对比（不含基准自身）"
    )

    unique_annual_benefit_yuan: float = Field(
        default=0.0,
        description="**唯一去重后的年度运营收益**（送财务引擎的唯一数值，§3.5、§7.6）",
    )
    reference_scenario: ScenarioKind = Field(
        default=ScenarioKind.PV_STORAGE,
        description="上述唯一收益取自哪个场景（默认光储；仅光伏项目应显式改为 PV_ONLY）",
    )
    dedup_verified: bool = Field(default=False, description="全部场景的收益去重校验是否通过")
    identity_max_deviation_yuan: float = Field(default=0.0, description="各场景恒等式偏差的最大值 元")

    finance_overrides: dict[int, object] = Field(
        default_factory=dict,
        description="逐年收益覆盖表 ``{年份: YearOverride}``，直接传给既有财务引擎（§3.5）",
    )

    messages: list[str] = Field(default_factory=list, description="中文说明（含全部关键数值）")
    warnings: list[str] = Field(default_factory=list, description="中文警告（口径限制与未建模项）")
    assumptions: list[str] = Field(default_factory=list, description="口径与假设（供报告披露）")

    # ------------------------------------------------------------------ #
    # 只读派生
    # ------------------------------------------------------------------ #
    def line_of(self, kind: ScenarioKind) -> ScenarioBillLine | None:
        """按场景取结果行（不存在时返回 ``None``）。"""
        for line in self.scenarios:
            if line.kind is kind:
                return line
        return None

    def comparison_of(self, kind: ScenarioKind) -> ScenarioBillComparison | None:
        """按场景取对比结果（基准场景没有对比，返回 ``None``）。"""
        for item in self.comparisons:
            if item.scenario.kind is kind:
                return item
        return None

    def reference_comparison(self) -> ScenarioBillComparison | None:
        """被选中作为财务输入的方案对比（``reference_scenario``）。"""
        return self.comparison_of(self.reference_scenario)
