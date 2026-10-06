"""核心数据模型（规范 §11–§12、§16、§18、§29、§34、§38、§48、§53、§57、§59、§62、§92）。

本模块只描述"数据长什么样"，**不含任何计算**。所有公式在 :mod:`cenep.calculation`。

分层校验策略
------------
* 本模块用 Pydantic 做**类型与取值范围**的底线约束（规范 §112）；
* 面向用户的中文报错由 :mod:`cenep.calculation.validator` 统一产生（规范 §132），
  保证 GUI 能提示"哪个参数有问题"而不是抛裸异常。
"""

from __future__ import annotations

from datetime import date

from pydantic import Field, model_validator

from .base import NON_NEG, RATIO, _Model
from .enums import (
    DepreciationMethod,
    InvestmentMode,
    OpexMode,
    ProjectType,
    RepaymentMethod,
    RoofRentMode,
    SensitivityVariable,
    TariffMode,
)
from .provenance import ParameterMeta
from .timeseries import TimeSeriesConfig

__all__ = ["NON_NEG", "RATIO", "_Model"]


# --------------------------------------------------------------------------- #
# §12 项目基本信息
# --------------------------------------------------------------------------- #
class BasicInfo(_Model):
    """项目基本信息（规范 §12、§98）。"""

    project_name: str = Field(default="未命名项目", min_length=1)
    province: str = "湖北"
    city: str = ""
    project_type: ProjectType = ProjectType.COMMERCIAL_PV
    evaluation_date: date = Field(default_factory=date.today)
    customer_name: str | None = None
    industry: str | None = None
    notes: str | None = None


# --------------------------------------------------------------------------- #
# §16 负荷
# --------------------------------------------------------------------------- #
class LoadConfig(_Model):
    """工商业负荷参数（规范 §16、§17）。

    ``annual_load_kwh`` 表示**用户全年实际用电量**，不是光伏发电量，也不是电网购电量。
    """

    annual_load_kwh: float = Field(default=0.0, ge=0.0, description="用户全年实际用电量 kWh")
    working_days: int = Field(default=300, ge=0, le=366, description="年工作天数")
    daytime_load_ratio: float = Field(default=0.5, ge=0.0, le=1.0, description="白天负荷占比")
    nighttime_load_ratio: float = Field(default=0.5, ge=0.0, le=1.0, description="夜间负荷占比")
    annual_load_growth_rate: float = Field(default=0.0, ge=-0.5, le=1.0, description="年用电量增长率（小数）")

    @model_validator(mode="after")
    def _check_ratios(self) -> LoadConfig:
        total = self.daytime_load_ratio + self.nighttime_load_ratio
        if abs(total - 1.0) > 1e-6:
            raise ValueError("白天负荷占比与夜间负荷占比之和必须等于 1")
        return self


# --------------------------------------------------------------------------- #
# §18 光伏
# --------------------------------------------------------------------------- #
class PVConfig(_Model):
    """光伏参数（规范 §18–§25、§99）。"""

    pv_capacity_kwp: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "直接输入装机容量 kWp。允许 0 —— 表示本项目不装光伏"
            "（V2 §78 要求 PV 容量为 0 时退化为纯电网负荷项目；储能侧容量同样允许 0）"
        ),
    )
    roof_area_m2: float = Field(default=0.0, ge=0.0, description="屋顶总面积 m²")
    usable_roof_area_m2: float = Field(default=0.0, ge=0.0, description="可利用屋顶面积 m²")
    area_per_kwp: float = Field(default=6.0, gt=0.0, description="单位容量占用面积 m²/kWp")
    equivalent_hours: float = Field(default=1100.0, ge=0.0, description="年等效利用小时 h")
    performance_ratio: float = Field(default=1.0, gt=0.0, le=1.0, description="系统综合损失修正系数")
    annual_degradation_rate: float = Field(default=0.005, ge=0.0, le=0.5, description="年衰减率")
    curtailment_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="限电率")
    self_consumption_ratio: float = Field(default=0.8, ge=0.0, le=1.0, description="自发自用比例")
    loss_ratio: float = Field(default=0.0, ge=0.0, le=1.0, description="其他损失比例（默认 0）")

    # ---- 设备更换（规范 §25 延伸）----
    replacement_year: int | None = Field(
        default=None, ge=1, le=60, description="光伏设备更换年份（如逆变器第 12 年更换一次）"
    )
    replacement_cost_per_kwp: float = Field(
        default=0.0, ge=0.0, description="光伏设备更换单价 元/kWp（仅在 replacement_year 发生一次）"
    )


# --------------------------------------------------------------------------- #
# §38 储能
# --------------------------------------------------------------------------- #
class StorageConfig(_Model):
    """储能参数（规范 §38–§45、§100）。"""

    storage_power_kw: float = Field(default=0.0, ge=0.0, description="储能功率 kW")
    storage_energy_kwh: float = Field(default=0.0, ge=0.0, description="储能容量 kWh")

    charge_efficiency: float | None = Field(default=None, gt=0.0, le=1.0)
    discharge_efficiency: float | None = Field(default=None, gt=0.0, le=1.0)
    round_trip_efficiency: float | None = Field(default=0.88, gt=0.0, le=1.0)

    annual_cycles: float = Field(default=330.0, ge=0.0, le=1000.0, description="年等效循环次数")
    depth_of_discharge: float = Field(default=0.9, gt=0.0, le=1.0, description="放电深度 DoD")
    annual_degradation_rate: float = Field(default=0.02, ge=0.0, le=0.5, description="年衰减率")
    replacement_year: int | None = Field(default=None, ge=1, le=60, description="更换电芯年份")

    # ---- §43 套利口径（可覆盖电价解析结果）----
    discharge_avoided_price: float | None = Field(default=None, ge=0.0, description="放电替代电价 元/kWh")
    charge_price: float | None = Field(default=None, ge=0.0, description="充电电价 元/kWh")

    # ---- §44 其他三类收益（元/年，V1 由用户输入或政策模板给出）----
    annual_capacity_revenue: float = Field(default=0.0, ge=0.0, description="容量收益 元/年")
    annual_ancillary_revenue: float = Field(default=0.0, ge=0.0, description="辅助服务收益 元/年")
    annual_other_revenue: float = Field(default=0.0, ge=0.0, description="其他收益 元/年")

    # ---- 电芯更换投资（元，更换年发生）----
    replacement_capex: float = Field(default=0.0, ge=0.0, description="更换电芯投资 元")

    @model_validator(mode="after")
    def _check_efficiency_source(self) -> StorageConfig:
        if self.charge_efficiency is None and self.discharge_efficiency is None and self.round_trip_efficiency is None:
            raise ValueError("必须提供充电/放电效率，或往返效率")
        return self


# --------------------------------------------------------------------------- #
# §29–§33 电价
# --------------------------------------------------------------------------- #
class TariffConfig(_Model):
    """电价参数（规范 §29–§33、§101）。"""

    tariff_mode: TariffMode = TariffMode.TOU

    average_price: float = Field(default=0.0, ge=0.0, description="固定电价 元/kWh")

    peak_price: float = Field(default=1.0, ge=0.0)
    flat_price: float = Field(default=0.7, ge=0.0)
    valley_price: float = Field(default=0.4, ge=0.0)
    peak_ratio: float = Field(default=0.3, ge=0.0, le=1.0)
    flat_ratio: float = Field(default=0.4, ge=0.0, le=1.0)
    valley_ratio: float = Field(default=0.3, ge=0.0, le=1.0)

    market_price: float = Field(default=0.0, ge=0.0, description="市场电价 元/kWh")

    custom_avoided_price: float | None = Field(default=None, ge=0.0)
    custom_charge_price: float | None = Field(default=None, ge=0.0)

    avoided_price_override: float | None = Field(default=None, ge=0.0, description="替代电价（用户覆盖）")
    charge_price_override: float | None = Field(default=None, ge=0.0, description="充电电价（用户覆盖）")

    export_price: float = Field(default=0.0, ge=0.0, description="余电上网电价 元/kWh")
    green_energy_price: float = Field(default=0.0, ge=0.0, description="绿电价格 元/kWh")
    green_environmental_value: float = Field(default=0.0, ge=0.0, description="绿色环境价值 元/kWh")

    @model_validator(mode="after")
    def _check_tou(self) -> TariffConfig:
        if self.tariff_mode == TariffMode.TOU:
            total = self.peak_ratio + self.flat_ratio + self.valley_ratio
            if abs(total - 1.0) > 1e-6:
                raise ValueError("峰、平、谷电量比例之和必须等于 1")
        return self


# --------------------------------------------------------------------------- #
# §48–§52 投资
# --------------------------------------------------------------------------- #
class InvestmentConfig(_Model):
    """投资参数（规范 §48–§52、§102）。"""

    mode: InvestmentMode = InvestmentMode.UNIT_PRICE

    pv_capex_per_kw: float = Field(default=3000.0, ge=0.0, description="光伏单位投资 元/kWp")
    storage_capex_per_kwh: float = Field(default=1000.0, ge=0.0, description="储能单位投资 元/kWh")

    pv_capex: float = Field(default=0.0, ge=0.0, description="DETAILED 模式下的光伏投资 元")
    storage_capex: float = Field(default=0.0, ge=0.0, description="DETAILED 模式下的储能投资 元")

    grid_connection_cost: float = Field(default=0.0, ge=0.0, description="并网投资 元")
    roof_cost: float = Field(default=0.0, ge=0.0, description="屋顶加固/改造 元")
    development_cost: float = Field(default=0.0, ge=0.0, description="开发费用 元")
    engineering_cost: float = Field(default=0.0, ge=0.0, description="工程费用 元")
    construction_cost: float = Field(default=0.0, ge=0.0, description="施工费用 元")
    other_capex: float = Field(default=0.0, ge=0.0, description="其他投资 元")
    contingency: float = Field(default=0.0, ge=0.0, description="预备费 元")

    detailed_items: dict[str, float] = Field(default_factory=dict, description="DETAILED 模式明细")


# --------------------------------------------------------------------------- #
# §53–§56 运维
# --------------------------------------------------------------------------- #
class OpexConfig(_Model):
    """运维参数（规范 §53–§56、§103）。"""

    pv_opex: float = Field(default=0.0, ge=0.0, description="光伏运维（值按模式解释）")
    pv_opex_mode: OpexMode = OpexMode.RATIO_OF_CAPEX

    storage_opex: float = Field(default=0.0, ge=0.0)
    storage_opex_mode: OpexMode = OpexMode.RATIO_OF_CAPEX

    insurance: float = Field(default=0.0, ge=0.0)
    insurance_mode: OpexMode = OpexMode.RATIO_OF_CAPEX

    management_cost: float = Field(default=0.0, ge=0.0)
    management_mode: OpexMode = OpexMode.FIXED

    other_opex: float = Field(default=0.0, ge=0.0)
    other_opex_mode: OpexMode = OpexMode.FIXED

    roof_rent_mode: RoofRentMode = RoofRentMode.FIXED
    rent_per_m2: float = Field(default=0.0, ge=0.0, description="面积模式单价 元/m²")
    rent_per_kw: float = Field(default=0.0, ge=0.0, description="容量模式单价 元/kWp")
    annual_fixed_rent: float = Field(default=0.0, ge=0.0, description="固定租金 元/年")

    annual_opex_growth_rate: float = Field(default=0.0, ge=-0.2, le=0.5, description="运维费用年增长率（小数）")


# --------------------------------------------------------------------------- #
# §57–§61 折旧与税
# --------------------------------------------------------------------------- #
class TaxConfig(_Model):
    """折旧与税务参数（规范 §57–§61）。"""

    depreciation_method: DepreciationMethod = DepreciationMethod.STRAIGHT_LINE
    depreciation_years: int = Field(default=20, gt=0, le=50, description="折旧年限")
    residual_value_ratio: float = Field(default=0.05, ge=0.0, le=1.0, description="残值率")
    depreciable_capex_ratio: float = Field(default=1.0, ge=0.0, le=1.0, description="可折旧投资占总投资比例")

    vat_rate: float = Field(default=0.13, ge=0.0, le=0.5, description="增值税税率")
    income_tax_rate: float = Field(default=0.25, ge=0.0, le=0.5, description="所得税税率")
    surcharge_rate: float = Field(default=0.0, ge=0.0, le=0.2, description="附加税费率（V1 简化：以不含税收入为基数）")
    other_tax_rate: float = Field(default=0.0, ge=0.0, le=0.2, description="其他税费率")

    revenue_is_vat_inclusive: bool = Field(default=False, description="收入是否为含税口径")

    # ---- LCOE/LCOS 成本抵减项（规范 §75 口径开关）----
    # 默认全部关闭，保持"成本 = 投资 + 运营成本"的原始口径；需要与行业/招标口径
    # （LCOE = [I₀ − 增值税抵扣 − 残值现值 + Σ运维] / Σ电量现值）对齐时开启。
    lcoe_vat_deductible_ratio: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="LCOE/LCOS 口径的增值税进项抵扣比例（0 = 不抵减）",
    )
    lcoe_residual_credit: bool = Field(
        default=False, description="是否将残值现值作为 LCOE/LCOS 的成本抵减"
    )


# --------------------------------------------------------------------------- #
# §62–§65 融资
# --------------------------------------------------------------------------- #
class FinancingConfig(_Model):
    """融资参数（规范 §62–§65、§104）。"""

    enabled: bool = True
    debt_ratio: float = Field(default=0.0, ge=0.0, le=1.0, description="贷款比例")
    equity_ratio: float = Field(default=1.0, ge=0.0, le=1.0, description="资本金比例")
    loan_amount: float | None = Field(default=None, ge=0.0, description="贷款金额（覆盖按比例计算的结果）")

    interest_rate: float = Field(default=0.04, ge=0.0, le=0.5, description="贷款利率")
    loan_term: int = Field(default=10, gt=0, le=40, description="贷款期限（含宽限期）")
    grace_period: int = Field(default=0, ge=0, le=39, description="宽限期（只付息不还本）")
    repayment_method: RepaymentMethod = RepaymentMethod.EQUAL_PRINCIPAL

    @model_validator(mode="after")
    def _check_ratios(self) -> FinancingConfig:
        if abs(self.debt_ratio + self.equity_ratio - 1.0) > 1e-6:
            raise ValueError("贷款比例与资本金比例之和必须等于 1")
        if self.enabled and self.grace_period >= self.loan_term:
            raise ValueError("宽限期必须小于贷款期限")
        return self


# --------------------------------------------------------------------------- #
# §34–§36 政策 Profile
# --------------------------------------------------------------------------- #
class PolicyProfile(_Model):
    """政策参数模型（规范 §34–§36、§89、§90）。

    **规范 §34：政策必须独立建模，不得写死在计算引擎里。**
    所有政策参数都带版本与出处，报告需显示"本测算采用 XX 政策版本"。
    """

    policy_id: str = ""
    policy_name: str = ""
    policy_version: str = ""
    effective_date: date | None = None
    expiry_date: date | None = None
    province: str = ""

    pricing_mechanism: str = Field(default="", description="价格机制说明")
    market_price: float = Field(default=0.0, ge=0.0)
    mechanism_price: float = Field(default=0.0, ge=0.0, description="机制电价 元/kWh")
    mechanism_volume_ratio: float = Field(default=0.0, ge=0.0, le=1.0, description="机制电量比例")
    green_energy_price: float = Field(default=0.0, ge=0.0)
    green_environmental_value: float = Field(default=0.0, ge=0.0)

    source: str = ""
    source_url: str = ""
    notes: str = ""

    @property
    def display_version(self) -> str:
        """"湖北新能源电价政策（版本：2025-10-01）"形式的展示名（规范 §35、§90）。"""
        if self.policy_version:
            return f"{self.policy_name}（版本：{self.policy_version}）"
        return self.policy_name


# --------------------------------------------------------------------------- #
# §92–§94 情景分析
# --------------------------------------------------------------------------- #
class ScenarioDelta(_Model):
    """情景相对基准的乘数（规范 §94）。

    这些乘数是**显式可修改的参数**，不是隐藏逻辑（规范 §158）。系统默认值仅为经验假设，
    使用时会自动标记为"假设值"（规范 §91）。
    """

    capex_multiplier: float = Field(default=1.0, gt=0.0)
    electricity_price_multiplier: float = Field(default=1.0, gt=0.0)
    generation_multiplier: float = Field(default=1.0, gt=0.0)
    opex_multiplier: float = Field(default=1.0, gt=0.0)
    self_consumption_ratio_multiplier: float = Field(default=1.0, gt=0.0)
    storage_cycles_multiplier: float = Field(default=1.0, gt=0.0)
    storage_capex_multiplier: float = Field(default=1.0, gt=0.0)
    interest_rate_multiplier: float = Field(default=1.0, gt=0.0)

    def describe(self) -> list[str]:
        """返回"参数=倍数"的可读列表，便于报告披露。"""
        labels = {
            "capex_multiplier": "总投资",
            "electricity_price_multiplier": "电价",
            "generation_multiplier": "发电量",
            "opex_multiplier": "运维成本",
            "self_consumption_ratio_multiplier": "自用比例",
            "storage_cycles_multiplier": "储能循环次数",
            "storage_capex_multiplier": "储能投资",
            "interest_rate_multiplier": "贷款利率",
        }
        out = []
        for key, label in labels.items():
            value = getattr(self, key)
            if abs(value - 1.0) > 1e-12:
                out.append(f"{label} × {value:g}")
        return out


def default_conservative_delta() -> ScenarioDelta:
    """系统默认"保守"情景乘数（假设值，可修改）。"""
    return ScenarioDelta(
        capex_multiplier=1.05,
        electricity_price_multiplier=0.90,
        generation_multiplier=0.95,
        opex_multiplier=1.05,
        self_consumption_ratio_multiplier=0.90,
        storage_cycles_multiplier=0.90,
        storage_capex_multiplier=1.05,
        interest_rate_multiplier=1.10,
    )


def default_optimistic_delta() -> ScenarioDelta:
    """系统默认"乐观"情景乘数（假设值，可修改）。"""
    return ScenarioDelta(
        capex_multiplier=0.95,
        electricity_price_multiplier=1.10,
        generation_multiplier=1.05,
        opex_multiplier=0.95,
        self_consumption_ratio_multiplier=1.10,
        storage_cycles_multiplier=1.10,
        storage_capex_multiplier=0.95,
        interest_rate_multiplier=0.90,
    )


class ScenarioConfig(_Model):
    """情景分析配置（规范 §92、§93）。

    **规范 §93：所有情景必须从 BASE 复制**，不允许"保守 → 乐观"链式推导。
    """

    enabled: bool = True
    conservative: ScenarioDelta = Field(default_factory=default_conservative_delta)
    optimistic: ScenarioDelta = Field(default_factory=default_optimistic_delta)


# --------------------------------------------------------------------------- #
# §95 敏感性分析
# --------------------------------------------------------------------------- #
class SensitivityConfig(_Model):
    """敏感性分析配置（规范 §95、§96）。"""

    enabled: bool = True
    steps: list[float] = Field(default_factory=lambda: [-0.2, -0.1, 0.0, 0.1, 0.2])
    variables: list[SensitivityVariable] = Field(
        default_factory=lambda: [
            SensitivityVariable.CAPEX,
            SensitivityVariable.ELECTRICITY_PRICE,
            SensitivityVariable.GENERATION,
            SensitivityVariable.OPEX,
            SensitivityVariable.SELF_CONSUMPTION_RATIO,
            SensitivityVariable.STORAGE_CYCLES,
            SensitivityVariable.STORAGE_CAPEX,
            SensitivityVariable.INTEREST_RATE,
        ]
    )


# --------------------------------------------------------------------------- #
# 项目聚合
# --------------------------------------------------------------------------- #
class Project(_Model):
    """一个完整的测算项目（规范 §11、§9）。

    保存为 ``.nep`` 文件；其中 ``parameter_registry`` 记录每个重要参数的来源（规范 §83）。
    """

    schema_version: str = "2.0"

    # V2 §65：V1 项目迁移留痕（既含参数未被修改，此处记录迁移过程供追溯）
    migration_notes: list[str] = Field(default_factory=list)

    basic_info: BasicInfo = Field(default_factory=BasicInfo)
    load: LoadConfig = Field(default_factory=LoadConfig)
    pv: PVConfig = Field(default_factory=PVConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    tariff: TariffConfig = Field(default_factory=TariffConfig)
    investment: InvestmentConfig = Field(default_factory=InvestmentConfig)
    opex: OpexConfig = Field(default_factory=OpexConfig)
    tax: TaxConfig = Field(default_factory=TaxConfig)
    financing: FinancingConfig = Field(default_factory=FinancingConfig)

    policy: PolicyProfile | None = None
    scenario: ScenarioConfig = Field(default_factory=ScenarioConfig)
    sensitivity: SensitivityConfig = Field(default_factory=SensitivityConfig)

    # V2 时序仿真配置（V2 §3、§6）。默认 enabled=False：完全走 V1 年度模式，
    # 保证 V1 项目打开后行为与结果不变（V2 §1.1）。
    timeseries: TimeSeriesConfig = Field(default_factory=TimeSeriesConfig)

    analysis_period: int = Field(default=25, gt=0, le=40, description="项目生命周期（年，规范 §15）")
    discount_rate: float = Field(default=0.08, ge=0.0, le=0.5, description="折现率（规范 §72）")

    parameter_registry: dict[str, ParameterMeta] = Field(default_factory=dict)

    @property
    def project_type(self) -> ProjectType:
        return self.basic_info.project_type

    def summary_line(self) -> str:
        """一行摘要，用于窗口标题与报告封面。"""
        info = self.basic_info
        return f"{info.project_name} | {info.project_type.label} | {info.province}{info.city}"
