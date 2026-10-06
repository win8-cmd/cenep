"""V2 时序仿真的**结果**模型（V2 §6、§16、§19、§41–§48、§55、§62、§63、§70）。

与 :mod:`cenep.domain.timeseries` 一样，本模块只描述"结果长什么样"，**不含任何计算**。

列式存储 vs 行视图（V2 §6 与 §87 的取舍）
--------------------------------------
:class:`HourlyResult` 是 V2 §6 要求的逐时结果模型，但它**不是**引擎内部的存储结构：
8760 个 Pydantic 对象会同时命中 V2 §87「禁止每小时创建大量 Python 对象」。
因此：

* :class:`TimeSeriesResultSet` 用**列式**（``list[float]``，由 NumPy 数组一次性 ``tolist()`` 转换）
  作为真实存储；
* :class:`HourlyResult` 由 :meth:`TimeSeriesResultSet.row` **按需**生成单行视图，
  用于报告、界面明细与测试断言；
* 二者字段一一对应，数值完全一致（V2 §105）。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from .base import NON_NEG, _Model
from .enums import DispatchAction, DispatchStrategy, OptimizationObjective, Resolution

#: 逐时结果的列名（列式存储与 :class:`HourlyResult` 的对应关系，顺序即字段顺序）
HOURLY_COLUMNS: tuple[str, ...] = (
    "load",
    "pv_generation",
    "pv_to_load",
    "pv_to_storage",
    "pv_to_grid",
    "pv_curtailed",
    "grid_to_load",
    "grid_to_storage",
    "storage_charge",
    "storage_discharge",
    "storage_soc_start",
    "storage_soc_end",
    "load_from_storage",
    "grid_import",
    "grid_export",
    "electricity_cost",
    "export_revenue",
    "storage_revenue",
    "total_revenue",
    "net_energy_cost",
    "electricity_price",
    "export_price",
)


# --------------------------------------------------------------------------- #
# V2 §16 逐时可解释调度决策
# --------------------------------------------------------------------------- #
class DispatchDecision(_Model):
    """储能逐时调度决策及其**可解释原因**（V2 §16）。

    规范要求"每一个小时必须能够解释：为什么充电？为什么放电？为什么不动作"，
    因此 ``reason`` 是必填的**人类可读中文原因**（由 :mod:`cenep.calculation.dispatch_engine`
    按固定原因码表产生，原因码表见 ``STORAGE_DISPATCH.md``）。
    """

    timestamp: datetime
    action: DispatchAction = DispatchAction.IDLE
    reason: str = ""
    reason_code: str = Field(default="", description="原因码，便于统计与测试")
    charge_energy: float = NON_NEG
    discharge_energy: float = NON_NEG


# --------------------------------------------------------------------------- #
# V2 §6 逐时结果（行视图）
# --------------------------------------------------------------------------- #
class HourlyResult(_Model):
    """单个小时的完整能源流与经济结果（V2 §6，字段"至少包括"清单）。

    电量单位 kWh（1 小时粒度下即 kW·h），电价单位 元/kWh，费用单位 元，SOC 为小数。
    """

    timestamp: datetime

    load: float = 0.0
    pv_generation: float = 0.0

    pv_to_load: float = 0.0
    pv_to_storage: float = 0.0
    pv_to_grid: float = 0.0
    pv_curtailed: float = 0.0

    grid_to_load: float = 0.0
    grid_to_storage: float = 0.0

    storage_charge: float = 0.0
    storage_discharge: float = 0.0
    storage_soc_start: float = 0.0
    storage_soc_end: float = 0.0
    load_from_storage: float = 0.0

    grid_import: float = 0.0
    grid_export: float = 0.0

    electricity_cost: float = 0.0
    export_revenue: float = 0.0
    storage_revenue: float = 0.0
    total_revenue: float = 0.0
    net_energy_cost: float = 0.0

    # ---- 附加字段（便于报告与追溯，规范 §105）----
    electricity_price: float = 0.0
    export_price: float = 0.0
    dispatch_action: DispatchAction = DispatchAction.IDLE
    dispatch_reason: str = ""


# --------------------------------------------------------------------------- #
# V2 §19、§70 能量平衡
# --------------------------------------------------------------------------- #
class EnergyBalance(_Model):
    """全场站能量平衡汇总（V2 §19、§70）。

    平衡式（V2 §19）：``PV + 购电 + 储能放电 = 负荷 + 储能充电 + 上网 + 弃光``。
    任何时点 ``BalanceError > tolerance`` 必须判定计算失败（V2 §19）。
    """

    pv_generation: float = 0.0
    pv_to_load: float = 0.0
    pv_to_storage: float = 0.0
    pv_to_grid: float = 0.0
    pv_curtailed: float = 0.0

    grid_to_load: float = 0.0
    grid_to_storage: float = 0.0
    grid_import: float = 0.0

    storage_charge: float = 0.0
    storage_discharge: float = 0.0
    storage_to_load: float = 0.0
    storage_to_grid: float = 0.0

    grid_export: float = 0.0
    load_total: float = 0.0

    supply_total: float = Field(default=0.0, description="PV + 购电 + 储能放电")
    demand_total: float = Field(default=0.0, description="负荷 + 储能充电 + 上网 + 弃光")
    error: float = Field(default=0.0, description="supply_total − demand_total")
    max_hourly_error: float = 0.0
    tolerance: float = 1e-6
    is_balanced: bool = False


# --------------------------------------------------------------------------- #
# V2 §55 数据质量
# --------------------------------------------------------------------------- #
class DataQualityIssue(_Model):
    """一条数据质量问题（V2 §53、§54）。"""

    level: str = Field(default="WARNING", description="ERROR / WARNING / INFO")
    category: str = Field(default="", description="completeness / continuity / outlier / source")
    message: str = ""
    count: int = 0
    samples: list[str] = Field(default_factory=list, description="示例时间戳，最多若干条")


class DataQualityScore(_Model):
    """数据质量评分（V2 §55），总分 0~100。

    四个分项：``completeness`` 完整性、``continuity`` 连续性、
    ``outlier`` 异常值、``source_credibility`` 来源可信度。
    """

    score: float = Field(default=0.0, ge=0.0, le=100.0)
    completeness: float = Field(default=0.0, ge=0.0, le=100.0)
    continuity: float = Field(default=0.0, ge=0.0, le=100.0)
    outlier: float = Field(default=0.0, ge=0.0, le=100.0)
    source_credibility: float = Field(default=0.0, ge=0.0, le=100.0)
    issues: list[DataQualityIssue] = Field(default_factory=list)

    @property
    def level_label(self) -> str:
        if self.score >= 90:
            return "优秀"
        if self.score >= 75:
            return "良好"
        if self.score >= 60:
            return "可用"
        return "较差"


# --------------------------------------------------------------------------- #
# V2 §6、§62 列式时序结果集
# --------------------------------------------------------------------------- #
class TimeSeriesResultSet(_Model):
    """列式时序结果集（V2 §6 的 ``HourlyResult`` 集合 + §87 的性能要求）。

    真实存储是列（``list[float]``）；逐时对象由 :meth:`row` 按需生成。
    构造由 :meth:`from_columns` 完成，内部走 ``model_construct`` 跳过逐元素校验
    —— 数据来自引擎自身、类型已确定，逐元素校验 8760×22 个浮点数是纯粹的开销
    （V2 §86 要求单项目 8760 计算 < 2 秒）。
    """

    resolution: Resolution = Resolution.HOURLY
    base_year: int = 2025
    timestamps: list[datetime] = Field(default_factory=list)

    load: list[float] = Field(default_factory=list)
    pv_generation: list[float] = Field(default_factory=list)
    pv_to_load: list[float] = Field(default_factory=list)
    pv_to_storage: list[float] = Field(default_factory=list)
    pv_to_grid: list[float] = Field(default_factory=list)
    pv_curtailed: list[float] = Field(default_factory=list)
    grid_to_load: list[float] = Field(default_factory=list)
    grid_to_storage: list[float] = Field(default_factory=list)
    storage_charge: list[float] = Field(default_factory=list)
    storage_discharge: list[float] = Field(default_factory=list)
    storage_soc_start: list[float] = Field(default_factory=list)
    storage_soc_end: list[float] = Field(default_factory=list)
    load_from_storage: list[float] = Field(default_factory=list)
    grid_import: list[float] = Field(default_factory=list)
    grid_export: list[float] = Field(default_factory=list)
    electricity_cost: list[float] = Field(default_factory=list)
    export_revenue: list[float] = Field(default_factory=list)
    storage_revenue: list[float] = Field(default_factory=list)
    total_revenue: list[float] = Field(default_factory=list)
    net_energy_cost: list[float] = Field(default_factory=list)
    electricity_price: list[float] = Field(default_factory=list)
    export_price: list[float] = Field(default_factory=list)

    dispatch_actions: list[str] = Field(default_factory=list, description="逐时动作码")
    dispatch_reasons: list[str] = Field(default_factory=list, description="逐时原因（中文）")

    @classmethod
    def from_columns(
        cls,
        *,
        timestamps: list[datetime],
        columns: dict[str, list[float]],
        resolution: Resolution = Resolution.HOURLY,
        base_year: int = 2025,
        dispatch_actions: list[str] | None = None,
        dispatch_reasons: list[str] | None = None,
    ) -> TimeSeriesResultSet:
        """由引擎的列式结果构造（跳过逐元素校验，见类文档）。"""
        payload: dict[str, object] = {
            "resolution": resolution,
            "base_year": base_year,
            "timestamps": timestamps,
            "dispatch_actions": dispatch_actions or [],
            "dispatch_reasons": dispatch_reasons or [],
        }
        for name in HOURLY_COLUMNS:
            payload[name] = columns.get(name, [])
        return cls.model_construct(**payload)

    def __len__(self) -> int:
        return len(self.timestamps)

    def column(self, name: str) -> list[float]:
        """按列名取列，列名见 :data:`HOURLY_COLUMNS`。"""
        if name not in HOURLY_COLUMNS:
            raise KeyError(f"未知的时序结果列：{name}")
        return getattr(self, name)

    def row(self, index: int) -> HourlyResult:
        """取第 ``index`` 行的逐时结果（行视图，V2 §6）。"""
        if not 0 <= index < len(self.timestamps):
            raise IndexError(f"逐时结果行号越界：{index}（共 {len(self.timestamps)} 行）")
        values = {name: self.column(name)[index] for name in HOURLY_COLUMNS}
        actions = self.dispatch_actions[index] if index < len(self.dispatch_actions) else ""
        reasons = self.dispatch_reasons[index] if index < len(self.dispatch_reasons) else ""
        return HourlyResult(
            timestamp=self.timestamps[index],
            dispatch_action=DispatchAction(actions) if actions else DispatchAction.IDLE,
            dispatch_reason=reasons,
            **values,
        )

    def rows(self, indices: list[int] | None = None) -> list[HourlyResult]:
        """取多行逐时结果；``indices=None`` 表示全部（注意 8760 行会产生大量对象）。"""
        if indices is None:
            indices = list(range(len(self.timestamps)))
        return [self.row(i) for i in indices]

    def slice_hours(self, start_hour: int, end_hour: int) -> TimeSeriesResultSet:
        """按小时区间切片（半开区间），用于界面「日 / 月 / 年」筛选（V2 §69）。"""
        payload: dict[str, object] = {
            "resolution": self.resolution,
            "base_year": self.base_year,
            "timestamps": self.timestamps[start_hour:end_hour],
            "dispatch_actions": self.dispatch_actions[start_hour:end_hour],
            "dispatch_reasons": self.dispatch_reasons[start_hour:end_hour],
        }
        for name in HOURLY_COLUMNS:
            payload[name] = self.column(name)[start_hour:end_hour]
        return TimeSeriesResultSet.model_construct(**payload)


# --------------------------------------------------------------------------- #
# V2 §41 时序关键指标
# --------------------------------------------------------------------------- #
class TimeSeriesMetrics(_Model):
    """时序仿真得到的关键指标（V2 §41）。

    自用率与自给率**必须区分**（V2 §22、§23）：
    ``self_consumption_rate`` = 光伏自用电量 ÷ 光伏发电量；
    ``self_sufficiency_rate`` = 光伏自用电量 ÷ 负荷电量。二者不得混用。
    """

    annual_pv_generation: float = 0.0
    annual_pv_curtailment: float = 0.0
    annual_grid_purchase: float = 0.0
    annual_grid_export: float = 0.0
    annual_load: float = 0.0

    annual_storage_charge: float = 0.0
    annual_storage_discharge: float = 0.0
    annual_grid_charge: float = 0.0
    configured_cycles: float = 0.0
    equivalent_cycles: float = 0.0

    self_consumption_rate: float = 0.0
    self_sufficiency_rate: float = 0.0

    peak_demand_before: float = 0.0
    peak_demand_after: float = 0.0
    demand_saving: float = 0.0
    monthly_max_demand_before: list[float] = Field(default_factory=list)
    monthly_max_demand_after: list[float] = Field(default_factory=list)

    baseline_electricity_cost: float = 0.0
    actual_electricity_cost: float = 0.0
    electricity_cost_saving: float = 0.0

    baseline_demand_cost: float = 0.0
    actual_demand_cost: float = 0.0
    demand_cost_saving: float = 0.0

    pv_self_consumption_saving: float = 0.0
    pv_export_revenue: float = 0.0
    storage_arbitrage_revenue: float = 0.0
    storage_capacity_revenue: float = 0.0
    storage_ancillary_revenue: float = 0.0
    other_revenue: float = 0.0
    total_benefit: float = 0.0


# --------------------------------------------------------------------------- #
# V2 §42 基准方案
# --------------------------------------------------------------------------- #
class BaselineResult(_Model):
    """基准方案：无光伏、无储能、全部负荷由电网供应（V2 §42）。

    仅用于计算 ``BaselineElectricityCost`` 与 ``BaselineDemandCost``。
    """

    annual_load: float = 0.0
    annual_electricity_cost: float = 0.0
    annual_demand_cost: float = 0.0
    peak_demand_kw: float = 0.0
    monthly_max_demand: list[float] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# V2 §43、§44 方案结果
# --------------------------------------------------------------------------- #
class ScenarioResult(_Model):
    """一个候选方案的完整结果摘要（V2 §43、§44）。"""

    name: str = ""
    label: str = ""
    is_baseline: bool = False

    pv_capacity_kwp: float = 0.0
    storage_power_kw: float = 0.0
    storage_energy_kwh: float = 0.0

    total_capex: float = 0.0
    project_irr: float | None = None
    equity_irr: float | None = None
    project_npv: float = 0.0
    static_payback: float | None = None
    discounted_payback: float | None = None
    lcoe: float | None = None
    lcos: float | None = None

    annual_saving: float = 0.0
    self_consumption_rate: float = 0.0
    self_sufficiency_rate: float = 0.0
    equivalent_cycles: float = 0.0
    demand_saving: float = 0.0

    metrics: TimeSeriesMetrics = Field(default_factory=TimeSeriesMetrics)


# --------------------------------------------------------------------------- #
# V2 §45–§48 优化与扫描
# --------------------------------------------------------------------------- #
class OptimizationCandidate(_Model):
    """扫描中的一个候选方案（V2 §45、§46）。"""

    run_id: str = ""
    variables: dict[str, float] = Field(default_factory=dict, description="扫描维度 → 取值")
    objective: OptimizationObjective = OptimizationObjective.MAX_NPV
    objective_value: float = 0.0
    feasible: bool = True
    note: str = ""
    scenario: ScenarioResult = Field(default_factory=ScenarioResult)


class OptimizationResult(_Model):
    """扫描与寻优结果（V2 §47、§48）。

    §48 明确**禁止黑盒**：必须同时给出输入参数、候选方案、约束条件、计算结果与最优方案，
    并说明"为什么这个方案是最优"，即 ``explanation``。
    """

    objective: OptimizationObjective = OptimizationObjective.MAX_NPV
    scan_variables: list[str] = Field(default_factory=list, description="本次扫描的维度")
    constraints: list[str] = Field(default_factory=list, description="生效的约束说明")
    candidates: list[OptimizationCandidate] = Field(default_factory=list)
    best_run_id: str = ""
    best_variables: dict[str, float] = Field(default_factory=dict)
    best_scenario: ScenarioResult | None = None
    explanation: list[str] = Field(default_factory=list, description="为什么该方案最优（中文）")
    elapsed_seconds: float = 0.0


# --------------------------------------------------------------------------- #
# V2 §62 时序相关的总结果容器
# --------------------------------------------------------------------------- #
class TimeSeriesReport(_Model):
    """``CalculationResult`` 里 V2 时序部分的总容器（V2 §62）。"""

    enabled: bool = False
    resolution: Resolution = Resolution.HOURLY
    base_year: int = 2025
    dispatch_strategy: DispatchStrategy = DispatchStrategy.PV_SELF_CONSUMPTION

    hourly: TimeSeriesResultSet | None = None
    metrics: TimeSeriesMetrics = Field(default_factory=TimeSeriesMetrics)
    balance: EnergyBalance | None = None
    baseline: BaselineResult | None = None
    data_quality: DataQualityScore | None = None

    as_of: date | None = None
