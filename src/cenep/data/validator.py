"""V2 时序数据校验（V2 §52、§53、§54；规则编号见 ``DATA_IMPORT_SPEC.md`` 第 5、7 节）。

处理分档（§5）
--------------
* **拒载** → ``level="ERROR"``    （V01 时间列不可用、V02 时间重复、V08 负值、V15 时间轴不一致）
* **告警** → ``level="WARNING"``  （V04 间隔错误、V06 跨年、V09 单位异常、V10 全零、
  V12 异常值、V13 电价恒定、V14 年电量偏差、V16 光伏夜间发电、V17 等效小时异常）
* **可修复** → ``level="INFO"``  （V03 时间缺失、V05 未排序、V07 平闰年点数、V11 空值/非数值）

``DataQualityIssue.category`` 取值
--------------------------------
``"completeness"`` 缺失、``"continuity"`` 时间轴连续性、``"outlier"`` 异常值、
``"unit"`` 单位疑点、``"source"`` 来源可信度。规则编号写在 ``message`` 开头
（例如 ``"[V02] 发现 3 个重复时间点…"``），便于测试断言与报告检索。

校验顺序固定为
``V01 → V02 → V05 → V03 → V07 → V04 → V06``（时间轴）
``→ V08 → V11 → V09 → V10 → V13 → V12``（数值）
``→ V14 → V15 → V16 → V17``（交叉），保证同一文件重复导入的报错条数与顺序完全一致（§5.4）。
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np

from ..calculation.errors import ValidationError
from ..calculation.timeseries_engine import TimeAxis
from ..domain.enums import MissingDataPolicy, Resolution
from ..domain.timeseries import TimeSeriesPoint
from ..domain.timeseries_results import DataQualityIssue
from .importer import detect_unit_issues

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SAMPLES",
    "VALUE_FIELD",
    "check_timeline_consistency",
    "detect_duplicates",
    "detect_missing",
    "detect_outliers",
    "fill_missing",
    "validate_series",
]

#: 每类问题最多保留的示例条数（§5.4 报告要求"给出位置、数值、可能原因"）
MAX_SAMPLES = 5

#: ``kind`` → 数值字段名
VALUE_FIELD: dict[str, str] = {
    "load": "load_kwh",
    "pv": "pv_generation_kwh",
    "price": "electricity_price",
    "export_price": "export_price",
}

#: 光伏夜间时段（§7 R-PV-NIGHT）：20:00–23:59 与 00:00–04:59
NIGHT_HOURS: tuple[int, ...] = (20, 21, 22, 23, 0, 1, 2, 3, 4)

#: 光伏年等效利用小时的常见区间（§7 R-PV-CAP）
PV_HOURS_RANGE: tuple[float, float] = (800.0, 1600.0)

#: 前值填充的最大回溯步数（§6.2）
FORWARD_FILL_LOOKBACK = 168

#: 线性插值推荐适用的最大缺口长度（§6.2：仅适合短缺口）
LINEAR_INTERPOLATION_MAX_GAP = 3


def _values(points: list[TimeSeriesPoint], kind: str) -> np.ndarray:
    field = VALUE_FIELD[kind]
    return np.asarray([getattr(p, field, float("nan")) for p in points], dtype=float)


def _sample_times(points: list[TimeSeriesPoint], indices) -> list[str]:
    out: list[str] = []
    for idx in list(indices)[:MAX_SAMPLES]:
        out.append(points[int(idx)].timestamp.strftime("%Y-%m-%d %H:%M"))
    return out


def _timestamps(points: list[TimeSeriesPoint]) -> np.ndarray:
    return np.asarray([p.timestamp for p in points], dtype="datetime64[ns]")


# --------------------------------------------------------------------------- #
# 时间轴校验
# --------------------------------------------------------------------------- #
def detect_duplicates(points: list[TimeSeriesPoint]) -> list[DataQualityIssue]:
    """V02 时间重复（§5.1）。同一 ``timestamp`` 出现多次即拒载。

    ``count`` 记**出现重复的时间点数**（即出现次数 > 1 的不同时间戳个数）。
    """
    if len(points) < 2:
        return []
    stamps = _timestamps(points)
    unique, counts = np.unique(stamps, return_counts=True)
    duplicated = unique[counts > 1]
    if duplicated.size == 0:
        return []
    samples = [
        np.datetime_as_string(ts, unit="m").replace("T", " ") for ts in duplicated[:MAX_SAMPLES]
    ]
    return [
        DataQualityIssue(
            level="ERROR",
            category="continuity",
            message=(
                f"[V02] 发现 {int(duplicated.size)} 个重复时间点，"
                f"例如 {samples[0] if samples else ''}"
            ),
            count=int(duplicated.size),
            samples=samples,
        )
    ]


def detect_missing(
    points: list[TimeSeriesPoint], axis: TimeAxis
) -> tuple[list[int], list[DataQualityIssue]]:
    """V03 时间缺失（§5.1）。返回 `(缺失位置索引, issues)`。

    位置索引是**时间轴上的下标**，这样填充与后续仿真能直接对齐。
    """
    expected = len(axis.timestamps)
    axis_index = {ts: i for i, ts in enumerate(axis.timestamps)}
    covered = np.zeros(expected, dtype=bool)
    for point in points:
        idx = axis_index.get(point.timestamp)
        if idx is not None:
            covered[idx] = True

    missing = np.flatnonzero(~covered).tolist()
    if not missing:
        return [], []

    completeness = (expected - len(missing)) / expected if expected else 0.0
    samples = [
        axis.timestamps[i].strftime("%Y-%m-%d %H:%M") for i in missing[:MAX_SAMPLES]
    ]
    return missing, [
        DataQualityIssue(
            level="INFO",
            category="completeness",
            message=(
                f"[V03] 发现 {len(missing)} 个时间点缺失"
                f"（应 {expected} 点，实得 {expected - len(missing)} 点，"
                f"完整性 {completeness:.2%}）。请选择处理方式："
                f"① 线性插值 ② 前值填充 ③ 典型日填充 ④ 拒绝计算"
            ),
            count=len(missing),
            samples=samples,
        )
    ]


def _detect_interval_errors(points: list[TimeSeriesPoint], axis: TimeAxis) -> list[DataQualityIssue]:
    """V04 间隔错误（§5.1）：相邻时间差 ≠ 声明分辨率。"""
    if len(points) < 2:
        return []
    stamps = _timestamps(points)
    deltas = np.diff(stamps).astype("timedelta64[s]").astype(np.int64)
    expected_seconds = int(axis.delta_hours * 3600.0) or 3600
    bad = np.flatnonzero(deltas != expected_seconds)
    if bad.size == 0:
        return []
    samples = []
    for idx in bad[:MAX_SAMPLES]:
        left, right = points[int(idx)], points[int(idx) + 1]
        samples.append(f"{left.timestamp:%Y-%m-%d %H:%M}→{right.timestamp:%H:%M}")
    first = points[int(bad[0])]
    actual_hours = deltas[int(bad[0])] / 3600.0
    return [
        DataQualityIssue(
            level="WARNING",
            category="continuity",
            message=(
                f"[V04] 有 {int(bad.size)} 处时间间隔不等于声明的"
                f"{axis.resolution.label}分辨率，例如 {first.timestamp:%Y-%m-%d %H:%M} "
                f"之后间隔 {actual_hours:g} 小时"
            ),
            count=int(bad.size),
            samples=samples,
        )
    ]


def _detect_unsorted(points: list[TimeSeriesPoint]) -> list[DataQualityIssue]:
    """V05 时间未排序（§5.1）。"""
    if len(points) < 2:
        return []
    stamps = _timestamps(points)
    inversions = int(np.sum(np.diff(stamps) < np.timedelta64(0, "s")))
    if inversions == 0:
        return []
    return [
        DataQualityIssue(
            level="INFO",
            category="continuity",
            message=f"[V05] 时间列未按升序排列（发现 {inversions} 处逆序），建议自动排序",
            count=inversions,
            samples=[],
        )
    ]


def _detect_cross_year(points: list[TimeSeriesPoint]) -> list[DataQualityIssue]:
    """V06 跨年（§5.1）。"""
    years = sorted({p.timestamp.year for p in points})
    if len(years) <= 1:
        return []
    return [
        DataQualityIssue(
            level="WARNING",
            category="continuity",
            message=(
                f"[V06] 数据跨越 {'–'.join(str(y) for y in years)} 共 {len(years)} 个年度，"
                f"将按年份分段处理"
            ),
            count=len(years),
            samples=[],
        )
    ]


def _detect_leap_mismatch(points: list[TimeSeriesPoint], axis: TimeAxis) -> list[DataQualityIssue]:
    """V07 平年/闰年点数不符（§5.1）。"""
    expected = len(axis.timestamps)
    actual = len(points)
    if actual == expected:
        return []
    other = 8760 if expected == 8784 else (8784 if expected == 8760 else None)
    if other is None or actual != other:
        return []
    leap_note = "2024 年为闰年，应为 8784 点" if axis.is_leap else "该年为平年，应为 8760 点"
    return [
        DataQualityIssue(
            level="INFO",
            category="completeness",
            message=f"[V07] {axis.year} 年点数与日历不符：{leap_note}，实得 {actual} 点",
            count=abs(expected - actual),
            samples=[],
        )
    ]


# --------------------------------------------------------------------------- #
# 数值校验
# --------------------------------------------------------------------------- #
def _detect_negative(points: list[TimeSeriesPoint], kind: str) -> list[DataQualityIssue]:
    """V08 负值（§5.2）。负荷/光伏/电价均不得为负。"""
    values = _values(points, kind)
    bad = np.flatnonzero(np.isfinite(values) & (values < 0.0))
    if bad.size == 0:
        return []
    label = {"load": "负荷", "pv": "光伏发电量", "price": "电价", "export_price": "上网电价"}[kind]
    unit = "kWh" if kind in ("load", "pv") else "元/kWh"
    first = int(bad[0])
    return [
        DataQualityIssue(
            level="ERROR",
            category="outlier",
            message=(
                f"[V08] 第 {first + 1} 个时间点{label}为 {values[first]:.4g} {unit}，"
                f"{label}不得为负（共 {int(bad.size)} 处）"
            ),
            count=int(bad.size),
            samples=_sample_times(points, bad),
        )
    ]


def _detect_nan(points: list[TimeSeriesPoint], kind: str) -> list[DataQualityIssue]:
    """V11 空值 / 非数值文本（§5.2）。导入器把空值与非数值统一记为 ``NaN``。"""
    values = _values(points, kind)
    bad = np.flatnonzero(~np.isfinite(values))
    if bad.size == 0:
        return []
    first = int(bad[0])
    return [
        DataQualityIssue(
            level="INFO",
            category="completeness",
            message=(
                f"[V11] 有 {int(bad.size)} 个时间点数值为空或非数值，"
                f"例如第 {first + 1} 个时间点，将按缺失处理"
            ),
            count=int(bad.size),
            samples=_sample_times(points, bad),
        )
    ]


def _detect_all_zero(points: list[TimeSeriesPoint], kind: str) -> list[DataQualityIssue]:
    """V10 全零列（§5.2）。"""
    values = _values(points, kind)
    finite = values[np.isfinite(values)]
    if finite.size == 0 or not np.all(finite == 0.0):
        return []
    label = {"load": "负荷", "pv": "光伏", "price": "电价", "export_price": "上网电价"}[kind]
    return [
        DataQualityIssue(
            level="WARNING",
            category="outlier",
            message=f"[V10] {label}列全部为 0，请确认是否漏填",
            count=int(finite.size),
            samples=[],
        )
    ]


def _detect_price_flat(points: list[TimeSeriesPoint], kind: str) -> list[DataQualityIssue]:
    """V13 电价全时段相同（§5.2）。"""
    if kind != "price":
        return []
    values = _values(points, kind)
    finite = values[np.isfinite(values)]
    if finite.size < 2 or float(np.ptp(finite)) > 1e-12:
        return []
    return [
        DataQualityIssue(
            level="WARNING",
            category="outlier",
            message=(
                f"[V13] 电价全时段相同（{finite[0]:.4f} 元/kWh），将无法体现分时价值"
            ),
            count=int(finite.size),
            samples=[],
        )
    ]


def detect_outliers(
    points: list[TimeSeriesPoint],
    axis: TimeAxis,
    kind: str,
    *,
    capacity_kwp: float | None = None,
) -> list[DataQualityIssue]:
    """V12 异常值（§5.2、§7 清单）与 V16/V17。

    实现下列规则（只告警、不拒载）：

    * ``R-LOAD-SPIKE``／``R-PV-SPIKE``：单点 > 中位数 × 10
    * ``R-LOAD-FLAT``：连续 ≥ 168 点完全相同
    * ``R-PV-NIGHT``：夜间时段光伏出力 > 0（§7）
    * ``R-PV-OVER``：出力 > 装机容量 × 1.05（需传 ``capacity_kwp``）
    * ``R-PV-CAP``：年等效小时落在 800–1600 h 之外（需传 ``capacity_kwp``）
    * ``R-PRICE-ZERO``：连续 ≥ 24 点电价为 0
    * ``R-PRICE-JUMP``：相邻小时电价变化 > 200%
    """
    issues: list[DataQualityIssue] = []
    values = _values(points, kind)
    finite = np.isfinite(values)
    if not finite.any():
        return issues

    if kind in ("load", "pv"):
        # 基准取**非零值**中位数：光伏夜间出力为 0 是正常的，
        # 若用全体中位数（≈0）会把正常出力误判成"尖峰"。
        positive = values[finite & (values > 0.0)]
        median = float(np.median(positive)) if positive.size else 0.0
        if median > 0.0:
            spikes = np.flatnonzero(finite & (values > median * 10.0))
            if spikes.size:
                code = "R-LOAD-SPIKE" if kind == "load" else "R-PV-SPIKE"
                first = int(spikes[0])
                issues.append(
                    DataQualityIssue(
                        level="WARNING",
                        category="outlier",
                        message=(
                            f"[{code}] 第 {first + 1} 个时间点数值 {values[first]:.4g} kWh，"
                            f"为非零中位数 {median:.4g} 的 {values[first] / median:.1f} 倍，"
                            f"疑为录入错误（共 {int(spikes.size)} 处）"
                        ),
                        count=int(spikes.size),
                        samples=_sample_times(points, spikes),
                    )
                )
        issues.extend(_detect_flat_run(points, values, kind))

    if kind == "pv":
        issues.extend(_detect_pv_night(points, values))
        if capacity_kwp and capacity_kwp > 0.0:
            limit = capacity_kwp * 1.05 * axis.delta_hours
            over = np.flatnonzero(finite & (values > limit))
            if over.size:
                first = int(over[0])
                issues.append(
                    DataQualityIssue(
                        level="WARNING",
                        category="outlier",
                        message=(
                            f"[R-PV-OVER] {points[first].timestamp:%Y-%m-%d %H:%M} 出力 "
                            f"{values[first]:.1f} kWh 超过装机容量 {capacity_kwp:.0f} kWp × 1.05"
                            f"（共 {int(over.size)} 处）"
                        ),
                        count=int(over.size),
                        samples=_sample_times(points, over),
                    )
                )
            annual = float(np.nansum(values))
            hours = annual / capacity_kwp
            if not (PV_HOURS_RANGE[0] <= hours <= PV_HOURS_RANGE[1]):
                direction = "低于" if hours < PV_HOURS_RANGE[0] else "高于"
                issues.append(
                    DataQualityIssue(
                        level="WARNING",
                        category="outlier",
                        message=(
                            f"[R-PV-CAP] 光伏年等效利用小时 {hours:.0f} h，"
                            f"{direction}常见区间 800–1600 h"
                        ),
                        count=1,
                        samples=[],
                    )
                )

    if kind == "price":
        issues.extend(_detect_price_zeros(values))
        jumps = _detect_price_jumps(points, values)
        issues.extend(jumps)

    return issues


def _detect_flat_run(
    points: list[TimeSeriesPoint], values: np.ndarray, kind: str
) -> list[DataQualityIssue]:
    """R-LOAD-FLAT／R-PV-FLAT：连续 ≥ 168 点完全相同。"""
    finite = np.isfinite(values)
    if finite.sum() < 168:
        return []
    same = np.zeros(values.size, dtype=bool)
    same[1:] = finite[1:] & finite[:-1] & (values[1:] == values[:-1])
    best_start = best_len = 0
    run_start = 0
    run_len = 1
    for i in range(1, same.size + 1):
        if i < same.size and same[i]:
            run_len += 1
        else:
            if run_len > best_len:
                best_start, best_len = run_start, run_len
            run_start, run_len = i, 1
    if best_len < 168:
        return []
    label = "负荷" if kind == "load" else "光伏出力" if kind == "pv" else "数值"
    code = "R-LOAD-FLAT" if kind == "load" else "R-PV-FLAT"
    return [
        DataQualityIssue(
            level="WARNING",
            category="outlier",
            message=(
                f"[{code}] 第 {best_start + 1}–{best_start + best_len} 个时间点{label}恒为 "
                f"{values[best_start]:.4g}（连续 {best_len} 点），疑为模拟值"
            ),
            count=best_len,
            samples=_sample_times(points, range(best_start, best_start + best_len)),
        )
    ]


def _detect_pv_night(points: list[TimeSeriesPoint], values: np.ndarray) -> list[DataQualityIssue]:
    """R-PV-NIGHT 光伏夜间发电（§7、§5.3 V16）。"""
    night = np.asarray([p.timestamp.hour in NIGHT_HOURS for p in points], dtype=bool)
    bad = np.flatnonzero(np.isfinite(values) & night & (values > 0.0))
    if bad.size == 0:
        return []
    first = int(bad[0])
    return [
        DataQualityIssue(
            level="WARNING",
            category="outlier",
            message=(
                f"[R-PV-NIGHT] 发现 {int(bad.size)} 个夜间时段光伏出力 > 0，"
                f"例如 {points[first].timestamp:%Y-%m-%d %H:%M} = {values[first]:.2f} kWh"
            ),
            count=int(bad.size),
            samples=_sample_times(points, bad),
        )
    ]


def _detect_price_zeros(values: np.ndarray) -> list[DataQualityIssue]:
    """R-PRICE-ZERO：连续 ≥ 24 点电价为 0。"""
    finite = np.isfinite(values)
    zeros = finite & (values == 0.0)
    if zeros.sum() < 24:
        return []
    best_len = run = 0
    for flag in zeros:
        run = run + 1 if flag else 0
        best_len = max(best_len, run)
    if best_len < 24:
        return []
    return [
        DataQualityIssue(
            level="WARNING",
            category="outlier",
            message=(
                f"[R-PRICE-ZERO] 出现连续 {best_len} 个时间点电价为 0，"
                f"疑为漏填（共 {int(zeros.sum())} 处）"
            ),
            count=int(zeros.sum()),
            samples=[],
        )
    ]


def _detect_price_jumps(points: list[TimeSeriesPoint], values: np.ndarray) -> list[DataQualityIssue]:
    """R-PRICE-JUMP：相邻小时电价变化 > 200%。"""
    if values.size < 2:
        return []
    prev, cur = values[:-1], values[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        relative = np.where(np.abs(prev) > 1e-12, np.abs(cur - prev) / np.abs(prev), 0.0)
    bad = np.flatnonzero(np.isfinite(relative) & (relative > 2.0))
    if bad.size == 0:
        return []
    first = int(bad[0])
    return [
        DataQualityIssue(
            level="WARNING",
            category="outlier",
            message=(
                f"[R-PRICE-JUMP] {points[first + 1].timestamp:%Y-%m-%d %H:%M} "
                f"电价由 {prev[first]:.4f} 跳至 {cur[first]:.4f}，请确认时段切换"
                f"（共 {int(bad.size)} 处）"
            ),
            count=int(bad.size),
            samples=_sample_times(points, bad + 1),
        )
    ]


# --------------------------------------------------------------------------- #
# 交叉校验
# --------------------------------------------------------------------------- #
def _detect_annual_deviation(
    points: list[TimeSeriesPoint], kind: str, annual_reference_kwh: float | None
) -> list[DataQualityIssue]:
    """V14 负荷年总量与 V1 ``annual_load_kwh`` 偏差 > 5%（§5.2）。"""
    if annual_reference_kwh is None or annual_reference_kwh <= 0.0:
        return []
    values = _values(points, kind)
    total = float(np.nansum(values))
    deviation = (total - annual_reference_kwh) / annual_reference_kwh
    if abs(deviation) <= 0.05:
        return []
    return [
        DataQualityIssue(
            level="WARNING",
            category="completeness",
            message=(
                f"[V14] 负荷合计 {total:,.0f} kWh 与项目年用电量 "
                f"{annual_reference_kwh:,.0f} kWh 偏差 {deviation:+.1%}"
            ),
            count=1,
            samples=[],
        )
    ]


def check_timeline_consistency(
    points: list[TimeSeriesPoint], other: list[TimeSeriesPoint], *, label: str = "光伏"
) -> list[DataQualityIssue]:
    """V15 负荷与光伏的时间轴不一致（§5.3，拒载）。"""
    left = {p.timestamp for p in points}
    right = {p.timestamp for p in other}
    if left == right:
        return []
    only_left = sorted(left - right)
    only_right = sorted(right - left)
    samples = [
        f"{ts:%Y-%m-%d %H:%M}" for ts in (only_left + only_right)[:MAX_SAMPLES]
    ]
    return [
        DataQualityIssue(
            level="ERROR",
            category="continuity",
            message=(
                f"[V15] 负荷与{label}的时间轴不一致："
                f"仅负荷有 {len(only_left)} 点、仅{label}有 {len(only_right)} 点，"
                f"请使用相同时间范围"
            ),
            count=len(only_left) + len(only_right),
            samples=samples,
        )
    ]


# --------------------------------------------------------------------------- #
# 组合校验（固定顺序）
# --------------------------------------------------------------------------- #
def validate_series(
    points: list[TimeSeriesPoint],
    axis: TimeAxis,
    *,
    kind: str = "load",
    capacity_kwp: float | None = None,
    annual_reference_kwh: float | None = None,
    other_series: list[TimeSeriesPoint] | None = None,
) -> list[DataQualityIssue]:
    """按 §5.4 的固定顺序执行全部校验，返回问题列表。

    :param points: 待校验的时间点（导入器产出；空值/非数值已记为 ``NaN``）
    :param axis: 时间轴（决定"应有点数"与间隔基准）
    :param kind: ``"load"`` / ``"pv"`` / ``"price"`` / ``"export_price"``
    :param capacity_kwp: 光伏装机容量，用于 R-PV-OVER 与 R-PV-CAP
    :param annual_reference_kwh: V1 的项目年用电量，用于 V14
    :param other_series: 另一条曲线（如负荷 vs 光伏），用于 V15 时间轴一致性
    """
    issues: list[DataQualityIssue] = []

    # --- V01 时间列不可用 ---
    if not points:
        issues.append(
            DataQualityIssue(
                level="ERROR",
                category="completeness",
                message="[V01] 时间列无法解析：请使用 YYYY-MM-DD HH:MM 格式",
                count=0,
                samples=[],
            )
        )
        return issues

    # --- 时间轴：V02 → V05 → V03 → V07 → V04 → V06 ---
    issues.extend(detect_duplicates(points))
    issues.extend(_detect_unsorted(points))
    missing_indices, missing_issues = detect_missing(points, axis)
    issues.extend(missing_issues)
    issues.extend(_detect_leap_mismatch(points, axis))
    issues.extend(_detect_interval_errors(points, axis))
    issues.extend(_detect_cross_year(points))

    # --- 数值：V08 → V11 → V09 → V10 → V13 → V12 ---
    issues.extend(_detect_negative(points, kind))
    issues.extend(_detect_nan(points, kind))
    values = _values(points, kind)
    issues.extend(detect_unit_issues(values, kind))
    issues.extend(_detect_all_zero(points, kind))
    issues.extend(_detect_price_flat(points, kind))
    issues.extend(detect_outliers(points, axis, kind, capacity_kwp=capacity_kwp))

    # --- 交叉：V14 → V15 ---
    issues.extend(_detect_annual_deviation(points, kind, annual_reference_kwh))
    if other_series is not None:
        issues.extend(check_timeline_consistency(points, other_series))

    del missing_indices  # 仅用于填充流程，此处无需返回
    logger.debug(
        "校验完成：kind=%s 点数=%d 问题数=%d（ERROR %d / WARNING %d / INFO %d）",
        kind,
        len(points),
        len(issues),
        sum(i.level == "ERROR" for i in issues),
        sum(i.level == "WARNING" for i in issues),
        sum(i.level == "INFO" for i in issues),
    )
    return issues


# --------------------------------------------------------------------------- #
# 缺失数据填充（V2 §53、§6）
# --------------------------------------------------------------------------- #
def _aligned_values(points: list[TimeSeriesPoint], axis: TimeAxis, kind: str) -> np.ndarray:
    field = VALUE_FIELD[kind]
    axis_index = {ts: i for i, ts in enumerate(axis.timestamps)}
    out = np.full(len(axis.timestamps), np.nan, dtype=float)
    for point in points:
        idx = axis_index.get(point.timestamp)
        if idx is not None:
            out[idx] = float(getattr(point, field, float("nan")))
    return out


def _linear_interpolate(values: np.ndarray) -> tuple[np.ndarray, int]:
    """线性插值（§6.2）：``x_t = x_a + (x_b − x_a) × (t − t_a) / (t_b − t_a)``。

    返回 ``(填充后数组, 最长缺口长度)``；首尾无法插值的部分保持 ``NaN``。
    """
    known = np.flatnonzero(np.isfinite(values))
    if known.size < 2:
        return values.copy(), 0
    filled = values.copy()
    gaps = np.diff(known)
    longest_gap = int(gaps.max() - 1) if gaps.size else 0
    idx = np.arange(values.size, dtype=float)
    filled = np.interp(idx, known.astype(float), values[known])
    # 插值范围外（首尾缺口）np.interp 会做水平外推，按规范应保持缺失
    filled[: known[0]] = np.nan
    filled[known[-1] + 1 :] = np.nan
    return filled, max(longest_gap, 0)


def _forward_fill(values: np.ndarray, lookback: int = FORWARD_FILL_LOOKBACK) -> np.ndarray:
    """前值填充（§6.2），最多回溯 ``lookback`` 步；超出范围保持缺失。"""
    filled = values.copy()
    last_valid = -10**9
    last_value = np.nan
    for i in range(values.size):
        if np.isfinite(values[i]):
            last_valid, last_value = i, values[i]
        elif i - last_valid <= lookback and np.isfinite(last_value):
            filled[i] = last_value
    return filled


def _typical_day_fill(
    values: np.ndarray, axis: TimeAxis, points: list[TimeSeriesPoint], kind: str
) -> np.ndarray:
    """典型日填充（§6.2）：``x_t = mean(同月、同 is_weekend、同 hour 的全部已知值)``。

    若某 (月, 日类型, 小时) 组合没有已知样本，则退化为按小时取均值；
    仍无样本时保持缺失。
    """
    months = np.asarray([ts.month for ts in axis.timestamps], dtype=np.int64)
    hours = np.asarray([ts.hour for ts in axis.timestamps], dtype=np.int64)
    weekend = np.asarray([ts.weekday() >= 5 for ts in axis.timestamps], dtype=bool)

    known = np.isfinite(values)
    filled = values.copy()
    hour_only_mean = np.full(24, np.nan, dtype=float)
    for h in range(24):
        mask = known & (hours == h)
        if mask.any():
            hour_only_mean[h] = float(np.mean(values[mask]))

    for m in range(1, 13):
        for is_weekend in (True, False):
            for h in range(24):
                target = (~known) & (months == m) & (weekend == is_weekend) & (hours == h)
                if not target.any():
                    continue
                sample = known & (months == m) & (weekend == is_weekend) & (hours == h)
                if sample.any():
                    filled[target] = float(np.mean(values[sample]))
                elif np.isfinite(hour_only_mean[h]):
                    filled[target] = hour_only_mean[h]
    return filled


def fill_missing(
    points: list[TimeSeriesPoint],
    axis: TimeAxis,
    policy: MissingDataPolicy,
    *,
    kind: str = "load",
) -> tuple[list[TimeSeriesPoint], list[DataQualityIssue]]:
    """按策略处理缺失（V2 §53、§6.2）。

    * ``REJECT`` → **抛错**，不返回数据（默认行为，§6.1 禁止静默填充）
    * ``LINEAR_INTERPOLATION`` / ``FORWARD_FILL`` / ``TYPICAL_DAY_FILL`` →
      返回填充后的时间点，并附带**留痕** issue（§6.3）

    :raises ValidationError: ``policy=REJECT`` 且确有缺失时；或填充后仍有缺口时
    """
    missing_indices, missing_issues = detect_missing(points, axis)
    missing_count = len(missing_indices)

    values = _aligned_values(points, axis, kind)
    nan_count = int(np.sum(~np.isfinite(values)))

    if policy is MissingDataPolicy.REJECT:
        if missing_count or nan_count:
            expected = len(axis.timestamps)
            actual = expected - missing_count
            completeness = actual / expected if expected else 0.0
            raise ValidationError(
                f"发现 {max(missing_count, nan_count)} 个时间点缺失"
                f"（应 {expected} 点，实得 {actual} 点，完整性 {completeness:.2%}）。"
                f"当前策略为「拒绝计算」，请改用线性插值/前值填充/典型日填充，"
                f"或补齐原始数据后重试。",
                field=f"data.missing.{kind}",
            )
        return list(points), []

    if not missing_count and not nan_count:
        return list(points), []

    issues: list[DataQualityIssue] = list(missing_issues)
    if policy is MissingDataPolicy.LINEAR_INTERPOLATION:
        filled, longest = _linear_interpolate(values)
        note = (
            f"[G1] 发现 {max(missing_count, nan_count)} 个时间点缺失，"
            f"已按线性插值填充（最长缺口 {longest} 点）"
        )
        if longest > LINEAR_INTERPOLATION_MAX_GAP:
            issues.append(
                DataQualityIssue(
                    level="WARNING",
                    category="completeness",
                    message=(
                        f"[G1] 最长缺口 {longest} 点，超过线性插值推荐的 3 点；"
                        f"长缺口建议改用典型日填充（§6.2）"
                    ),
                    count=longest,
                    samples=[],
                )
            )
    elif policy is MissingDataPolicy.FORWARD_FILL:
        filled = _forward_fill(values)
        note = f"[G1] 发现 {max(missing_count, nan_count)} 个时间点缺失，已按前值填充"
    else:  # TYPICAL_DAY_FILL
        filled = _typical_day_fill(values, axis, points, kind)
        note = (
            f"[G1] 发现 {max(missing_count, nan_count)} 个时间点缺失，"
            f"已按同月同类型日的平均曲线填充；该部分为**合成数据**，"
            f"来源可信度将降级为「假设值」（§6.3 G2）"
        )

    still_missing = int(np.sum(~np.isfinite(filled)))
    issues.append(
        DataQualityIssue(
            level="INFO",
            category="completeness",
            message=f"{note}；填充后仍有 {still_missing} 点无法补齐",
            count=max(missing_count, nan_count),
            samples=[
                axis.timestamps[i].strftime("%Y-%m-%d %H:%M")
                for i in missing_indices[:MAX_SAMPLES]
            ],
        )
    )

    field = VALUE_FIELD[kind]
    filled_points: list[TimeSeriesPoint] = []
    axis_index = {ts: i for i, ts in enumerate(axis.timestamps)}
    by_stamp = {p.timestamp: p for p in points}
    for i, stamp in enumerate(axis.timestamps):
        value = filled[i]
        existing = by_stamp.get(stamp)
        if existing is None:
            filled_points.append(TimeSeriesPoint(timestamp=stamp, **{field: float(value)}))
        else:
            existing_set = existing.model_copy(deep=True)
            setattr(existing_set, field, float(value))
            filled_points.append(existing_set)
    del axis_index
    return filled_points, issues
