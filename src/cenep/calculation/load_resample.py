"""负荷重采样与功率/电量换算（V2.2 §3.2、§6.4；阶段 3）。

本模块是"15/30/60 分钟负荷如何归一到统一时间轴"的**唯一公式来源**
（V1 §104、V2 §61：公式不得散落在界面或导入器里）。

口径（必须先读，且这些口径会写进结果的 ``assumptions``）
======================================================

**1. 负荷数据是能量还是功率？**

CENEP 内部**统一存电量**：一个时间点的值 = 该间隔内的电量 ``E_i``，单位 **kWh/间隔**，
区间语义为左闭右开 ``[t, t + Δt)``（V2.2 §2.2）。功率 ``P_i`` 是派生量：

.. code-block:: text

    E_i [kWh] = P_i [kW] × Δt_i [h]          （规格书 §3.2 的第一条公式）
    P_i [kW]  = E_i [kWh] / Δt_i [h]

因此"源文件是功率还是电量"必须显式声明（:class:`~cenep.domain.enums.LoadValueKind`）：

* 源文件是 **kWh**：直接累加，**不得再乘 Δt**（再乘一次就是 4 倍错误）；
* 源文件是 **kW**：先乘 Δt 变电量，再进入后续计算。

**2. 降采样（15 → 60 分钟）：电量求和，功率按时长加权平均**

.. code-block:: text

    E_hour = Σ_{i∈hour} E_i                  ← 电量求和（能量守恒，唯一正确口径）
    P_hour = Σ_{i∈hour} (P_i × Δt_i) / Σ_{i∈hour} Δt_i   ← 时长加权平均（等长间隔即算术平均）

对**等长细分**，两者严格自洽：``P_hour × 1 h = Σ E_i``。**不允许**对电量取算术平均，
也不允许对功率求和（那会得到"4 小时的功率之和"，量纲上等于 4 倍电量）。

**3. 升采样（60 → 15 分钟）：电量均分，功率阶梯保持**

.. code-block:: text

    E_i = E_hour / 4          （假设：小时内功率恒定 —— 这是**假设**，必须记录）
    P_i = P_hour              （阶梯保持，与上面的均分严格等价）

均分是 §6.4 明确允许的"可配置假设"："优先将小时能量均匀分配到四个 15 分钟间隔
仅可作为可配置假设，并在结果中标记"。因此本模块把该假设写入 ``ResamplePlan.assumptions``，
并让结果数据集带上同一批假设（下游报告必须如实披露）。

**4. 能量守恒自检**

降采样与升采样都满足 ``Σ E_out = Σ E_in``（浮点容差内）。本模块对每次转换做自检，
不满足即抛**中文**错误（宁可失败也不产出"看起来差不多"的曲线）。

边界条件
--------
* ``from_minutes`` 与 ``to_minutes`` 必须为正整数分钟且能整除（15⇄30⇄60 均可；
  15→45、15→100 这类不整除的组合直接报中文错误，不做近似）；
* 降采样要求输入点数能被比例整除（尾部不完整的一段必须先补齐或裁剪，
  由调用方显式决定，本模块不静默丢弃）；
* 空输入返回空结果（由调用方决定是否算作错误）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from ..domain.enums import LoadDataSourceType, LoadValueKind, Resolution
from ..domain.load_data import HighFrequencyLoadDataset
from ..domain.timeseries import TimeSeriesPoint
from .errors import ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "ENERGY_TOLERANCE_KWH",
    "ResamplePlan",
    "downsample_energy",
    "downsample_power",
    "energy_from_power",
    "power_from_energy",
    "resample_dataset",
    "resample_energy",
    "resample_power",
    "to_energy_kwh",
    "to_power_kw",
    "upsample_energy",
    "upsample_power",
]

#: 能量守恒自检容差（kWh）：与 V2 §19 的平衡容差同量级，但按"整条曲线"判定，
#: 因此按点数放大到 1e-6 × 点数，避免 35040 点时浮点累积误判。
ENERGY_TOLERANCE_KWH = 1e-6


@dataclass(frozen=True)
class ResamplePlan:
    """一次重采样的完整口径说明（可被报告与测试直接断言）。"""

    from_minutes: int
    to_minutes: int
    value_kind: LoadValueKind
    direction: str
    factor: int
    method: str
    assumptions: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        """一行中文口径说明，供界面与报告显示。"""
        return (
            f"{self.from_minutes} 分钟 → {self.to_minutes} 分钟（每 {self.factor} 个间隔合并/拆分）："
            f"源口径「{self.value_kind.label}」，{self.method}"
        )


# --------------------------------------------------------------------------- #
# 功率 ⇄ 电量（唯一换算入口，Δt 在这里且只在这里出现）
# --------------------------------------------------------------------------- #
def _as_float_array(values, *, label: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValidationError(f"{label}必须是一维序列，实际维度为 {array.ndim}", field="load.resample")
    if array.size and not np.all(np.isfinite(array)):
        bad = int(np.sum(~np.isfinite(array)))
        raise ValidationError(
            f"{label}中有 {bad} 个非有限数值（空值/文本/无穷大），"
            f"请先按缺失处理策略补齐再重采样（V2.2 §6.4）",
            field="load.resample",
        )
    return array


def energy_from_power(power_kw, delta_hours: float) -> np.ndarray:
    """``E_i = P_i × Δt_h``（规格书 §3.2）。单位：kW × h = kWh。

    :param delta_hours: 该间隔的时长（小时）；15 分钟为 0.25，30 分钟为 0.5，1 小时为 1.0
    :raises ValidationError: 数组非一维、含非有限值、或 ``delta_hours`` 非正
    """
    dt = _positive_delta(delta_hours)
    return _as_float_array(power_kw, label="功率序列") * dt


def power_from_energy(energy_kwh, delta_hours: float) -> np.ndarray:
    """``P_i = E_i / Δt_h``（规格书 §3.2）：得到该间隔的**平均功率**（kW）。"""
    dt = _positive_delta(delta_hours)
    return _as_float_array(energy_kwh, label="电量序列") / dt


def _positive_delta(delta_hours: float) -> float:
    try:
        dt = float(delta_hours)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 类型错误属调用方 bug
        raise ValidationError(f"Δt 必须是小时数，实际为 {delta_hours!r}", field="load.resample") from exc
    if not np.isfinite(dt) or dt <= 0.0:
        raise ValidationError(
            f"Δt 必须为正的小时数（15 分钟 = 0.25 h），实际为 {delta_hours!r}；"
            f"Δt 为 0 会把电量与功率混为一谈（V2.2 §3.2）",
            field="load.resample",
        )
    return dt


def to_energy_kwh(values, value_kind: LoadValueKind, delta_hours: float) -> np.ndarray:
    """按声明的口径把源数值统一转成**电量 kWh**（导入与重采样的唯一入口）。

    * ``INTERVAL_ENERGY_KWH`` → 原样返回（**不乘 Δt**）；
    * ``POWER_KW`` → ``× Δt_h``。
    """
    array = _as_float_array(values, label="源数值序列")
    if value_kind is LoadValueKind.INTERVAL_ENERGY_KWH:
        return array.copy()
    return array * _positive_delta(delta_hours)


def to_power_kw(values, value_kind: LoadValueKind, delta_hours: float) -> np.ndarray:
    """按声明的口径把源数值统一转成**间隔平均功率 kW**。"""
    array = _as_float_array(values, label="源数值序列")
    if value_kind is LoadValueKind.POWER_KW:
        return array.copy()
    return array / _positive_delta(delta_hours)


# --------------------------------------------------------------------------- #
# 重采样比例校验
# --------------------------------------------------------------------------- #
def _ratio(from_minutes: int, to_minutes: int) -> tuple[int, str]:
    """返回 ``(比例, 方向)``；不整除时报中文错误。"""
    try:
        source = int(from_minutes)
        target = int(to_minutes)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"时间间隔必须是整数分钟，实际为 {from_minutes!r} → {to_minutes!r}",
            field="load.resample",
        ) from exc
    if source <= 0 or target <= 0:
        raise ValidationError(
            f"时间间隔必须为正数，实际为 {source} 分钟 → {target} 分钟", field="load.resample"
        )
    if source == target:
        raise ValidationError(
            f"源间隔与目标间隔相同（均为 {source} 分钟），无需重采样；"
            f"如需统一时间轴请直接使用原曲线（V2.2 §6.4）",
            field="load.resample",
        )
    if source % target == 0:
        return source // target, "upsample"
    if target % source == 0:
        return target // source, "downsample"
    raise ValidationError(
        f"不支持 {source} 分钟 → {target} 分钟的重采样：两者不能整除，"
        f"无法在不丢失或臆造电量的前提下对齐（V2.2 §6.4）；"
        f"请先把数据聚合到两者的公共间隔（如 15/30/60 分钟）再对齐",
        field="load.resample",
    )


def _check_divisible(size: int, factor: int, *, from_minutes: int, to_minutes: int) -> None:
    if size % factor != 0:
        raise ValidationError(
            f"{from_minutes} 分钟数据共 {size} 点，无法按 {factor} 个一组聚合成 {to_minutes} 分钟"
            f"（尾部缺少 {(-size) % factor} 点）。请先补齐或裁剪到完整区间，"
            f"系统不会静默丢弃尾部数据（V2.2 §6.4）",
            field="load.resample",
        )


# --------------------------------------------------------------------------- #
# 降采样 / 升采样（电量口径与功率口径分别实现，绝不混用）
# --------------------------------------------------------------------------- #
def downsample_energy(energy_kwh, from_minutes: int, to_minutes: int) -> np.ndarray:
    """电量降采样：**求和**（能量守恒）。例如 15 → 60 分钟即 4 个间隔电量相加。"""
    factor, direction = _ratio(from_minutes, to_minutes)
    if direction != "downsample":
        raise ValidationError(
            f"downsample_energy 只用于降采样，收到 {from_minutes} → {to_minutes} 分钟；"
            f"升采样请调用 upsample_energy（V2.2 §3.2）",
            field="load.resample",
        )
    values = _as_float_array(energy_kwh, label="电量序列")
    _check_divisible(values.size, factor, from_minutes=from_minutes, to_minutes=to_minutes)
    return values.reshape(-1, factor).sum(axis=1)


def upsample_energy(energy_kwh, from_minutes: int, to_minutes: int) -> np.ndarray:
    """电量升采样：**均分**到子间隔（假设子间隔内功率恒定）。

    该假设由 :func:`resample_energy` 写入 ``ResamplePlan.assumptions``，
    调用方必须把它带进结果与报告（V2.2 §6.4）。
    """
    factor, direction = _ratio(from_minutes, to_minutes)
    if direction != "upsample":
        raise ValidationError(
            f"upsample_energy 只用于升采样，收到 {from_minutes} → {to_minutes} 分钟；"
            f"降采样请调用 downsample_energy（V2.2 §3.2）",
            field="load.resample",
        )
    values = _as_float_array(energy_kwh, label="电量序列")
    return np.repeat(values / factor, factor)


def downsample_power(power_kw, from_minutes: int, to_minutes: int) -> np.ndarray:
    """功率降采样：**按区间时长加权平均**（等长细分下即算术平均）。

    得到的是目标间隔的**平均功率**；其对应电量 = 结果 × 目标 Δt_h，
    与 :func:`downsample_energy` 的结果严格一致。
    """
    factor, direction = _ratio(from_minutes, to_minutes)
    if direction != "downsample":
        raise ValidationError(
            f"downsample_power 只用于降采样，收到 {from_minutes} → {to_minutes} 分钟",
            field="load.resample",
        )
    values = _as_float_array(power_kw, label="功率序列")
    _check_divisible(values.size, factor, from_minutes=from_minutes, to_minutes=to_minutes)
    # 等长细分：时长加权平均退化为算术平均；写成加权形式是为了把口径写死在代码里
    weights = np.full(factor, float(from_minutes), dtype=float)
    blocks = values.reshape(-1, factor)
    return (blocks * weights).sum(axis=1) / weights.sum()


def upsample_power(power_kw, from_minutes: int, to_minutes: int) -> np.ndarray:
    """功率升采样：**阶梯保持**（子间隔平均功率 = 父间隔平均功率）。"""
    factor, direction = _ratio(from_minutes, to_minutes)
    if direction != "upsample":
        raise ValidationError(
            f"upsample_power 只用于升采样，收到 {from_minutes} → {to_minutes} 分钟",
            field="load.resample",
        )
    values = _as_float_array(power_kw, label="功率序列")
    return np.repeat(values, factor)


# --------------------------------------------------------------------------- #
# 带口径记录与守恒自检的组合入口
# --------------------------------------------------------------------------- #
def _check_conservation(
    before: float, after: float, *, plan_desc: str, points: int
) -> None:
    tolerance = ENERGY_TOLERANCE_KWH * max(points, 1)
    if abs(before - after) > tolerance:
        raise ValidationError(
            f"重采样后电量不守恒：{plan_desc}，重采样前 {before:.6f} kWh、"
            f"重采样后 {after:.6f} kWh，差 {after - before:+.6f} kWh（容差 {tolerance:.1e} kWh）。"
            f"该结果不可用，请检查间隔与数据完整性（V2.2 §3.2、§19）",
            field="load.resample",
        )


def resample_energy(
    energy_kwh, from_minutes: int, to_minutes: int
) -> tuple[np.ndarray, ResamplePlan]:
    """电量序列重采样，返回 ``(新序列, 口径说明)``；电量守恒（求和 / 均分）。"""
    factor, direction = _ratio(from_minutes, to_minutes)
    values = _as_float_array(energy_kwh, label="电量序列")
    if direction == "downsample":
        result = downsample_energy(values, from_minutes, to_minutes)
        method = f"电量求和（每 {factor} 个间隔的电量相加，能量守恒）"
        assumptions: tuple[str, ...] = ()
    else:
        result = upsample_energy(values, from_minutes, to_minutes)
        method = f"电量均分（每个间隔均分到 {factor} 个子间隔）"
        assumptions = (
            f"升采样假设：{from_minutes} 分钟间隔内功率恒定，"
            f"因此该间隔电量被均分到 {factor} 个 {to_minutes} 分钟子间隔"
            "（规格书 §6.4 允许的可配置假设，必须随结果披露）",
        )
    plan = ResamplePlan(
        from_minutes=int(from_minutes),
        to_minutes=int(to_minutes),
        value_kind=LoadValueKind.INTERVAL_ENERGY_KWH,
        direction=direction,
        factor=factor,
        method=method,
        assumptions=assumptions,
    )
    _check_conservation(
        float(values.sum()), float(result.sum()), plan_desc=plan.describe(), points=values.size
    )
    logger.debug("电量重采样：%s；合计 %.6f kWh 保持不变", plan.describe(), float(result.sum()))
    return result, plan


def resample_power(
    power_kw, from_minutes: int, to_minutes: int
) -> tuple[np.ndarray, ResamplePlan]:
    """功率序列重采样，返回 ``(新序列, 口径说明)``。

    * 降采样：时长加权平均 → 得到目标间隔**平均功率**；
    * 升采样：阶梯保持 → 得到子间隔平均功率（等价于电量均分）。

    两种情形都校验"由功率换算出的电量守恒"，防止把平均与求和混用。
    """
    factor, direction = _ratio(from_minutes, to_minutes)
    values = _as_float_array(power_kw, label="功率序列")
    if direction == "downsample":
        result = downsample_power(values, from_minutes, to_minutes)
        method = f"功率按区间时长加权平均（每 {factor} 个间隔取平均，得到 {to_minutes} 分钟平均功率）"
        assumptions: tuple[str, ...] = ()
    else:
        result = upsample_power(values, from_minutes, to_minutes)
        method = f"功率阶梯保持（每个间隔复制到 {factor} 个子间隔，与电量均分严格等价）"
        assumptions = (
            f"升采样假设：{from_minutes} 分钟间隔内功率恒定，"
            f"因此该间隔平均功率直接复制到 {factor} 个 {to_minutes} 分钟子间隔"
            "（规格书 §6.4 允许的可配置假设，必须随结果披露）",
        )
    plan = ResamplePlan(
        from_minutes=int(from_minutes),
        to_minutes=int(to_minutes),
        value_kind=LoadValueKind.POWER_KW,
        direction=direction,
        factor=factor,
        method=method,
        assumptions=assumptions,
    )
    before = float(values.sum()) * (from_minutes / 60.0)
    after = float(result.sum()) * (to_minutes / 60.0)
    _check_conservation(before, after, plan_desc=plan.describe(), points=values.size)
    logger.debug("功率重采样：%s；等效电量 %.6f kWh 保持不变", plan.describe(), after)
    return result, plan


def _check_contiguous(dataset: HighFrequencyLoadDataset) -> None:
    """重采样要求源数据点在时间上连续（间隔恰为声明值）。

    否则"重新生成时间戳"会把缺口一并掩盖，产出看似完整实则错位的曲线。
    """
    points = dataset.points
    if len(points) < 2:
        return
    expected = timedelta(minutes=dataset.interval_minutes)
    for index in range(1, len(points)):
        actual = points[index].timestamp - points[index - 1].timestamp
        if actual != expected:
            raise ValidationError(
                f"重采样前数据必须连续：第 {index} 个点到第 {index + 1} 个点的间隔为 "
                f"{actual.total_seconds() / 60:g} 分钟，而声明间隔为 "
                f"{dataset.interval_minutes} 分钟（{points[index - 1].timestamp:%Y-%m-%d %H:%M} → "
                f"{points[index].timestamp:%Y-%m-%d %H:%M}）。请先按缺失处理策略补齐缺口"
                f"（V2.2 §6.4：缺失不得默认按 0 处理）",
                field="load.resample",
            )


# --------------------------------------------------------------------------- #
# 数据集级重采样（供阶段 4 的 PV/负荷对齐调用）
# --------------------------------------------------------------------------- #
def resample_dataset(
    dataset: HighFrequencyLoadDataset,
    to_minutes: int,
    *,
    project_id: str | None = None,
) -> tuple[HighFrequencyLoadDataset, ResamplePlan]:
    """把负荷数据集重采样到目标间隔，返回 ``(新数据集, 口径说明)``。

    口径由**数据集的 ``value_kind``** 决定（源文件是功率还是电量），
    因此不存在"同一个数组被两种口径各算一遍"的可能：

    * ``INTERVAL_ENERGY_KWH`` → :func:`resample_energy`（求和 / 均分）；
    * ``POWER_KW`` → 先按 Δt 转成电量，再走 :func:`resample_energy`
      （对降采样，等价于对功率取时长加权平均）。

    新数据集的 ``value_kind`` 一律记为 ``INTERVAL_ENERGY_KWH``：
    CENEP 内部存储口径就是电量，源口径与换算过程写入 ``assumptions`` 以便追溯。
    ``source_type`` 保持不变（重采样**不改变**"实测还是估算"的标签，§0.2）。
    """
    if dataset.point_count == 0:
        raise ValidationError(
            "负荷数据集没有数据点，无法重采样", field="load.resample"
        )
    target_resolution = Resolution.from_interval_minutes(to_minutes)
    if target_resolution is None:
        raise ValidationError(
            f"目标间隔 {to_minutes} 分钟没有对应的内部分辨率，"
            f"V2.2 支持 15/30/60 分钟（以及日、月）；"
            f"其它粒度须先确认口径（V2.2 §6.1）",
            field="load.resample",
        )

    source_delta_hours = dataset.delta_hours
    energy = to_energy_kwh(
        dataset.interval_energy_kwh, dataset.value_kind, source_delta_hours
    )
    result, plan = resample_energy(energy, dataset.interval_minutes, to_minutes)

    assumptions = list(dataset.assumptions)
    assumptions.append(plan.describe())
    if dataset.value_kind is LoadValueKind.POWER_KW:
        assumptions.append(
            f"源文件为间隔平均功率 kW，已按 E = P × Δt（Δt = {source_delta_hours:g} h）"
            f"转为电量后再重采样，未对功率直接求和"
        )
    assumptions.extend(plan.assumptions)

    # 时间戳按目标间隔**重新生成**（升采样时原来一个间隔要展开成多个子间隔，
    # 直接沿用原时间戳会得到"4 个点同一个时刻"的假曲线）
    step = timedelta(minutes=to_minutes)
    _check_contiguous(dataset)
    start = dataset.points[0].timestamp
    points = [
        TimeSeriesPoint(timestamp=start + step * index, load_kwh=float(value))
        for index, value in enumerate(result)
    ]
    new_dataset = HighFrequencyLoadDataset(
        profile_id=f"{dataset.profile_id or 'load'}->{to_minutes}min",
        project_id=dataset.project_id if project_id is None else project_id,
        name=f"{dataset.name or dataset.profile_id}（{to_minutes} 分钟）",
        source_type=dataset.source_type,
        value_kind=LoadValueKind.INTERVAL_ENERGY_KWH,
        interval_minutes=int(to_minutes),
        resolution=target_resolution,
        timezone=dataset.timezone,
        period_start=points[0].timestamp if points else None,
        period_end=(points[-1].timestamp + step) if points else None,
        points=points,
        annualized=dataset.annualized,
        estimated=dataset.estimated,
        coverage_ratio=dataset.coverage_ratio,
        missing_intervals=0,
        duplicate_intervals=0,
        irregular_intervals=0,
        quality_status=dataset.quality_status,
        quality_messages=list(dataset.quality_messages),
        source_file_name=dataset.source_file_name,
        source_sheet=dataset.source_sheet,
        mapping_config=dict(dataset.mapping_config),
        assumptions=assumptions,
        created_at=dataset.created_at,
    )
    logger.info(
        "负荷重采样完成：%s；%d 点 → %d 点，年电量 %.3f kWh",
        plan.describe(),
        dataset.point_count,
        new_dataset.point_count,
        new_dataset.annual_energy_kwh,
    )
    return new_dataset, plan
