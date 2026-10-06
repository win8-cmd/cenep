"""V2 光伏出力引擎（V2 §3 P0.3、§9）。

把 :class:`cenep.domain.timeseries.PVProfileConfig` 的四种取得方式解析为
**逐周期发电量数组**（kWh/周期）。本模块是光伏出力的唯一公式来源（V1 §104、V2 §61）。

基础公式（§9.1）

.. code-block:: text

    PV_t = Profile_t × PV_Capacity × PR          0 ≤ Profile_t ≤ 1

``Profile_t`` 是**归一化出力系数**（相对额定容量的出力比例）。
年衰减（§9.2）：

.. code-block:: text

    PV_t,n = PV_t,1 × (1 − degradation)^(n-1)

四种模式
--------
======================  ==================================================================
``HOURLY``              导入曲线：``hourly.capacity_kwp > 0`` 时先归一化再按目标容量缩放；
                        为 ``0`` 时视为**已是目标容量**的出力，只乘 ``PR``
``TYPICAL_DAY``         24 点系数 × 12 个月度系数
``MONTHLY_HOUR_FACTOR`` 12 个月度系数 × 24 个小时系数（两者相乘）
``EQUIVALENT_HOURS``    退化为 V1 口径：全年合计 = ``容量 × 等效小时 × PR``
======================  ==================================================================

**面积归一化的两种口径**（``TYPICAL_DAY`` 与 ``MONTHLY_HOUR_FACTOR``）

* 若 ``equivalent_hours > 0``：系数曲线只定义**形状**，整体缩放到
  ``容量 × 等效小时 × PR``，保证与 V1 的年发电量口径一致（V2 §77）；
* 若 ``equivalent_hours == 0``：**按系数面积归一化** —— 即把系数曲线的时间积分
  直接当作等效小时数（``Σ Profile_t × Δt``），不再额外缩放。

实现约束：全程 NumPy 向量化，**不逐小时 Python 循环**（V2 §86、§87）。
"""

from __future__ import annotations

import logging

import numpy as np

from ..domain.enums import PVProfileMode
from ..domain.timeseries import PVProfileConfig
from .errors import ValidationError
from .timeseries_engine import TimeAxis, annual_growth_factor

logger = logging.getLogger(__name__)

#: 判定夜间的小时（§54 把"PV 夜间发电"列为必须告警的异常值）
NIGHT_HOURS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 20, 21, 22, 23)

#: 默认归一化典型日出力系数（峰值 = 1），仅用于 ``EQUIVALENT_HOURS`` 模式。
#: **系统默认形状**（``SYSTEM_DEFAULT``），用户可用典型日或 8760 曲线覆盖。
#: 注意 0–5 时与 20–23 时（:data:`NIGHT_HOURS`）必须为 0，否则会被夜间发电校验器判为异常数据。
DEFAULT_PV_SHAPE: tuple[float, ...] = (
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.12, 0.30, 0.52, 0.72, 0.88, 0.97,
    1.00, 0.97, 0.88, 0.72, 0.52, 0.30, 0.12, 0.02, 0.00, 0.00, 0.00, 0.00,
)

#: 默认月度发电量系数（华中地区经验值）。用途是让逐时曲线具备季节性；
#: 在 ``EQUIVALENT_HOURS`` 模式下**不改变年发电量**（末尾整体归一化到目标电量）。
DEFAULT_MONTHLY_FACTORS: tuple[float, ...] = (
    0.62, 0.70, 0.86, 1.02, 1.12, 1.10, 1.18, 1.15, 1.05, 0.95, 0.78, 0.62,
)

#: 归一化系数越界容差
_COEF_TOL = 1e-9


def normalize_profile(series: np.ndarray, capacity_kwp: float) -> np.ndarray:
    """把出力序列归一化为**相对额定容量的出力系数**（§9.1）。

    ``Profile_t = PV_t ÷ PV_Capacity``。注意本函数按 V2 §9.1 的定义只除以容量，
    因此调用方需保证传入的序列与容量处于**同一时间粒度**：
    小时粒度下电量数值等于功率，可直接使用；15 分钟粒度下应先除以 ``Δt``
    （即传入功率序列而非电量序列），否则系数会被低估 4 倍。
    """
    if capacity_kwp <= 0.0:
        raise ValidationError(
            "归一化出力系数需要正的光伏容量", field="timeseries.pv.capacity_kwp"
        )
    return np.asarray(series, dtype=float) / float(capacity_kwp)


def check_coefficient_bounds(
    coefficient: np.ndarray, *, field: str = "timeseries.pv"
) -> None:
    """校验归一化出力系数落在 ``[0, 1]``（§9.1），越界抛中文错误。"""
    coef = np.asarray(coefficient, dtype=float)
    if coef.size == 0:
        return
    if np.any(coef < -_COEF_TOL):
        raise ValidationError(
            f"归一化出力系数出现负值（最小 {float(coef.min()):.6f}），请检查光伏曲线",
            field=field,
        )
    peak = float(coef.max())
    if peak > 1.0 + _COEF_TOL:
        raise ValidationError(
            f"归一化出力系数越界：最大 {peak:.6f} > 1.0（§9.1 要求 0 ≤ Profile ≤ 1）。"
            "常见原因是曲线容量填写偏小，或导入的是功率而容量按 kWp 直流侧填写。",
            field=field,
        )


def night_generation_mask(axis: TimeAxis) -> np.ndarray:
    """夜间小时掩码（供校验器发现"PV 夜间发电"，§54）。

    本引擎**不会**静默清零夜间出力——那是数据问题，应由上层校验器报告（V2 §53）。
    """
    return np.isin(np.asarray(axis.hour, dtype=np.int64), np.asarray(NIGHT_HOURS))


def _expand_daily(curve: list[float] | tuple[float, ...], axis: TimeAxis) -> np.ndarray:
    arr = np.asarray(curve, dtype=float)
    return arr[np.asarray(axis.hour, dtype=np.int64)]


def _monthly_factor_array(factors: list[float], axis: TimeAxis) -> np.ndarray:
    if not factors:
        return np.ones(axis.point_count, dtype=float)
    return np.asarray(factors, dtype=float)[np.asarray(axis.month, dtype=np.int64) - 1]


def resolve_pv_series(
    config: PVProfileConfig,
    axis: TimeAxis,
    capacity_kwp: float,
    year_index: int = 1,
    degradation_rate: float = 0.0,
) -> np.ndarray:
    """解析逐周期光伏发电量（kWh/周期）。

    :param config: 光伏曲线配置（V2 §6、§9）
    :param axis: 时间轴
    :param capacity_kwp: 项目装机容量 kWp（由调用方传 ``project.pv.pv_capacity_kwp``）
    :param year_index: 第几个运营年，``1`` 为首年（§9.2）
    :param degradation_rate: 年衰减率，由调用方传 ``project.pv.annual_degradation_rate``
    :returns: ``float64`` 数组，长度 = ``axis.point_count``
    """
    n = axis.point_count
    if n == 0:
        raise ValidationError("时间轴为空，无法解析光伏曲线", field="timeseries.pv")
    if capacity_kwp < 0.0:
        raise ValidationError("光伏装机容量不能为负数", field="timeseries.pv.capacity_kwp")
    if capacity_kwp == 0.0:
        # V2 §78：PV 容量为 0 时项目退化为纯电网负荷项目，出力恒为 0。
        # 这是合法输入（储能单独项目、容量扫描的 0 点），不是错误。
        return np.zeros(n, dtype=float)

    pr = float(config.performance_ratio)
    target = float(config.capacity_kwp or capacity_kwp)
    dt = axis.delta_hours if axis.delta_hours > 0.0 else 1.0
    hours = float(config.equivalent_hours)

    if config.mode is PVProfileMode.HOURLY:
        if config.hourly is None or config.hourly.is_empty:
            raise ValidationError(
                "光伏模式为「导入 8760 出力曲线」但未提供曲线数据", field="timeseries.pv.hourly"
            )
        points = config.hourly.points
        if len(points) != n:
            raise ValidationError(
                f"光伏曲线点数（{len(points)}）与时间轴（{n}）不一致；"
                f"{axis.year} 年应为 {n} 点"
                + ("（闰年 8784 点）" if axis.is_leap else "（平年 8760 点）"),
                field="timeseries.pv.hourly.points",
            )
        raw = np.fromiter((p.pv_generation_kwh for p in points), dtype=float, count=n)
        curve_capacity = float(config.hourly.capacity_kwp)
        if curve_capacity > 0.0:
            coefficient = normalize_profile(raw, curve_capacity)
            check_coefficient_bounds(coefficient, field="timeseries.pv.hourly")
            values = coefficient * target * pr * dt
        else:
            # 曲线视为本项目在该容量下的实际发电量
            values = raw * pr

    elif config.mode is PVProfileMode.TYPICAL_DAY:
        if not config.typical_day:
            raise ValidationError(
                "光伏模式为「典型日曲线」但未提供 typical_day（24 点）",
                field="timeseries.pv.typical_day",
            )
        shape = _expand_daily(config.typical_day, axis) * _monthly_factor_array(
            config.monthly_factors, axis
        )
        check_coefficient_bounds(shape, field="timeseries.pv.typical_day")
        values = shape * target * pr * dt
        if hours > 0.0:
            area_kwh = float(values.sum())
            if area_kwh <= 0.0:
                raise ValidationError("光伏典型日曲线合计为 0", field="timeseries.pv.typical_day")
            values = values * (target * hours * pr / area_kwh)

    elif config.mode is PVProfileMode.MONTHLY_HOUR_FACTOR:
        shape = _expand_daily(config.hour_factors, axis) * _monthly_factor_array(
            config.monthly_factors, axis
        )
        check_coefficient_bounds(shape, field="timeseries.pv.monthly_hours")
        values = shape * target * pr * dt
        if hours > 0.0:
            area_kwh = float(values.sum())
            if area_kwh <= 0.0:
                raise ValidationError(
                    "月度系数 × 小时系数合计为 0，无法按等效小时归一化",
                    field="timeseries.pv.monthly_hours",
                )
            values = values * (target * hours * pr / area_kwh)

    else:  # EQUIVALENT_HOURS：V1 口径，全年合计必须 = 容量 × 等效小时 × PR
        if hours <= 0.0:
            raise ValidationError(
                "光伏模式为「年等效小时」时，等效利用小时必须大于 0",
                field="timeseries.pv.equivalent_hours",
            )
        shape = _expand_daily(DEFAULT_PV_SHAPE, axis) * _monthly_factor_array(
            list(DEFAULT_MONTHLY_FACTORS), axis
        )
        area_kwh = float(shape.sum())
        if area_kwh <= 0.0:
            raise ValidationError("光伏默认出力形状合计为 0", field="timeseries.pv")
        values = shape * (target * hours * pr / area_kwh)

    factor = (1.0 - float(degradation_rate)) ** (year_index - 1)
    values = np.maximum(values * factor, 0.0)

    logger.debug(
        "光伏解析：模式=%s 年序=%d 点数=%d 年发电量=%.1f kWh 等效小时=%.3f h",
        config.mode.value, year_index, n, float(values.sum()),
        float(values.sum()) / capacity_kwp if capacity_kwp > 0 else 0.0,
    )
    return values


def annual_generation(series: np.ndarray) -> float:
    """逐周期发电量合计（kWh）。"""
    return float(np.sum(series))


def equivalent_hours(series: np.ndarray, capacity_kwp: float) -> float:
    """年等效利用小时（h）。"""
    if capacity_kwp <= 0.0:
        return 0.0
    return float(np.sum(series)) / float(capacity_kwp)


def _delta_hours_of(axis_or_resolution: object) -> float:
    """从 ``TimeAxis`` 或 ``Resolution`` 取 Δt；两者都没有时退回 1 h。"""
    dt = getattr(axis_or_resolution, "delta_hours", 1.0)
    try:
        dt = float(dt)
    except (TypeError, ValueError):
        dt = 1.0
    return dt if dt > 0.0 else 1.0


def peak_power(series: np.ndarray, axis_or_resolution: object = None) -> float:
    """峰值出力（kW）= 单周期最大发电量 ÷ Δt。

    ``axis_or_resolution`` 可传 :class:`TimeAxis` 或 ``Resolution``；
    必须用真实 Δt，否则 15 分钟粒度下功率会被低估 4 倍。
    """
    if series.size == 0:
        return 0.0
    return float(np.max(series)) / _delta_hours_of(axis_or_resolution)


def resolve_pv(
    config: PVProfileConfig,
    axis: TimeAxis,
    capacity_kwp: float,
    degradation_rate: float = 0.0,
    year_index: int = 1,
) -> np.ndarray:
    """向后兼容别名：参数顺序沿用旧签名 ``(..., degradation_rate, year_index)``。"""
    return resolve_pv_series(
        config, axis, capacity_kwp, year_index=year_index, degradation_rate=degradation_rate
    )
