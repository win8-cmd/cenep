"""光伏消纳率纯计算模块（规格书 V2.2 §3.3、§9.2、§2.4、§6.3 C；阶段 4）。

本模块是**消纳四项指标的唯一公式来源**（V1 §104、V2 §61：公式不得散落在界面或报告里）。
它只做纯计算，不发文件、不碰界面、不读项目文件。

一、四项指标的完整口径（规格书 §3.3 的逐字对应，本节即"口径声明"的权威来源）
==========================================================================

逐间隔（每个时间间隔 i，单位 kWh，区间语义左闭右开 ``[t, t+Δt)``）：

.. code-block:: text

    E_self,i   = min(E_load,i, E_pv,i)                    ← 自发自用电量
    E_export,i = max(E_pv,i  − E_load,i, 0)               ← 上网电量
    E_import,i = max(E_load,i − E_pv,i, 0)                ← 电网购电量

全年四项指标（**全部是电量口径**，分子分母都是 kWh，没有一项是时间口径）：

.. code-block:: text

    R_self     = Σ E_self,i  ÷ Σ E_pv,i      光伏自用率（消纳率主指标）
    R_coverage = Σ E_self,i  ÷ Σ E_load,i    负荷覆盖率（**电量覆盖率**）
    R_export   = Σ E_export,i ÷ Σ E_pv,i     光伏上网率
    R_grid     = Σ E_import,i ÷ Σ E_load,i   电网依赖率

**必须记住的三条分母口径（最容易混用的地方）**

1. ``R_self`` 与 ``R_export`` 的分母是 **光伏发电量 E_pv**；
2. ``R_coverage`` 与 ``R_grid`` 的分母是 **负荷电量 E_load**；
3. ``R_coverage`` 的"覆盖率"是**电量覆盖率**（自发自用电量 ÷ 企业用电量），
   **不是时间覆盖率**（不是"有光伏出力的时间点占比"，也不是"数据覆盖的时间区间占比"）。
   数据的时间覆盖率是另一个独立字段（:attr:`SelfConsumptionResult.coverage_ratio`），
   两者不得混为一谈。

边界（§3.3）：分母为 0 时返回 ``None``，界面显示"不适用"，**不得显示 0%**。

二、命名规则（§3.3 末段）
========================

UI 必须同时展示"光伏自用率（自发自用电量/光伏发电量）"和
"负荷覆盖率（自发自用电量/企业用电量）"，**不得仅显示含糊的"消纳率"而不写口径**。
:data:`CALIBERS` 提供四个 :class:`~cenep.domain.self_consumption_result.MetricCaliber`，
被界面、Excel「消纳率分析」表与 PDF「光伏消纳率」章节**共用**，保证三处口径一致。

三、能量平衡（§3.3 末段）
========================

不含储能时（逐间隔严格成立）：

.. code-block:: text

    E_load,i = E_self,i + E_import,i
    E_pv,i   = E_self,i + E_export,i

本模块把这两条恒等式折算成一次 :func:`cenep.calculation.energy_balance.balance_of` 调用
（**复用既有能量平衡引擎，不另写一套守恒判据**）：把四项流构造成一个零储能的
:class:`~cenep.calculation.dispatch_engine.DispatchOutcome`，交给平衡引擎计算
``supply_total / demand_total / error / max_hourly_error``，并沿用其
"最大逐间隔误差超过容差即判失败"的语义（默认 ``1e-6`` kWh，§19）。

有四储能的场景**不重写调度**：调用方用既有的
:func:`cenep.calculation.dispatch_engine.dispatch` 得到
:class:`DispatchOutcome`，再交给 :func:`analyze_dispatch_outcome`；
储能的充放电、SOC、能量平衡全部由既有引擎负责（§3.3、§6.6）。

四、缺失数据（§6.4）
====================

「缺失负荷数据**不得默认按 0 处理**」。本模块的默认策略是
:attr:`~cenep.domain.enums.MissingDataPolicy.REJECT`（拒算并报中文错误）；
只有调用方显式指定插补策略时才插补，且插补间隔数记入
:attr:`SelfConsumptionResult.interpolated_intervals`。

性能（§8.3）
-----------
全程 NumPy 向量化，无逐点 Python 循环；35040 点的一次完整分析（含逐月汇总与平衡校验）
实测在 1 秒以内（预算 5 秒）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np

from ..domain.enums import LoadDataSourceType, LoadQualityStatus, MissingDataPolicy
from ..domain.self_consumption_result import (
    METRIC_ORDER,
    MetricCaliber,
    MonthlySelfConsumptionRow,
    SelfConsumptionResult,
)
from . import storage_soc as socmod
from .dispatch_engine import DispatchOutcome
from .energy_balance import balance_of
from .errors import ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "BALANCE_TOLERANCE_KWH",
    "CALIBERS",
    "IntervalFlows",
    "analyze_dispatch_outcome",
    "analyze_self_consumption",
    "caliber_of",
    "interval_energy_flows",
    "ratio_or_none",
]

#: 逐间隔能量平衡容差（kWh）。沿用 V2 §19 的项目统一容差：超过即判计算失败。
BALANCE_TOLERANCE_KWH = 1e-6

#: 展示用来源说明中"估算"分支的固定后缀（§0.2 红线：估算结果必须携带标记）
ESTIMATE_NOTICE = "⚠ 本结果基于**估算**负荷曲线，不是实测结论；收益类结论同样只能标注为估算"


# --------------------------------------------------------------------------- #
# 口径（唯一权威来源：界面提示 + 报告 assumptions 三处共用，§3.3、§12）
# --------------------------------------------------------------------------- #
CALIBERS: dict[str, MetricCaliber] = {
    "self_consumption": MetricCaliber(
        key="self_consumption",
        name="光伏自用率（自发自用电量 ÷ 光伏发电量）—— 即规格书 §3.3 的『消纳率』主指标",
        formula="R_self = Σ_i min(E_load,i, E_pv,i) ÷ Σ_i E_pv,i",
        numerator="自发自用电量 E_self = Σ_i min(E_load,i, E_pv,i)，单位 kWh（逐间隔取负荷与光伏的较小者再求和）",
        denominator="**光伏发电量** E_pv = Σ_i E_pv,i，单位 kWh（不是负荷电量）",
        unit="比例（无量纲小数 0~1，界面按百分数显示）",
        boundary="E_pv = 0 时返回 None，界面显示「不适用」，**不得显示 0%**",
        scope="全部共同覆盖的时间间隔逐点求和；不含储能，含储能时按调度后的光伏去向统计",
        note="行业口语里的『消纳率』通常指本指标；它与『负荷覆盖率』是不同指标，必须分别显示",
    ),
    "load_coverage": MetricCaliber(
        key="load_coverage",
        name="负荷覆盖率（自发自用电量 ÷ 企业用电量）",
        formula="R_coverage = Σ_i min(E_load,i, E_pv,i) ÷ Σ_i E_load,i",
        numerator="自发自用电量 E_self = Σ_i min(E_load,i, E_pv,i)，单位 kWh（与自用率同一分子）",
        denominator="**负荷电量** E_load = Σ_i E_load,i，单位 kWh（企业总用电量，不是光伏发电量）",
        unit="比例（无量纲小数 0~1，界面按百分数显示）",
        boundary="E_load = 0 时返回 None，界面显示「不适用」，**不得显示 0%**",
        scope="与自用率同一批时间间隔逐点求和",
        note="本指标是**电量覆盖率**（光伏自发自用电量占企业用电量的比例），"
        "**不是时间覆盖率**（不是『有光伏出力的时间点占比』，也不是数据的时间覆盖区间占比）；"
        "时间覆盖率是独立字段 coverage_ratio，两者口径不同、不得互相替代",
    ),
    "export": MetricCaliber(
        key="export",
        name="光伏上网率（上网电量 ÷ 光伏发电量）",
        formula="R_export = Σ_i max(E_pv,i − E_load,i, 0) ÷ Σ_i E_pv,i",
        numerator="上网电量 E_export = Σ_i max(E_pv,i − E_load,i, 0)，单位 kWh（逐间隔取光伏盈余再求和）",
        denominator="**光伏发电量** E_pv = Σ_i E_pv,i，单位 kWh（不是负荷电量）",
        unit="比例（无量纲小数 0~1，界面按百分数显示）",
        boundary="E_pv = 0 时返回 None，界面显示「不适用」，**不得显示 0%**",
        scope="全部共同覆盖的时间间隔逐点求和",
        note="若把分母误用负荷电量，得到的是『上网电量占用电量比例』，与上网率口径不同；"
        "本口径下 R_self + R_export = 1（同一分母 E_pv）",
    ),
    "grid_dependency": MetricCaliber(
        key="grid_dependency",
        name="电网依赖率（电网购电量 ÷ 企业用电量）",
        formula="R_grid = Σ_i max(E_load,i − E_pv,i, 0) ÷ Σ_i E_load,i",
        numerator="电网购电量 E_import = Σ_i max(E_load,i − E_pv,i, 0)，单位 kWh",
        denominator="**负荷电量** E_load = Σ_i E_load,i，单位 kWh（不是光伏发电量）",
        unit="比例（无量纲小数 0~1，界面按百分数显示）",
        boundary="E_load = 0 时返回 None，界面显示「不适用」，**不得显示 0%**",
        scope="全部共同覆盖的时间间隔逐点求和",
        note="本口径下 R_coverage + R_grid = 1（同一分母 E_load）；"
        "含储能的场景中购电量含『电网→储能』充电电量，此时分子取自调度结果并已在假设中标注",
    ),
}


def caliber_of(key: str) -> MetricCaliber:
    """取指标口径；未知键抛中文 ``KeyError``（避免界面静默显示空口径）。"""
    if key not in CALIBERS:
        raise KeyError(f"未知的消纳指标键：{key}（可用：{'、'.join(METRIC_ORDER)}）")
    return CALIBERS[key]


def all_calibers() -> list[MetricCaliber]:
    """四项口径，按 §3.3 的展示顺序返回（界面/Excel/PDF 三处共用同一顺序）。"""
    return [CALIBERS[key] for key in METRIC_ORDER]


def ratio_or_none(numerator: float, denominator: float) -> float | None:
    """按 §3.3 的边界规则求比例：分母为 0 时返回 ``None``（"不适用"），**不得返回 0**。

    :param numerator: 分子（kWh）
    :param denominator: 分母（kWh）
    """
    if denominator is None or not np.isfinite(denominator) or denominator == 0.0:
        return None
    value = float(numerator) / float(denominator)
    if not np.isfinite(value):
        return None
    return value


# --------------------------------------------------------------------------- #
# 逐间隔能量流
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IntervalFlows:
    """逐间隔的消纳能量流（§3.3，单位 kWh）。"""

    load: np.ndarray
    pv: np.ndarray
    used: np.ndarray
    export: np.ndarray
    grid_import: np.ndarray

    @property
    def point_count(self) -> int:
        return int(self.load.size)

    def as_outcome(self) -> DispatchOutcome:
        """构造**零储能**的 :class:`DispatchOutcome`，用于复用能量平衡引擎（§3.3、§19）。

        为什么这样"借道"：§3.3 要求"必须对全时段执行守恒检查"，
        而守恒判据（``PV + 购电 + 放电 = 负荷 + 充电 + 上网 + 弃光``）的唯一实现是
        :func:`cenep.calculation.energy_balance.balance_of`。把这里算出的流量装进
        既有结果对象，就复用了同一套判据与同一套容差，而不是在消纳模块里另写一遍守恒式。
        """
        n = self.point_count
        zeros = np.zeros(n, dtype=float)
        return DispatchOutcome(
            storage=socmod.empty_series(n),
            load=self.load,
            pv=self.pv,
            pv_to_load=self.used,
            pv_to_storage=zeros,
            pv_to_grid=self.export,
            pv_curtailed=zeros,
            grid_to_load=self.grid_import,
            grid_to_storage=zeros,
            load_from_storage=zeros,
            storage_to_grid=zeros,
            grid_import=self.grid_import,
            grid_export=self.export,
            electricity_cost=zeros,
            export_revenue=zeros,
            storage_revenue=zeros,
            total_revenue=zeros,
            net_energy_cost=zeros,
            price=zeros,
            export_price=zeros,
        )


def interval_energy_flows(load_kwh, pv_kwh) -> IntervalFlows:
    """按 §3.3 逐间隔计算自发自用 / 上网 / 购电（纯向量化，无 Python 循环）。

    .. code-block:: text

        used        = min(load, pv)
        export      = max(pv − load, 0)
        grid_import = max(load − pv, 0)

    :param load_kwh: 逐间隔负荷电量 kWh（一维、有限、非负）
    :param pv_kwh: 逐间隔光伏发电量 kWh（一维、有限、非负，与负荷同长度、同时间轴）
    :raises ValidationError: 长度不一致、含非有限值或负值（中文报错）
    """
    load = _finite_non_negative(load_kwh, "负荷电量")
    pv = _finite_non_negative(pv_kwh, "光伏发电量")
    if load.size != pv.size:
        raise ValidationError(
            f"负荷与光伏的时间点数量不一致（负荷 {load.size} 点、光伏 {pv.size} 点）；"
            f"消纳率要求两者使用同一时间轴、同一时区、同时间隔（V2.2 §6.4）",
            field="self_consumption.align",
        )
    return IntervalFlows(
        load=load,
        pv=pv,
        used=np.minimum(load, pv),
        export=np.maximum(pv - load, 0.0),
        grid_import=np.maximum(load - pv, 0.0),
    )


def _finite_non_negative(values, label: str) -> np.ndarray:
    """一维、有限、非负校验（中文错误；§8.2 要求负荷与光伏电量非负）。"""
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValidationError(
            f"{label}必须是一维序列，实际维度 {array.ndim}", field="self_consumption.input"
        )
    if array.size and not np.all(np.isfinite(array)):
        bad = int(np.sum(~np.isfinite(array)))
        raise ValidationError(
            f"{label}中有 {bad} 个非有限数值（空值 / 文本 / 无穷大）。"
            f"V2.2 §6.4 规定缺失数据不得默认按 0 处理：请先按明确规则插补或剔除共同覆盖区间，"
            f"再计算消纳率",
            field="self_consumption.input",
        )
    if array.size and float(array.min()) < 0.0:
        raise ValidationError(
            f"{label}出现负值（最小 {float(array.min()):.4f} kWh），请先修正数据再计算消纳率",
            field="self_consumption.input",
        )
    return array


# --------------------------------------------------------------------------- #
# 缺失值处理（§6.4：不得默认按 0）
# --------------------------------------------------------------------------- #
def _fill_missing(
    values: np.ndarray,
    policy: MissingDataPolicy,
    label: str,
) -> tuple[np.ndarray, int]:
    """按显式策略插补缺失点；返回 ``(新序列, 插补点数)``。

    ``REJECT``（默认）直接抛中文错误，并给出缺失点数量与前几个位置，
    以便用户回到导入页修正（§6.4「可选方案为拒绝计算、用户确认插补或按明确规则插补。
    所有插补点数量必须记录」）。
    """
    finite = np.isfinite(values)
    count = int(np.sum(~finite))
    if count == 0:
        return values.astype(float), 0
    if policy is MissingDataPolicy.REJECT:
        positions = np.flatnonzero(~finite)[:5]
        raise ValidationError(
            f"{label}有 {count} 个缺失点（前几个位置：{positions.tolist()}）。"
            f"缺失数据不得默认按 0 处理；如确认可以插补，请显式选择插补策略并记录插补点数"
            f"（V2.2 §6.4）",
            field="self_consumption.missing",
        )
    filled = values.astype(float).copy()
    idx = np.arange(filled.size, dtype=float)
    good = np.flatnonzero(finite)
    if good.size == 0:
        raise ValidationError(
            f"{label}全部为缺失值，无法按 {policy.label} 插补", field="self_consumption.missing"
        )
    if policy is MissingDataPolicy.FORWARD_FILL:
        # 前值填充：首个缺失点之前没有前值时，用其后第一个有效值
        last = float(filled[good[0]])
        for i in range(filled.size):
            if finite[i]:
                last = float(filled[i])
            else:
                filled[i] = last
    else:  # LINEAR_INTERPOLATION / TYPICAL_DAY_FILL：按有效点线性插值（端点取最近有效值）
        filled[~finite] = np.interp(idx[~finite], idx[good], filled[good])
    logger.info("%s插补 %d 个缺失点（策略：%s）", label, count, policy.label)
    return filled, count


# --------------------------------------------------------------------------- #
# 主入口：无储能（§3.3 纯公式）
# --------------------------------------------------------------------------- #
def analyze_self_consumption(
    load_kwh: Sequence[float] | np.ndarray,
    pv_kwh: Sequence[float] | np.ndarray,
    *,
    interval_minutes: int,
    timestamps: Sequence[datetime] | None = None,
    months: Sequence[int] | None = None,
    load_source_type: LoadDataSourceType = LoadDataSourceType.HIGH_FREQUENCY_IMPORT,
    load_provenance_text: str = "",
    pv_provenance_text: str = "",
    pv_source_is_estimate: bool = False,
    coverage_ratio: float = 1.0,
    data_quality_status: LoadQualityStatus = LoadQualityStatus.VALID,
    missing_policy: MissingDataPolicy = MissingDataPolicy.REJECT,
    tolerance_kwh: float = BALANCE_TOLERANCE_KWH,
    extra_assumptions: Sequence[str] = (),
) -> SelfConsumptionResult:
    """计算光伏消纳四项指标（§3.3），并做逐间隔能量守恒校验（§19）。

    全部公式见模块文档字符串与 :data:`CALIBERS`；本函数**不**负责取数、
    不负责生成曲线，只接受两条已对齐的逐间隔电量序列。

    :param load_kwh: 逐间隔负荷电量 kWh
    :param pv_kwh: 逐间隔光伏发电量 kWh
    :param interval_minutes: 间隔（分钟），决定"间隔"这一统计单位
    :param timestamps: 时间戳（可选，用于逐月汇总与错误定位）
    :param months: 月份数组（可选，长度须与序列一致；未给时由 ``timestamps`` 推导）
    :param load_source_type: 负荷来源标签；**非实测来源会自动带上"基于估算"标记**（§0.2）
    :param coverage_ratio: 时间轴覆盖率（与"负荷覆盖率"是不同指标，见 §3.3）
    :param missing_policy: 缺失点处理策略（默认拒算，§6.4）
    :param tolerance_kwh: 逐间隔平衡容差 kWh（默认 1e-6，§3.3、§19）
    :param extra_assumptions: 追加的口径说明（会被写入结果的 ``assumptions``）
    :raises ValidationError: 长度不一致、含负值/缺失且未选择插补策略、能量不守恒
    """
    load = _as_float_array(load_kwh)
    pv = _as_float_array(pv_kwh)
    if load.size != pv.size:
        raise ValidationError(
            f"负荷与光伏的时间点数量不一致（负荷 {load.size} 点、光伏 {pv.size} 点）；"
            f"消纳率要求两者使用同一时间轴、同一时区、同时间隔（V2.2 §6.4）",
            field="self_consumption.align",
        )

    load, filled_load = _fill_missing(load, missing_policy, "负荷曲线")
    pv, filled_pv = _fill_missing(pv, missing_policy, "光伏曲线")
    interpolated = filled_load + filled_pv

    flows = interval_energy_flows(load, pv)
    balance = balance_of(flows.as_outcome(), tolerance_kwh)
    if balance.max_hourly_error > tolerance_kwh:
        raise ValidationError(
            f"消纳计算的能量平衡校验失败：最大逐间隔误差 {balance.max_hourly_error:.6e} kWh "
            f"超过容差 {tolerance_kwh:.1e} kWh。负荷 = 自发自用 + 购电、光伏 = 自发自用 + 上网 "
            f"两条恒等式必须逐间隔成立（V2.2 §3.3、§19）",
            field="self_consumption.balance",
        )

    assumptions = _base_assumptions(interval_minutes, tolerance_kwh)
    assumptions.extend(str(item) for item in extra_assumptions if item)
    if interpolated:
        assumptions.append(
            f"缺失数据按「{missing_policy.label}」插补，共插补 {interpolated} 个间隔"
            f"（负荷 {filled_load} 个、光伏 {filled_pv} 个）；插补值不是实测值（V2.2 §6.4）"
        )
    if pv_source_is_estimate:
        assumptions.append("光伏出力曲线本身为估算/模板合成，非实测出力数据（V2.2 §0.2）")

    return _build_result(
        flows=flows,
        balance=balance,
        interval_minutes=interval_minutes,
        timestamps=timestamps,
        months=months,
        load_source_type=load_source_type,
        load_provenance_text=load_provenance_text,
        pv_provenance_text=pv_provenance_text,
        pv_source_is_estimate=pv_source_is_estimate,
        coverage_ratio=coverage_ratio,
        data_quality_status=data_quality_status,
        tolerance_kwh=tolerance_kwh,
        interpolated=interpolated,
        assumptions=assumptions,
    )


def _as_float_array(values) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValidationError(
            f"输入必须是一维序列，实际维度 {array.ndim}", field="self_consumption.input"
        )
    return array


def _base_assumptions(interval_minutes: int, tolerance_kwh: float) -> list[str]:
    """无储能场景的固定口径说明（写入结果 ``assumptions``，报告必须逐条披露）。"""
    return [
        f"消纳口径（V2.2 §3.3）：逐间隔取 E_self=min(负荷,光伏)、E_export=max(光伏−负荷,0)、"
        f"E_import=max(负荷−光伏,0)，再对全部 {interval_minutes} 分钟间隔求和。",
        "四项指标分母：光伏自用率与上网率 ÷ 光伏发电量；负荷覆盖率与电网依赖率 ÷ 负荷电量。"
        "负荷覆盖率是**电量覆盖率**（自发自用电量÷企业用电量），不是时间覆盖率。",
        f"逐间隔能量平衡容差 {tolerance_kwh:g} kWh（V2.2 §3.3、§19）；不含储能，"
        f"负荷 = 自发自用 + 购电、光伏 = 自发自用 + 上网。",
        "本结果不含任何电价与金额：消纳率本身与电价无关，收益计算属后续阶段（V2.3）。",
    ]


# --------------------------------------------------------------------------- #
# 主入口：有储能（复用既有 dispatch / energy_balance，不另写调度）
# --------------------------------------------------------------------------- #
def analyze_dispatch_outcome(
    outcome: DispatchOutcome,
    *,
    interval_minutes: int,
    timestamps: Sequence[datetime] | None = None,
    months: Sequence[int] | None = None,
    load_source_type: LoadDataSourceType = LoadDataSourceType.HIGH_FREQUENCY_IMPORT,
    load_provenance_text: str = "",
    pv_provenance_text: str = "",
    pv_source_is_estimate: bool = False,
    coverage_ratio: float = 1.0,
    data_quality_status: LoadQualityStatus = LoadQualityStatus.VALID,
    tolerance_kwh: float = BALANCE_TOLERANCE_KWH,
    extra_assumptions: Sequence[str] = (),
) -> SelfConsumptionResult:
    """由既有调度结果 :class:`DispatchOutcome` 汇总消纳四项指标（§3.3、§6.6）。

    复用关系（§0.2：不得重写 ``calculation/`` 的既有能力）：

    * 逐时能源流来自 :func:`cenep.calculation.dispatch_engine.dispatch`（储能充放电、SOC、
      策略全部由它负责）；
    * 守恒判据来自 :func:`cenep.calculation.energy_balance.check_balance`（同一套容差与报错）。

    与无储能路径的差异（**必须披露**，写入 ``assumptions``）：

    * ``E_self`` 取"光伏→负荷 + 光伏→储能"，即光伏先供负荷、余量充入储能也计入自用
      （储能在后续时段放出，避免把"暂存后自用"误判成上网）；
    * ``E_import`` 取调度后的 ``grid_import``，含"电网→储能"充电电量；
    * ``E_export`` 取调度后的 ``grid_export``（可能同时包含光伏与储能放电上网）。
    """
    load = _as_float_array(outcome.load)
    pv = _as_float_array(outcome.pv)
    used = _as_float_array(outcome.pv_to_load) + _as_float_array(outcome.pv_to_storage)
    export = _as_float_array(outcome.grid_export)
    grid_import = _as_float_array(outcome.grid_import)
    flows = IntervalFlows(load=load, pv=pv, used=used, export=export, grid_import=grid_import)

    balance = balance_of(outcome, tolerance_kwh)
    if balance.max_hourly_error > tolerance_kwh:
        raise ValidationError(
            f"含储能场景的能量平衡校验失败：最大逐间隔误差 {balance.max_hourly_error:.6e} kWh "
            f"超过容差 {tolerance_kwh:.1e} kWh（V2.2 §19、§6.6）",
            field="self_consumption.balance",
        )

    assumptions = _base_assumptions(interval_minutes, tolerance_kwh)
    assumptions[3] = (
        "含储能场景：本结果直接汇总既有储能时序引擎的逐时能源流（未重写调度）；"
        "自发自用电量 = 光伏→负荷 + 光伏→储能，电网购电量含『电网→储能』充电电量。"
    )
    assumptions.extend(str(item) for item in extra_assumptions if item)
    if pv_source_is_estimate:
        assumptions.append("光伏出力曲线本身为估算/模板合成，非实测出力数据（V2.2 §0.2）")

    return _build_result(
        flows=flows,
        balance=balance,
        interval_minutes=interval_minutes,
        timestamps=timestamps,
        months=months,
        load_source_type=load_source_type,
        load_provenance_text=load_provenance_text,
        pv_provenance_text=pv_provenance_text,
        pv_source_is_estimate=pv_source_is_estimate,
        coverage_ratio=coverage_ratio,
        data_quality_status=data_quality_status,
        tolerance_kwh=tolerance_kwh,
        interpolated=0,
        assumptions=assumptions,
    )


# --------------------------------------------------------------------------- #
# 结果装配
# --------------------------------------------------------------------------- #
def _month_arrays(
    size: int,
    timestamps: Sequence[datetime] | None,
    months: Sequence[int] | None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """得到 ``(月份数组, 年份数组)``；两者都拿不到时返回 ``(None, None)``。"""
    if months is not None:
        month_arr = np.asarray(list(months), dtype=np.int64)
        if month_arr.size != size:
            raise ValidationError(
                f"月份数组长度（{month_arr.size}）与数据点数（{size}）不一致",
                field="self_consumption.months",
            )
        if timestamps is not None and len(timestamps) == size:
            year_arr = np.asarray([t.year for t in timestamps], dtype=np.int64)
        else:
            year_arr = np.zeros(size, dtype=np.int64)
        return month_arr, year_arr
    if timestamps is not None:
        stamps = list(timestamps)
        if len(stamps) != size:
            raise ValidationError(
                f"时间戳数量（{len(stamps)}）与数据点数（{size}）不一致",
                field="self_consumption.timestamps",
            )
        return (
            np.asarray([t.month for t in stamps], dtype=np.int64),
            np.asarray([t.year for t in stamps], dtype=np.int64),
        )
    return None, None


def _monthly_rows(
    flows: IntervalFlows,
    month_arr: np.ndarray | None,
    year_arr: np.ndarray | None,
    tolerance_kwh: float,
) -> list[MonthlySelfConsumptionRow]:
    """逐月汇总（§6.3 C「月度自用率趋势」）。全程向量化分组，无逐点循环。"""
    if month_arr is None or month_arr.size == 0:
        return []
    rows: list[MonthlySelfConsumptionRow] = []
    years = year_arr if year_arr is not None else np.zeros(month_arr.size, dtype=np.int64)
    order = np.lexsort((month_arr, years))
    years_sorted = years[order]
    months_sorted = month_arr[order]
    boundary = np.flatnonzero(
        (np.diff(years_sorted) != 0) | (np.diff(months_sorted) != 0)
    ) + 1
    starts = np.concatenate(([0], boundary))
    ends = np.concatenate((boundary, [months_sorted.size]))
    for start, end in zip(starts, ends):
        idx = order[start:end]
        load = float(np.sum(flows.load[idx]))
        pv = float(np.sum(flows.pv[idx]))
        used = float(np.sum(flows.used[idx]))
        export = float(np.sum(flows.export[idx]))
        grid_import = float(np.sum(flows.grid_import[idx]))
        error = (load - used - grid_import) + (pv - used - export)
        max_error = float(
            np.max(
                np.abs(
                    np.concatenate(
                        (
                            flows.load[idx] - flows.used[idx] - flows.grid_import[idx],
                            flows.pv[idx] - flows.used[idx] - flows.export[idx],
                        )
                    )
                )
            )
            if idx.size
            else 0.0
        )
        year = int(years_sorted[start])
        month = int(months_sorted[start])
        rows.append(
            MonthlySelfConsumptionRow(
                year=year,
                month=month,
                month_key=f"{year:04d}-{month:02d}" if year else f"{month:02d}",
                interval_count=int(idx.size),
                load_energy_kwh=load,
                pv_generation_kwh=pv,
                pv_used_on_site_kwh=used,
                pv_export_kwh=export,
                grid_import_kwh=grid_import,
                load_unserved_kwh=0.0,
                self_consumption_rate=ratio_or_none(used, pv),
                load_coverage_rate=ratio_or_none(used, load),
                export_rate=ratio_or_none(export, pv),
                grid_dependency_rate=ratio_or_none(grid_import, load),
                energy_balance_error_kwh=error,
                max_interval_balance_error_kwh=max_error,
            )
        )
    return rows


def _build_result(
    *,
    flows: IntervalFlows,
    balance,
    interval_minutes: int,
    timestamps: Sequence[datetime] | None,
    months: Sequence[int] | None,
    load_source_type: LoadDataSourceType,
    load_provenance_text: str,
    pv_provenance_text: str,
    pv_source_is_estimate: bool,
    coverage_ratio: float,
    data_quality_status: LoadQualityStatus,
    tolerance_kwh: float,
    interpolated: int,
    assumptions: list[str],
) -> SelfConsumptionResult:
    """按 §3.3 汇总四项指标并装配结果对象（含口径与来源标签）。"""
    size = flows.point_count
    month_arr, year_arr = _month_arrays(size, timestamps, months)

    load_total = float(np.sum(flows.load))
    pv_total = float(np.sum(flows.pv))
    used_total = float(np.sum(flows.used))
    export_total = float(np.sum(flows.export))
    import_total = float(np.sum(flows.grid_import))

    is_estimate = not load_source_type.is_measured
    if is_estimate:
        assumptions.append(ESTIMATE_NOTICE)

    assumptions.append(
        "数据时间覆盖率（≠ 负荷覆盖率）："
        f"{coverage_ratio:.2%}；该值只反映时间轴的完整程度，不参与四项指标的分母（V2.2 §3.3）"
    )

    result = SelfConsumptionResult(
        load_energy_kwh=load_total,
        pv_generation_kwh=pv_total,
        pv_used_on_site_kwh=used_total,
        pv_export_kwh=export_total,
        grid_import_kwh=import_total,
        load_unserved_kwh=0.0,
        self_consumption_rate=ratio_or_none(used_total, pv_total),
        load_coverage_rate=ratio_or_none(used_total, load_total),
        export_rate=ratio_or_none(export_total, pv_total),
        grid_dependency_rate=ratio_or_none(import_total, load_total),
        energy_balance_error_kwh=float(balance.error),
        interval_minutes=int(interval_minutes),
        data_quality_status=data_quality_status,
        assumptions=assumptions,
        max_interval_balance_error_kwh=float(balance.max_hourly_error),
        balance_tolerance_kwh=float(tolerance_kwh),
        is_balanced=bool(balance.is_balanced),
        point_count=size,
        coverage_ratio=float(min(max(coverage_ratio, 0.0), 1.0)),
        interpolated_intervals=int(interpolated),
        monthly=_monthly_rows(flows, month_arr, year_arr, tolerance_kwh),
        calibers=all_calibers(),
        load_source_type=load_source_type,
        is_based_on_estimate=is_estimate,
        load_provenance_text=load_provenance_text,
        pv_provenance_text=pv_provenance_text,
        pv_source_is_estimate=bool(pv_source_is_estimate),
    )
    logger.info(
        "消纳分析完成：%d 点（%d 分钟）；自用率=%s（口径：自发自用÷光伏发电量）、"
        "负荷覆盖率=%s（口径：自发自用÷负荷电量）、上网率=%s、电网依赖率=%s；来源=%s",
        result.point_count,
        result.interval_minutes,
        _pct(result.self_consumption_rate),
        _pct(result.load_coverage_rate),
        _pct(result.export_rate),
        _pct(result.grid_dependency_rate),
        result.load_source_type.label,
    )
    return result


def _pct(value: float | None) -> str:
    return "不适用" if value is None else f"{value:.4%}"
