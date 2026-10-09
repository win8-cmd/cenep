"""V2 数据质量评分（V2 §55、§56；规则见 ``DATA_IMPORT_SPEC.md`` 第 8 节）。

计分规则（**文档即契约**，可被测试逐条断言）
------------------------------------------

.. code-block:: text

    DataQualityScore = 完整性 + 连续性 + 异常值 + 来源可信度        满分 100
                        (0–40)   (0–25)   (0–20)   (0–15)

    完整性 = 40 × 有效点数 / 应有点数
             （按**原始**缺失率计分：填充不恢复，见 §6.3 G3）
    连续性 = 25 × (1 − 断点数 / (应有点数 − 1))
             （断点 = 相邻间隔 ≠ 声明分辨率的次数）
    异常值 = 20 × max(0, 1 − 异常点数 / 应有点数 / 0.05)
             （异常率 5% 即得 0 分）
    来源   = SOURCE_CREDIBILITY[source_type]

等级阈值（§8.3）：``>=90`` 优秀、``>=75`` 良好、``>=60`` 一般、``<60`` 较差。

``异常点数`` 的取法
------------------
只统计 ``category == "outlier"`` 的问题（``DataQualityIssue.count`` 之和），
并**截断到应有点数**，避免整列类告警（如全零列 V10、电价恒定 V13）把该项压成负数。
``completeness`` 类（缺失、空值）、``continuity`` 类（重复、间隔、跨年）、
``unit`` 类问题不计入异常值维度，避免同一问题被重复扣分。
"""

from __future__ import annotations

import logging

import numpy as np

from ..calculation.bill_calculator import reconcile_bill
from ..calculation.timeseries_engine import TimeAxis
from ..domain.bill_models import BillQualityScore, BillTolerance, ElectricityBill
from ..domain.enums import BillQualityStatus, SourceType, TariffStructure
from ..domain.timeseries import TimeSeriesPoint
from ..domain.timeseries_results import DataQualityIssue, DataQualityScore
from .validator import VALUE_FIELD, validate_series

logger = logging.getLogger(__name__)

__all__ = [
    "BILL_CORE_FIELDS",
    "BILL_WEIGHT_COMPLETENESS",
    "BILL_WEIGHT_CONSISTENCY",
    "BILL_WEIGHT_SOURCE",
    "OUTLIER_ZERO_RATE",
    "SOURCE_CREDIBILITY",
    "WEIGHT_COMPLETENESS",
    "WEIGHT_CONTINUITY",
    "WEIGHT_OUTLIER",
    "WEIGHT_SOURCE",
    "score_bill_quality",
    "score_quality",
    "source_credibility_of",
]

#: 四个维度的权重（§8.1）
WEIGHT_COMPLETENESS = 40.0
WEIGHT_CONTINUITY = 25.0
WEIGHT_OUTLIER = 20.0
WEIGHT_SOURCE = 15.0

#: 异常值维度归零的异常率阈值（§8.1③：异常率 5% 即得 0 分）
OUTLIER_ZERO_RATE = 0.05

#: 来源可信度分值表（§8.2）
SOURCE_CREDIBILITY: dict[SourceType, float] = {
    SourceType.CONTRACT: 15.0,
    SourceType.POLICY: 15.0,
    SourceType.HISTORICAL: 13.0,
    SourceType.USER_INPUT: 11.0,
    SourceType.CALCULATED: 10.0,
    SourceType.ASSUMPTION: 7.0,
    SourceType.EXPERIENCE: 5.0,
    SourceType.SYSTEM_DEFAULT: 0.0,
}

#: 等级阈值（§8.3）
LEVEL_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (90.0, "优秀"),
    (75.0, "良好"),
    (60.0, "一般"),
    (0.0, "较差"),
)


def source_credibility_of(source_type: SourceType) -> float:
    """按 §8.2 查来源可信度分值；未知类型按 0 分处理。"""
    return float(SOURCE_CREDIBILITY.get(source_type, 0.0))


def level_of(score: float) -> str:
    """按 §8.3 给出等级标签。"""
    for threshold, label in LEVEL_THRESHOLDS:
        if score >= threshold:
            return label
    return "较差"


def _valid_and_expected(
    points: list[TimeSeriesPoint], axis: TimeAxis, kind: str
) -> tuple[int, int]:
    """返回 ``(有效点数, 应有点数)``。

    有效 = 落在时间轴上且数值有限的点；重复时间戳只按一次计算（重复已由 V02 单独报告）。
    """
    field = VALUE_FIELD[kind]
    axis_index = {ts: i for i, ts in enumerate(axis.timestamps)}
    seen = np.zeros(len(axis.timestamps), dtype=bool)
    for point in points:
        idx = axis_index.get(point.timestamp)
        if idx is None or seen[idx]:
            continue
        value = getattr(point, field, float("nan"))
        if isinstance(value, (int, float)) and np.isfinite(value):
            seen[idx] = True
    return int(seen.sum()), len(axis.timestamps)


def _gap_count(points: list[TimeSeriesPoint], axis: TimeAxis) -> int:
    """断点数 = 相邻间隔 ≠ 声明分辨率的次数（§8.1②）。"""
    if len(points) < 2:
        return 0
    stamps = np.asarray([p.timestamp for p in points], dtype="datetime64[ns]")
    deltas = np.diff(stamps).astype("timedelta64[s]").astype(np.int64)
    expected_seconds = int(axis.delta_hours * 3600.0) or 3600
    return int(np.sum(deltas != expected_seconds))


def _outlier_count(issues: list[DataQualityIssue], expected_points: int) -> int:
    """异常点数 = ``category == "outlier"`` 的问题条数之和，截断到应有点数。"""
    total = sum(int(issue.count) for issue in issues if issue.category == "outlier")
    return min(total, max(expected_points, 0))


def score_quality(
    points: list[TimeSeriesPoint],
    axis: TimeAxis,
    *,
    issues: list[DataQualityIssue] | None = None,
    source_type: SourceType = SourceType.USER_INPUT,
    kind: str = "load",
    capacity_kwp: float | None = None,
    annual_reference_kwh: float | None = None,
) -> DataQualityScore:
    """计算数据质量评分（V2 §56）。

    :param points: 待评分的时间点（**原始**数据；已填充的数据请仍传原始点数，
        因为完整性按原始缺失率计分，§6.3 G3）
    :param axis: 时间轴
    :param issues: 已有的校验问题；为 ``None`` 时内部调用
        :func:`cenep.data.validator.validate_series` 现算
    :param source_type: 数据来源类型（决定来源可信度分值，§8.2）
    :param kind: ``"load"`` / ``"pv"`` / ``"price"`` / ``"export_price"``
    :param capacity_kwp: 光伏容量，透传给校验器（R-PV-OVER / R-PV-CAP）
    :param annual_reference_kwh: V1 项目年用电量，透传给校验器（V14）
    """
    if issues is None:
        issues = validate_series(
            points,
            axis,
            kind=kind,
            capacity_kwp=capacity_kwp,
            annual_reference_kwh=annual_reference_kwh,
        )

    valid_points, expected_points = _valid_and_expected(points, axis, kind)
    gaps = _gap_count(points, axis)
    outliers = _outlier_count(issues, expected_points)

    if expected_points > 0:
        completeness = WEIGHT_COMPLETENESS * valid_points / expected_points
        denominator = max(expected_points - 1, 1)
        continuity = WEIGHT_CONTINUITY * max(0.0, 1.0 - gaps / denominator)
        outlier = WEIGHT_OUTLIER * max(
            0.0, 1.0 - (outliers / expected_points) / OUTLIER_ZERO_RATE
        )
    else:
        completeness = continuity = outlier = 0.0

    credibility = source_credibility_of(source_type)
    total = completeness + continuity + outlier + credibility

    score = DataQualityScore(
        score=round(min(total, 100.0), 4),
        completeness=round(completeness, 4),
        continuity=round(continuity, 4),
        outlier=round(outlier, 4),
        source_credibility=round(credibility, 4),
        issues=list(issues),
    )
    logger.debug(
        "质量评分：来源=%s 有效=%d/%d 断点=%d 异常=%d → 完整性 %.2f 连续性 %.2f "
        "异常值 %.2f 来源 %.2f 总分 %.2f（%s）",
        source_type.value,
        valid_points,
        expected_points,
        gaps,
        outliers,
        score.completeness,
        score.continuity,
        score.outlier,
        score.source_credibility,
        score.score,
        level_of(score.score),
    )
    return score


# --------------------------------------------------------------------------- #
# V2.1 §5.5 账单数据质量评分（增量：不改动上面的时序评分逻辑）
# --------------------------------------------------------------------------- #
#: 账单评分三个维度的满分（V2.1 §5.5；沿用 V2 §56 的"文档即契约"做法）
BILL_WEIGHT_COMPLETENESS = 50.0   # 关键字段提供了多少
BILL_WEIGHT_CONSISTENCY = 35.0    # ΔE / ΔC / 电度二层核对通过了多少
BILL_WEIGHT_SOURCE = 15.0         # 来源可信度（复用 §8.2 分值表）

#: 账单"关键字段"：缺失会让后续分析无法进行（V2.1 §2.1、§3.1）
BILL_CORE_FIELDS: tuple[str, ...] = (
    "energy_total_kwh",
    "bill_total_yuan",
    "energy_charge_yuan",
    "voltage_level",
    "tariff_structure",
)


def score_bill_quality(
    bill: ElectricityBill, *, tolerance: BillTolerance | None = None
) -> BillQualityScore:
    """账单数据质量评分（V2.1 §5.5）。

    计分（满分 100）::

        完整性 = 50 × 已提供关键字段数 / 关键字段总数
        一致性 = 35 × 通过的核对项 / 可判断的核对项
                 （可判断项为 0 时按满分 35 计，避免"没数据"被当成"数据很差"）
        来源   = 15 × 来源可信度 / 15（复用 §8.2 的分值表）

    ``tariff_structure`` 只有非 ``unknown`` 才算提供；
    两部制账单额外要求"合同容量或计费需量至少有一个"。

    ``status`` **不由分数决定**，而由问题级别决定（任一 ERROR → 无效），
    防止用高分掩盖"分项合计不一致""存在负值"这类硬问题。
    """
    tol = tolerance or BillTolerance()
    outcome = reconcile_bill(bill, tolerance=tol)

    # ---- 完整性 ----
    total_fields = len(BILL_CORE_FIELDS)
    provided = 0
    for name in BILL_CORE_FIELDS:
        value = getattr(bill, name, None)
        if name == "tariff_structure":
            provided += int(value is not None and value is not TariffStructure.UNKNOWN)
        else:
            provided += int(value is not None and value != "")
    if bill.tariff_structure is TariffStructure.TWO_PART:
        total_fields += 1
        provided += int(
            bill.contract_capacity_kva is not None or bill.billing_demand_kw is not None
        )
    completeness = BILL_WEIGHT_COMPLETENESS * provided / total_fields if total_fields else 0.0

    # ---- 一致性 ----
    checks = [
        outcome.energy_consistent,
        outcome.amount_consistent,
        outcome.energy_sub_consistent,
    ]
    judgeable = [c for c in checks if c is not None]
    if judgeable:
        consistency = BILL_WEIGHT_CONSISTENCY * sum(1 for c in judgeable if c) / len(judgeable)
    else:
        consistency = BILL_WEIGHT_CONSISTENCY  # 无可判断项：不奖不罚
    completeness = min(completeness, BILL_WEIGHT_COMPLETENESS)

    # ---- 来源 ----
    # §8.2 的分值表本身就是 0–15 分制，直接作为来源维度得分
    source_score = source_credibility_of(bill.source_type.parameter_source)

    score = BillQualityScore(
        score=round(min(completeness + consistency + source_score, 100.0), 4),
        completeness=round(completeness, 4),
        consistency=round(consistency, 4),
        source_credibility=round(source_score, 4),
        status=outcome.quality_status,
        issues=list(outcome.issues),
    )
    logger.debug(
        "账单质量评分：%s 来源=%s → 完整性 %.2f 一致性 %.2f 来源 %.2f 总分 %.2f（%s）",
        bill.bill_id,
        bill.source_type.value,
        score.completeness,
        score.consistency,
        score.source_credibility,
        score.score,
        score.status.label,
    )
    return score
