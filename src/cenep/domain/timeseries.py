"""V2 时序数据的**输入**模型（V2 §3、§6、§7、§8、§9、§17、§18、§51–§56）。

本模块只描述"时序数据长什么样"，**不含任何计算**（V1 规范 §104、V2 §61）：
所有公式在 :mod:`cenep.calculation`。

两个关键设计取舍
----------------
**1. 时间字段用 computed_field 派生，不落库**

V2 §6 把 ``year / month / day / hour / weekday / is_weekend`` 列为 ``TimeSeriesPoint`` 的字段，
V2 §7 又要求"必须使用 timestamp 作为主时间索引，禁止用 1~8760 序号当时间依据"。
本实现把这些字段做成 ``@computed_field``，由 ``timestamp`` 派生：

* 类型层面保证不会出现 ``hour`` 与 ``timestamp`` 互相矛盾的数据点；
* 8760 点 × 3 条曲线的对象属性数量与 ``.nep`` 体积显著下降（V2 §87 内存要求）；
* 序列化时仍会输出这些字段，与 §6 的字段清单一致。

``is_holiday`` 无法由 ``timestamp`` 派生（依赖节假日日历），因此是**真实存储字段**。

**2. 数值字段不设 ge=0 硬约束**

V2 §52–§54 要求导入校验必须**报告**「负负荷」「PV 夜间发电」等问题（"发现 XXX 个时间点…"），
而不是抛裸异常。因此本模块**不**对 ``load_kwh`` 等加 ``ge=0``，
把"是否有非法值"交给 :mod:`cenep.data.validator` 统一收集并给出中文报错（V1 规范 §132）；
引擎在仿真前会强制校验，非法数据不会进入计算。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, ClassVar

from pydantic import Field, computed_field, model_validator

from .base import NON_NEG, _Model
from .enums import (
    DayType,
    DispatchStrategy,
    LoadProfileMode,
    MissingDataPolicy,
    PVProfileMode,
    Resolution,
    SourceType,
    TariffPeriod,
)


# --------------------------------------------------------------------------- #
# V2 §6 TimeSeriesPoint
# --------------------------------------------------------------------------- #
class TimeSeriesPoint(_Model):
    """单个时间点的时序数据（V2 §6）。

    ``timestamp`` 是**唯一主时间索引**（V2 §7）；其余时间字段由它派生。
    单位：``load_kwh`` / ``pv_generation_kwh`` 为该周期内的**电量** kWh（1 小时粒度即 kW·h），
    ``electricity_price`` / ``export_price`` 为元/kWh。
    """

    timestamp: datetime

    is_holiday: bool = Field(default=False, description="是否节假日（依赖节假日日历，需存储）")

    load_kwh: float = Field(default=0.0, description="该周期负荷电量 kWh（V2 §8.2：必须 ≥ 0）")
    pv_generation_kwh: float = Field(default=0.0, description="该周期光伏发电量 kWh")
    electricity_price: float = Field(default=0.0, description="该周期购电电价 元/kWh（V2 §17.1）")
    export_price: float = Field(default=0.0, description="该周期上网电价 元/kWh")

    # ---- 由 timestamp 派生（V2 §6 字段清单）----
    @computed_field  # type: ignore[prop-decorator]
    @property
    def year(self) -> int:
        return self.timestamp.year

    @computed_field  # type: ignore[prop-decorator]
    @property
    def month(self) -> int:
        return self.timestamp.month

    @computed_field  # type: ignore[prop-decorator]
    @property
    def day(self) -> int:
        return self.timestamp.day

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hour(self) -> int:
        return self.timestamp.hour

    @computed_field  # type: ignore[prop-decorator]
    @property
    def weekday(self) -> int:
        """星期，``0 = 周一`` … ``6 = 周日``（与 :meth:`datetime.weekday` 一致）。"""
        return self.timestamp.weekday()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_weekend(self) -> bool:
        return self.timestamp.weekday() >= 5

    #: 由 ``timestamp`` 派生的字段名（序列化会输出、反序列化需剥离）
    DERIVED_TIME_FIELDS: ClassVar[tuple[str, ...]] = (
        "year",
        "month",
        "day",
        "hour",
        "weekday",
        "is_weekend",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_derived_time_fields(cls, data: Any) -> Any:
        """反序列化时剥离派生时间字段，并校验其与 ``timestamp`` 一致。

        为什么需要它：``model_dump()`` 会输出 computed_field（V2 §6 要求这些字段出现在 JSON 中），
        但基类配置了 ``extra="forbid"``，直接回读会被判定为"多余字段"而失败
        —— 表现为项目存得下、读不回。这里在进入字段校验前把这些键取出，
        **顺便校验一致性**：若外部数据（手改的 .nep、第三方导入）给出的
        ``hour`` 与 ``timestamp`` 矛盾，立即报中文错误，而不是静默采信。
        """
        if not isinstance(data, dict):
            return data
        present = [k for k in cls.DERIVED_TIME_FIELDS if k in data]
        if not present:
            return data

        raw_ts = data.get("timestamp")
        cleaned = {k: v for k, v in data.items() if k not in cls.DERIVED_TIME_FIELDS}
        if raw_ts is None:
            return cleaned

        try:
            ts = raw_ts if isinstance(raw_ts, datetime) else datetime.fromisoformat(str(raw_ts))
        except (TypeError, ValueError):
            # timestamp 本身非法：交回字段校验去报错，避免在这里掩盖真实问题
            return cleaned

        expected: dict[str, Any] = {
            "year": ts.year,
            "month": ts.month,
            "day": ts.day,
            "hour": ts.hour,
            "weekday": ts.weekday(),
            "is_weekend": ts.weekday() >= 5,
        }
        for key in present:
            got = data[key]
            want = expected[key]
            if got != want:
                raise ValueError(
                    f"时间字段自相矛盾：timestamp={ts.isoformat()} 对应 {key}={want}，"
                    f"但输入给出 {key}={got}"
                )
        return cleaned


# --------------------------------------------------------------------------- #
# V2 §6 三条曲线
# --------------------------------------------------------------------------- #
class TimeSeriesProfile(_Model):
    """时序曲线的公共字段（V2 §6、§56）。"""

    profile_id: str = ""
    name: str = ""
    resolution: Resolution = Resolution.HOURLY
    source: str = Field(default="", description="数据来源说明（文件名、系统、表号等）")
    source_date: date | None = None
    source_type: SourceType = SourceType.USER_INPUT
    points: list[TimeSeriesPoint] = Field(default_factory=list, description="逐时数据点")

    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def is_empty(self) -> bool:
        return not self.points


class LoadProfile(TimeSeriesProfile):
    """负荷曲线（V2 §6、§8）。单位 kWh。"""

    annual_energy: float = NON_NEG


class PVProfile(TimeSeriesProfile):
    """光伏出力曲线（V2 §6、§9）。

    ``points[*].pv_generation_kwh`` 存**归一化出力系数 × 容量**的两种形态之一：

    * 用户导入实际出力（kWh）时，``capacity_kwp`` 记录该曲线对应的装机容量；
    * 导入归一化系数时，由 :mod:`cenep.calculation.pv_profile` 按容量缩放。

    ``annual_generation`` 为曲线对应的年发电量（kWh），用于交叉校验。
    """

    capacity_kwp: float = NON_NEG
    annual_generation: float = NON_NEG


# --------------------------------------------------------------------------- #
# V2 §6 / §17 分时电价
# --------------------------------------------------------------------------- #
class TimePeriodRule(_Model):
    """一条分时时段规则（V2 §3 P0.4、§17.1）。

    支持按 **月份 / 日类型（工作日·周末·节假日）/ 小时** 三个维度限定，
    以表达"尖峰 / 高峰 / 平段 / 谷段 / 深谷 / 自定义"以及季节性与节假日差异。
    空列表表示"该维度不限制"。
    """

    period: TariffPeriod
    months: list[int] = Field(default_factory=list, description="生效月份 1~12；空 = 全年")
    day_types: list[DayType] = Field(default_factory=list, description="生效日类型；空 = 全部")
    hours: list[int] = Field(default_factory=list, description="生效小时 0~23")

    @model_validator(mode="after")
    def _check_ranges(self) -> TimePeriodRule:
        bad_month = [m for m in self.months if not 1 <= m <= 12]
        if bad_month:
            raise ValueError(f"分时时段规则的月份必须在 1~12，实际出现：{bad_month}")
        bad_hour = [h for h in self.hours if not 0 <= h <= 23]
        if bad_hour:
            raise ValueError(f"分时时段规则的小时必须在 0~23，实际出现：{bad_hour}")
        return self


class TariffProfile(_Model):
    """分时电价模板（V2 §6、§17、§18）。

    字段清单来自 V2 §6；其中 ``sharp_peak_price`` 是为满足 §3 P0.4「必须支持尖峰」而增加的字段
    （§6 清单未列尖峰电价，但 §3 P0.4 与 §17.1 都要求尖峰时段可用）。

    ``demand_charge`` 单位 元/kW·月，``basic_charge`` 单位 元/月（V2 §18）。
    """

    tariff_id: str = ""
    name: str = ""
    effective_date: date | None = None
    region: str = "湖北"

    time_periods: list[TimePeriodRule] = Field(default_factory=list, description="时段划分规则")

    sharp_peak_price: float = NON_NEG
    peak_price: float = NON_NEG
    flat_price: float = NON_NEG
    valley_price: float = NON_NEG
    deep_valley_price: float = NON_NEG
    custom_price: float = NON_NEG

    export_price: float = NON_NEG
    demand_charge: float = NON_NEG
    basic_charge: float = NON_NEG


# --------------------------------------------------------------------------- #
# V2 §6 / §10–§15 储能调度配置
# --------------------------------------------------------------------------- #
#: 与 V1 一致的往返效率（``StorageConfig.round_trip_efficiency = 0.88``）。
V1_ROUND_TRIP_EFFICIENCY = 0.88

#: 单向效率默认值 = ``√0.88``。
#: 取精确均分而**不是** 0.938，是为了让 ``η_charge × η_discharge`` 严格等于 V1 的 0.88：
#: ``0.938² = 0.879844``，会带来 0.016% 的口径漂移，进而影响储能套利收益与 LCOS。
DEFAULT_ONE_WAY_EFFICIENCY = math.sqrt(V1_ROUND_TRIP_EFFICIENCY)


class StorageDispatchConfig(_Model):
    """储能调度配置（V2 §6、§10–§15、§20、§21）。

    三个布尔开关的语义（避免 §6 字段清单中的重复项产生歧义）：

    * ``allow_grid_charge``：**电网充电总开关**，§21 默认 ``False``；
    * ``charge_from_grid``：策略是否**主动**从电网充电；在总开关为假时不生效；
    * ``allow_export``：是否允许储能向电网放电，§20 默认 ``False``（禁止无意义上网）。

    ``max_charge_power`` / ``max_discharge_power`` 为 ``0`` 表示按储能额定功率取值（V2 §11）。
    """

    strategy: DispatchStrategy = DispatchStrategy.PV_SELF_CONSUMPTION

    charge_from_pv: bool = True
    charge_from_grid: bool = False
    allow_grid_charge: bool = False
    allow_export: bool = False
    allow_arbitrage: bool = True

    soc_min: float = Field(default=0.10, ge=0.0, le=1.0, description="SOC 下限（V2 §10.1）")
    soc_max: float = Field(default=1.00, ge=0.0, le=1.0, description="SOC 上限（V2 §10.1）")
    initial_soc: float = Field(default=0.10, ge=0.0, le=1.0, description="仿真起始 SOC")

    charge_efficiency: float = Field(
        default=DEFAULT_ONE_WAY_EFFICIENCY, gt=0.0, le=1.0, description="充电效率 η_charge（V2 §10.3）"
    )
    discharge_efficiency: float = Field(
        default=DEFAULT_ONE_WAY_EFFICIENCY,
        gt=0.0,
        le=1.0,
        description="放电效率 η_discharge（V2 §10.4）",
    )

    @property
    def round_trip_efficiency(self) -> float:
        """往返效率 ``η_charge × η_discharge``（与 V1 的 0.88 保持同一口径）。"""
        return self.charge_efficiency * self.discharge_efficiency

    max_charge_power: float = NON_NEG
    max_discharge_power: float = NON_NEG

    charge_price_threshold: float = NON_NEG
    discharge_price_threshold: float = NON_NEG

    @model_validator(mode="after")
    def _check_consistency(self) -> StorageDispatchConfig:
        if self.soc_min >= self.soc_max:
            raise ValueError(
                f"SOC 下限必须小于上限，实际为 {self.soc_min:.2%} / {self.soc_max:.2%}"
            )
        if not self.soc_min <= self.initial_soc <= self.soc_max:
            raise ValueError(
                f"起始 SOC（{self.initial_soc:.2%}）必须落在 SOC 区间 "
                f"[{self.soc_min:.2%}, {self.soc_max:.2%}] 内"
            )
        if self.charge_from_grid and not self.allow_grid_charge:
            raise ValueError(
                "已选择从电网充电，但电网充电总开关（allow_grid_charge）为关闭；"
                "如确需电网充电请同时打开该开关。"
            )
        if (
            self.charge_price_threshold > 0.0
            and self.discharge_price_threshold > 0.0
            and self.charge_price_threshold >= self.discharge_price_threshold
        ):
            raise ValueError(
                "峰谷套利的充电价格阈值必须小于放电价格阈值，实际为 "
                f"{self.charge_price_threshold:.4f} / {self.discharge_price_threshold:.4f} 元/kWh"
            )
        return self


# --------------------------------------------------------------------------- #
# V2 §3 P0.2 / P0.3 / P0.4 三条曲线的取得方式
# --------------------------------------------------------------------------- #
class LoadProfileConfig(_Model):
    """负荷曲线的取得方式（V2 §3 P0.2、§8）。

    优先级（V2 §8.1）：用户 8760 数据 > 用户典型日 > 系统模板 > 经验参数。
    """

    mode: LoadProfileMode = LoadProfileMode.ANNUAL_SIMPLE

    hourly: LoadProfile | None = Field(default=None, description="HOURLY 模式：8760 逐时负荷")
    typical_workday: list[float] = Field(default_factory=list, description="典型工作日 24 点 kWh")
    typical_weekend: list[float] = Field(default_factory=list, description="典型周末 24 点 kWh")
    monthly_factors: list[float] = Field(default_factory=list, description="12 个月度系数；空 = 全 1")

    annual_energy_kwh: float = NON_NEG
    annual_growth_rate: float = Field(default=0.0, ge=-0.5, le=1.0, description="年负荷增长率（V2 §8.3）")

    missing_data_policy: MissingDataPolicy = MissingDataPolicy.REJECT

    @model_validator(mode="after")
    def _check_shape(self) -> LoadProfileConfig:
        if self.typical_workday and len(self.typical_workday) != 24:
            raise ValueError(f"典型工作日曲线必须是 24 点，实际 {len(self.typical_workday)} 点")
        if self.typical_weekend and len(self.typical_weekend) != 24:
            raise ValueError(f"典型周末曲线必须是 24 点，实际 {len(self.typical_weekend)} 点")
        if self.monthly_factors and len(self.monthly_factors) != 12:
            raise ValueError(f"月度系数必须是 12 个，实际 {len(self.monthly_factors)} 个")
        if self.mode is LoadProfileMode.HOURLY and (self.hourly is None or self.hourly.is_empty):
            raise ValueError("负荷模式为「导入 8760 小时曲线」时必须提供 hourly 曲线数据")
        if self.mode is LoadProfileMode.TYPICAL_DAY and not self.typical_workday:
            raise ValueError("负荷模式为「典型日曲线」时必须提供 typical_workday（24 点）")
        return self


class PVProfileConfig(_Model):
    """光伏出力曲线的取得方式（V2 §3 P0.3、§9）。"""

    mode: PVProfileMode = PVProfileMode.EQUIVALENT_HOURS

    hourly: PVProfile | None = Field(default=None, description="HOURLY 模式：8760 出力曲线")
    typical_day: list[float] = Field(default_factory=list, description="典型日出力系数 24 点（0~1）")
    monthly_factors: list[float] = Field(default_factory=list, description="12 个月度系数")
    hour_factors: list[float] = Field(default_factory=list, description="24 个小时系数")

    equivalent_hours: float = NON_NEG
    performance_ratio: float = Field(default=1.0, gt=0.0, le=1.0)
    capacity_kwp: float | None = Field(default=None, gt=0.0, description="覆盖 V1 的 pv.pv_capacity_kwp")
    annual_growth_rate: float = Field(default=0.0, ge=-1.0, le=1.0)

    missing_data_policy: MissingDataPolicy = MissingDataPolicy.REJECT

    @model_validator(mode="after")
    def _check_shape(self) -> PVProfileConfig:
        if self.typical_day and len(self.typical_day) != 24:
            raise ValueError(f"典型日出力曲线必须是 24 点，实际 {len(self.typical_day)} 点")
        if self.monthly_factors and len(self.monthly_factors) != 12:
            raise ValueError(f"月度系数必须是 12 个，实际 {len(self.monthly_factors)} 个")
        if self.hour_factors and len(self.hour_factors) != 24:
            raise ValueError(f"小时系数必须是 24 个，实际 {len(self.hour_factors)} 个")
        if self.mode is PVProfileMode.HOURLY and (self.hourly is None or self.hourly.is_empty):
            raise ValueError("光伏模式为「导入 8760 出力曲线」时必须提供 hourly 曲线数据")
        if self.mode is PVProfileMode.TYPICAL_DAY and not self.typical_day:
            raise ValueError("光伏模式为「典型日曲线」时必须提供 typical_day（24 点）")
        if self.mode is PVProfileMode.MONTHLY_HOUR_FACTOR and (
            not self.monthly_factors or not self.hour_factors
        ):
            raise ValueError("光伏模式为「月度系数 × 小时系数」时必须同时提供两者")
        return self


class TariffSeriesConfig(_Model):
    """分时电价曲线的取得方式与年度变化（V2 §3 P0.4、§17、§18）。"""

    profile: TariffProfile = Field(default_factory=TariffProfile)
    annual_growth_rate: float = Field(
        default=0.0, ge=-0.5, le=1.0, description="电价年度变化率（V2 §17.2）"
    )
    demand_charge_enabled: bool = Field(default=False, description="是否计入需量电费（V2 §18）")
    basic_charge_enabled: bool = Field(default=False, description="是否计入基本电费")


class TimeSeriesConfig(_Model):
    """V2 时序仿真总配置（V2 §3、§6、§7、§19）。

    ``enabled=False``（默认）时**完全走 V1 年度模式**，V1 项目行为与结果不受任何影响
    （V2 §1.1 兼容性承诺）。
    """

    enabled: bool = Field(default=False, description="是否启用 8760 时序仿真")

    resolution: Resolution = Resolution.HOURLY
    base_year: int = Field(default=2025, ge=2000, le=2100, description="仿真基准年，决定平年/闰年")

    load: LoadProfileConfig = Field(default_factory=LoadProfileConfig)
    pv: PVProfileConfig = Field(default_factory=PVProfileConfig)
    tariff: TariffSeriesConfig = Field(default_factory=TariffSeriesConfig)

    dispatch: StorageDispatchConfig = Field(default_factory=StorageDispatchConfig)

    holidays: list[date] = Field(default_factory=list, description="节假日日历（V2 §3 P0.4）")

    balance_tolerance: float = Field(
        default=1e-6, gt=0.0, description="能量平衡容差 kWh（V2 §19：超限判定计算失败）"
    )

    optimization_enabled: bool = Field(
        default=False,
        description=(
            "是否在计算时执行方案寻优（V2 §45–§48）。**默认关闭**：寻优要逐个候选跑"
            "时序仿真并对最优候选做完整运营期精确复核（V2 §86），耗时远高于常规计算，"
            "因此只有用户显式开启时才执行（方案比较不受该开关影响，始终执行）。"
        ),
    )
