"""时间轴与分辨率（V2 §3 P0.1、§7）。

V2 的时间模型
-------------
* 内部统一 **1 小时 = 一个计算周期**，平年 8760 点、闰年 8784 点（§3 P0.1、§7）；
* 为 **15 分钟**（35040 / 35044 点）预留接口，另有日、月粒度；
* ``timestamp`` 是**唯一主时间索引**，禁止用 1~8760 序号当时间依据（§7）。
  因此本模块把年月日时分、星期、日类型都**一次算好并向量化**，
  下游引擎只按位置索引数组，不再各自从序号反推时间。

性能（§86、§87）
---------------
整条时间轴用 NumPy 数组表示，8760 点只构造约 10 个长度 8760 的数组，
不产生逐小时 Python 对象。``timestamps`` 保留为 ``datetime`` 元组供报告与序列化使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from ..domain.enums import DayType, Resolution

#: ``datetime64[D].astype(int)`` 得到的 0 对应 1970-01-01，那天是**星期四**。
#: Python 的 ``weekday()`` 约定周一 = 0，故星期 = (天数 + 3) % 7。
_EPOCH_WEEKDAY_OFFSET = 3


def is_leap_year(year: int) -> bool:
    """是否闰年（V2 §3 P0.1：闰年扩展结构 8784）。"""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def points_per_year(year: int, resolution: Resolution = Resolution.HOURLY) -> int:
    """给定年份与分辨率的点数（平年 / 闰年自动区分）。"""
    leap = 1 if is_leap_year(year) else 0
    if resolution is Resolution.HOURLY:
        return 8760 + 24 * leap
    if resolution is Resolution.QUARTER_HOURLY:
        return 35040 + 96 * leap
    if resolution is Resolution.DAILY:
        return 365 + leap
    return 12


def _numpy_step(resolution: Resolution) -> np.timedelta64:
    if resolution is Resolution.HOURLY:
        return np.timedelta64(1, "h")
    if resolution is Resolution.QUARTER_HOURLY:
        return np.timedelta64(15, "m")
    if resolution is Resolution.DAILY:
        return np.timedelta64(1, "D")
    return np.timedelta64(1, "M")


@dataclass(frozen=True)
class TimeAxis:
    """一条仿真时间轴（V2 §7）。

    所有派生时间字段都是**数组**且与 ``timestamps`` 一一对应；
    下游引擎用 ``axis.month``、``axis.hour``、``axis.is_high_price_period``
    之类的数组做向量化判定，不再逐点解析日期。
    """

    year: int
    resolution: Resolution
    timestamps: tuple[datetime, ...]

    month: np.ndarray
    day: np.ndarray
    hour: np.ndarray
    weekday: np.ndarray
    is_weekend: np.ndarray
    is_holiday: np.ndarray
    day_type: np.ndarray

    @property
    def point_count(self) -> int:
        return len(self.timestamps)

    @property
    def delta_hours(self) -> float:
        """单周期时长（小时），用于 ``Δt`` 换算（V2 §10）。"""
        return self.resolution.delta_hours

    @property
    def is_leap(self) -> bool:
        return is_leap_year(self.year)

    def __len__(self) -> int:
        return len(self.timestamps)

    # ---- 常用索引切片 ----
    def month_mask(self, month: int) -> np.ndarray:
        """某月（1~12）的布尔掩码。"""
        return self.month == month

    def month_index_ranges(self) -> list[tuple[int, int]]:
        """12 个月的 ``[起, 止)`` 索引区间，供需量电费按月取最大值（V2 §18）。"""
        ranges: list[tuple[int, int]] = []
        for m in range(1, 13):
            idx = np.flatnonzero(self.month == m)
            if idx.size:
                ranges.append((int(idx[0]), int(idx[-1]) + 1))
        return ranges

    def mask_day_type(self, day_type: DayType) -> np.ndarray:
        return self.day_type == day_type.value

    def describe(self) -> str:
        kind = "闰年" if self.is_leap else "平年"
        return (
            f"{self.year} 年（{kind}）· {self.resolution.label} · "
            f"{self.point_count} 个计算周期"
        )


def build_time_axis(
    year: int,
    resolution: Resolution = Resolution.HOURLY,
    holidays: tuple[date, ...] | list[date] = (),
) -> TimeAxis:
    """构造一条时间轴（V2 §7）。

    :param year: 仿真基准年，决定平年 / 闰年（V2 §3 P0.1）
    :param resolution: 计算粒度，默认 1 小时
    :param holidays: 节假日日历，用于区分 工作日 / 周末 / 节假日（V2 §3 P0.4）
    """
    if resolution is Resolution.MONTHLY:
        months = np.arange(1, 13)
        stamps_dt = tuple(datetime(year, int(m), 1) for m in months)
        size = 12
        return TimeAxis(
            year=year,
            resolution=resolution,
            timestamps=stamps_dt,
            month=months.astype(np.int16),
            day=np.ones(size, dtype=np.int16),
            hour=np.zeros(size, dtype=np.int16),
            weekday=np.zeros(size, dtype=np.int8),
            is_weekend=np.zeros(size, dtype=bool),
            is_holiday=np.zeros(size, dtype=bool),
            day_type=np.array([DayType.WORKDAY.value] * size, dtype=object),
        )

    n = points_per_year(year, resolution)
    start = np.datetime64(f"{year:04d}-01-01T00:00")
    stamps = start + np.arange(n, dtype="int64") * _numpy_step(resolution)

    day_units = stamps.astype("datetime64[D]")
    hour_units = stamps.astype("datetime64[h]")

    month = (stamps.astype("datetime64[M]").astype(np.int64) % 12 + 1).astype(np.int16)
    day = (day_units - day_units.astype("datetime64[M]").astype("datetime64[D]")).astype(
        np.int64
    ) + 1
    hour = (hour_units - day_units.astype("datetime64[h]")).astype(np.int64)
    # 15 分钟粒度下 hour 仍是 0~23，分钟由 timestamp 体现（V2 §3 P0.1 预留）
    weekday = ((day_units.astype(np.int64) + _EPOCH_WEEKDAY_OFFSET) % 7).astype(np.int8)
    is_weekend = weekday >= 5

    holiday_set = {d for d in holidays}
    if holiday_set:
        holiday_days = np.array(
            [np.datetime64(d.isoformat(), "D") for d in holiday_set], dtype="datetime64[D]"
        )
        is_holiday = np.isin(day_units, holiday_days)
    else:
        is_holiday = np.zeros(n, dtype=bool)

    day_type = np.where(
        is_holiday,
        DayType.HOLIDAY.value,
        np.where(is_weekend, DayType.WEEKEND.value, DayType.WORKDAY.value),
    ).astype(object)

    timestamps = tuple(
        datetime(
            year,
            int(month[i]),
            int(day[i]),
            int(hour[i]),
            int((stamps[i] - hour_units[i]).astype("timedelta64[m]").astype(int)),
        )
        for i in range(n)
    )

    return TimeAxis(
        year=year,
        resolution=resolution,
        timestamps=timestamps,
        month=month,
        day=day.astype(np.int16),
        hour=hour.astype(np.int8),
        weekday=weekday,
        is_weekend=is_weekend,
        is_holiday=is_holiday,
        day_type=day_type,
    )


def annual_growth_factor(rate: float, year_index: int) -> float:
    """第 n 年的增长因子 ``(1 + rate)^(n-1)``（V2 §8.3、§9.2、§17.2）。

    ``year_index`` 从 1 开始；第 1 年恒为 1.0。
    """
    return float((1.0 + float(rate)) ** (year_index - 1))


__all__ = [
    "TimeAxis",
    "annual_growth_factor",
    "build_time_axis",
    "is_leap_year",
    "points_per_year",
]
