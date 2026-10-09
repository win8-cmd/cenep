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
    """时序数据分辨率（V2 §3 P0.1、V2.2 §6.1）。

    V2 默认 **1 小时粒度**；为 15 分钟数据预留接口（§3 P0.1）。

    **V2.2 阶段 3 增量**：新增成员 ``HALF_HOURLY``（30 分钟）。
    规格书 §2.2／§6.1 要求"支持 15、30、60 分钟负荷数据"，而 V2 的
    ``Resolution`` 只有 15 分钟与 1 小时两档，30 分钟数据若强行记为
    ``QUARTER_HOURLY`` 会把 17520 点伪装成 35040 点的 15 分钟曲线
    （违反 §0.2"不得把估算/异粒度数据伪装成实测粒度"的同类红线），
    因此这里**只追加成员**，既有四个成员的取值与语义完全不变，
    既有项目文件与对外接口不受影响。
    """

    HOURLY = "HOURLY"
    HALF_HOURLY = "HALF_HOURLY"
    QUARTER_HOURLY = "QUARTER_HOURLY"
    DAILY = "DAILY"
    MONTHLY = "MONTHLY"

    @property
    def label(self) -> str:
        return {
            Resolution.HOURLY: "1 小时",
            Resolution.HALF_HOURLY: "30 分钟",
            Resolution.QUARTER_HOURLY: "15 分钟",
            Resolution.DAILY: "日",
            Resolution.MONTHLY: "月",
        }[self]

    @property
    def points_per_year(self) -> int:
        """平年点数。闰年为 ``points_per_year + 1``（日粒度）或 ``+24``（小时粒度）。"""
        return {
            Resolution.HOURLY: 8760,
            Resolution.HALF_HOURLY: 17520,
            Resolution.QUARTER_HOURLY: 35040,
            Resolution.DAILY: 365,
            Resolution.MONTHLY: 12,
        }[self]

    @property
    def delta_hours(self) -> float:
        """单个周期的时长（小时），用于 ``Δt`` 换算。"""
        return {
            Resolution.HOURLY: 1.0,
            Resolution.HALF_HOURLY: 0.5,
            Resolution.QUARTER_HOURLY: 0.25,
            Resolution.DAILY: 24.0,
            Resolution.MONTHLY: 0.0,  # 月粒度不用固定 Δt，按当月小时数处理
        }[self]

    @property
    def interval_minutes(self) -> int:
        """单周期分钟数；月粒度无固定间隔，返回 0。"""
        return {
            Resolution.HOURLY: 60,
            Resolution.HALF_HOURLY: 30,
            Resolution.QUARTER_HOURLY: 15,
            Resolution.DAILY: 1440,
            Resolution.MONTHLY: 0,
        }[self]

    @classmethod
    def from_interval_minutes(cls, minutes: float) -> Resolution | None:
        """间隔分钟数 → 分辨率；不支持的间隔返回 ``None``。

        调用方（``cenep.data.load_profile_importer``）必须对 ``None`` 给出**中文错误**，
        不得在这里静默猜一个近似粒度（V2.2 §6.1、§9.2）。
        """
        mapping = {
            15: cls.QUARTER_HOURLY,
            30: cls.HALF_HOURLY,
            60: cls.HOURLY,
            1440: cls.DAILY,
        }
        try:
            key = int(round(float(minutes)))
        except (TypeError, ValueError):
            return None
        return mapping.get(key)


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


# --------------------------------------------------------------------------- #
# V2.1 §2.1 月电费账单事实
# --------------------------------------------------------------------------- #
# 说明（增量开发约束 V2.1 §0.2）：
# 以下枚举为 **V2.1 新增**，仅追加在文件末尾，未修改任何既有枚举的成员或取值，
# 因此 V2 的公开接口与既有序列化数据完全不受影响。
#
# 与既有枚举的命名差异：本文件的既有枚举取值用 ``UPPER_CASE``（如
# ``TariffPeriod.SHARP_PEAK``），而账单三类的取值必须保持 V2.1 §2.1 规定的
# ``snake_case`` 字面量（``single_part`` / ``two_part`` / ``unknown`` /
# ``manual`` / ``excel`` / ``estimated`` / ``valid`` / ``warning`` / ``invalid``），
# 因为它们是账单模型对外的字段契约。中文名一律通过 :attr:`label` 提供。
# --------------------------------------------------------------------------- #
class TariffStructure(StrEnum):
    """计费方式：单一制 / 两部制 / 未知（V2.1 §2.1）。"""

    SINGLE_PART = "single_part"
    TWO_PART = "two_part"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            TariffStructure.SINGLE_PART: "单一制",
            TariffStructure.TWO_PART: "两部制",
            TariffStructure.UNKNOWN: "未知",
        }[self]

    @property
    def is_two_part(self) -> bool:
        """是否两部制（两部制才有基本电费：容量电费或需量电费，§3.4）。"""
        return self is TariffStructure.TWO_PART


class BillSourceType(StrEnum):
    """账单事实的数据来源（V2.1 §2.1）。

    ``estimated`` 只用于"用户明确标注为估算的账单"，**不得**用它给 Excel 导入或
    手动录入的数据打标（V2.1 §0.2：估算与实测必须可区分）。
    """

    MANUAL = "manual"
    EXCEL = "excel"
    ESTIMATED = "estimated"

    @property
    def label(self) -> str:
        return {
            BillSourceType.MANUAL: "手动录入",
            BillSourceType.EXCEL: "Excel 导入",
            BillSourceType.ESTIMATED: "估算",
        }[self]

    @property
    def parameter_source(self) -> SourceType:
        """映射到既有来源枚举（规范 §84、§91），供质量评分与报告复用。

        ``manual`` / ``excel`` 都是用户提供的账单事实，按"用户输入"计；
        ``estimated`` 是估算，必须按"假设值"计（不得与事实同分）。
        """
        return {
            BillSourceType.MANUAL: SourceType.USER_INPUT,
            BillSourceType.EXCEL: SourceType.USER_INPUT,
            BillSourceType.ESTIMATED: SourceType.ASSUMPTION,
        }[self]


class BillQualityStatus(StrEnum):
    """账单数据质量状态（V2.1 §2.1、§5.5）。"""

    VALID = "valid"
    WARNING = "warning"
    INVALID = "invalid"

    @property
    def label(self) -> str:
        return {
            BillQualityStatus.VALID: "有效",
            BillQualityStatus.WARNING: "有警告",
            BillQualityStatus.INVALID: "无效",
        }[self]


class BillEnergyPeriod(StrEnum):
    """账单分时电量时段（V2.1 §2.1、§3.1）。

    **命名约定（§2.1 明确要求"统一枚举定义"）**：

    * ``valley`` = **低谷**（谷段）；
    * ``offpeak`` = **深谷**，与低谷**不是**同一时段，不得理解为低谷的同义词；
    * ``flat`` = 平段，因此不得把 ``offpeak`` 读成"非峰段/平段"。

    与政策侧时段枚举 :class:`TariffPeriod` 的对应关系见 :attr:`tariff_period`，
    保证账单事实与政策时段规则使用同一套语义（§0.2：时段规则与电价数值分离）。
    """

    SHARP = "sharp"
    PEAK = "peak"
    FLAT = "flat"
    VALLEY = "valley"
    OFFPEAK = "offpeak"

    @property
    def label(self) -> str:
        return {
            BillEnergyPeriod.SHARP: "尖峰",
            BillEnergyPeriod.PEAK: "高峰",
            BillEnergyPeriod.FLAT: "平段",
            BillEnergyPeriod.VALLEY: "低谷",
            BillEnergyPeriod.OFFPEAK: "深谷",
        }[self]

    @property
    def field_name(self) -> str:
        """:class:`~cenep.domain.bill_models.ElectricityBill` 中对应的字段名。"""
        return {
            BillEnergyPeriod.SHARP: "energy_sharp_kwh",
            BillEnergyPeriod.PEAK: "energy_peak_kwh",
            BillEnergyPeriod.FLAT: "energy_flat_kwh",
            BillEnergyPeriod.VALLEY: "energy_valley_kwh",
            BillEnergyPeriod.OFFPEAK: "energy_offpeak_kwh",
        }[self]

    @property
    def tariff_period(self) -> TariffPeriod:
        """映射到政策侧时段（V2 §3 P0.4、§17.1）。"""
        return {
            BillEnergyPeriod.SHARP: TariffPeriod.SHARP_PEAK,
            BillEnergyPeriod.PEAK: TariffPeriod.PEAK,
            BillEnergyPeriod.FLAT: TariffPeriod.FLAT,
            BillEnergyPeriod.VALLEY: TariffPeriod.VALLEY,
            BillEnergyPeriod.OFFPEAK: TariffPeriod.DEEP_VALLEY,
        }[self]


class DuplicateStrategy(StrEnum):
    """重复账单处理策略（V2.1 §5.5：跳过 / 替换 / 保留）。

    重复的判定口径见 :func:`cenep.domain.bill_models.duplicate_key`：
    **项目 + 账期（起止日期）+ 计量点**。
    """

    SKIP = "skip"
    REPLACE = "replace"
    KEEP_BOTH = "keep_both"

    @property
    def label(self) -> str:
        return {
            DuplicateStrategy.SKIP: "跳过已存在的账单",
            DuplicateStrategy.REPLACE: "用新导入的替换旧账单",
            DuplicateStrategy.KEEP_BOTH: "两条都保留",
        }[self]


# --------------------------------------------------------------------------- #
# V2.2 §2.2、§6.1 高频负荷数据（阶段 3 新增，仅追加，不改既有枚举）
# --------------------------------------------------------------------------- #
# 说明：本节的三个枚举是 **V2.2 阶段 3 新增**，追加在文件末尾，
# 未修改任何既有枚举成员或取值，V2／V2.1 的公开接口与既有项目文件不受影响。
# --------------------------------------------------------------------------- #
class LoadDataSourceType(StrEnum):
    """负荷曲线的数据来源标签（V2.2 §2.2、§6.1、§0.2 红线）。

    **这是"实测高频负荷"与"月账单估算负荷"必须区分的唯一权威标签**（规格书 §0.2）：

    * ``HIGH_FREQUENCY_IMPORT``：用户导入的真实 15/30/60 分钟（或其它规则间隔）实测数据；
    * ``MONTHLY_BILL_ESTIMATE``：由月电量按运行参数／典型曲线**估算**得到的曲线（阶段 4）；
    * ``SYNTHETIC_TEMPLATE``：直接选用内置可编辑模板生成的曲线（未与任何账单电量回归）。

    取值采用规格书 §2.2 规定的 ``snake_case`` 字面量作为字段契约，中文名经 :attr:`label` 提供。
    ``is_measured`` 与 :attr:`parameter_source` 供质量评分、界面配色与报告共同使用，
    保证估算曲线在**任何**环节都不会被标成实测曲线（阶段 3 先把标签与防线准备好）。
    """

    HIGH_FREQUENCY_IMPORT = "high_frequency_import"
    MONTHLY_BILL_ESTIMATE = "monthly_bill_estimate"
    SYNTHETIC_TEMPLATE = "synthetic_template"

    @property
    def label(self) -> str:
        return {
            LoadDataSourceType.HIGH_FREQUENCY_IMPORT: "实测高频负荷导入",
            LoadDataSourceType.MONTHLY_BILL_ESTIMATE: "月账单估算负荷",
            LoadDataSourceType.SYNTHETIC_TEMPLATE: "典型模板合成负荷",
        }[self]

    @property
    def is_measured(self) -> bool:
        """是否**实测**数据（只有高频导入是实测；估算与模板都不是）。"""
        return self is LoadDataSourceType.HIGH_FREQUENCY_IMPORT

    @property
    def report_badge(self) -> str:
        """报告／界面必须显示的来源徽标（规格书 §8.1、§6.3 A：估算必须显著标记）。"""
        return {
            LoadDataSourceType.HIGH_FREQUENCY_IMPORT: "实测数据",
            LoadDataSourceType.MONTHLY_BILL_ESTIMATE: "估算数据（不是实测）",
            LoadDataSourceType.SYNTHETIC_TEMPLATE: "系统模板合成（不是实测）",
        }[self]

    @property
    def parameter_source(self) -> SourceType:
        """映射到既有来源枚举（规范 §84、§91），供质量评分复用（§8.2 分值表）。

        实测历史负荷按 ``HISTORICAL``（13 分）计；估算按 ``ASSUMPTION``（7 分）计；
        纯模板按 ``SYSTEM_DEFAULT``（0 分）计 —— 估算**不得**与实测同分。
        """
        return {
            LoadDataSourceType.HIGH_FREQUENCY_IMPORT: SourceType.HISTORICAL,
            LoadDataSourceType.MONTHLY_BILL_ESTIMATE: SourceType.ASSUMPTION,
            LoadDataSourceType.SYNTHETIC_TEMPLATE: SourceType.SYSTEM_DEFAULT,
        }[self]


class LoadValueKind(StrEnum):
    """负荷文件的**数值口径**：间隔平均功率 kW 还是间隔电量 kWh（V2.2 §2.2、§3.2）。

    这是本阶段最容易出错、也最必须显式声明的一项：

    * ``POWER_KW``：``P_i`` 为该间隔**平均功率**（kW），间隔电量 ``E_i = P_i × Δt_h``；
    * ``INTERVAL_ENERGY_KWH``：``E_i`` 为该间隔**电量**（kWh），可直接累加，**不得再乘 Δt**。

    规格书 §3.2 把两者写成两条不同的公式；把 kWh 当 kW 再乘一次 Δt
    （或反之）会造成 4 倍／0.25 倍的系统性错误，因此导入时必须由用户或表头明确口径，
    无法判定时报中文错误而不是猜（§6.1、§9.2）。
    """

    POWER_KW = "power_kw"
    INTERVAL_ENERGY_KWH = "interval_energy_kwh"

    @property
    def label(self) -> str:
        return {
            LoadValueKind.POWER_KW: "间隔平均功率（kW）",
            LoadValueKind.INTERVAL_ENERGY_KWH: "间隔电量（kWh）",
        }[self]

    @property
    def unit(self) -> str:
        return {
            LoadValueKind.POWER_KW: "kW",
            LoadValueKind.INTERVAL_ENERGY_KWH: "kWh",
        }[self]

    @property
    def is_power(self) -> bool:
        return self is LoadValueKind.POWER_KW


class LoadQualityStatus(StrEnum):
    """负荷数据集质量状态（V2.2 §2.2、§6.3 A）。

    取值与 :class:`BillQualityStatus` 一致，但分属不同业务对象（账单事实 vs 负荷曲线），
    不共用同一个枚举，避免"账单有效"与"负荷有效"在类型层面被混用。
    """

    VALID = "valid"
    WARNING = "warning"
    INVALID = "invalid"

    @property
    def label(self) -> str:
        return {
            LoadQualityStatus.VALID: "有效",
            LoadQualityStatus.WARNING: "有警告",
            LoadQualityStatus.INVALID: "无效",
        }[self]


# --------------------------------------------------------------------------- #
# V2.2 §3.2、§6.5 月账单估算负荷：月电量的来源（阶段 4 新增，仅追加）
# --------------------------------------------------------------------------- #
# 说明：本枚举是 **V2.2 阶段 4 新增**，追加在文件末尾；未修改任何既有枚举的
# 成员或取值，V2／V2.1 的公开接口与既有项目文件不受影响。
# 它回答的是"这条月电量数字是怎么来的"，与 LoadDataSourceType（曲线是实测还是估算）
# 是两个不同维度：一条 BILL 来源的月电量仍然只能生成 **估算** 曲线（§0.2 红线）。
# --------------------------------------------------------------------------- #
class LoadEstimateSource(StrEnum):
    """月电量输入的来源（V2.2 §3.2、§6.5）。

    * ``BILL``：取自项目已录入的月电费账单（``ElectricityBill.energy_total_kwh``）；
    * ``MANUAL``：用户手工录入该月电量；
    * ``ANNUAL_SPLIT``：只给年电量，按用户填写的月度比例拆分；
    * ``UNIFORM_DEFAULT``：只给年电量且未提供月度比例，**均匀分摊**（明确的默认估算假设）。
    """

    BILL = "bill"
    MANUAL = "manual"
    ANNUAL_SPLIT = "annual_split"
    UNIFORM_DEFAULT = "uniform_default"

    @property
    def label(self) -> str:
        return {
            LoadEstimateSource.BILL: "月电费账单",
            LoadEstimateSource.MANUAL: "手工录入",
            LoadEstimateSource.ANNUAL_SPLIT: "年电量按比例拆分",
            LoadEstimateSource.UNIFORM_DEFAULT: "年电量均匀分摊（默认假设）",
        }[self]

    @property
    def is_default_assumption(self) -> bool:
        """是否为"没有用户信息、只能按明确默认假设"的来源（§6.5 要求显著标注）。"""
        return self is LoadEstimateSource.UNIFORM_DEFAULT
