"""月账单估算负荷引擎：可编辑典型负荷模板 + 月电量严格回归（V2.2 §3.2、§6.5、§9.2）。

本模块是"由月电量生成估算逐时负荷"的**唯一公式来源**（V1 §104、V2 §61）。
它只做纯计算：模板权重的解析、按班次/周末/检修日的形状调整、按月电量归一化、
间隔展开与严格回归自检。取数、持久化、界面与报告分别在
``application/load_profile_service.py``、``ui/`` 与 ``reports/``。

规格书 §3.2 的公式（逐字对应）
==============================

.. code-block:: text

    P_i = E_month × w_i ÷ Σ_{i∈month} w_i × 60 ÷ Δt_min

其中 ``w_i`` 是**非负权重**（无量纲），``Δt_min`` 是间隔分钟数。
本模块内部统一先算**间隔电量**（CENEP 的唯一存储口径，§2.2）：

.. code-block:: text

    E_i = E_month × w_i ÷ Σ_{i∈month} w_i        （kWh/间隔）
    P_i = E_i ÷ Δt_h = E_i × 60 ÷ Δt_min          （kW，两个式子严格等价）

**绝不能把归一化权重直接当 kW**（§3.2 末句）：权重先归一化、再乘月电量。
本模块返回的曲线始终是**电量口径**（``value_kind=INTERVAL_ENERGY_KWH``），
功率只是由 :attr:`HighFrequencyLoadDataset.values_kw` 派生的视图。

严格回归（§6.5、§9.2）
======================

「每个月的估算间隔电量必须严格回归到输入月电量；数值误差不超过 ``1e-6`` kWh」。
本模块在按月归一化后**再做一次精确校正**（把浮点残差整体摊回该月权重最大的间隔），
并对每个月独立记录 :class:`MonthlyRegressionRow`；只要有一个月的
``|Σ E_i − E_month| > 容差``，:attr:`LoadEstimateResult.all_months_regressed` 即为 False，
报告必须如实披露（不得声称"严格回归"）。

来源标签（§0.2 红线）
====================

本模块生成的曲线 ``source_type ∈ {MONTHLY_BILL_ESTIMATE, SYNTHETIC_TEMPLATE}``、
``estimated=True``；:class:`LoadEstimateResult` 在模型层强制 ``is_estimate=True``。
因此"把月账单估算负荷伪装成实测 15/30/60 分钟曲线"在本模块**无法构造**。

子小时展开是**假设**（§6.4）
============================

模板权重是 24 点（每小时一个）。当目标间隔为 15/30 分钟时，小时内的子间隔
**均分**该小时权重（等价于"小时内功率恒定"）。这是规格书 §6.4 明确允许的
"可配置假设"，本模块把它写进 :attr:`LoadEstimateResult.assumptions`，
界面与报告必须如实披露。

性能（§8.3）
-----------
全程 NumPy 向量化；35040 点的估算（含逐月回归校验与 35040 个时间点对象的构造）
实测在 1 秒量级。
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

import numpy as np

from ..domain.enums import (
    LoadDataSourceType,
    LoadEstimateSource,
    LoadQualityStatus,
    LoadValueKind,
    Resolution,
)
from ..domain.load_data import TIMEZONE_DEFAULT, HighFrequencyLoadDataset, make_dataset_id
from ..domain.load_estimate import (
    DAYTIME_HOURS,
    HOURS_PER_DAY,
    REGRESSION_TOLERANCE_KWH,
    LoadEstimateParams,
    LoadEstimateResult,
    MonthlyLoadEnergy,
    MonthlyRegressionRow,
    TypicalLoadTemplate,
    builtin_template,
    template_ids,
)
from ..domain.timeseries import TimeSeriesPoint
from .errors import ValidationError
from .timeseries_engine import build_time_axis

logger = logging.getLogger(__name__)

__all__ = [
    "DAY_TYPE_HOLIDAY",
    "DAY_TYPE_MAINTENANCE",
    "DAY_TYPE_WEEKEND",
    "DAY_TYPE_WORKDAY",
    "estimate_from_monthly_energy",
    "expand_hourly_weights",
    "monthly_energies_from_annual",
    "regress_month_to_energy",
    "resolve_day_type_weights",
]

#: 日类型标签（估算引擎内部使用；与 ``cenep.domain.enums.DayType`` 取值保持一致的工作日/周末，
#: 并额外区分"节假日"与"停产/检修日"两类在 §6.5 中要求单独配置的日子）
DAY_TYPE_WORKDAY = "WORKDAY"
DAY_TYPE_WEEKEND = "WEEKEND"
DAY_TYPE_HOLIDAY = "HOLIDAY"
DAY_TYPE_MAINTENANCE = "MAINTENANCE"

#: 权重合计的下限（低于该值视为"该日类型全是 0 权重"，无法归一化）
_WEIGHT_EPS = 1e-12


# --------------------------------------------------------------------------- #
# 权重解析
# --------------------------------------------------------------------------- #
def expand_hourly_weights(hourly_weights: list[float], interval_minutes: int) -> np.ndarray:
    """把 24 点小时权重展开成"每间隔一个权重"的数组（§6.4 的均分假设）。

    ``interval_minutes=60`` 时逐位返回；``30`` 时每小时复制 2 份；``15`` 时复制 4 份。
    这等价于"小时内功率恒定"，属**假设**，调用方必须把它写进结果与报告。

    :param hourly_weights: 24 点非负权重
    :param interval_minutes: 15 / 30 / 60
    :raises ValidationError: 点数不为 24、出现负值（中文报错）
    """
    weights = np.asarray(hourly_weights, dtype=float)
    if weights.size != HOURS_PER_DAY:
        raise ValidationError(
            f"典型日权重必须是 {HOURS_PER_DAY} 点（每小时一个），实际 {weights.size} 点；"
            f"请检查模板或用户覆盖的权重（V2.2 §6.5）",
            field="load_estimate.weights",
        )
    if np.any(weights < 0.0):
        raise ValidationError(
            f"典型日权重出现负值（最小 {float(weights.min()):.6f}）；"
            f"§3.2 要求 w_i 为非负权重（V2.2 §3.2）",
            field="load_estimate.weights",
        )
    if interval_minutes not in (15, 30, 60):
        raise ValidationError(
            f"估算间隔只支持 15 / 30 / 60 分钟，实际 {interval_minutes} 分钟（V2.2 §6.1）",
            field="load_estimate.interval",
        )
    repeat = int(round(60 / interval_minutes))
    return np.repeat(weights, repeat)


def resolve_day_type_weights(
    template: TypicalLoadTemplate,
    params: LoadEstimateParams,
) -> dict[str, np.ndarray]:
    """按日类型解析**小时权重**（模板默认值 + 用户覆盖 + 班次裁剪），返回 4 组 24 点权重。

    规则（全部写入 ``assumptions``，避免"未经说明的默认值"）：

    ================  ==================================================================
    权重来源          用户覆盖（``params.*_weights``）> 模板自带 > 回退到工作日权重
    班次裁剪          ``params.offshift_run_ratio`` 为 ``None`` 时**不裁剪**（保留模板形状）；
                      给出数值时，班次外小时权重 = ``offshift_run_ratio × 班内最大权重``
    日水平            工作日的"日水平因子"=1；周末/节假日/检修日分别乘
                      ``weekend_run_ratio`` / ``holiday_run_ratio`` / ``maintenance_run_ratio``
                      （"日水平因子"是**整体缩放**，只改水平不改形状）
    ================  ==================================================================

    :raises ValidationError: 模板与覆盖都为空、或班次裁剪后班内权重全为 0（中文报错）
    """
    workday = params.workday_weights or template.workday_weights
    if not workday:
        raise ValidationError(
            f"典型负荷模板「{template.name or template.template_id}」未提供工作日权重，"
            f"且用户也未覆盖；无法生成估算曲线（V2.2 §6.5）",
            field="load_estimate.weights",
        )
    workday = list(workday)
    weekend = list(params.weekend_weights or template.weekend_weights or workday)
    holiday = list(params.holiday_weights or template.holiday_weights or weekend)

    if params.offshift_run_ratio is not None:
        start, end = int(params.shift_start_hour), int(params.shift_end_hour)
        for label, weights in (("工作日", workday), ("休息日", weekend), ("节假日", holiday)):
            arr = np.asarray(weights, dtype=float).copy()
            inside = np.zeros(HOURS_PER_DAY, dtype=bool)
            inside[start:end] = True
            if not inside.any():
                raise ValidationError(
                    f"班次区间 [{start}:00, {end}:00) 未覆盖任何小时，无法裁剪{label}权重"
                    f"（V2.2 §6.5）",
                    field="load_estimate.shift",
                )
            peak = float(arr[inside].max())
            if peak <= 0.0:
                raise ValidationError(
                    f"{label}权重在班次区间 [{start}:00, {end}:00) 内全为 0，"
                    f"按非班次比例裁剪后没有可归一化的形状；请修正权重或班次时间（V2.2 §6.5）",
                    field="load_estimate.shift",
                )
            arr[~inside] = float(params.offshift_run_ratio) * peak
            weights = arr.tolist()
            if label == "工作日":
                workday = weights
            elif label == "休息日":
                weekend = weights
            else:
                holiday = weights

    levels = {
        DAY_TYPE_WORKDAY: 1.0,
        DAY_TYPE_WEEKEND: float(params.weekend_run_ratio),
        DAY_TYPE_HOLIDAY: float(params.holiday_run_ratio),
        DAY_TYPE_MAINTENANCE: float(params.maintenance_run_ratio),
    }
    result: dict[str, np.ndarray] = {}
    for day_type, weights in (
        (DAY_TYPE_WORKDAY, workday),
        (DAY_TYPE_WEEKEND, weekend),
        (DAY_TYPE_HOLIDAY, holiday),
        # 停产/检修日：形状沿用工作日形状，水平由 maintenance_run_ratio 决定（§6.5）
        (DAY_TYPE_MAINTENANCE, workday),
    ):
        arr = np.asarray(weights, dtype=float)
        if arr.size != HOURS_PER_DAY:
            raise ValidationError(
                f"{day_type} 的权重必须是 {HOURS_PER_DAY} 点，实际 {arr.size} 点（V2.2 §6.5）",
                field="load_estimate.weights",
            )
        if float(arr.sum()) <= _WEIGHT_EPS and levels[day_type] > 0.0:
            raise ValidationError(
                f"{day_type} 的权重合计为 0，无法生成该日类型的负荷形状（V2.2 §6.5）",
                field="load_estimate.weights",
            )
        result[day_type] = arr * levels[day_type]
    return result


# --------------------------------------------------------------------------- #
# 日类型判定
# --------------------------------------------------------------------------- #
def _day_type_array(
    axis,
    params: LoadEstimateParams,
) -> np.ndarray:
    """逐点日类型（工作日/休息日/节假日/停产检修日），向量化判定。

    优先级（写入 assumptions 披露）：**停产/检修日 > 节假日 > 周末 > 工作日**。
    一周内的生产日取"周一为一周第一天"的前 ``workdays_per_week`` 天
    （5 → 周一~周五；6 → 周一~周六；7 → 每天）。
    """
    weekday = np.asarray(axis.weekday, dtype=np.int64)  # 0=周一 … 6=周日
    working_days = int(params.workdays_per_week)
    is_work = weekday < working_days

    day_type = np.where(is_work, DAY_TYPE_WORKDAY, DAY_TYPE_WEEKEND).astype(object)

    def _mask(days: list[date]) -> np.ndarray:
        if not days:
            return np.zeros(axis.point_count, dtype=bool)
        targets = np.array([np.datetime64(d.isoformat(), "D") for d in days], dtype="datetime64[D]")
        stamps = np.array([np.datetime64(t.date().isoformat(), "D") for t in axis.timestamps],
                          dtype="datetime64[D]")
        return np.isin(stamps, targets)

    if params.holidays:
        day_type[_mask(list(params.holidays))] = DAY_TYPE_HOLIDAY
    if params.shutdown_days:
        # 检修/停产日优先级最高（§6.5：停产/检修日单独配置）
        day_type[_mask(list(params.shutdown_days))] = DAY_TYPE_MAINTENANCE
    return day_type


# --------------------------------------------------------------------------- #
# 月电量来源
# --------------------------------------------------------------------------- #
def monthly_energies_from_annual(
    params: LoadEstimateParams,
    *,
    missing_months: list[int],
) -> list[MonthlyLoadEnergy]:
    """为缺失月份生成月电量：按用户月度比例拆分，或**均匀分摊**（明确默认假设，§6.5）。

    :raises ValidationError: 既没有月电量、也没有年电量；或月度比例合计为 0（中文报错）
    """
    if params.annual_energy_kwh <= 0.0:
        raise ValidationError(
            f"缺少 {missing_months} 月的用电量，且未提供年用电量，无法估算这些月份；"
            f"请补充月电量或填写年用电量（V2.2 §6.5）",
            field="load_estimate.monthly_energy",
        )
    ratios = list(params.monthly_split_ratios)
    if ratios:
        total = float(sum(ratios))
        shares = [value / total for value in ratios]
        source = LoadEstimateSource.ANNUAL_SPLIT
    else:
        shares = [1.0 / 12.0] * 12
        source = LoadEstimateSource.UNIFORM_DEFAULT
    return [
        MonthlyLoadEnergy(
            year=params.year,
            month=month,
            energy_kwh=params.annual_energy_kwh * shares[month - 1],
            source=source,
            note=(
                "年电量 × 用户月度比例"
                if ratios
                else "年电量均匀分摊（未提供月度比例，属明确的默认估算假设）"
            ),
        )
        for month in missing_months
        if params.annual_energy_kwh * shares[month - 1] > 0.0
    ]


# --------------------------------------------------------------------------- #
# 单月回归（§3.2、§6.5）
# --------------------------------------------------------------------------- #
def regress_month_to_energy(
    weights_kwh: np.ndarray,
    month_energy_kwh: float,
    *,
    tolerance_kwh: float = REGRESSION_TOLERANCE_KWH,
) -> tuple[np.ndarray, float]:
    """把某月的逐间隔权重严格回归到输入月电量，返回 ``(逐间隔电量 kWh, 残差 kWh)``。

    两步（§3.2 + §6.5）：

    1. 归一化：``E_i = E_month × w_i ÷ Σ w_i``；
    2. 精确校正：把浮点残差整体加到权重最大的那个间隔上，使 ``Σ E_i`` 与 ``E_month``
       的差降到浮点可表示极限（远小于 ``1e-6`` kWh）。

    :raises ValidationError: 权重合计为 0、出现负权重、月电量非正（中文报错）
    """
    weights = np.asarray(weights_kwh, dtype=float)
    if weights.ndim != 1 or weights.size == 0:
        raise ValidationError(
            "估算权重必须是非空一维序列", field="load_estimate.weights"
        )
    if np.any(weights < 0.0):
        raise ValidationError(
            f"估算权重出现负值（最小 {float(weights.min()):.6f}）；w_i 必须非负（V2.2 §3.2）",
            field="load_estimate.weights",
        )
    total_weight = float(weights.sum())
    if total_weight <= _WEIGHT_EPS:
        raise ValidationError(
            "该月全部间隔的权重合计为 0（例如整月都是停产日且停产运行比例为 0），"
            "无法把月电量分配到这一个月；请调整停产日或比例参数（V2.2 §6.5）",
            field="load_estimate.weights",
        )
    energy = float(month_energy_kwh)
    if not math.isfinite(energy) or energy <= 0.0:
        raise ValidationError(
            f"月用电量必须为正数，实际给出 {month_energy_kwh!r} kWh（V2.2 §3.2）",
            field="load_estimate.monthly_energy",
        )

    values = weights * (energy / total_weight)
    residual = energy - float(values.sum())
    if residual != 0.0:
        # 精确校正：整体加到权重最大的间隔，保持形状（只挪动浮点残差量级）
        target = int(np.argmax(weights))
        values[target] += residual
    residual = energy - float(values.sum())
    if abs(residual) > tolerance_kwh:  # pragma: no cover - 结构自检
        raise ValidationError(
            f"月电量回归失败：输入 {energy:.6f} kWh、估算合计 {float(values.sum()):.6f} kWh，"
            f"残差 {residual:.3e} kWh 超过容差 {tolerance_kwh:g} kWh（V2.2 §6.5）",
            field="load_estimate.regression",
        )
    return values, residual


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def estimate_from_monthly_energy(
    monthly: list[MonthlyLoadEnergy],
    params: LoadEstimateParams,
    *,
    template: TypicalLoadTemplate | None = None,
    project_id: str = "",
    tolerance_kwh: float = REGRESSION_TOLERANCE_KWH,
) -> LoadEstimateResult:
    """由"可编辑典型负荷模板 + 月电量"生成**明确标记为估算**的逐时负荷曲线（§3.2、§6.5）。

    :param monthly: 各月用电量（kWh）；缺失月份用 ``params.annual_energy_kwh`` 补齐
    :param params: 运行参数（模板 ID、周末/节假日/检修比例、班次、白天夜间比例、间隔）
    :param template: 模板；``None`` 时按 ``params.template_id`` 取内置模板副本
    :param project_id: 归属项目标识（写入数据集的 ``project_id``）
    :param tolerance_kwh: 月电量回归容差（§6.5：1e-6 kWh 量级）
    :raises ValidationError: 参数不自洽、权重不可归一化、月电量缺失无法补齐（全部中文报错）
    """
    resolved_template = template or _template_or_error(params)
    if not resolved_template.name:
        resolved_template = resolved_template.model_copy(
            update={"name": resolved_template.template_id}
        )

    day_weights = resolve_day_type_weights(resolved_template, params)
    interval_weights = {
        day_type: expand_hourly_weights(weights.tolist(), params.interval_minutes)
        for day_type, weights in day_weights.items()
    }

    resolution = Resolution.from_interval_minutes(params.interval_minutes)
    if resolution is None:  # pragma: no cover - 参数校验已拦截
        raise ValidationError(
            f"间隔 {params.interval_minutes} 分钟没有对应的内部分辨率（V2.2 §6.1）",
            field="load_estimate.interval",
        )
    axis = build_time_axis(params.year, resolution, holidays=tuple(params.holidays))
    day_type = _day_type_array(axis, params)

    # 逐间隔权重：先按日类型取形状，再按"小时列"索引（向量化，无逐点循环）
    months = np.asarray(axis.month, dtype=np.int64)
    hours = np.asarray(axis.hour, dtype=np.int64)
    repeat = params.intervals_per_hour
    sub_index = np.tile(np.arange(repeat, dtype=np.int64), axis.point_count // repeat + 1)[
        : axis.point_count
    ]

    weights = np.zeros(axis.point_count, dtype=float)
    for label, block in interval_weights.items():
        mask = day_type == label
        if not mask.any():
            continue
        # 每小时的权重块（repeat 个相同值）：直接按小时索引取，再按子间隔顺序取
        hourly_block = block.reshape(HOURS_PER_DAY, repeat)
        weights[mask] = hourly_block[hours[mask], sub_index[mask]]

    # 白天/夜间电量比例（§6.5）：只改两组之间的电量分配，不改组内形状
    if params.daytime_load_ratio is not None:
        weights = _apply_day_night_split(weights, hours, params)

    # 按月严格回归（§3.2、§6.5）
    month_energy = _month_energy_map(monthly, params)
    values = np.zeros(axis.point_count, dtype=float)
    rows: list[MonthlyRegressionRow] = []
    # §6.5 明确要求"数值误差不超过 1e-6 kWh"，因此这里**不做按点数放宽**；
    # 精确校正后实测残差在 1e-9 kWh 量级（远小于容差），放在测试里锁定。
    tolerance_used = tolerance_kwh
    day_units = np.asarray(
        [np.datetime64(t.date().isoformat(), "D") for t in axis.timestamps], dtype="datetime64[D]"
    )
    for month in range(1, 13):
        mask = months == month
        if not mask.any():
            continue
        energy = month_energy.get(month)
        if energy is None:
            continue
        values[mask], residual = regress_month_to_energy(
            weights[mask], energy, tolerance_kwh=tolerance_used
        )
        month_values = values[mask]
        day_count = int(np.unique(day_units[mask]).size)
        delta_hours = params.delta_hours
        rows.append(
            MonthlyRegressionRow(
                year=params.year,
                month=month,
                month_key=f"{params.year:04d}-{month:02d}",
                source=month_energy_source(monthly, params, month),
                day_count=day_count,
                interval_count=int(mask.sum()),
                input_energy_kwh=float(energy),
                estimated_energy_kwh=float(month_values.sum()),
                residual_kwh=float(residual),
                residual_ratio=float(residual) / float(energy) if energy else 0.0,
                within_tolerance=abs(residual) <= tolerance_used,
                estimated_peak_power_kw=float(month_values.max()) / delta_hours,
                estimated_avg_power_kw=float(month_values.sum()) / (day_count * 24.0),
                tolerance_kwh=tolerance_used,
            )
        )

    if not rows:
        raise ValidationError(
            "没有可估算的月份：既没有月电量输入，也没有可用于补齐的年电量（V2.2 §6.5）",
            field="load_estimate.monthly_energy",
        )

    missing = sorted(set(range(1, 13)) - {row.month for row in rows})
    if missing:
        raise ValidationError(
            f"以下月份无法估算（既无月电量、也无法由年电量补齐）：{missing}；"
            f"请补充这些月份的电量或提供年电量与月度比例（V2.2 §6.5）",
            field="load_estimate.monthly_energy",
        )

    points = [
        TimeSeriesPoint(timestamp=axis.timestamps[i], load_kwh=float(values[i]))
        for i in range(axis.point_count)
    ]
    step = timedelta(minutes=params.interval_minutes)
    assumptions = _estimate_assumptions(resolved_template, params, rows, tolerance_used)
    dataset = HighFrequencyLoadDataset(
        profile_id=make_dataset_id(
            f"{params.year}年月账单估算_{resolved_template.template_id}", params.interval_minutes
        ),
        project_id=project_id,
        name=(
            f"{params.year} 年月账单估算负荷（模板：{resolved_template.name}，"
            f"{params.interval_minutes} 分钟）"
        ),
        source_type=LoadDataSourceType.MONTHLY_BILL_ESTIMATE,
        value_kind=LoadValueKind.INTERVAL_ENERGY_KWH,
        interval_minutes=params.interval_minutes,
        resolution=resolution,
        timezone=TIMEZONE_DEFAULT,
        period_start=axis.timestamps[0],
        period_end=axis.timestamps[-1] + step,
        points=points,
        annualized=False,
        estimated=True,
        coverage_ratio=1.0,
        missing_intervals=0,
        duplicate_intervals=0,
        irregular_intervals=0,
        quality_status=_quality_status(rows),
        quality_messages=[
            "[E01] 本曲线由月电量与典型负荷模板估算生成，**不是实测曲线**（V2.2 §0.2、§6.5）"
        ],
        source_file_name="",
        source_sheet="",
        mapping_config={},
        assumptions=list(assumptions),
        created_at=datetime.now(),
    )

    total_input = float(sum(row.input_energy_kwh for row in rows))
    total_estimated = float(sum(row.estimated_energy_kwh for row in rows))
    max_residual = max(abs(row.residual_kwh) for row in rows)
    result = LoadEstimateResult(
        dataset=dataset,
        params=params,
        template_id=resolved_template.template_id,
        template_name=resolved_template.name or resolved_template.template_id,
        monthly=rows,
        total_input_energy_kwh=total_input,
        total_estimated_energy_kwh=total_estimated,
        max_abs_residual_kwh=max_residual,
        tolerance_kwh=tolerance_used,
        all_months_regressed=all(row.within_tolerance for row in rows),
        assumptions=assumptions,
    )
    logger.info(
        "月账单估算完成：模板=%s、间隔=%d 分钟、%d 点；输入月电量合计 %.3f kWh、"
        "估算合计 %.3f kWh（最大单月残差 %.3e kWh）；曲线标记=%s",
        resolved_template.template_id,
        params.interval_minutes,
        dataset.point_count,
        total_input,
        total_estimated,
        max_residual,
        dataset.source_type.report_badge,
    )
    return result


def _template_or_error(params: LoadEstimateParams) -> TypicalLoadTemplate:
    try:
        return builtin_template(params.template_id)
    except KeyError as exc:
        raise ValidationError(
            f"未知的典型负荷模板「{params.template_id}」；可用模板：{'、'.join(template_ids())}。"
            f"如使用自定义模板，请直接传入 template 参数（V2.2 §6.5）",
            field="load_estimate.template_id",
        ) from exc


def _apply_day_night_split(
    weights: np.ndarray,
    hours: np.ndarray,
    params: LoadEstimateParams,
) -> np.ndarray:
    """按"白天 xx% / 夜间 yy%"改写权重的两组水平（§6.5）。

    只调整白天组与夜间组之间的电量分配，组内形状不变；两组各自归一化到目标电量份额。
    白天 = ``06:00–18:00``（:data:`cenep.domain.load_estimate.DAYTIME_HOURS`），
    其余为夜间；该口径写入 ``assumptions``。
    """
    day_share = float(params.daytime_load_ratio or 0.0)
    night_share = float(params.nighttime_load_ratio or 0.0)
    is_day = np.isin(hours, np.asarray(DAYTIME_HOURS, dtype=np.int64))
    out = weights.copy()
    for mask, share, label in (
        (is_day, day_share, "白天（06:00–18:00）"),
        (~is_day, night_share, "夜间（18:00–次日 06:00）"),
    ):
        if not mask.any():
            raise ValidationError(
                f"{label}没有任何估算间隔，无法按白天/夜间比例分配电量；"
                f"请检查间隔设置（V2.2 §6.5）",
                field="load_estimate.day_night",
            )
        total = float(out[mask].sum())
        if total <= _WEIGHT_EPS:
            raise ValidationError(
                f"{label}的权重合计为 0，无法把目标电量比例（{share:.2%}）分配到该时段；"
                f"请调整模板权重、周末/停产比例或班次设置（V2.2 §6.5）",
                field="load_estimate.day_night",
            )
        out[mask] = out[mask] * (share / total)
    return out


def _month_energy_map(
    monthly: list[MonthlyLoadEnergy],
    params: LoadEstimateParams,
) -> dict[int, float]:
    """把月电量列表整理成 ``{月: kWh}``；缺月用年电量补齐（§6.5）。"""
    energies: dict[int, float] = {}
    for item in monthly:
        if item.year != params.year:
            raise ValidationError(
                f"月电量 {item.month_key} 的年份（{item.year}）与估算年份（{params.year}）不一致；"
                f"跨年估算请分年分别生成估算曲线（V2.2 §6.5）",
                field="load_estimate.monthly_energy",
            )
        if item.month in energies:
            raise ValidationError(
                f"月电量输入中 {item.year}-{item.month:02d} 出现重复条目；"
                f"同一个月只能有一条电量（V2.2 §6.5）",
                field="load_estimate.monthly_energy",
            )
        energies[item.month] = float(item.energy_kwh)

    missing = sorted(set(range(1, 13)) - set(energies))
    if missing:
        for item in monthly_energies_from_annual(params, missing_months=missing):
            energies[item.month] = float(item.energy_kwh)
    return energies


def month_energy_source(
    monthly: list[MonthlyLoadEnergy],
    params: LoadEstimateParams,
    month: int,
) -> LoadEstimateSource:
    """该月电量的来源标签（账单 / 手工 / 年电量拆分 / 均匀分摊）。"""
    for item in monthly:
        if item.month == month:
            return item.source
    if params.monthly_split_ratios:
        return LoadEstimateSource.ANNUAL_SPLIT
    return LoadEstimateSource.UNIFORM_DEFAULT


def _quality_status(rows: list[MonthlyRegressionRow]) -> LoadQualityStatus:
    """估算曲线的质量状态：回归全部通过为"有警告"（估算数据本身不应被判为"有效实测"）。"""
    if not all(row.within_tolerance for row in rows):
        return LoadQualityStatus.INVALID
    return LoadQualityStatus.WARNING


def _estimate_assumptions(
    template: TypicalLoadTemplate,
    params: LoadEstimateParams,
    rows: list[MonthlyRegressionRow],
    tolerance_kwh: float,
) -> list[str]:
    """"估算依据"章节的全部假设（§6.5 要求逐条列出假设与数据缺口）。"""
    lines = [
        f"估算方法（V2.2 §3.2）：E_i = E_month × w_i ÷ Σ_{{i∈month}} w_i，"
        f"对应平均功率 P_i = E_i × 60 ÷ {params.interval_minutes}；"
        f"w_i 是**非负权重**，不是 kW。",
        f"典型负荷模板「{template.name or template.template_id}」：{template.description}；"
        f"模板是**可编辑的权重曲线，不是行业实测事实**（V2.2 §6.5）。"
        + (f" {template.notes}" if template.notes else ""),
        f"每月的估算间隔电量严格回归到输入月电量；逐月残差与容差见『月电量回归』明细"
        f"（本月最大单月残差 {max(abs(r.residual_kwh) for r in rows):.3e} kWh、"
        f"容差 {tolerance_kwh:g} kWh）。",
        f"班次与运行参数：每周生产 {params.workdays_per_week} 天（周一为一周第一天）；"
        + (
            f"班次 [{params.shift_start_hour}:00, {params.shift_end_hour}:00)，"
            f"非班次时段负荷 = 班内峰值 × {params.offshift_run_ratio:.2%}；"
            if params.offshift_run_ratio is not None
            else "未启用班次裁剪（offshift_run_ratio 为空），保留模板自带的日内形状；"
        )
        + f"周末负荷 = 工作日 × {params.weekend_run_ratio:.2%}、"
        f"节假日 = 工作日 × {params.holiday_run_ratio:.2%}、"
        f"停产/检修日 = 工作日 × {params.maintenance_run_ratio:.2%}。",
        "日类型优先级：停产/检修日 > 节假日 > 周末 > 工作日（同一天同时列入时按前者处理）。",
    ]
    if params.offshift_run_ratio is not None:
        lines.append(
            f"班次裁剪口径：班次外每个小时的权重被设为「班内最大权重 × "
            f"{params.offshift_run_ratio:.4f}」，这会**覆盖模板自带的夜间形状**（V2.2 §6.5）。"
        )
    if params.interval_minutes != 60:
        lines.append(
            f"子小时展开假设（V2.2 §6.4）：模板权重为 24 点小时曲线，"
            f"目标间隔 {params.interval_minutes} 分钟时，小时内的 "
            f"{params.intervals_per_hour} 个子间隔**均分**该小时权重"
            f"（等价于『小时内功率恒定』）；该假设可配置，但必须随结果披露。"
        )
    if params.daytime_load_ratio is not None:
        lines.append(
            f"白天/夜间电量分配（V2.2 §6.5）：白天 06:00–18:00 占 "
            f"{params.daytime_load_ratio:.2%}、夜间占 {params.nighttime_load_ratio:.2%}；"
            f"两组各自归一化到目标份额，组内形状不变。"
        )
    if any(row.source is LoadEstimateSource.UNIFORM_DEFAULT for row in rows):
        lines.append(
            "数据缺口：有月份未提供月电量、也未提供月度比例，该月年电量按**均匀分摊**处理——"
            "这是明确的默认估算假设，不代表真实月度分布（V2.2 §6.5）。"
        )
    elif any(row.source is LoadEstimateSource.ANNUAL_SPLIT for row in rows):
        lines.append(
            "数据缺口：有月份未提供月电量，改按用户填写的 12 个月度比例拆分年电量；"
            "月度比例是用户输入，未与账单逐月核对（V2.2 §6.5）。"
        )
    sources = sorted({row.source.label for row in rows})
    lines.append(f"月电量来源：{'、'.join(sources)}。")
    lines.append(
        "不确定性与适用范围：仅有月度电量时**无法唯一确定小时负荷曲线**，"
        "估算曲线只能用于消纳率与收益的**范围估算**，不得当作实测逐时数据；"
        "如需精确结论请导入 15/30/60 分钟实测负荷（V2.2 §6.5、§6.6）。"
    )
    lines.append(
        "⚠ 本曲线已标记为估算（月账单估算负荷），在任何界面与报告中都不得显示为实测曲线"
        "（V2.2 §0.2 红线）。"
    )
    return lines
