"""时间轴与分辨率测试（V2 §3 P0.1、§7、§86）。

V2 §7 要求 ``timestamp`` 是唯一主时间索引，因此本文件的核心断言是：
**向量化算出的每一个派生时间字段，都必须与对应 timestamp 完全一致**（逐点校验 8760 次）。

测试用 :func:`axis_for` 取时间轴：模块级缓存，同参数只构造一次（约 90 ms），
避免 pytest 类作用域 fixture 的弃用警告，也让每个用例可以自由取不同年份 / 分辨率的轴。
"""

from __future__ import annotations

import time
from datetime import date, datetime

import numpy as np
import pytest

from cenep.calculation.timeseries_engine import (
    TimeAxis,
    annual_growth_factor,
    build_time_axis,
    is_leap_year,
    points_per_year,
)
from cenep.domain.enums import DayType, Resolution

_AXIS_CACHE: dict[tuple[int, Resolution, tuple[date, ...]], TimeAxis] = {}


def axis_for(
    year: int = 2025,
    resolution: Resolution = Resolution.HOURLY,
    holidays: tuple[date, ...] = (),
) -> TimeAxis:
    """取（并缓存）一条时间轴。"""
    key = (year, resolution, holidays)
    if key not in _AXIS_CACHE:
        _AXIS_CACHE[key] = build_time_axis(year, resolution, holidays)
    return _AXIS_CACHE[key]


class TestLeapYear:
    @pytest.mark.parametrize(
        ("year", "expected"),
        [
            (2024, True), (2025, False), (2026, False), (2027, False), (2028, True),
            (1900, False), (2000, True), (2100, False),
        ],
    )
    def test_is_leap_year(self, year, expected):
        """§3 P0.1：闰年结构必须正确，含「百年不闰、四百年再闰」。"""
        assert is_leap_year(year) is expected


class TestPointsPerYear:
    def test_hourly(self):
        assert points_per_year(2025, Resolution.HOURLY) == 8760
        assert points_per_year(2024, Resolution.HOURLY) == 8784

    def test_quarter_hourly(self):
        assert points_per_year(2025, Resolution.QUARTER_HOURLY) == 35040
        assert points_per_year(2024, Resolution.QUARTER_HOURLY) == 35136

    def test_daily(self):
        assert points_per_year(2025, Resolution.DAILY) == 365
        assert points_per_year(2024, Resolution.DAILY) == 366

    def test_monthly(self):
        assert points_per_year(2025, Resolution.MONTHLY) == 12


class TestTimeAxisHourly:
    def test_length_and_span(self):
        """§7：平年 8760 点，从 1/1 00:00 到 12/31 23:00。"""
        axis = axis_for(2025)
        assert axis.point_count == 8760
        assert len(axis) == 8760
        assert axis.timestamps[0] == datetime(2025, 1, 1, 0, 0)
        assert axis.timestamps[-1] == datetime(2025, 12, 31, 23, 0)
        assert axis.is_leap is False

    def test_every_point_matches_timestamp(self):
        """§7 关键断言：全部 8760 点的派生字段与 timestamp 逐点一致。"""
        axis = axis_for(2025)
        for i, ts in enumerate(axis.timestamps):
            assert axis.month[i] == ts.month, f"第 {i} 点 month 不一致"
            assert axis.day[i] == ts.day, f"第 {i} 点 day 不一致"
            assert axis.hour[i] == ts.hour, f"第 {i} 点 hour 不一致"
            assert axis.weekday[i] == ts.weekday(), f"第 {i} 点 weekday 不一致"
            assert bool(axis.is_weekend[i]) == (ts.weekday() >= 5), f"第 {i} 点 is_weekend 不一致"

    def test_weekday_convention_monday_zero(self):
        """weekday 约定 0=周一（与 datetime.weekday 一致）。"""
        axis = axis_for(2025)
        idx = next(i for i, ts in enumerate(axis.timestamps) if ts.date() == date(2025, 1, 6))
        assert axis.weekday[idx] == 0
        assert bool(axis.is_weekend[idx]) is False

    def test_array_lengths_all_match(self):
        axis = axis_for(2025)
        for name in ("month", "day", "hour", "weekday", "is_weekend", "is_holiday", "day_type"):
            assert len(getattr(axis, name)) == axis.point_count, f"{name} 长度不等于点数"

    def test_hour_cycles_0_to_23(self):
        axis = axis_for(2025)
        assert axis.hour[:24].tolist() == list(range(24))
        assert axis.hour[24:48].tolist() == list(range(24))
        assert int(axis.hour.max()) == 23 and int(axis.hour.min()) == 0

    def test_delta_hours(self):
        assert axis_for(2025).delta_hours == 1.0

    def test_describe_mentions_year_and_resolution(self):
        text = axis_for(2025).describe()
        assert "2025" in text and "平年" in text and "8760" in text


class TestHolidays:
    def test_holiday_marks_day_type(self):
        """§3 P0.4：节假日要能与工作日、周末区分。"""
        holidays = (date(2025, 1, 1), date(2025, 5, 1), date(2025, 10, 1))
        axis = axis_for(2025, Resolution.HOURLY, holidays)

        assert bool(axis.is_holiday[0]) is True
        assert axis.day_type[0] == DayType.HOLIDAY.value
        assert int(axis.is_holiday.sum()) == 24 * len(holidays)

        may1 = 24 * (31 + 28 + 31 + 30)
        assert axis.day_type[may1] == DayType.HOLIDAY.value

    def test_weekend_distinguished_from_holiday(self):
        axis = axis_for(2025, Resolution.HOURLY, (date(2025, 1, 4),))  # 1/4 是周六
        day = 24 * 3
        assert axis.day_type[day] == DayType.HOLIDAY.value  # 节假日优先于周末
        assert axis.day_type[day + 24] == DayType.WEEKEND.value  # 1/5 周日

    def test_no_holidays_means_only_workday_and_weekend(self):
        axis = axis_for(2025)
        assert bool(axis.is_holiday.any()) is False
        assert set(axis.day_type.tolist()) == {DayType.WORKDAY.value, DayType.WEEKEND.value}

    def test_mask_day_type(self):
        axis = axis_for(2025)
        assert axis.mask_day_type(DayType.WEEKEND).sum() == int(axis.is_weekend.sum())
        assert np.array_equal(
            axis.mask_day_type(DayType.WORKDAY), axis.day_type == DayType.WORKDAY.value
        )


class TestMonthRanges:
    def test_twelve_ranges_cover_all_points(self):
        """§18 需量电费要按月取最大值，月度索引区间必须完整且不重叠。"""
        axis = axis_for(2025)
        ranges = axis.month_index_ranges()
        assert len(ranges) == 12
        assert sum(b - a for a, b in ranges) == axis.point_count
        for (_, b1), (a2, _) in zip(ranges, ranges[1:]):
            assert b1 == a2, "月度区间必须首尾相接、不重叠"

    @pytest.mark.parametrize(("month", "hours"), [(1, 744), (2, 672), (4, 720), (12, 744)])
    def test_month_hour_counts(self, month, hours):
        assert int((axis_for(2025).month == month).sum()) == hours

    def test_february_leap_year(self):
        assert int((axis_for(2024).month == 2).sum()) == 696  # 29 天


class TestOtherResolutions:
    def test_quarter_hourly_reserved(self):
        """§3 P0.1：为 15 分钟数据预留接口。"""
        axis = axis_for(2025, Resolution.QUARTER_HOURLY)
        assert axis.point_count == 35040
        assert axis.delta_hours == 0.25
        assert int(axis.hour[0]) == 0 and int(axis.hour[4]) == 1
        assert axis.timestamps[1].minute == 15
        assert axis.timestamps[0] == datetime(2025, 1, 1, 0, 0)

    def test_quarter_hourly_derived_fields_consistent(self):
        axis = axis_for(2025, Resolution.QUARTER_HOURLY)
        for i in (0, 1, 5, 1000, axis.point_count - 1):
            ts = axis.timestamps[i]
            assert axis.hour[i] == ts.hour
            assert axis.day[i] == ts.day and axis.month[i] == ts.month
            assert axis.weekday[i] == ts.weekday()

    def test_daily(self):
        axis = axis_for(2025, Resolution.DAILY)
        assert axis.point_count == 365
        assert axis.timestamps[0] == datetime(2025, 1, 1, 0, 0)
        assert axis.timestamps[-1] == datetime(2025, 12, 31, 0, 0)

    def test_monthly(self):
        axis = axis_for(2025, Resolution.MONTHLY)
        assert axis.point_count == 12
        assert axis.month.tolist() == list(range(1, 13))
        assert axis.timestamps[0] == datetime(2025, 1, 1)

    def test_leap_year_hourly(self):
        axis = axis_for(2024)
        assert axis.point_count == 8784
        assert axis.is_leap is True
        assert "闰年" in axis.describe()


class TestAnnualGrowthFactor:
    def test_first_year_is_one(self):
        """§8.3：第 1 年因子恒为 1。"""
        assert annual_growth_factor(0.05, 1) == 1.0
        assert annual_growth_factor(0.0, 1) == 1.0

    def test_compounding(self):
        assert annual_growth_factor(0.05, 3) == pytest.approx(1.05**2)
        assert annual_growth_factor(-0.02, 5) == pytest.approx(0.98**4)

    def test_zero_rate_stays_one(self):
        for year in (1, 5, 25):
            assert annual_growth_factor(0.0, year) == 1.0


class TestPerformanceAndContract:
    def test_axis_build_under_budget(self):
        """§86：单项目 8760 计算目标 < 2 秒，时间轴构造只占其中一小部分。"""
        start = time.perf_counter()
        axis = build_time_axis(2025)
        elapsed = time.perf_counter() - start
        assert axis.point_count == 8760
        assert elapsed < 1.0, f"时间轴构造耗时 {elapsed:.3f}s，超出预算"

    def test_axis_is_frozen(self):
        axis = axis_for(2025)
        with pytest.raises(Exception):
            axis.year = 2030  # type: ignore[misc]

    def test_cache_returns_same_instance(self):
        assert axis_for(2025) is axis_for(2025)
