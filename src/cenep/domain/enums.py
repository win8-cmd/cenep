"""枚举类型（规范 §13、§29、§34、§52、§57、§62、§84、§92、§144）。"""

from __future__ import annotations

from enum import StrEnum


class ProjectType(StrEnum):
    """项目类型（规范 §13）。V1 只允许三种工商业项目。"""

    COMMERCIAL_PV = "COMMERCIAL_PV"
    COMMERCIAL_STORAGE = "COMMERCIAL_STORAGE"
    PV_STORAGE = "PV_STORAGE"

    @property
    def label(self) -> str:
        return {
            ProjectType.COMMERCIAL_PV: "工商业光伏",
            ProjectType.COMMERCIAL_STORAGE: "工商业储能",
            ProjectType.PV_STORAGE: "工商业光储",
        }[self]

    @property
    def has_pv(self) -> bool:
        return self in (ProjectType.COMMERCIAL_PV, ProjectType.PV_STORAGE)

    @property
    def has_storage(self) -> bool:
        return self in (ProjectType.COMMERCIAL_STORAGE, ProjectType.PV_STORAGE)


class TariffMode(StrEnum):
    """电价模式（规范 §29）。"""

    FIXED = "FIXED"
    TOU = "TOU"
    MARKET = "MARKET"
    CUSTOM = "CUSTOM"

    @property
    def label(self) -> str:
        return {
            TariffMode.FIXED: "固定电价",
            TariffMode.TOU: "峰平谷电价",
            TariffMode.MARKET: "市场电价",
            TariffMode.CUSTOM: "自定义电价",
        }[self]


class InvestmentMode(StrEnum):
    """投资模式（规范 §52）。"""

    UNIT_PRICE = "UNIT_PRICE"
    DETAILED = "DETAILED"


class DepreciationMethod(StrEnum):
    """折旧方法（规范 §57）。V1 只支持直线法。"""

    STRAIGHT_LINE = "STRAIGHT_LINE"

    @property
    def label(self) -> str:
        return {"STRAIGHT_LINE": "直线法（年限平均法）"}[self.value]


class RepaymentMethod(StrEnum):
    """还款方式（规范 §62）。"""

    EQUAL_PRINCIPAL = "EQUAL_PRINCIPAL"
    EQUAL_INSTALLMENT = "EQUAL_INSTALLMENT"

    @property
    def label(self) -> str:
        return {
            RepaymentMethod.EQUAL_PRINCIPAL: "等额本金",
            RepaymentMethod.EQUAL_INSTALLMENT: "等额本息",
        }[self]


class OpexMode(StrEnum):
    """运维费用计算模式（规范 §54）。"""

    FIXED = "FIXED"
    RATIO_OF_CAPEX = "RATIO_OF_CAPEX"


class RoofRentMode(StrEnum):
    """屋顶租金模式（规范 §56）。"""

    AREA = "AREA"
    CAPACITY = "CAPACITY"
    FIXED = "FIXED"


class SourceType(StrEnum):
    """参数来源类型（规范 §84）。"""

    USER_INPUT = "USER_INPUT"
    POLICY = "POLICY"
    CONTRACT = "CONTRACT"
    HISTORICAL = "HISTORICAL"
    EXPERIENCE = "EXPERIENCE"
    ASSUMPTION = "ASSUMPTION"
    SYSTEM_DEFAULT = "SYSTEM_DEFAULT"
    CALCULATED = "CALCULATED"

    @property
    def label(self) -> str:
        return {
            SourceType.USER_INPUT: "用户输入",
            SourceType.POLICY: "政策参数",
            SourceType.CONTRACT: "合同参数",
            SourceType.HISTORICAL: "历史数据",
            SourceType.EXPERIENCE: "行业经验",
            SourceType.ASSUMPTION: "假设值",
            SourceType.SYSTEM_DEFAULT: "系统默认",
            SourceType.CALCULATED: "系统计算",
        }[self]

    @property
    def ui_color(self) -> str:
        """界面配色（规范 §144）。"""
        return {
            SourceType.USER_INPUT: "blue",
            SourceType.POLICY: "gray",
            SourceType.CONTRACT: "blue",
            SourceType.HISTORICAL: "blue",
            SourceType.EXPERIENCE: "yellow",
            SourceType.ASSUMPTION: "yellow",
            SourceType.SYSTEM_DEFAULT: "yellow",
            SourceType.CALCULATED: "green",
        }[self]


class ScenarioType(StrEnum):
    """情景类型（规范 §92）。"""

    CONSERVATIVE = "CONSERVATIVE"
    BASE = "BASE"
    OPTIMISTIC = "OPTIMISTIC"

    @property
    def label(self) -> str:
        return {
            ScenarioType.CONSERVATIVE: "保守",
            ScenarioType.BASE: "基准",
            ScenarioType.OPTIMISTIC: "乐观",
        }[self]


class SensitivityVariable(StrEnum):
    """敏感性分析变量（规范 §94）。"""

    CAPEX = "CAPEX"
    ELECTRICITY_PRICE = "ELECTRICITY_PRICE"
    GENERATION = "GENERATION"
    OPEX = "OPEX"
    SELF_CONSUMPTION_RATIO = "SELF_CONSUMPTION_RATIO"
    STORAGE_CYCLES = "STORAGE_CYCLES"
    STORAGE_CAPEX = "STORAGE_CAPEX"
    INTEREST_RATE = "INTEREST_RATE"

    @property
    def label(self) -> str:
        return {
            SensitivityVariable.CAPEX: "总投资",
            SensitivityVariable.ELECTRICITY_PRICE: "电价",
            SensitivityVariable.GENERATION: "发电量",
            SensitivityVariable.OPEX: "运维成本",
            SensitivityVariable.SELF_CONSUMPTION_RATIO: "自用比例",
            SensitivityVariable.STORAGE_CYCLES: "储能循环次数",
            SensitivityVariable.STORAGE_CAPEX: "储能投资",
            SensitivityVariable.INTEREST_RATE: "贷款利率",
        }[self]


class RiskLevel(StrEnum):
    """结果颜色分级（规范 §106）。仅作提示，**不得**据此直接判定项目"可行/不可行"。"""

    EXCELLENT = "EXCELLENT"
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    RISK = "RISK"

    @property
    def label(self) -> str:
        return {
            RiskLevel.EXCELLENT: "优秀",
            RiskLevel.NORMAL: "正常",
            RiskLevel.WATCH: "关注",
            RiskLevel.RISK: "风险",
        }[self]


# --------------------------------------------------------------------------- #
# V2 §3 P0.1 时序分辨率
# --------------------------------------------------------------------------- #
class Resolution(StrEnum):
    """时序数据分辨率（V2 §3 P0.1）。

    V2 默认 **1 小时粒度**；为 15 分钟数据预留接口（§3 P0.1）。
    """

    HOURLY = "HOURLY"
    QUARTER_HOURLY = "QUARTER_HOURLY"
    DAILY = "DAILY"
    MONTHLY = "MONTHLY"

    @property
    def label(self) -> str:
        return {
            Resolution.HOURLY: "1 小时",
            Resolution.QUARTER_HOURLY: "15 分钟",
            Resolution.DAILY: "日",
            Resolution.MONTHLY: "月",
        }[self]

    @property
    def points_per_year(self) -> int:
        """平年点数。闰年为 ``points_per_year + 1``（日粒度）或 ``+24``（小时粒度）。"""
        return {
            Resolution.HOURLY: 8760,
            Resolution.QUARTER_HOURLY: 35040,
            Resolution.DAILY: 365,
            Resolution.MONTHLY: 12,
        }[self]

    @property
    def delta_hours(self) -> float:
        """单个周期的时长（小时），用于 ``Δt`` 换算。"""
        return {
            Resolution.HOURLY: 1.0,
            Resolution.QUARTER_HOURLY: 0.25,
            Resolution.DAILY: 24.0,
            Resolution.MONTHLY: 0.0,  # 月粒度不用固定 Δt，按当月小时数处理
        }[self]


# --------------------------------------------------------------------------- #
# V2 §3 P0.2 负荷曲线来源模式
# --------------------------------------------------------------------------- #
class LoadProfileMode(StrEnum):
    """负荷曲线的三种取得方式（V2 §3 P0.2、§8）。"""

    HOURLY = "HOURLY"            # 用户导入 8760
    TYPICAL_DAY = "TYPICAL_DAY"  # 典型日曲线 × 月度系数
    ANNUAL_SIMPLE = "ANNUAL_SIMPLE"  # 年电量 + 昼夜占比（退化为 V1 口径）

    @property
    def label(self) -> str:
        return {
            LoadProfileMode.HOURLY: "导入 8760 小时曲线",
            LoadProfileMode.TYPICAL_DAY: "典型日曲线 × 月度系数",
            LoadProfileMode.ANNUAL_SIMPLE: "年电量 + 昼夜占比",
        }[self]


# --------------------------------------------------------------------------- #
# V2 §3 P0.3 光伏曲线来源模式
# --------------------------------------------------------------------------- #
class PVProfileMode(StrEnum):
    """光伏出力曲线来源（V2 §3 P0.3、§9）。"""

    HOURLY = "HOURLY"                # 用户导入 8760 出力系数
    TYPICAL_DAY = "TYPICAL_DAY"      # 典型小时曲线 × 月度系数
    MONTHLY_HOUR_FACTOR = "MONTHLY_HOUR_FACTOR"  # 月度 + 小时系数
    EQUIVALENT_HOURS = "EQUIVALENT_HOURS"        # 退化为 V1 口径（年等效小时）

    @property
    def label(self) -> str:
        return {
            PVProfileMode.HOURLY: "导入 8760 出力曲线",
            PVProfileMode.TYPICAL_DAY: "典型日曲线 × 月度系数",
            PVProfileMode.MONTHLY_HOUR_FACTOR: "月度系数 × 小时系数",
            PVProfileMode.EQUIVALENT_HOURS: "年等效小时（V1 口径）",
        }[self]


# --------------------------------------------------------------------------- #
# V2 §3 P0.4、§17 分时电价时段
# --------------------------------------------------------------------------- #
class TariffPeriod(StrEnum):
    """分时电价时段类型（V2 §3 P0.4、§17.1）。

    规范列举：尖峰 / 高峰 / 平段 / 谷段 / 深谷 / 自定义。
    """

    SHARP_PEAK = "SHARP_PEAK"
    PEAK = "PEAK"
    FLAT = "FLAT"
    VALLEY = "VALLEY"
    DEEP_VALLEY = "DEEP_VALLEY"
    CUSTOM = "CUSTOM"

    @property
    def label(self) -> str:
        return {
            TariffPeriod.SHARP_PEAK: "尖峰",
            TariffPeriod.PEAK: "高峰",
            TariffPeriod.FLAT: "平段",
            TariffPeriod.VALLEY: "谷段",
            TariffPeriod.DEEP_VALLEY: "深谷",
            TariffPeriod.CUSTOM: "自定义",
        }[self]

    @property
    def is_high(self) -> bool:
        """是否属于高价时段（用于储能放电判定）。"""
        return self in (TariffPeriod.SHARP_PEAK, TariffPeriod.PEAK)

    @property
    def is_low(self) -> bool:
        """是否属于低价时段（用于储能充电判定）。"""
        return self in (TariffPeriod.VALLEY, TariffPeriod.DEEP_VALLEY)


class DayType(StrEnum):
    """日类型（V2 §3 P0.4：工作日 / 周末 / 节假日）。"""

    WORKDAY = "WORKDAY"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"

    @property
    def label(self) -> str:
        return {
            DayType.WORKDAY: "工作日",
            DayType.WEEKEND: "周末",
            DayType.HOLIDAY: "节假日",
        }[self]


# --------------------------------------------------------------------------- #
# V2 §12–§14 储能调度策略
# --------------------------------------------------------------------------- #
class DispatchStrategy(StrEnum):
    """储能调度策略（V2 §12 峰谷套利 / §13 光伏自用优先 / §14 自动经济优化）。"""

    PEAK_VALLEY = "PEAK_VALLEY"
    PV_SELF_CONSUMPTION = "PV_SELF_CONSUMPTION"
    ECONOMIC_OPTIMIZATION = "ECONOMIC_OPTIMIZATION"

    @property
    def label(self) -> str:
        return {
            DispatchStrategy.PEAK_VALLEY: "峰谷套利",
            DispatchStrategy.PV_SELF_CONSUMPTION: "光伏自发自用优先",
            DispatchStrategy.ECONOMIC_OPTIMIZATION: "自动经济优化",
        }[self]


class DispatchAction(StrEnum):
    """储能逐时动作（V2 §16）。"""

    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"
    IDLE = "IDLE"

    @property
    def label(self) -> str:
        return {
            DispatchAction.CHARGE: "充电",
            DispatchAction.DISCHARGE: "放电",
            DispatchAction.IDLE: "不动作",
        }[self]


# --------------------------------------------------------------------------- #
# V2 §47 优化目标
# --------------------------------------------------------------------------- #
class OptimizationObjective(StrEnum):
    """方案寻优目标（V2 §47），默认 Max NPV。"""

    MAX_IRR = "MAX_IRR"
    MAX_NPV = "MAX_NPV"
    MIN_PAYBACK = "MIN_PAYBACK"
    MIN_LCOE = "MIN_LCOE"
    MIN_LCOS = "MIN_LCOS"
    MIN_ANNUAL_COST = "MIN_ANNUAL_COST"

    @property
    def label(self) -> str:
        return {
            OptimizationObjective.MAX_IRR: "最大 IRR",
            OptimizationObjective.MAX_NPV: "最大 NPV",
            OptimizationObjective.MIN_PAYBACK: "最短回收期",
            OptimizationObjective.MIN_LCOE: "最低 LCOE",
            OptimizationObjective.MIN_LCOS: "最低 LCOS",
            OptimizationObjective.MIN_ANNUAL_COST: "最低年度电费",
        }[self]


# --------------------------------------------------------------------------- #
# V2 §46 参数扫描维度
# --------------------------------------------------------------------------- #
class ScanVariable(StrEnum):
    """参数扫描维度（V2 §46）。"""

    PV_CAPACITY = "PV_CAPACITY"
    STORAGE_CAPACITY = "STORAGE_CAPACITY"
    STORAGE_POWER = "STORAGE_POWER"
    STORAGE_PRICE = "STORAGE_PRICE"
    TARIFF = "TARIFF"
    PEAK_VALLEY_SPREAD = "PEAK_VALLEY_SPREAD"
    LOAD = "LOAD"
    CAPEX = "CAPEX"

    @property
    def label(self) -> str:
        return {
            ScanVariable.PV_CAPACITY: "光伏容量",
            ScanVariable.STORAGE_CAPACITY: "储能容量",
            ScanVariable.STORAGE_POWER: "储能功率",
            ScanVariable.STORAGE_PRICE: "储能价格",
            ScanVariable.TARIFF: "电价",
            ScanVariable.PEAK_VALLEY_SPREAD: "峰谷价差",
            ScanVariable.LOAD: "负荷",
            ScanVariable.CAPEX: "CAPEX",
        }[self]


# --------------------------------------------------------------------------- #
# V2 §53 缺失数据处理策略
# --------------------------------------------------------------------------- #
class MissingDataPolicy(StrEnum):
    """缺失数据处理策略（V2 §53）。默认 **禁止静默填充**。"""

    REJECT = "REJECT"                      # 不允许计算（默认）
    LINEAR_INTERPOLATION = "LINEAR_INTERPOLATION"
    FORWARD_FILL = "FORWARD_FILL"
    TYPICAL_DAY_FILL = "TYPICAL_DAY_FILL"

    @property
    def label(self) -> str:
        return {
            MissingDataPolicy.REJECT: "不允许计算（报错）",
            MissingDataPolicy.LINEAR_INTERPOLATION: "线性插值",
            MissingDataPolicy.FORWARD_FILL: "前值填充",
            MissingDataPolicy.TYPICAL_DAY_FILL: "典型日填充",
        }[self]
