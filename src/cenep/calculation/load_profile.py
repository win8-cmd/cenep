"""V2 负荷曲线引擎（V2 §3 P0.2、§8）。

把 :class:`cenep.domain.timeseries.LoadProfileConfig` 的三种取得方式解析为
**逐周期负荷电量数组**（kWh/周期）。本模块是负荷的唯一公式来源（V1 §104、V2 §61）。

三种模式（§8.1 优先级：用户 8760 > 用户典型日 > 系统模板/经验参数）

====================  ==================================================================
``HOURLY``            直接取用户导入的逐时负荷；点数必须与时间轴一致
``TYPICAL_DAY``       典型工作日/周末 24 点曲线 × 月度系数；**节假日按周末曲线处理**
``ANNUAL_SIMPLE``     只给年电量；按内置典型日形状比例摊到全年，使全年合计 == 年电量
====================  ==================================================================

年负荷增长（§8.3）

.. code-block:: text

    Load_n[t] = Load_1[t] × (1 + g)^(n-1)

**ANNUAL_SIMPLE 的形状规则**（必须明确，否则"年电量"落不到逐时）

仅有年电量时，用内置 :data:`DEFAULT_LOAD_SHAPE`（24 点相对负荷，峰值 = 1，
刻画两班制厂房"夜间基荷低、白天高"的形态）按小时重复到整条时间轴，
再整体缩放使 ``Σ Load = annual_energy_kwh``。因此该模式下**年度合计恒等于用户输入的年电量**，
逐时形状则来自系统默认形状（``source_type = SYSTEM_DEFAULT``），
用户若掌握真实形状应改用 ``TYPICAL_DAY`` 或 ``HOURLY``。

实现约束：全程 NumPy 向量化，**不逐小时 Python 循环**（V2 §86、§87）。
"""

from __future__ import annotations

import logging

import numpy as np

from ..domain.enums import LoadProfileMode
from ..domain.timeseries import LoadProfileConfig
from .errors import ValidationError
from .timeseries_engine import TimeAxis, annual_growth_factor

logger = logging.getLogger(__name__)

#: 两班制厂房的默认 24 点相对负荷形状（峰值 = 1，仅用于 ``ANNUAL_SIMPLE``）。
#: 夜间保留 ~28%~35% 基荷（空压机、照明、值班），白天 ~85%~100%。
#: 属**系统默认形状**，不改变年电量，只决定月度/逐时分布。
DEFAULT_LOAD_SHAPE: tuple[float, ...] = (
    0.35, 0.30, 0.28, 0.28, 0.30, 0.35, 0.45, 0.65,
    0.85, 0.95, 1.00, 1.00, 0.95, 0.98, 1.00, 0.98,
    0.90, 0.75, 0.60, 0.50, 0.45, 0.42, 0.40, 0.38,
)

#: 判断"周末曲线"用的日类型集合（节假日按周末处理，V2 §3 P0.4）
_REST_DAY_TYPES = ("WEEKEND", "HOLIDAY")


def _day_type_array(axis: TimeAxis) -> np.ndarray:
    """日类型数组（字符串），统一为 ``str`` 便于 ``np.isin`` 比较。"""
    return np.asarray(axis.day_type).astype(str)


def _rest_day_mask(axis: TimeAxis) -> np.ndarray:
    """周末或节假日掩码。"""
    return np.isin(_day_type_array(axis), _REST_DAY_TYPES)


def _expand_daily(curve: list[float] | tuple[float, ...], axis: TimeAxis) -> np.ndarray:
    """把 24 点曲线按小时展开到整条时间轴（向量化，无 Python 循环）。"""
    arr = np.asarray(curve, dtype=float)
    return arr[np.asarray(axis.hour, dtype=np.int64)]


def _monthly_factor_array(factors: list[float], axis: TimeAxis) -> np.ndarray:
    """12 个月度系数展开为逐点系数；空列表视为全年 1.0。"""
    if not factors:
        return np.ones(axis.point_count, dtype=float)
    return np.asarray(factors, dtype=float)[np.asarray(axis.month, dtype=np.int64) - 1]


def resolve_load_series(
    config: LoadProfileConfig, axis: TimeAxis, year_index: int = 1
) -> np.ndarray:
    """解析逐周期负荷电量（kWh/周期）。

    :param config: 负荷曲线配置（V2 §6、§8）
    :param axis: 时间轴（:func:`cenep.calculation.timeseries_engine.build_time_axis`）
    :param year_index: 第几个运营年，``1`` 为首年（§8.3 年增长因子）
    :returns: ``float64`` 数组，长度 = ``axis.point_count``
    :raises ValidationError: 点数不匹配、年电量非正、出现负值等（中文报错）
    """
    n = axis.point_count
    if n == 0:
        raise ValidationError("时间轴为空，无法解析负荷曲线", field="timeseries.load")

    if config.mode is LoadProfileMode.HOURLY:
        if config.hourly is None or config.hourly.is_empty:
            raise ValidationError(
                "负荷模式为「导入 8760 小时曲线」但未提供曲线数据",
                field="timeseries.load.hourly",
            )
        points = config.hourly.points
        if len(points) != n:
            raise ValidationError(
                f"负荷曲线点数（{len(points)}）与时间轴（{n}）不一致；"
                f"{axis.year} 年应为 {n} 点"
                + ("（闰年 8784 点）" if axis.is_leap else "（平年 8760 点）"),
                field="timeseries.load.hourly.points",
            )
        values = np.fromiter((p.load_kwh for p in points), dtype=float, count=n)

    elif config.mode is LoadProfileMode.TYPICAL_DAY:
        workday = config.typical_workday or config.typical_weekend
        weekend = config.typical_weekend or workday
        if not workday:
            raise ValidationError(
                "负荷模式为「典型日曲线」但未提供工作日曲线（24 点）",
                field="timeseries.load.typical_workday",
            )
        rest = _rest_day_mask(axis)
        values = np.where(
            rest, _expand_daily(weekend, axis), _expand_daily(workday, axis)
        ) * _monthly_factor_array(config.monthly_factors, axis)
        if config.annual_energy_kwh > 0.0:
            # 典型日曲线只定义**形状**，``annual_energy_kwh`` 定义**水平**：
            # 整体缩放到目标年电量。这样"已知年用电量但不掌握逐时形状"的用户也能用典型日。
            total = float(values.sum())
            if total <= 0.0:
                raise ValidationError(
                    "典型日负荷曲线合计为 0，无法按年用电量归一化",
                    field="timeseries.load.typical_workday",
                )
            values = values * (config.annual_energy_kwh / total)

    else:  # ANNUAL_SIMPLE
        if config.annual_energy_kwh <= 0.0:
            raise ValidationError(
                "负荷模式为「年电量 + 昼夜占比」时，年用电量必须大于 0",
                field="timeseries.load.annual_energy_kwh",
            )
        shape = _expand_daily(DEFAULT_LOAD_SHAPE, axis)
        shape = shape * _monthly_factor_array(config.monthly_factors, axis)
        area = float(shape.sum())
        if area <= 0.0:
            raise ValidationError("负荷形状合计为 0，无法按年电量分配", field="timeseries.load")
        values = shape * (config.annual_energy_kwh / area)

    values = values * annual_growth_factor(config.annual_growth_rate, year_index)

    if np.any(values < 0.0):
        worst = float(values.min())
        raise ValidationError(
            f"负荷出现负值（最小 {worst:.4f} kWh），请检查曲线数据（§8.2：Load[t] ≥ 0）",
            field="timeseries.load",
        )

    logger.debug(
        "负荷解析：模式=%s 年序=%d 点数=%d 年电量=%.1f kWh",
        config.mode.value, year_index, n, float(values.sum()),
    )
    return values


def annual_energy_of(series: np.ndarray, axis: TimeAxis) -> float:
    """逐周期负荷电量合计（kWh）。"""
    return float(np.sum(series))


def typical_day_from_series(series: np.ndarray, axis: TimeAxis) -> list[float]:
    """从逐时序列反推 24 点典型日形状（各小时均值），供报告与"另存为典型日"。"""
    hours = np.asarray(axis.hour, dtype=np.int64)
    values = np.asarray(series, dtype=float)
    out = np.zeros(24, dtype=float)
    sums = np.zeros(24, dtype=float)
    np.add.at(sums, hours, values)
    counts = np.bincount(hours, minlength=24).astype(float)
    nz = counts > 0
    out[nz] = sums[nz] / counts[nz]
    return [float(v) for v in out]


def _delta_hours_of(axis_or_resolution: object) -> float:
    """从 ``TimeAxis`` 或 ``Resolution`` 取 Δt；两者都没有时退回 1 h。"""
    dt = getattr(axis_or_resolution, "delta_hours", 1.0)
    try:
        dt = float(dt)
    except (TypeError, ValueError):
        dt = 1.0
    return dt if dt > 0.0 else 1.0


def peak_load_of(series: np.ndarray, axis: TimeAxis) -> float:
    """最大负荷功率（kW）= 单周期最大电量 ÷ Δt（必须用真实 Δt）。"""
    if series.size == 0:
        return 0.0
    return float(np.max(series)) / _delta_hours_of(axis)


def peak_load(series: np.ndarray, axis_or_resolution: object = None) -> float:
    """向后兼容别名，``axis_or_resolution`` 可传 :class:`TimeAxis` 或 ``Resolution``。"""
    if series.size == 0:
        return 0.0
    return float(np.max(series)) / _delta_hours_of(axis_or_resolution)


#: 向后兼容别名（旧名 ``resolve_load``）
resolve_load = resolve_load_series
