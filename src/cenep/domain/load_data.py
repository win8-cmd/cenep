"""高频负荷数据集模型（V2.2 §2.2、§6.1、§6.4；阶段 3）。

职责边界（规格书 §1）
--------------------
* 本模块**只描述"负荷数据集长什么样"**，不含任何读取、校验或重采样计算：
  读取与列映射在 :mod:`cenep.data.load_profile_importer`，
  重采样与功率/电量换算在 :mod:`cenep.calculation.load_resample`。
* 复用既有时序框架：内部**时间点仍然存 :class:`cenep.domain.timeseries.TimeSeriesPoint`**，
  不另建第二套时序结构；本模型只是在它之上补充"间隔、口径、来源标签、质量与覆盖"元数据。

内部统一表示（本阶段的核心口径）
------------------------------
1. **时间点 = 区间左端点**，区间语义统一为**左闭右开** ``[t, t + interval_minutes)``（§2.2）。
2. **电量字段单位 kWh，功率字段单位 kW**；``TimeSeriesPoint.load_kwh`` 始终表示
   **该间隔内的电量**（kWh/间隔），这是 CENEP 内部唯一的存储口径；
   :attr:`HighFrequencyLoadDataset.values_kw` 只是**按 Δt 换算出来的派生视图**
   （``P_i = E_i / Δt_h``），因此不可能出现"电量与功率两套数据各存一份、彼此矛盾"的情况。
3. ``value_kind`` 记录**源文件**给的是功率还是电量，用于报告追溯与"是否乘过 Δt"的口径复查
   （规格书 §3.2 的两条公式必须可区分；把 kWh 当 kW 再乘 Δt 会造成 4 倍错误）。

来源标签红线（V2.2 §0.2）
------------------------
:class:`~cenep.domain.enums.LoadDataSourceType` 是"实测高频负荷"与"月账单估算负荷"的
唯一权威标签，本模型在**构造时**强制 ``source_type`` 与 ``estimated`` 自洽：

* :attr:`LoadDataSourceType.HIGH_FREQUENCY_IMPORT` ⇒ ``estimated`` 必须为 ``False``；
* :attr:`LoadDataSourceType.MONTHLY_BILL_ESTIMATE` / :attr:`LoadDataSourceType.SYNTHETIC_TEMPLATE`
  ⇒ ``estimated`` 必须为 ``True``。

因此"把月账单估算负荷伪装成真实 15/30/60 分钟曲线"在**类型层面**就不可能构造出来，
阶段 4 的估算引擎与界面只需读取 :attr:`HighFrequencyLoadDataset.provenance_text`。
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import Field, model_validator

from .base import NON_NEG, _Model
from .enums import LoadDataSourceType, LoadQualityStatus, LoadValueKind, Resolution
from .timeseries import LoadProfile, TimeSeriesPoint

__all__ = [
    "FIELD_LABELS",
    "SUPPORTED_INTERVAL_MINUTES",
    "TIMEZONE_DEFAULT",
    "HighFrequencyLoadDataset",
    "make_dataset_id",
    "provides_measured_curve",
]

#: V2.2 §6.1 要求一等支持的间隔（分钟）；其它规则间隔允许导入但会被标记为非标准（§2.2）
SUPPORTED_INTERVAL_MINUTES: tuple[int, ...] = (15, 30, 60)

#: V2.2 只支持中国时区（无夏令时，恒为 UTC+8；§2.2、§6.4）
TIMEZONE_DEFAULT = "Asia/Shanghai"

#: 字段中文名（含单位）。界面、导入报错、报告统一从这里取，避免各处硬编码。
FIELD_LABELS: dict[str, str] = {
    "profile_id": "负荷数据集编号",
    "project_id": "项目标识",
    "name": "名称",
    "source_type": "数据来源",
    "value_kind": "源文件数值口径",
    "interval_minutes": "时间间隔（分钟）",
    "resolution": "时间分辨率",
    "timezone": "时区",
    "period_start": "数据起始时间（含）",
    "period_end": "数据结束时间（不含，左闭右开）",
    "points": "逐间隔数据点",
    "annualized": "是否已年化",
    "coverage_ratio": "覆盖率",
    "missing_intervals": "缺失间隔数",
    "duplicate_intervals": "重复时间戳数",
    "irregular_intervals": "不规则间隔数",
    "estimated": "是否估算数据",
    "quality_status": "质量状态",
    "quality_messages": "质量问题（中文）",
    "source_file_name": "来源文件名",
    "source_sheet": "来源工作表",
    "mapping_config": "列映射配置",
    "assumptions": "口径与假设",
    "created_at": "创建时间",
    "annual_energy_kwh": "年用电量（kWh）",
    "peak_power_kw": "最大功率（kW）",
}


def make_dataset_id(file_stem: str, interval_minutes: int) -> str:
    """生成负荷数据集编号：``文件名@30min``（不含路径，便于项目文件跨机器复现）。"""
    stem = (file_stem or "load").strip() or "load"
    return f"{stem}@{int(interval_minutes)}min"


class HighFrequencyLoadDataset(_Model):
    """一条负荷数据集（V2.2 §2.2 的字段清单，单位与口径见模块文档字符串）。"""

    profile_id: str = ""
    project_id: str = ""
    name: str = ""

    source_type: LoadDataSourceType = LoadDataSourceType.HIGH_FREQUENCY_IMPORT
    value_kind: LoadValueKind = LoadValueKind.INTERVAL_ENERGY_KWH

    interval_minutes: int = Field(default=60, gt=0, le=1440, description="时间间隔（分钟）")
    resolution: Resolution = Resolution.HOURLY
    timezone: str = TIMEZONE_DEFAULT

    period_start: datetime | None = None
    period_end: datetime | None = Field(
        default=None, description="数据结束时间（**不含**，与左闭右开区间配套，§2.2）"
    )

    points: list[TimeSeriesPoint] = Field(default_factory=list, description="逐间隔时间点")

    annualized: bool = Field(
        default=False,
        description="是否已年化（按代表日/日均值扩到全年）；为真时报告必须标注『年化估算』（§2.2）",
    )
    estimated: bool = Field(
        default=False, description="是否估算数据；必须与 source_type 自洽（§0.2 红线）"
    )

    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0, description="有效时段覆盖率")
    missing_intervals: int = Field(default=0, ge=0, description="缺失间隔数（相对完整时间轴）")
    duplicate_intervals: int = Field(default=0, ge=0, description="重复时间戳数")
    irregular_intervals: int = Field(default=0, ge=0, description="不规则间隔数")

    quality_status: LoadQualityStatus = LoadQualityStatus.VALID
    quality_messages: list[str] = Field(default_factory=list, description="中文质量问题与提示")

    source_file_name: str = ""
    source_sheet: str = ""
    mapping_config: dict[str, str] = Field(default_factory=dict, description="列映射（字段→表头）")
    assumptions: list[str] = Field(default_factory=list, description="口径与假设（必须随结果输出）")

    created_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # 结构性校验（只拦"无法由用户部分修正"的问题；数值问题走质量清单）
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _check_consistency(self) -> HighFrequencyLoadDataset:
        if self.timezone != TIMEZONE_DEFAULT:
            raise ValueError(
                f"时区只支持 {TIMEZONE_DEFAULT}（中国无夏令时，恒为 UTC+8），"
                f"实际给出 {self.timezone!r}；如需其它时区请先换算后再导入（V2.2 §2.2）"
            )

        expected = Resolution.from_interval_minutes(self.interval_minutes)
        if expected is not None and self.resolution is not expected:
            raise ValueError(
                f"时间间隔 {self.interval_minutes} 分钟应对应分辨率"
                f"「{expected.label}」，实际为「{self.resolution.label}」；"
                f"分辨率与间隔必须一致，否则 Δt 换算与时间轴点数都会错（V2.2 §3.2）"
            )

        if self.source_type.is_measured and self.estimated:
            raise ValueError(
                "数据来源为「实测高频负荷导入」时不得同时标记为估算数据；"
                "若该曲线是由月电量估算得到，请把数据来源改为"
                f"「{LoadDataSourceType.MONTHLY_BILL_ESTIMATE.label}」（V2.2 §0.2 红线："
                "严禁把月账单估算负荷伪装成真实 15/30/60 分钟曲线）"
            )
        if not self.source_type.is_measured and not self.estimated:
            raise ValueError(
                f"数据来源「{self.source_type.label}」不是实测数据，estimated 必须为 True；"
                "否则估算曲线会被下游当成实测曲线使用（V2.2 §0.2 红线）"
            )

        if self.points:
            if self.period_start is None:
                self.period_start = min(p.timestamp for p in self.points)
            if self.period_end is None:
                self.period_end = max(p.timestamp for p in self.points)
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError(
                f"数据结束时间（{self.period_end:%Y-%m-%d %H:%M}）早于起始时间"
                f"（{self.period_start:%Y-%m-%d %H:%M}），请检查时间列"
            )
        return self

    # ------------------------------------------------------------------ #
    # 派生视图（不存储第二份数据，见模块文档字符串第 2 条）
    # ------------------------------------------------------------------ #
    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def delta_hours(self) -> float:
        """单间隔时长（小时）``Δt_h``。"""
        return self.interval_minutes / 60.0

    @property
    def is_standard_interval(self) -> bool:
        """是否 15/30/60 分钟之一（§6.1 要求一等支持的粒度）。"""
        return self.interval_minutes in SUPPORTED_INTERVAL_MINUTES

    @property
    def interval_energy_kwh(self) -> list[float]:
        """逐间隔电量（kWh/间隔），即 ``TimeSeriesPoint.load_kwh`` 的列视图。

        缺失点（``NaN``，导入时由空单元格／非数字产生）**原样保留**，
        以便下游按缺失策略处理；统计类属性会自动跳过它们（见下）。
        """
        return [float(point.load_kwh) for point in self.points]

    @property
    def missing_value_count(self) -> int:
        """数值缺失（空单元格／非数值）的时间点数量（与"时间点缺失"不同，见 §6.4）。"""
        return int(sum(1 for point in self.points if not math.isfinite(float(point.load_kwh))))

    @property
    def values_kw(self) -> list[float]:
        """逐间隔**平均功率**（kW）＝ ``E_i / Δt_h``（规格书 §2.2、§3.2）。

        这是派生量：电量与功率不会各存一份，因此不存在"两个口径互相矛盾"的可能。
        """
        dt = self.delta_hours
        return [float(point.load_kwh) / dt for point in self.points]

    @property
    def annual_energy_kwh(self) -> float:
        """数据覆盖区间内的负荷电量合计（kWh）。

        **只累加有限值**：空单元格（``NaN``）不计入合计，其数量由
        :attr:`missing_value_count` 与质量问题清单（V11）单独报告，
        绝不按 0 静默混入（V2.2 §6.4）。合计**未年化**，是否年化见 :attr:`annualized`。
        """
        return float(
            sum(float(p.load_kwh) for p in self.points if math.isfinite(float(p.load_kwh)))
        )

    @property
    def finite_values_kw(self) -> list[float]:
        """剔除缺失后的逐间隔平均功率（kW），供最大/平均值统计使用。"""
        dt = self.delta_hours
        return [
            float(point.load_kwh) / dt
            for point in self.points
            if math.isfinite(float(point.load_kwh))
        ]

    @property
    def peak_power_kw(self) -> float:
        """区间平均功率的最大值（kW）。注意：它不是电表计费需量（§3.4、§7.5）。"""
        values = self.finite_values_kw
        return max(values) if values else 0.0

    @property
    def average_power_kw(self) -> float:
        """覆盖区间内的平均功率（kW），按区间时长加权（等长间隔下即算术平均）。"""
        values = self.finite_values_kw
        if not values:
            return 0.0
        return sum(values) / len(values)

    @property
    def load_factor(self) -> float:
        """负荷率 = 平均功率 / 最大功率；最大功率为 0 时返回 0（界面需显示"不适用"）。"""
        peak = self.peak_power_kw
        return self.average_power_kw / peak if peak > 0.0 else 0.0

    # ------------------------------------------------------------------ #
    # 来源标签与报告文本
    # ------------------------------------------------------------------ #
    @property
    def provenance_text(self) -> str:
        """界面/报告必须显示的来源说明（实测 or 估算，规格书 §6.3 A、§8.1）。"""
        kind = "实测" if self.source_type.is_measured else "估算"
        parts = [
            f"{self.source_type.label}",
            f"间隔 {self.interval_minutes} 分钟",
            f"{self.point_count} 点",
        ]
        if self.estimated or not self.source_type.is_measured:
            parts.append(f"⚠ {self.source_type.report_badge}")
        if self.annualized:
            parts.append("⚠ 已年化（按代表日/日均扩到全年）")
        return "｜".join([f"{kind}负荷曲线", *parts])

    def quality_summary_text(self) -> str:
        """一行中文质量小结，供导入预览与报告使用（§6.3 A）。"""
        return (
            f"覆盖率 {self.coverage_ratio:.2%}、缺失 {self.missing_intervals} 点、"
            f"重复 {self.duplicate_intervals} 点、不规则间隔 {self.irregular_intervals} 处，"
            f"质量状态「{self.quality_status.label}」（{len(self.quality_messages)} 条问题）"
        )

    # ------------------------------------------------------------------ #
    # 与既有模型的衔接（复用 V2 的持久化字段，不新增迁移）
    # ------------------------------------------------------------------ #
    def to_load_profile(self, *, source: str = "") -> LoadProfile:
        """转成既有 :class:`~cenep.domain.timeseries.LoadProfile`（V2 §6）。

        V2 的项目文件已经能存 ``LoadProfile``（``TimeSeriesConfig.load.hourly``），
        因此 15/30/60 分钟负荷可以直接搭在该字段上持久化，
        **无需新增项目文件字段、无需数据迁移**（§0.2、§8.2）。
        ``resolution`` 原样带过去，下游据此决定 Δt。
        """
        return LoadProfile(
            profile_id=self.profile_id,
            name=self.name or self.profile_id,
            resolution=self.resolution,
            source=source or self.source_file_name,
            source_type=self.source_type.parameter_source,
            points=list(self.points),
            annual_energy=self.annual_energy_kwh,
        )


def provides_measured_curve(dataset: HighFrequencyLoadDataset) -> bool:
    """该数据集是否可作为**实测**负荷曲线用于对外结论（§0.2 红线的唯一判定入口）。

    只有"来源为高频实测导入、未做估算标记、且不是年化合成"的数据才返回 ``True``。
    任何下游（阶段 4 的消纳率、阶段 7 的报告）都应当调用本函数，
    而不是自行判断文件名或点数。
    """
    return bool(
        dataset.source_type.is_measured
        and not dataset.estimated
        and not dataset.annualized
    )
