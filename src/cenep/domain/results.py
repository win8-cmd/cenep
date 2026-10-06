"""计算结果数据结构（规范 §80、§81）。

* :class:`AnnualResult` —— 每一年必须生成的完整年度结果（§80）
* :class:`CalculationResult` —— 唯一结果对象（§81），GUI / Excel / PDF 都只展示它
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .timeseries_results import (
    BaselineResult,
    DataQualityScore,
    DispatchDecision,
    EnergyBalance,
    OptimizationResult,
    ScenarioResult,
    TimeSeriesReport,
)


class AnnualResult(BaseModel):
    """年度结果（规范 §80）。所有金额单位：元；电量单位：kWh。"""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(description="年份，1 = 第一个运营年度")

    # ---- 负荷与光伏 ----
    load_kwh: float = 0.0
    pv_generation_kwh: float = 0.0
    pv_self_use_kwh: float = 0.0
    pv_export_kwh: float = 0.0
    pv_to_storage_kwh: float = 0.0
    pv_loss_kwh: float = 0.0

    # ---- 储能 ----
    storage_available_kwh: float = 0.0
    storage_charge_kwh: float = 0.0
    storage_discharge_kwh: float = 0.0
    storage_grid_charge_kwh: float = 0.0

    # ---- 收入 ----
    pv_self_use_revenue: float = 0.0
    pv_export_revenue: float = 0.0
    pv_other_revenue: float = 0.0
    storage_arbitrage_revenue: float = 0.0
    storage_capacity_revenue: float = 0.0
    storage_ancillary_revenue: float = 0.0
    storage_other_revenue: float = 0.0
    total_revenue: float = 0.0

    # ---- 成本与利润 ----
    revenue_net: float = Field(default=0.0, description="参与利润表的不含税收入口径")
    opex: float = 0.0
    depreciation: float = 0.0
    ebitda: float = 0.0
    ebit: float = 0.0
    interest: float = 0.0
    ebt: float = 0.0
    taxable_income: float = 0.0
    income_tax: float = 0.0
    surcharge: float = 0.0
    other_tax: float = 0.0
    cash_tax: float = 0.0

    # ---- 投资 ----
    capex: float = 0.0
    replacement_capex: float = 0.0
    residual_value: float = 0.0

    # ---- 现金流 ----
    project_cashflow: float = 0.0
    equity_cashflow: float = 0.0

    # ---- 债务 ----
    debt_begin: float = 0.0
    debt_drawdown: float = 0.0
    principal_repayment: float = 0.0
    debt_end: float = 0.0

    # ---- 偿债 ----
    cfads: float = 0.0
    debt_service: float = 0.0
    dscr: float | None = None

    # ---- 累计 ----
    cumulative_project_cashflow: float = 0.0
    cumulative_equity_cashflow: float = 0.0


class SensitivityRow(BaseModel):
    """敏感性分析单行（规范 §95、§96）。

    ``change`` 为参数变化率（-0.2 = -20%）；``irr_change`` 为 IRR 相对基准的变化率；
    ``coefficient`` 为敏感度系数 = IRR 变化率 / 参数变化率。
    """

    model_config = ConfigDict(extra="forbid")

    variable: str
    variable_label: str
    change: float
    project_irr: float | None = None
    equity_irr: float | None = None
    project_npv: float = 0.0
    static_payback: float | None = None
    irr_change: float | None = None
    coefficient: float | None = None


class ScenarioSummary(BaseModel):
    """情景分析结果摘要（规范 §92、§93）。"""

    model_config = ConfigDict(extra="forbid")

    scenario: str
    label: str
    total_capex: float = 0.0
    first_year_revenue: float = 0.0
    project_irr: float | None = None
    equity_irr: float | None = None
    project_npv: float = 0.0
    static_payback: float | None = None
    deltas: list[str] = Field(default_factory=list)


class CalculationResult(BaseModel):
    """唯一计算结果对象（规范 §81）。

    GUI、Excel、PDF **只能**消费本对象，不得重新计算（规范 §109、§148–§150）。
    """

    model_config = ConfigDict(extra="forbid")

    # ---- 项目标识 ----
    project_name: str = ""
    project_type: str = ""
    province: str = ""
    city: str = ""

    # ---- 规模 ----
    pv_capacity_kwp: float = 0.0
    storage_power_kw: float = 0.0
    storage_energy_kwh: float = 0.0
    storage_duration_hours: float = 0.0
    analysis_period: int = 0

    # ---- 投资 ----
    total_capex: float = 0.0
    unit_investment: dict[str, float] = Field(default_factory=dict)
    capex_breakdown: dict[str, float] = Field(default_factory=dict)

    # ---- 融资 ----
    loan_amount: float = 0.0
    equity_amount: float = 0.0

    # ---- 首年概览 ----
    first_year_generation: float = 0.0
    first_year_self_use_energy: float = 0.0
    first_year_export_energy: float = 0.0
    first_year_revenue: float = 0.0
    first_year_opex: float = 0.0
    first_year_cashflow: float = 0.0
    annual_revenue: float = 0.0
    annual_opex: float = 0.0

    # ---- 指标 ----
    project_irr: float | None = None
    equity_irr: float | None = None
    project_npv: float = 0.0
    equity_npv: float = 0.0
    static_payback: float | None = None
    discounted_payback: float | None = None
    lcoe: float | None = None
    lcos: float | None = None
    roi: float | None = None
    min_dscr: float | None = None

    # ---- 现金流序列（含 Year 0）----
    project_cashflows: list[float] = Field(default_factory=list)
    equity_cashflows: list[float] = Field(default_factory=list)
    cumulative_cashflow: list[float] = Field(default_factory=list)

    # ---- 明细 ----
    annual_results: list[AnnualResult] = Field(default_factory=list)

    # ---- 情景与敏感性（§92、§95，同属唯一结果对象，供 GUI/Excel/PDF 使用）----
    scenarios: list[ScenarioSummary] = Field(default_factory=list)
    sensitivity: list[SensitivityRow] = Field(default_factory=list)

    # ---- V2 时序仿真（V2 §62 要求 CalculationResult 增加的字段）----
    # time_series_results：8760 时序仿真总容器（列式逐时结果 + 指标 + 能量平衡 + 数据质量）
    # baseline_results   ：基准方案（无 PV、无储能）电费与需量，用于计算节省额（V2 §42）
    # dispatch_results   ：逐时调度决策与可解释原因（V2 §16）
    # energy_balance     ：全场站能量守恒汇总（V2 §19、§70）
    # data_quality       ：导入数据质量评分（V2 §55）
    # scenario_results   ：方案比较（V2 §43、§44）
    # optimization_results：扫描与寻优结果（V2 §45–§48）
    # 注：annual_results 为 V1 既有字段，V2 沿用同一口径存放年度经济结果（V2 §62）。
    time_series_results: TimeSeriesReport | None = None
    baseline_results: BaselineResult | None = None
    dispatch_results: list[DispatchDecision] = Field(default_factory=list)
    energy_balance: EnergyBalance | None = None
    data_quality: DataQualityScore | None = None
    scenario_results: list[ScenarioResult] = Field(default_factory=list)
    optimization_results: OptimizationResult | None = None

    # ---- 参数来源（§83、§108）----
    parameter_sources: dict[str, dict] = Field(default_factory=dict)

    # ---- 口径说明 ----
    notes: list[str] = Field(default_factory=list)
