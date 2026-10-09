"""负荷画像（V2.2 §6.3 B：年/月/典型日负荷曲线与负荷率；阶段 4）。

规格书 §6.3 B 要求页面呈现：

* 年、月、典型工作日/休息日负荷曲线；
* 月总用电量和年化估算；
* 最大功率、平均功率、负荷率（有足够数据时）；
* 数据覆盖率和质量等级。

本模块只做**统计聚合**（分组求和 / 分组最大 / 分组平均），不含任何经济或消纳公式：
* 逐月电量、峰值功率、平均功率、负荷率；
* 典型日曲线（按日类型对同一天内相同时刻取均值，得到"典型日形状"）；
* 年化估算（**明确是年化估算**，不是实测年度电量，§2.2、§2.2 V24）。

单位：电量 **kWh**、功率 **kW**、负荷率与覆盖率**无量纲小数**。

实现约束：全程 NumPy 向量化分组（``np.add.reduceat`` / ``np.maximum.reduceat``），
不逐点 Python 循环（V2 §86、§87）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..domain.enums import LoadDataSourceType, LoadQualityStatus
from ..domain.load_data import HighFrequencyLoadDataset
from .errors import ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "LoadPortrait",
    "MonthPortrait",
    "TypicalDayPortrait",
    "build_portrait",
    "typical_day_curve",
]

#: 数值缺失时用于分组最大值的"中性元"（-inf 在 reduceat 中会被 max 忽略）
_NEG_INF = -np.inf


@dataclass(frozen=True)
class MonthPortrait:
    """单月负荷画像（电量 kWh、功率 kW）。"""

    year: int
    month: int
    month_key: str
    day_count: int
    interval_count: int
    energy_kwh: float
    peak_power_kw: float
    avg_power_kw: float
    load_factor: float
    missing_value_count: int = 0


@dataclass(frozen=True)
class TypicalDayPortrait:
    """典型日曲线（同一天内相同时刻取均值的形状）。

    ``values_kwh`` 的每个元素是该时刻的**平均间隔电量**（kWh/间隔），
    对应功率 = 值 ÷ Δt_h；``day_type`` 取 ``WORKDAY`` / ``WEEKEND`` / ``ALL``。
    """

    day_type: str
    sample_days: int
    values_kwh: tuple[float, ...]
    interval_minutes: int

    @property
    def peak_power_kw(self) -> float:
        if not self.values_kwh:
            return 0.0
        return float(max(self.values_kwh)) / (self.interval_minutes / 60.0)

    def as_power_kw(self) -> tuple[float, ...]:
        delta = self.interval_minutes / 60.0
        return tuple(value / delta for value in self.values_kwh)


@dataclass(frozen=True)
class LoadPortrait:
    """一个负荷数据集的完整画像（§6.3 B）。"""

    profile_id: str = ""
    name: str = ""
    source_type: LoadDataSourceType = LoadDataSourceType.HIGH_FREQUENCY_IMPORT
    estimated: bool = False
    quality_status: LoadQualityStatus = LoadQualityStatus.VALID
    interval_minutes: int = 60
    point_count: int = 0
    coverage_ratio: float = 0.0
    annualized: bool = False

    annual_energy_kwh: float = 0.0
    annualized_energy_kwh: float = 0.0
    peak_power_kw: float = 0.0
    avg_power_kw: float = 0.0
    load_factor: float = 0.0
    missing_value_count: int = 0

    monthly: list[MonthPortrait] = field(default_factory=list)
    typical_days: list[TypicalDayPortrait] = field(default_factory=list)

    @property
    def provenance_text(self) -> str:
        kind = "实测" if self.source_type.is_measured else "估算"
        return f"{kind}｜{self.source_type.label}｜{self.source_type.report_badge}"

    def portrait_rows(self) -> list[tuple[str, str]]:
        """首页画像键值行（界面与报告共用，避免各自拼字符串）。"""
        return [
            ("数据来源", f"{self.source_type.label}（{self.provenance_text}）"),
            ("时间间隔", f"{self.interval_minutes} 分钟"),
            ("数据点数", f"{self.point_count} 点"),
            ("时间覆盖率", f"{self.coverage_ratio:.2%}"),
            ("质量等级", self.quality_status.label),
            (
                "覆盖区间电量",
                f"{self.annual_energy_kwh:,.2f} kWh"
                + ("（已年化）" if self.annualized else "（未年化，按实际覆盖区间统计）"),
            ),
            ("年化估算电量", f"{self.annualized_energy_kwh:,.2f} kWh（按覆盖率折算，属估算）"),
            ("最大功率", f"{self.peak_power_kw:,.2f} kW（区间平均功率的最大值，不是电表计费需量）"),
            ("平均功率", f"{self.avg_power_kw:,.2f} kW"),
            ("负荷率", f"{self.load_factor:.2%}（平均功率 ÷ 最大功率）"),
        ]


def _month_keys(timestamps: list[datetime]) -> tuple[np.ndarray, np.ndarray]:
    """``(年数组, 月数组)``。"""
    return (
        np.asarray([t.year for t in timestamps], dtype=np.int64),
        np.asarray([t.month for t in timestamps], dtype=np.int64),
    )


def build_portrait(
    dataset: HighFrequencyLoadDataset,
    *,
    timestamps: list[datetime] | None = None,
    typical_day_types: tuple[str, ...] = ("WORKDAY", "WEEKEND", "ALL"),
) -> LoadPortrait:
    """构造负荷画像（§6.3 B）。

    :param dataset: 负荷数据集（内部只存电量 kWh，功率由 Δt 派生）
    :param timestamps: 时间戳序列；``None`` 时用 ``dataset.points`` 的时间戳
    :param typical_day_types: 需要生成哪些典型日曲线（``WORKDAY`` / ``WEEKEND`` / ``ALL``）
    :raises ValidationError: 时间戳数量与数据点不一致（中文报错）
    """
    stamps = timestamps if timestamps is not None else [p.timestamp for p in dataset.points]
    if len(stamps) != dataset.point_count:
        raise ValidationError(
            f"时间戳数量（{len(stamps)}）与负荷数据点数（{dataset.point_count}）不一致，"
            f"无法生成负荷画像（V2.2 §6.3）",
            field="load_profile.timestamps",
        )

    delta_hours = dataset.delta_hours
    energy = np.asarray([float(p.load_kwh) for p in dataset.points], dtype=float)
    finite = np.isfinite(energy)
    safe = np.where(finite, energy, 0.0)

    years, months = _month_keys(stamps)
    monthly: list[MonthPortrait] = []
    if energy.size:
        order = np.lexsort((months, years))
        years_sorted, months_sorted = years[order], months[order]
        boundary = np.flatnonzero(
            (np.diff(years_sorted) != 0) | (np.diff(months_sorted) != 0)
        ) + 1
        starts = np.concatenate(([0], boundary))
        ends = np.concatenate((boundary, [energy.size]))
        day_units = np.asarray(
            [np.datetime64(t.date().isoformat(), "D") for t in stamps], dtype="datetime64[D]"
        )
        for start, end in zip(starts, ends):
            idx = order[start:end]
            values = safe[idx]
            total = float(values.sum())
            day_count = int(np.unique(day_units[idx]).size)
            peak_energy = float(values.max()) if values.size else 0.0
            peak_power = peak_energy / delta_hours
            avg_power = (
                total / (day_count * 24.0) if day_count else 0.0
            )
            month = int(months_sorted[start])
            year = int(years_sorted[start])
            monthly.append(
                MonthPortrait(
                    year=year,
                    month=month,
                    month_key=f"{year:04d}-{month:02d}" if year else f"{month:02d}",
                    day_count=day_count,
                    interval_count=int(idx.size),
                    energy_kwh=total,
                    peak_power_kw=peak_power,
                    avg_power_kw=avg_power,
                    load_factor=(avg_power / peak_power) if peak_power > 0.0 else 0.0,
                    missing_value_count=int(np.sum(~finite[idx])),
                )
            )

    typical = [
        curve
        for day_type in typical_day_types
        if (curve := typical_day_curve(dataset, stamps, day_type)) is not None
    ]

    peak = dataset.peak_power_kw
    coverage = float(dataset.coverage_ratio)
    annualized_energy = (
        float(dataset.annual_energy_kwh) / coverage if 0.0 < coverage < 1.0 else float(dataset.annual_energy_kwh)
    )
    portrait = LoadPortrait(
        profile_id=dataset.profile_id,
        name=dataset.name,
        source_type=dataset.source_type,
        estimated=bool(dataset.estimated),
        quality_status=dataset.quality_status,
        interval_minutes=dataset.interval_minutes,
        point_count=dataset.point_count,
        coverage_ratio=coverage,
        annualized=bool(dataset.annualized),
        annual_energy_kwh=dataset.annual_energy_kwh,
        annualized_energy_kwh=annualized_energy,
        peak_power_kw=peak,
        avg_power_kw=dataset.average_power_kw,
        load_factor=dataset.load_factor,
        missing_value_count=dataset.missing_value_count,
        monthly=monthly,
        typical_days=typical,
    )
    logger.debug(
        "负荷画像：%s；%d 个月、%d 条典型日曲线；年电量 %.3f kWh、最大功率 %.2f kW",
        dataset.profile_id or dataset.name,
        len(monthly),
        len(typical),
        portrait.annual_energy_kwh,
        portrait.peak_power_kw,
    )
    return portrait


def typical_day_curve(
    dataset: HighFrequencyLoadDataset,
    timestamps: list[datetime] | None = None,
    day_type: str = "ALL",
) -> TypicalDayPortrait | None:
    """典型日曲线：对同一天内**相同时刻**的间隔电量取均值（§6.3 B）。

    口径（必须明确，否则"典型日"会被误读）：

    * 横轴是"一天内的第几个间隔"（0 起，共 ``24×60/Δt`` 个点）；
    * 纵轴是该时刻的**平均间隔电量** kWh/间隔 → 功率 = 值 ÷ Δt_h；
    * ``WORKDAY`` / ``WEEKEND`` 按 :meth:`datetime.weekday` 判定（0=周一，5/6 = 周末），
      节假日日历不在本统计中（节假日需要日历，负荷数据集不含该信息）；
    * 数据不足一天时返回 ``None``（不臆造形状）。

    :raises ValidationError: 时间戳数量与数据点不一致（中文报错）
    """
    stamps = timestamps if timestamps is not None else [p.timestamp for p in dataset.points]
    if len(stamps) != dataset.point_count:
        raise ValidationError(
            f"时间戳数量（{len(stamps)}）与负荷数据点数（{dataset.point_count}）不一致，"
            f"无法生成典型日曲线（V2.2 §6.3）",
            field="load_profile.timestamps",
        )
    intervals_per_day = int(round(24 * 60 / dataset.interval_minutes))
    if len(stamps) < intervals_per_day:
        return None

    normalized = day_type.upper()
    if normalized == "WORKDAY":
        mask = np.asarray([t.weekday() < 5 for t in stamps], dtype=bool)
    elif normalized == "WEEKEND":
        mask = np.asarray([t.weekday() >= 5 for t in stamps], dtype=bool)
    elif normalized == "ALL":
        mask = np.ones(len(stamps), dtype=bool)
    else:
        raise ValidationError(
            f"未知的典型日类型「{day_type}」；只支持 WORKDAY / WEEKEND / ALL（V2.2 §6.3）",
            field="load_profile.day_type",
        )

    index = np.flatnonzero(mask)
    if index.size < intervals_per_day:
        return None

    energy = np.asarray([float(p.load_kwh) for p in dataset.points], dtype=float)
    # 时刻槽由时间戳直接推导（不依赖"数据必然从 00:00 开始"的假设）
    slots_all = np.asarray(
        [(t.hour * 60 + t.minute) // dataset.interval_minutes for t in stamps], dtype=np.int64
    )
    slot = slots_all[index]
    # 逐"时刻槽"累加（向量化，不按天循环）
    sums = np.bincount(slot, weights=np.where(np.isfinite(energy[index]), energy[index], 0.0),
                       minlength=intervals_per_day)
    counts = np.bincount(slot, minlength=intervals_per_day).astype(float)
    values = np.divide(sums, counts, out=np.zeros(intervals_per_day, dtype=float), where=counts > 0)
    sample_days = int(counts.max()) if counts.size else 0
    return TypicalDayPortrait(
        day_type=normalized,
        sample_days=sample_days,
        values_kwh=tuple(float(v) for v in values),
        interval_minutes=dataset.interval_minutes,
    )
