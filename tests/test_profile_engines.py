"""V2 Phase 3 三条曲线引擎测试（V2 §3 P0.2–P0.4、§8、§9、§17、§18）。

覆盖 :mod:`cenep.calculation.load_profile`、:mod:`cenep.calculation.pv_profile`、
:mod:`cenep.calculation.tariff_series` 的全部模式、年增长/年度变化、规则优先级、
边界与中文报错，并有一条 8760 真实轴的全链路断言。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest
from pydantic import ValidationError as PydanticValidationError

from cenep.calculation import load_profile as lp
from cenep.calculation import pv_profile as pp
from cenep.calculation import tariff_series as ts
from cenep.calculation.errors import ValidationError
from cenep.calculation.timeseries_engine import build_time_axis
from cenep.domain.enums import (
    DayType,
    LoadProfileMode,
    PVProfileMode,
    Resolution,
    TariffPeriod,
)
from cenep.domain.timeseries import (
    LoadProfile,
    LoadProfileConfig,
    PVProfile,
    PVProfileConfig,
    TariffProfile,
    TariffSeriesConfig,
    TimePeriodRule,
    TimeSeriesPoint,
)

WORKDAY = [10.0] * 24
WEEKEND = [4.0] * 24


@pytest.fixture(scope="module")
def axis() -> object:
    """8760 点真实时间轴。"""
    return build_time_axis(2025)


@pytest.fixture(scope="module")
def axis_leap() -> object:
    return build_time_axis(2024)


@pytest.fixture(scope="module")
def axis_month() -> object:
    """12 点月粒度轴，用于低成本构造边界用例。"""
    return build_time_axis(2025, resolution=Resolution.MONTHLY)


def _flat_rules() -> list[TimePeriodRule]:
    """谷 0–6、峰 10–12、其余平段。"""
    out: list[TimePeriodRule] = []
    for h in range(24):
        if h in (10, 11, 12):
            out.append(TimePeriodRule(period=TariffPeriod.PEAK, hours=[h]))
        elif h in range(7):
            out.append(TimePeriodRule(period=TariffPeriod.VALLEY, hours=[h]))
        else:
            out.append(TimePeriodRule(period=TariffPeriod.FLAT, hours=[h]))
    return out


def _tariff(**over) -> TariffProfile:
    base: dict = dict(
        sharp_peak_price=1.20,
        peak_price=1.0,
        flat_price=0.65,
        valley_price=0.35,
        deep_valley_price=0.30,
        custom_price=0.50,
        export_price=0.35,
        demand_charge=42.0,
        basic_charge=1000.0,
        time_periods=_flat_rules(),
    )
    base.update(over)
    return TariffProfile(**base)


# =========================================================================== #
# 负荷：三种模式
# =========================================================================== #
class TestLoadHourly:
    def _points(self, axis, value: float = 100.0, n: int | None = None) -> list[TimeSeriesPoint]:
        ts_list = axis.timestamps[: n or axis.point_count]
        return [TimeSeriesPoint(timestamp=t, load_kwh=value) for t in ts_list]

    def test_shape_and_total(self, axis):
        pts = self._points(axis)
        out = lp.resolve_load_series(
            LoadProfileConfig(mode=LoadProfileMode.HOURLY, hourly=LoadProfile(points=pts)), axis
        )
        assert out.shape == (axis.point_count,)
        assert out.dtype == np.float64
        assert lp.annual_energy_of(out, axis) == pytest.approx(100.0 * axis.point_count, rel=1e-12)

    def test_leap_year_8784(self, axis_leap):
        pts = self._points(axis_leap, value=2.0)
        out = lp.resolve_load_series(
            LoadProfileConfig(mode=LoadProfileMode.HOURLY, hourly=LoadProfile(points=pts)), axis_leap
        )
        assert out.size == 8784
        assert lp.annual_energy_of(out, axis_leap) == pytest.approx(2.0 * 8784, rel=1e-12)

    def test_never_rescaled_by_annual_energy(self, axis):
        """HOURLY 用户给的就是实际值：即使填了 annual_energy_kwh 也严禁缩放。"""
        pts = self._points(axis, value=1.0)
        out = lp.resolve_load_series(
            LoadProfileConfig(
                mode=LoadProfileMode.HOURLY,
                hourly=LoadProfile(points=pts),
                annual_energy_kwh=9_999_999.0,
            ),
            axis,
        )
        assert lp.annual_energy_of(out, axis) == pytest.approx(float(axis.point_count), rel=1e-12)

    def test_negative_load_raises_chinese_error(self, axis_month):
        pts = [
            TimeSeriesPoint(timestamp=t, load_kwh=-5.0 if i == 3 else 1.0)
            for i, t in enumerate(axis_month.timestamps)
        ]
        with pytest.raises(ValidationError, match="负荷出现负值"):
            lp.resolve_load_series(
                LoadProfileConfig(mode=LoadProfileMode.HOURLY, hourly=LoadProfile(points=pts)),
                axis_month,
            )


class TestLoadTypicalDay:
    def _cfg(self, **over) -> LoadProfileConfig:
        base: dict = dict(
            mode=LoadProfileMode.TYPICAL_DAY, typical_workday=WORKDAY, typical_weekend=WEEKEND
        )
        base.update(over)
        return LoadProfileConfig(**base)

    def test_weekend_uses_weekend_curve(self, axis):
        out = lp.resolve_load_series(self._cfg(), axis)
        assert out.shape == (axis.point_count,)
        weekend_idx = np.where(~np.asarray(axis.is_weekend) & ~np.asarray(axis.is_holiday))[0]
        rest_idx = np.where(np.asarray(axis.is_weekend))[0]
        assert np.allclose(out[weekend_idx], 10.0)
        assert np.allclose(out[rest_idx], 4.0)

    def test_holiday_uses_weekend_curve(self):
        """§3 P0.4：节假日按周末曲线处理。"""
        hol = datetime(2025, 1, 8).date()   # 周三
        axis = build_time_axis(2025, holidays=[hol])
        out = lp.resolve_load_series(self._cfg(), axis)
        idx = [i for i, t in enumerate(axis.timestamps) if t.date() == hol and t.hour == 0]
        assert out[idx[0]] == pytest.approx(4.0)

    def test_annual_energy_normalizes_level(self, axis):
        out = lp.resolve_load_series(self._cfg(annual_energy_kwh=1_500_000.0), axis)
        assert lp.annual_energy_of(out, axis) == pytest.approx(1_500_000.0, rel=1e-9)

    def test_without_annual_energy_shape_is_absolute(self, axis):
        out = lp.resolve_load_series(self._cfg(), axis)
        expected = 10.0 * float((~np.asarray(axis.is_weekend) & ~np.asarray(axis.is_holiday)).sum())
        expected += 4.0 * float(np.asarray(axis.is_weekend).sum())
        assert lp.annual_energy_of(out, axis) == pytest.approx(expected, rel=1e-12)

    def test_monthly_factors_change_distribution_only(self, axis):
        factors = [1.0, 1.0, 1.5, 1.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        out = lp.resolve_load_series(
            self._cfg(annual_energy_kwh=1_200_000.0, monthly_factors=factors), axis
        )
        assert lp.annual_energy_of(out, axis) == pytest.approx(1_200_000.0, rel=1e-9)
        mar = out[np.asarray(axis.month) == 3].mean()
        jan = out[np.asarray(axis.month) == 1].mean()
        assert mar > jan

    def test_growth_year_three(self, axis):
        cfg = self._cfg(annual_energy_kwh=1_000_000.0, annual_growth_rate=0.05)
        y1 = lp.annual_energy_of(lp.resolve_load_series(cfg, axis, year_index=1), axis)
        y3 = lp.annual_energy_of(lp.resolve_load_series(cfg, axis, year_index=3), axis)
        assert y3 == pytest.approx(y1 * (1.05 ** 2), rel=1e-12)

    def test_shape_validation(self):
        with pytest.raises(PydanticValidationError, match="24 点"):
            LoadProfileConfig(mode=LoadProfileMode.TYPICAL_DAY, typical_workday=[1.0] * 23)
        with pytest.raises(PydanticValidationError, match="12 个"):
            LoadProfileConfig(mode=LoadProfileMode.TYPICAL_DAY, typical_workday=WORKDAY,
                              monthly_factors=[1.0] * 11)


class TestLoadAnnualSimple:
    def _cfg(self, **over) -> LoadProfileConfig:
        base: dict = dict(mode=LoadProfileMode.ANNUAL_SIMPLE, annual_energy_kwh=1_200_000.0)
        base.update(over)
        return LoadProfileConfig(**base)

    def test_total_equals_annual_energy(self, axis):
        out = lp.resolve_load_series(self._cfg(), axis)
        assert out.shape == (axis.point_count,)
        assert lp.annual_energy_of(out, axis) == pytest.approx(1_200_000.0, rel=1e-12)

    def test_shape_is_documented_default(self, axis):
        """形状来自 DEFAULT_LOAD_SHAPE：夜间低于白天。"""
        out = lp.resolve_load_series(self._cfg(), axis)
        hours = np.asarray(axis.hour)
        assert out[hours == 11].mean() > out[hours == 3].mean()

    def test_requires_positive_annual_energy(self, axis):
        with pytest.raises(ValidationError, match="年用电量必须大于 0"):
            lp.resolve_load_series(
                LoadProfileConfig(mode=LoadProfileMode.ANNUAL_SIMPLE), axis
            )

    def test_growth(self, axis):
        cfg = self._cfg(annual_growth_rate=0.03)
        y1 = lp.annual_energy_of(lp.resolve_load_series(cfg, axis, 1), axis)
        y3 = lp.annual_energy_of(lp.resolve_load_series(cfg, axis, 3), axis)
        assert y3 == pytest.approx(y1 * 1.03 ** 2, rel=1e-12)

    def test_monthly_factors_apply(self, axis):
        factors = [0.5] + [1.0] * 11
        out = lp.resolve_load_series(self._cfg(monthly_factors=factors), axis)
        assert lp.annual_energy_of(out, axis) == pytest.approx(1_200_000.0, rel=1e-12)
        assert out[np.asarray(axis.month) == 1].mean() < out[np.asarray(axis.month) == 6].mean()


class TestLoadHelpers:
    def test_typical_day_from_series(self, axis):
        out = lp.resolve_load_series(
            LoadProfileConfig(
                mode=LoadProfileMode.TYPICAL_DAY,
                typical_workday=[float(h) for h in range(24)],
                typical_weekend=[float(h) for h in range(24)],
            ),
            axis,
        )
        shape = lp.typical_day_from_series(out, axis)
        assert len(shape) == 24
        assert shape[20] > shape[3]

    def test_peak_load_uses_delta_hours(self):
        values = np.array([10.0, 20.0])
        assert lp.peak_load(values, Resolution.HOURLY) == pytest.approx(20.0)
        assert lp.peak_load(values, Resolution.QUARTER_HOURLY) == pytest.approx(80.0)
        assert lp.peak_load(np.array([])) == 0.0

    def test_point_count_mismatch_raises(self, axis_month):
        pts = [TimeSeriesPoint(timestamp=t, load_kwh=1.0) for t in axis_month.timestamps[:6]]
        with pytest.raises(ValidationError, match="点数"):
            lp.resolve_load_series(
                LoadProfileConfig(mode=LoadProfileMode.HOURLY, hourly=LoadProfile(points=pts)),
                axis_month,
            )


# =========================================================================== #
# 光伏：四种模式
# =========================================================================== #
class TestPVEquivalentHours:
    def _cfg(self, hours: float = 1100.0, **over) -> PVProfileConfig:
        base: dict = dict(mode=PVProfileMode.EQUIVALENT_HOURS, equivalent_hours=hours)
        base.update(over)
        return PVProfileConfig(**base)

    def test_annual_equals_capacity_times_hours(self, axis):
        out = pp.resolve_pv_series(self._cfg(), axis, capacity_kwp=1000.0)
        assert out.shape == (axis.point_count,)
        assert pp.annual_generation(out) == pytest.approx(1_100_000.0, rel=1e-9)

    def test_equivalent_hours_helper_roundtrip(self, axis):
        out = pp.resolve_pv_series(self._cfg(), axis, capacity_kwp=1000.0)
        assert pp.equivalent_hours(out, 1000.0) == pytest.approx(1100.0, rel=1e-9)

    def test_performance_ratio_multiplies(self, axis):
        out = pp.resolve_pv_series(self._cfg(performance_ratio=0.8), axis, capacity_kwp=1000.0)
        assert pp.annual_generation(out) == pytest.approx(1_100_000.0 * 0.8, rel=1e-9)

    def test_degradation_year_three(self, axis):
        cfg = self._cfg()
        y1 = pp.annual_generation(pp.resolve_pv_series(cfg, axis, 1000.0, year_index=1,
                                                      degradation_rate=0.005))
        y3 = pp.annual_generation(pp.resolve_pv_series(cfg, axis, 1000.0, year_index=3,
                                                      degradation_rate=0.005))
        assert y3 == pytest.approx(y1 * (0.995 ** 2), rel=1e-12)

    def test_no_nighttime_output(self, axis):
        out = pp.resolve_pv_series(self._cfg(), axis, capacity_kwp=1000.0)
        assert float(out[pp.night_generation_mask(axis)].max()) == 0.0

    def test_requires_positive_hours(self, axis):
        with pytest.raises(ValidationError, match="等效利用小时必须大于 0"):
            pp.resolve_pv_series(self._cfg(hours=0.0), axis, capacity_kwp=1000.0)

    def test_zero_capacity_returns_zero_series(self, axis):
        """V2 §78：PV 容量为 0 是**合法**输入（退化为纯电网负荷项目），出力恒为 0。

        早期实现把 0 当错误抛异常，与 §78 的退化要求冲突，已改为返回全 0 序列。
        """
        out = pp.resolve_pv_series(self._cfg(), axis, capacity_kwp=0.0)
        assert out.shape == (axis.point_count,)
        assert np.all(out == 0.0)

    def test_negative_capacity_rejected(self, axis):
        with pytest.raises(ValidationError, match="不能为负数"):
            pp.resolve_pv_series(self._cfg(), axis, capacity_kwp=-1.0)


class TestPVHourlyAndCoefficients:
    def _profile(self, axis, value: float, capacity: float) -> PVProfile:
        pts = [TimeSeriesPoint(timestamp=t, pv_generation_kwh=value) for t in axis.timestamps]
        return PVProfile(points=pts, capacity_kwp=capacity, annual_generation=value * len(pts))

    def test_normalize_profile_is_per_kwp(self):
        series = np.array([100.0, 500.0, 0.0])
        coef = pp.normalize_profile(series, 1000.0)
        assert np.allclose(coef, [0.1, 0.5, 0.0])

    def test_normalize_profile_requires_capacity(self):
        with pytest.raises(ValidationError, match="需要正的光伏容量"):
            pp.normalize_profile(np.array([1.0]), 0.0)

    def test_coefficient_within_bounds(self, axis):
        prof = self._profile(axis, 1.0, 1.0)
        out = pp.resolve_pv_series(
            PVProfileConfig(mode=PVProfileMode.HOURLY, hourly=prof), axis, capacity_kwp=1000.0
        )
        coef = pp.normalize_profile(out, 1000.0)
        assert float(coef.min()) >= 0.0
        assert float(coef.max()) <= 1.0 + 1e-9

    def test_coefficient_out_of_range_raises_chinese_error(self, axis_month):
        """曲线容量填小了 → 归一化系数 > 1，必须报中文错误（§9.1）。"""
        pts = [TimeSeriesPoint(timestamp=t, pv_generation_kwh=5.0) for t in axis_month.timestamps]
        prof = PVProfile(points=pts, capacity_kwp=1.0)
        with pytest.raises(ValidationError, match="归一化出力系数越界"):
            pp.resolve_pv_series(
                PVProfileConfig(mode=PVProfileMode.HOURLY, hourly=prof),
                axis_month,
                capacity_kwp=1000.0,
            )

    def test_check_coefficient_bounds_negative(self):
        with pytest.raises(ValidationError, match="出现负值"):
            pp.check_coefficient_bounds(np.array([0.5, -0.1]))

    def test_check_coefficient_bounds_ok(self):
        pp.check_coefficient_bounds(np.array([0.0, 0.5, 1.0]))

    def test_scaled_by_capacity_ratio(self, axis):
        prof = self._profile(axis, 1.0, 1.0)
        out = pp.resolve_pv_series(
            PVProfileConfig(mode=PVProfileMode.HOURLY, hourly=prof, performance_ratio=1.0),
            axis, capacity_kwp=1000.0,
        )
        assert pp.annual_generation(out) == pytest.approx(1000.0 * axis.point_count, rel=1e-12)

    def test_zero_curve_capacity_is_absolute(self, axis):
        prof = self._profile(axis, 3.0, 0.0)
        out = pp.resolve_pv_series(
            PVProfileConfig(mode=PVProfileMode.HOURLY, hourly=prof), axis, capacity_kwp=1000.0
        )
        assert pp.annual_generation(out) == pytest.approx(3.0 * axis.point_count, rel=1e-12)

    def test_point_mismatch_raises(self, axis_month):
        pts = [TimeSeriesPoint(timestamp=t, pv_generation_kwh=1.0) for t in axis_month.timestamps[:5]]
        with pytest.raises(ValidationError, match="点数"):
            pp.resolve_pv_series(
                PVProfileConfig(mode=PVProfileMode.HOURLY, hourly=PVProfile(points=pts)),
                axis_month, capacity_kwp=100.0,
            )

    def test_night_generation_mask_shape(self, axis):
        mask = pp.night_generation_mask(axis)
        assert mask.shape == (axis.point_count,)
        assert bool(mask[np.asarray(axis.hour) == 2].all())
        assert not bool(mask[np.asarray(axis.hour) == 12].any())


class TestPVTypicalDayAndFactors:
    def test_typical_day_area_defines_energy(self, axis):
        """equivalent_hours=0 时按系数面积归一化：Σ(coef×Δt) 即等效小时。"""
        shape = [0.0] * 6 + [0.5] * 12 + [0.0] * 6      # 面积 6.0
        out = pp.resolve_pv_series(
            PVProfileConfig(mode=PVProfileMode.TYPICAL_DAY, typical_day=shape),
            axis, capacity_kwp=100.0,
        )
        assert pp.equivalent_hours(out, 100.0) == pytest.approx(365 * 6.0, rel=1e-9)

    def test_typical_day_scaled_to_equivalent_hours(self, axis):
        shape = [0.0] * 6 + [0.5] * 12 + [0.0] * 6
        out = pp.resolve_pv_series(
            PVProfileConfig(
                mode=PVProfileMode.TYPICAL_DAY, typical_day=shape, equivalent_hours=1200.0
            ),
            axis, capacity_kwp=100.0,
        )
        assert pp.equivalent_hours(out, 100.0) == pytest.approx(1200.0, rel=1e-9)

    def test_monthly_hour_factor_area(self, axis):
        hours = [0.0] * 6 + [1.0] * 12 + [0.0] * 6
        out = pp.resolve_pv_series(
            PVProfileConfig(
                mode=PVProfileMode.MONTHLY_HOUR_FACTOR,
                hour_factors=hours, monthly_factors=[1.0] * 12,
            ),
            axis, capacity_kwp=100.0,
        )
        assert pp.equivalent_hours(out, 100.0) == pytest.approx(365 * 12.0, rel=1e-9)

    def test_monthly_hour_factor_scaled_to_hours(self, axis):
        hours = [0.0] * 6 + [1.0] * 12 + [0.0] * 6
        out = pp.resolve_pv_series(
            PVProfileConfig(
                mode=PVProfileMode.MONTHLY_HOUR_FACTOR, hour_factors=hours,
                monthly_factors=[1.0] * 12, equivalent_hours=1000.0,
            ),
            axis, capacity_kwp=100.0,
        )
        assert pp.equivalent_hours(out, 100.0) == pytest.approx(1000.0, rel=1e-9)

    def test_monthly_factors_shape_seasonality(self, axis):
        shape = [0.0] * 6 + [0.5] * 12 + [0.0] * 6
        factors = [0.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.5, 1.5, 1.0, 1.0, 1.0, 0.5]
        out = pp.resolve_pv_series(
            PVProfileConfig(
                mode=PVProfileMode.TYPICAL_DAY, typical_day=shape, monthly_factors=factors
            ),
            axis, capacity_kwp=100.0,
        )
        jul = out[np.asarray(axis.month) == 7].mean()
        jan = out[np.asarray(axis.month) == 1].mean()
        assert jul > jan

    def test_peak_power_uses_delta_hours(self):
        values = np.array([10.0, 20.0])
        assert pp.peak_power(values, Resolution.HOURLY) == pytest.approx(20.0)
        assert pp.peak_power(values, Resolution.QUARTER_HOURLY) == pytest.approx(80.0)
        assert pp.peak_power(np.array([])) == 0.0


# =========================================================================== #
# 电价
# =========================================================================== #
class TestTariffPeriodMapping:
    def test_all_period_prices(self):
        profile = _tariff()
        assert ts.price_for_period(profile, TariffPeriod.SHARP_PEAK) == pytest.approx(1.20)
        assert ts.price_for_period(profile, TariffPeriod.PEAK) == pytest.approx(1.0)
        assert ts.price_for_period(profile, TariffPeriod.FLAT) == pytest.approx(0.65)
        assert ts.price_for_period(profile, TariffPeriod.VALLEY) == pytest.approx(0.35)
        assert ts.price_for_period(profile, TariffPeriod.DEEP_VALLEY) == pytest.approx(0.30)
        assert ts.price_for_period(profile, TariffPeriod.CUSTOM) == pytest.approx(0.50)

    def test_period_array_types(self, axis):
        arr = ts.resolve_period_array(_tariff(), axis)
        assert arr.shape == (axis.point_count,)
        assert arr[0] is TariffPeriod.VALLEY
        assert arr[10] is TariffPeriod.PEAK
        assert arr[8] is TariffPeriod.FLAT

    def test_price_series_matches_periods(self, axis):
        price, export = ts.resolve_price_series(TariffSeriesConfig(profile=_tariff()), axis)
        assert price.shape == (axis.point_count,)
        assert export.shape == (axis.point_count,)
        assert price[0] == pytest.approx(0.35)
        assert price[10] == pytest.approx(1.0)
        assert np.allclose(export, 0.35)

    def test_sharp_peak_and_deep_valley(self, axis):
        rules = [
            TimePeriodRule(period=TariffPeriod.SHARP_PEAK, hours=[12]),
            TimePeriodRule(period=TariffPeriod.DEEP_VALLEY, hours=[3]),
        ]
        rules += [
            TimePeriodRule(period=TariffPeriod.FLAT, hours=[h]) for h in range(24) if h not in (3, 12)
        ]
        price, _ = ts.resolve_price_series(
            TariffSeriesConfig(profile=_tariff(time_periods=rules)), axis
        )
        assert price[12] == pytest.approx(1.20)
        assert price[3] == pytest.approx(0.30)


class TestTariffRulePrecedence:
    def test_later_rule_overrides_earlier(self, axis):
        """优先级：按声明顺序，**后面的规则覆盖前面的**。"""
        rules = [
            TimePeriodRule(period=TariffPeriod.PEAK, hours=[10]),        # 先声明
            TimePeriodRule(period=TariffPeriod.DEEP_VALLEY, hours=[10]),  # 后声明 → 生效
            TimePeriodRule(period=TariffPeriod.FLAT, hours=[h for h in range(24) if h != 10]),
        ]
        arr = ts.resolve_period_array(_tariff(time_periods=rules), axis)
        assert arr[10] is TariffPeriod.DEEP_VALLEY

    def test_specific_seasonal_rule_after_generic(self, axis):
        rules = [
            TimePeriodRule(period=TariffPeriod.PEAK, hours=[11, 12, 13]),          # 全年高峰
            TimePeriodRule(period=TariffPeriod.SHARP_PEAK, months=[7, 8], hours=[11, 12, 13]),
            TimePeriodRule(period=TariffPeriod.FLAT, hours=[h for h in range(24) if h not in (11, 12, 13)]),
        ]
        price, _ = ts.resolve_price_series(TariffSeriesConfig(profile=_tariff(time_periods=rules)), axis)
        jul = (np.asarray(axis.month) == 7) & (np.asarray(axis.hour) == 11)
        jan = (np.asarray(axis.month) == 1) & (np.asarray(axis.hour) == 11)
        assert price[jul][0] == pytest.approx(1.20)
        assert price[jan][0] == pytest.approx(1.0)


class TestTariffDayTypeAndMonth:
    def test_weekend_rule_applies(self, axis):
        rules = [
            TimePeriodRule(period=TariffPeriod.PEAK, hours=[10]),
            TimePeriodRule(period=TariffPeriod.DEEP_VALLEY, day_types=[DayType.WEEKEND], hours=[10]),
            TimePeriodRule(period=TariffPeriod.FLAT, hours=[h for h in range(24) if h != 10]),
        ]
        price, _ = ts.resolve_price_series(TariffSeriesConfig(profile=_tariff(time_periods=rules)), axis)
        sat = [i for i, t in enumerate(axis.timestamps) if t.weekday() == 5 and t.hour == 10]
        wed = [i for i, t in enumerate(axis.timestamps) if t.weekday() == 2 and t.hour == 10]
        assert price[sat[0]] == pytest.approx(0.30)
        assert price[wed[0]] == pytest.approx(1.0)

    def test_holiday_rule_applies(self):
        hol = datetime(2025, 1, 8).date()
        axis = build_time_axis(2025, holidays=[hol])
        rules = [
            TimePeriodRule(period=TariffPeriod.PEAK, hours=[10]),
            TimePeriodRule(period=TariffPeriod.DEEP_VALLEY, day_types=[DayType.HOLIDAY], hours=[10]),
            TimePeriodRule(period=TariffPeriod.FLAT, hours=[h for h in range(24) if h != 10]),
        ]
        price, _ = ts.resolve_price_series(TariffSeriesConfig(profile=_tariff(time_periods=rules)), axis)
        idx = [i for i, t in enumerate(axis.timestamps) if t.hour == 10 and t.date() == hol]
        assert price[idx[0]] == pytest.approx(0.30)

    def test_month_rule_applies(self, axis):
        rules = [
            TimePeriodRule(period=TariffPeriod.PEAK, hours=[10]),
            TimePeriodRule(period=TariffPeriod.SHARP_PEAK, months=[12, 1, 2], hours=[10]),
            TimePeriodRule(period=TariffPeriod.FLAT, hours=[h for h in range(24) if h != 10]),
        ]
        price, _ = ts.resolve_price_series(TariffSeriesConfig(profile=_tariff(time_periods=rules)), axis)
        dec = (np.asarray(axis.month) == 12) & (np.asarray(axis.hour) == 10)
        may = (np.asarray(axis.month) == 5) & (np.asarray(axis.hour) == 10)
        assert price[dec][0] == pytest.approx(1.20)
        assert price[may][0] == pytest.approx(1.0)


class TestTariffUnmatchedAndGrowth:
    def test_unmatched_falls_back_to_flat(self, axis):
        """未命中规则的小时用 FLAT 兜底，不抛异常，并能被上层感知。"""
        rules = [TimePeriodRule(period=TariffPeriod.PEAK, hours=[10])]
        profile = _tariff(time_periods=rules)
        mask = ts.unmatched_mask(profile, axis)
        assert int(mask.sum()) == axis.point_count - int((np.asarray(axis.hour) == 10).sum())
        price, _ = ts.resolve_price_series(TariffSeriesConfig(profile=profile), axis)
        assert price[np.asarray(axis.hour) == 10][0] == pytest.approx(1.0)
        assert price[np.asarray(axis.hour) == 5][0] == pytest.approx(0.65)

    def test_no_rules_means_all_flat(self, axis):
        profile = _tariff(time_periods=[])
        assert bool(ts.unmatched_mask(profile, axis).all())
        price, _ = ts.resolve_price_series(TariffSeriesConfig(profile=profile), axis)
        assert np.allclose(price, 0.65)

    def test_full_coverage_no_unmatched(self, axis):
        assert int(ts.unmatched_mask(_tariff(), axis).sum()) == 0

    def test_annual_growth_applies_to_purchase_price(self, axis):
        cfg = TariffSeriesConfig(profile=_tariff(), annual_growth_rate=0.03)
        p1, _ = ts.resolve_price_series(cfg, axis, year_index=1)
        p3, _ = ts.resolve_price_series(cfg, axis, year_index=3)
        assert p3[10] == pytest.approx(p1[10] * 1.03 ** 2, rel=1e-12)

    def test_export_price_does_not_grow(self, axis):
        """§17.2：上网电价由政策单独决定，不随购电电价增长。"""
        cfg = TariffSeriesConfig(profile=_tariff(), annual_growth_rate=0.10)
        _, e1 = ts.resolve_price_series(cfg, axis, year_index=1)
        _, e3 = ts.resolve_price_series(cfg, axis, year_index=3)
        assert np.allclose(e3, e1)
        assert e3[0] == pytest.approx(0.35)

    def test_negative_price_raises(self, axis):
        """模型层已用 ``ge=0`` 拦截负电价；这里绕过模型校验，验证引擎层兜底检查。"""
        profile = TariffProfile.model_construct(
            tariff_id="", name="", effective_date=None, region="湖北",
            time_periods=_flat_rules(),
            sharp_peak_price=1.20, peak_price=1.0, flat_price=-0.1,
            valley_price=0.35, deep_valley_price=0.30, custom_price=0.50,
            export_price=0.35, demand_charge=0.0, basic_charge=0.0,
        )
        config = TariffSeriesConfig.model_construct(
            profile=profile, annual_growth_rate=0.0,
            demand_charge_enabled=False, basic_charge_enabled=False,
        )
        with pytest.raises(ValidationError, match="电价出现负值"):
            ts.resolve_price_series(config, axis)


class TestTariffSeriesCompat:
    """``resolve_tariff`` / ``TariffSeries`` 供调度引擎使用，行为需保持可用。"""

    def test_resolve_tariff_shape(self, axis):
        series = ts.resolve_tariff(TariffSeriesConfig(profile=_tariff()), axis)
        assert len(series) == axis.point_count
        assert series.price.shape == (axis.point_count,)
        assert series.period_code.shape == (axis.point_count,)

    def test_period_share_sums_to_one(self, axis):
        series = ts.resolve_tariff(TariffSeriesConfig(profile=_tariff()), axis)
        assert sum(series.period_share().values()) == pytest.approx(1.0, rel=1e-12)

    def test_energy_cost(self, axis):
        series = ts.resolve_tariff(TariffSeriesConfig(profile=_tariff()), axis)
        energy = np.full(axis.point_count, 10.0)
        assert series.energy_cost(energy) == pytest.approx(float(np.sum(energy * series.price)), rel=1e-12)

    def test_weighted_average_and_spread(self, axis):
        series = ts.resolve_tariff(TariffSeriesConfig(profile=_tariff()), axis)
        energy = np.ones(axis.point_count)
        assert ts.weighted_average_price(series, energy) == pytest.approx(float(series.price.mean()), rel=1e-12)
        assert ts.weighted_average_price(series, np.zeros(axis.point_count)) == 0.0
        assert ts.peak_valley_spread(series) == pytest.approx(1.0 - 0.35, rel=1e-12)

    def test_demand_charge_grows_with_factor(self, axis):
        cfg = TariffSeriesConfig(profile=_tariff(), annual_growth_rate=0.05, demand_charge_enabled=True)
        s1 = ts.resolve_tariff(cfg, axis, year_index=1)
        s3 = ts.resolve_tariff(cfg, axis, year_index=3)
        assert s3.demand_charge == pytest.approx(s1.demand_charge * 1.05 ** 2, rel=1e-12)
        assert s3.demand_charge_enabled is True


# =========================================================================== #
# 全链路：8760 真实轴
# =========================================================================== #
class TestFullChainOnRealAxis:
    def test_three_engines_align_with_axis(self, axis):
        """8760 真实轴全链路：三个引擎输出长度必须都等于 axis.point_count。"""
        load = lp.resolve_load_series(
            LoadProfileConfig(mode=LoadProfileMode.ANNUAL_SIMPLE, annual_energy_kwh=1_500_000.0),
            axis,
        )
        pv = pp.resolve_pv_series(
            PVProfileConfig(mode=PVProfileMode.EQUIVALENT_HOURS, equivalent_hours=1100.0),
            axis, capacity_kwp=1000.0, degradation_rate=0.005,
        )
        price, export = ts.resolve_price_series(TariffSeriesConfig(profile=_tariff()), axis)

        for arr in (load, pv, price, export):
            assert arr.shape == (axis.point_count,)
            assert arr.dtype == np.float64

        assert lp.annual_energy_of(load, axis) == pytest.approx(1_500_000.0, rel=1e-12)
        # 首年不衰减（衰减因子 (1-d)^(n-1)，n=1 时为 1）
        assert pp.equivalent_hours(pv, 1000.0) == pytest.approx(1100.0, rel=1e-9)
        assert float(price.min()) >= 0.0 and float(export.min()) >= 0.0
        assert int(ts.unmatched_mask(_tariff(), axis).sum()) == 0

    def test_leap_year_full_chain(self, axis_leap):
        load = lp.resolve_load_series(
            LoadProfileConfig(mode=LoadProfileMode.ANNUAL_SIMPLE, annual_energy_kwh=1_000_000.0),
            axis_leap,
        )
        pv = pp.resolve_pv_series(
            PVProfileConfig(mode=PVProfileMode.EQUIVALENT_HOURS, equivalent_hours=1100.0),
            axis_leap, capacity_kwp=1000.0,
        )
        price, _ = ts.resolve_price_series(TariffSeriesConfig(profile=_tariff()), axis_leap)
        assert load.size == pv.size == price.size == 8784

    def test_quarter_hourly_chain(self):
        ax = build_time_axis(2025, resolution=Resolution.QUARTER_HOURLY)
        load = lp.resolve_load_series(
            LoadProfileConfig(mode=LoadProfileMode.ANNUAL_SIMPLE, annual_energy_kwh=1_200_000.0), ax
        )
        price, _ = ts.resolve_price_series(TariffSeriesConfig(profile=_tariff()), ax)
        assert load.shape == (35040,)
        assert price.shape == (35040,)
        assert lp.annual_energy_of(load, ax) == pytest.approx(1_200_000.0, rel=1e-12)
