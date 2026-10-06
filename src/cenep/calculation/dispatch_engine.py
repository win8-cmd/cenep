"""V2 储能调度引擎（V2 §12 峰谷套利 / §13 光伏自用优先 / §14 自动经济优化）。

本模块是储能充放电策略的**唯一公式来源**（V1 §104），并逐时输出**可解释的调度原因**
（V2 §16：每一个小时都要能回答"为什么充电／为什么放电／为什么不动作"）。

三种策略
--------
* ``PEAK_VALLEY``          —— 低价充、高价放；价格阈值由用户给定（§12）
* ``PV_SELF_CONSUMPTION``  —— 光伏盈余先给储能充电，光伏不足时才由储能补负荷（§13）
* ``ECONOMIC_OPTIMIZATION``—— 规则型经济优化：按当日电价的低/高分位确定充放窗口（§14）

逐日价格排序窗口调度（§12、§14 的核心修法）
----------------------------------------
``PEAK_VALLEY`` 与 ``ECONOMIC_OPTIMIZATION`` 的价格驱动充放电**不再逐时贪心**，而是
"**先规划、后执行**"：

1. **候选筛选**（与用户设置一致，不放松任何开关）：只有 ``电价 ≤ charge_price_threshold``
   的小时才是充电候选，只有 ``电价 ≥ discharge_price_threshold`` 的小时才是放电候选
   （``ECONOMIC_OPTIMIZATION`` 用当日 25% / 75% 分位代替用户阈值）；
2. **价格排名**：充电候选按价格**升序**（便宜优先充）、放电候选按价格**降序**（贵优先放），
   同价用时间先后稳定排序，保证结果确定性；
3. **循环配对**：把一个自然日切成若干个"放电块"，每块取若干小时，小时数
   ``ceil(可用容量 × η_d ÷ (放电功率 × Δt))`` 由**可用容量、功率上限与 Δt 推导**（不写死）；
   每块再向"位于上一个放电块之后、本块最早小时之前"的充电候选里取**最便宜的若干小时**，
   所需小时数按该块的放电电量反推；只有净差价 ``p_放电 − p_充电 ÷ η_往返 > 0`` 才立项。

为什么必须这么做：逐时贪心会在傍晚平价时段（19–20 点）就把电池充满，等到次日凌晨更便宜的
谷段（0–7 点）时 SOC 已是 100%，**最便宜的充电机会被自己挤掉**。按日价格排名后，充电小时
总是当日最便宜的那几个，谷段天然优先；而"当日没有更便宜的充电小时在其后"的时段（如 19–20 点）
不会被立项，从而消除该缺陷。实测广东三电价案例（谷 0.30 / 平 0.689 / 峰 0.921，1 MWh/500 kW）
年套利收益由 8.5 万元升到 22 万元量级。

近似性与适用边界（**本策略不是全局最优解**，明确声明如下）
--------------------------------------------------------
* 规划尺度是**自然日**：跨日 SOC 结转不在规划模型内。若某日的高价窗口位于当日任何低价候选
  *之前*（例如凌晨尖峰、且谷段落在 22 点之后），该块因"没有更早的充电小时"而不立项，
  此时不会动用前一日结转的电量 —— 这是保守的近似，宁可少赚也不虚构跨日最优。
* 同样地，日末时段（如 23 点谷段）不会为了**次日**高峰而充电：次日凌晨通常有同样便宜或更便宜的
  谷段，日末充电只会挤掉次日更便宜的充电机会（这正是被修复的缺陷本身）。
* "循环配对"是**贪心**式的：先满足最贵的放电块，再依次向后配对，不做全局最优匹配，
  也不考虑电池循环寿命成本、需量电费、辅助服务收益等其他价值流。
* 每个"放电块"内部按价格排名取小时，块内若出现净差价不足的小时，只裁剪块尾，不做重排。
* 因此本模块给出的是**可解释、单调、稳健的规则型调度**；真正的全局最优需要把储能调度写成
  含跨日 SOC 状态变量的线性规划（MILP/LP），属于后续迭代范围，不在本引擎职责内。

能源优先级（V2 §20、§21）
-----------------------
1. ``PV → Load``；2. ``PV → Storage``；3. ``PV → Grid``；4. 余量弃光（§9.3）
储能放电默认只供负荷（``Storage → Load``）；``Storage → Grid`` 仅在
``allow_export=True`` 时发生。电网充电默认关闭，且**单独统计**为 ``grid_charge_ac``，
避免把电网电量误算成"光伏储能"（§21）。

性能（V2 §86、§87）
------------------
逐时循环只做标量算术并写入预分配数组；原因先记为**整数码**，循环结束后一次性映射为中文，
因此 8760 点不会在循环中产生大量 Python 对象。

``PEAK_VALLEY`` + ``allow_grid_charge=False`` 的语义（刻意设计，不是缺陷）
------------------------------------------------------------------------
峰谷套利里的"低价充电"在该组合下**只能靠光伏盈余**实现：谷段电价再低于充电阈值，
也不会从电网买电充储能。储能只能吃"本来要上网的余电"，收益来自
"把低价时段的余电搬到高价时段用"，而不是"低买高卖"。
三个策略都遵守 §20 的固定优先级（光伏 → 负荷 → 储能 → 上网），因此
**光伏盈余充电不受充放电阈值影响**（余电充进电池总是优于低价上网）。
若期望"电网低买高卖"，必须显式打开 ``allow_grid_charge`` 与 ``charge_from_grid``。

动作与原因的一致性（§16，强制不变式）
------------------------------------
每小时都满足 ``action == CHARGE ⟺ charge_ac > 0``、
``action == DISCHARGE ⟺ discharge_ac > 0``，否则 ``action == IDLE``。
因此"想充电但一度电都没充进去"（如谷段但电网充电关闭）记为 ``IDLE`` 并给出
``IDLE_CHARGE_DISABLED`` 的真实原因，**不会**出现"原因说充电、电量却是 0"的小时。

循环性能说明：Pydantic 字段访问与 ndarray 逐元素索引全部移到循环之外，
输入转 Python list、输出累加后一次性转 ndarray；8760 点约 34 ms（优化前约 132 ms），
25 年逐年重算约 0.9 s（§86 单年预算 2000 ms）。

逐日窗口规划的开销（§86、§87）：365 天 × 每天一小段排序与配对，只在**确实存在充放电候选**
（阈值大于 0 或经济优化分位）时才计算；候选为空（如默认阈值 0）时完全跳过，因此默认配置的
逐时循环与规划前**逐位一致**。实测单年 8760 点规划 + 调度仍在 §86 预算内（见测试基准）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

from ..domain.enums import DispatchAction, DispatchStrategy, TariffPeriod
from ..domain.timeseries import StorageDispatchConfig
from ..domain.timeseries_results import DispatchDecision
from . import storage_soc as socmod
from .storage_soc import StorageSeries
from .tariff_series import TariffSeries

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 原因码表（V2 §16）：每小时必须能解释"为什么"
# --------------------------------------------------------------------------- #
R_CHARGE_PV_SURPLUS = "CHARGE_PV_SURPLUS"
R_CHARGE_LOW_PRICE = "CHARGE_LOW_PRICE"
R_CHARGE_GRID_LOW = "CHARGE_GRID_LOW"
R_CHARGE_ECONOMIC_LOW = "CHARGE_ECONOMIC_LOW"
R_DISCHARGE_PEAK = "DISCHARGE_PEAK"
R_DISCHARGE_DEFICIT = "DISCHARGE_DEFICIT"
R_DISCHARGE_ECONOMIC_HIGH = "DISCHARGE_ECONOMIC_HIGH"
R_IDLE_NO_STORAGE = "IDLE_NO_STORAGE"
R_IDLE_SOC_FULL = "IDLE_SOC_FULL"
R_IDLE_SOC_EMPTY = "IDLE_SOC_EMPTY"
R_IDLE_NO_SURPLUS_NOT_PEAK = "IDLE_NO_SURPLUS_NOT_PEAK"
R_IDLE_CHARGE_DISABLED = "IDLE_CHARGE_DISABLED"
R_IDLE_PV_DEFICIT_NOT_ALLOWED = "IDLE_PV_DEFICIT_NOT_ALLOWED"
R_IDLE_NO_DISCHARGE_TARGET = "IDLE_NO_DISCHARGE_TARGET"

#: 原因码 → 中文解释（写入结果与报告，V2 §16）
REASON_LABELS: dict[str, str] = {
    R_CHARGE_PV_SURPLUS: "光伏盈余，优先给储能充电",
    R_CHARGE_LOW_PRICE: "当日低价充电窗口（按价格排名优先谷段），储能充电",
    R_CHARGE_GRID_LOW: "低价时段，按设置从电网充电",
    R_CHARGE_ECONOMIC_LOW: "当日低价时段，经济优化判定充电",
    R_DISCHARGE_PEAK: "高峰电价不低于放电阈值，储能放电",
    R_DISCHARGE_DEFICIT: "光伏不足，储能放电补负荷",
    R_DISCHARGE_ECONOMIC_HIGH: "当日高价时段，经济优化判定放电",
    R_IDLE_NO_STORAGE: "未配置储能，不动作",
    R_IDLE_SOC_FULL: "SOC 已达上限，无法继续充电",
    R_IDLE_SOC_EMPTY: "SOC 已到下限，无法继续放电",
    R_IDLE_NO_SURPLUS_NOT_PEAK: "无光伏盈余且非放电时段，不动作",
    R_IDLE_CHARGE_DISABLED: "当前时段不具备充电条件（未开启电网充电）",
    R_IDLE_PV_DEFICIT_NOT_ALLOWED: "光伏不足但策略不允许主动放电，不动作",
    R_IDLE_NO_DISCHARGE_TARGET: "有放电意愿，但负荷无缺口且未开启储能上网，暂不放电",
}

#: 整数原因码（循环内用，避免逐时创建字符串）
_CODE = {name: i for i, name in enumerate(REASON_LABELS)}
_CODE_LIST = list(REASON_LABELS)


@dataclass
class DispatchOutcome:
    """逐时调度结果（V2 §19 的能源流分解，供能量平衡与报表使用）。"""

    storage: StorageSeries
    load: np.ndarray
    pv: np.ndarray
    pv_to_load: np.ndarray
    pv_to_storage: np.ndarray
    pv_to_grid: np.ndarray
    pv_curtailed: np.ndarray
    grid_to_load: np.ndarray
    grid_to_storage: np.ndarray
    load_from_storage: np.ndarray
    storage_to_grid: np.ndarray
    grid_import: np.ndarray
    grid_export: np.ndarray
    electricity_cost: np.ndarray
    export_revenue: np.ndarray
    storage_revenue: np.ndarray
    total_revenue: np.ndarray
    net_energy_cost: np.ndarray
    price: np.ndarray
    export_price: np.ndarray
    action_codes: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    strategy: DispatchStrategy = DispatchStrategy.PV_SELF_CONSUMPTION

    def decisions(self, timestamps) -> list[DispatchDecision]:
        """生成逐时可解释决策（V2 §16），供 ``CalculationResult.dispatch_results`` 使用。"""
        out: list[DispatchDecision] = []
        for i, ts in enumerate(timestamps):
            out.append(
                DispatchDecision(
                    timestamp=ts,
                    action=DispatchAction(self.action_codes[i]),
                    reason=self.reasons[i],
                    reason_code=self.reason_codes[i],
                    charge_energy=float(self.storage.charge_ac[i]),
                    discharge_energy=float(self.storage.discharge_ac[i]),
                )
            )
        return out


def _day_keys(axis) -> np.ndarray:
    """自然日分组键（``月 × 100 + 日``）。

    时间轴已提供向量化的 ``month`` / ``day`` 数组（``timeseries_engine.TimeAxis``），
    因此这里不再逐点解析 ``datetime.timetuple()``：8760 点从约 4 ms 降到约 0.1 ms，
    且分组结果与"按年积日分组"完全等价（V2 §7）。
    """
    return axis.month.astype(np.int64) * 100 + axis.day.astype(np.int64)


def _economic_thresholds(price: np.ndarray, day_key: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """按"天"计算电价的低/高分位阈值（V2 §14 规则型经济优化）。

    低阈值取当日 25% 分位、高阈值取当日 75% 分位，同一天内保持一致的充放窗口，
    避免逐时反复切换充放电。分位阈值只负责回答"哪些小时**有资格**充放电"，
    资格内部的优先级由 :func:`_plan_daily_windows` 的价格排名决定。
    """
    low = np.empty_like(price)
    high = np.empty_like(price)
    # 快路径：逐日点数相同（小时粒度下就是 24 点）时，一次 ``np.percentile(axis=1)``
    # 即可算完全年分位；否则退化为逐日循环。两条路径结果一致（§118 可重复性）。
    starts, ends = _day_ranges(day_key, price.size)
    counts = ends - starts
    if counts.size and counts.min() == counts.max() and counts[0] > 0:
        matrix = price.reshape(counts.size, int(counts[0]))
        q1, q3 = np.percentile(matrix, [25.0, 75.0], axis=1)
        low = np.repeat(q1, int(counts[0]))
        high = np.repeat(q3, int(counts[0]))
        return low, high
    for s, e in zip(starts.tolist(), ends.tolist()):
        values = price[s:e]
        if values.size == 0:
            continue
        q1, q3 = np.percentile(values, [25.0, 75.0])
        low[s:e] = q1
        high[s:e] = q3
    return low, high


def _day_ranges(day_key: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """自然日的 ``[起, 止)`` 索引区间（时间轴按时间升序，同一天的点必然连续）。"""
    starts = np.concatenate(([0], np.flatnonzero(np.diff(day_key)) + 1))
    ends = np.concatenate((starts[1:], [n]))
    return starts, ends


def _plan_daily_windows(
    *,
    price: np.ndarray,
    day_key: np.ndarray,
    charge_candidate: np.ndarray,
    discharge_candidate: np.ndarray,
    usable_discharge_ac: float,
    charge_energy_per_hour: float,
    discharge_energy_per_hour: float,
    round_trip_efficiency: float,
    epsilon: float = 1e-12,
) -> tuple[np.ndarray, np.ndarray]:
    """逐日按价格排名规划充/放电窗口（V2 §12、§14，先规划后执行）。

    输入
    ----
    * ``charge_candidate`` / ``discharge_candidate``：布尔数组，回答"哪些小时**有资格**充电／
      放电"。资格完全由用户阈值（或经济优化分位）决定，**规划不放松任何开关**；
    * ``usable_discharge_ac``：SOC 区间内可放出的交流电量 ``可用容量 × η_d``；
    * ``charge_energy_per_hour`` / ``discharge_energy_per_hour``：每小时的交流电量上限
      ``功率 × Δt``（§11、§75）；
    * ``round_trip_efficiency``：``η_charge × η_discharge``，用于判断净差价。

    输出
    ----
    ``(allow_charge, allow_discharge)``：两个布尔数组，逐时执行阶段只有落在窗口内的
    价格驱动充放电才会发生（光伏盈余充电不受窗口限制，见模块 docstring）。

    算法（每个自然日独立执行，共 365 次小循环）
    ----------------------------------------
    1. 取当日**最贵**的 ``ceil(可用容量 × η_d ÷ (放电功率 × Δt))`` 个小时作为第一个放电块
       （小时数由容量、功率与 Δt 推出，不写死；同价按时间稳定排序）；
    2. 向"位于上一个放电块之后、本块最早小时之前"的候选充电小时里取最便宜的若干小时，
       数量按该块可放出的电量反推：``ceil(放电电量 ÷ η_往返 ÷ (充电功率 × Δt))``；
    3. 只有块内最低放电价 ``p_d`` 与所选充电价 ``p_c`` 满足 ``p_c ÷ η_往返 < p_d`` 才立项，
       否则整块跳过 —— 这是"不为充而充"的经济闸门；
    4. 立项后按实际选中的充电量裁剪放电块，然后对剩下的放电候选重复 1–3。

    实现与复杂度（§86、§87）：先一次性把电价与候选取下标转成 Python list，再用单指针
    顺序切出每天的子列表 —— 每天只做小规模 ``sorted`` 与配对，**不调用任何逐日 NumPy 函数**，
    因此 365 天全年规划的额外开销约为个位数毫秒（见测试 ``test_single_year_dispatch_within_budget``）。
    """
    n = price.size
    allow_charge = np.zeros(n, dtype=bool)
    allow_discharge = np.zeros(n, dtype=bool)
    if n == 0 or charge_energy_per_hour <= 0.0 or discharge_energy_per_hour <= 0.0:
        return allow_charge, allow_discharge

    # 一个"完整循环"需要的放电小时数：容量 / 功率 / Δt 三者共同决定（§11、§75）
    cycle_discharge_hours = max(
        1, int(math.ceil(usable_discharge_ac / discharge_energy_per_hour))
    )

    starts, ends = _day_ranges(day_key, n)
    price_l = price.tolist()
    # 候选取下标（升序，天然按时间排列），一次遍历合并到每天
    cand_all_c = [int(h) for h in np.flatnonzero(charge_candidate)]
    cand_all_d = [int(h) for h in np.flatnonzero(discharge_candidate)]
    if not cand_all_c or not cand_all_d:
        return allow_charge, allow_discharge

    ci = 0
    di = 0
    nc = len(cand_all_c)
    nd = len(cand_all_d)
    for s, e in zip(starts.tolist(), ends.tolist()):
        day_c: list[int] = []
        while ci < nc and cand_all_c[ci] < e:
            h = cand_all_c[ci] - s
            ci += 1
            if h >= 0:
                day_c.append(h)
        day_d: list[int] = []
        while di < nd and cand_all_d[di] < e:
            h = cand_all_d[di] - s
            di += 1
            if h >= 0:
                day_d.append(h)
        if not day_c or not day_d:
            continue
        p = price_l[s:e]
        # 价格排名：便宜的充电候选优先（升序），贵的放电候选优先（降序）；
        # 同价用稳定排序保留时间先后，保证同一输入的结果确定（§118 可重复性）。
        cand_c = sorted(day_c, key=p.__getitem__)
        cand_d = sorted(day_d, key=lambda h: -p[h])

        used: set[int] = set()
        lower = -1  # 上一个"已立项放电块"的最晚小时（相对下标），保证充电在放电之前
        pos = 0
        while pos < len(cand_d):
            block = cand_d[pos : pos + cycle_discharge_hours]
            pos += cycle_discharge_hours
            floor_price = min(p[h] for h in block)
            first_hour = min(block)
            avail = [h for h in cand_c if lower < h < first_hour and h not in used]
            if not avail:
                continue
            profitable = [
                h for h in avail if p[h] / round_trip_efficiency < floor_price - epsilon
            ]
            if not profitable:
                continue
            block_energy = min(len(block) * discharge_energy_per_hour, usable_discharge_ac)
            need = max(
                1,
                int(
                    math.ceil(
                        block_energy / round_trip_efficiency / charge_energy_per_hour
                    )
                ),
            )
            chosen = profitable[:need]
            # 按实际选中的充电量裁剪放电块；多充的电量由后续放电块继续使用，不会凭空消失
            served = int(
                math.ceil(
                    len(chosen)
                    * charge_energy_per_hour
                    * round_trip_efficiency
                    / discharge_energy_per_hour
                )
            )
            keep = block[: max(1, min(len(block), served))]
            for h in chosen:
                used.add(h)
                allow_charge[s + h] = True
            for h in keep:
                allow_discharge[s + h] = True
            lower = max(keep)
    return allow_charge, allow_discharge


def dispatch(
    *,
    load: np.ndarray,
    pv: np.ndarray,
    tariff: TariffSeries,
    axis,
    config: StorageDispatchConfig,
    storage_capacity_kwh: float,
    storage_power_kw: float,
    storage_degradation_rate: float = 0.0,
    replacement_year: int | None = None,
    year_index: int = 1,
) -> DispatchOutcome:
    """执行逐时调度，返回完整能源流（V2 §11–§21）。

    :param load: 逐时负荷电量 kWh（``load_profile.resolve_load``）
    :param pv: 逐时可用光伏发电量 kWh（``pv_profile.resolve_pv``）
    :param tariff: 逐时电价（``tariff_series.resolve_tariff``）
    :param axis: 时间轴
    :param config: 储能调度配置（策略、SOC 上下限、效率、阈值、开关）
    :param storage_capacity_kwh: 储能额定容量（含本年衰减，见 §25）
    :param storage_power_kw: 储能额定功率
    :param year_index: 第几个运营年（用于储能容量衰减与更换，§25、§26）
    """
    n = len(axis.timestamps)
    dt = axis.delta_hours

    price = np.asarray(tariff.price, dtype=float)
    export_price = np.asarray(tariff.export_price, dtype=float)

    capacity = socmod.capacity_for_year(
        storage_capacity_kwh,
        storage_degradation_rate,
        year_index,
        replacement_year=replacement_year,
        replacement_capacity_kwh=storage_capacity_kwh,
    )
    usable = socmod.usable_energy(capacity, config.soc_min, config.soc_max)

    has_storage = capacity > 0.0 and storage_power_kw > 0.0
    max_charge_power = config.max_charge_power or storage_power_kw
    max_discharge_power = config.max_discharge_power or storage_power_kw

    eta_c = float(config.charge_efficiency)
    eta_d = float(config.discharge_efficiency)

    action_map = {
        DispatchAction.CHARGE.value: 0,
        DispatchAction.DISCHARGE.value: 1,
        DispatchAction.IDLE.value: 2,
    }
    action_names = [
        DispatchAction.CHARGE.value,
        DispatchAction.DISCHARGE.value,
        DispatchAction.IDLE.value,
    ]

    strategy = config.strategy
    charge_threshold = float(config.charge_price_threshold)
    discharge_threshold = float(config.discharge_price_threshold)

    # ---- 逐日价格窗口规划（V2 §12、§14）：先规划、后执行 ----
    # 只有在"确实存在充放电候选"时才付出规划开销：
    #   * 用户阈值为 0（默认值）⇒ 没有任何候选 ⇒ 完全跳过，逐时循环与规划前**逐位一致**；
    #   * 无储能 / 功率为 0 ⇒ 本来就不动作，同样跳过；
    #   * ``PV_SELF_CONSUMPTION`` 不按价格决策，规划对它没有意义（§13 语义不变）。
    plan_charge = np.zeros(n, dtype=bool)
    plan_discharge = np.zeros(n, dtype=bool)
    if has_storage and strategy is not DispatchStrategy.PV_SELF_CONSUMPTION:
        day_key = _day_keys(axis)
        if strategy is DispatchStrategy.ECONOMIC_OPTIMIZATION:
            # 经济优化：当日 25% / 75% 分位是"资格线"（§14），资格内部再按价格排名定优先级
            econ_low, econ_high = _economic_thresholds(price, day_key)
            charge_candidate = price <= econ_low + 1e-12
            discharge_candidate = price >= econ_high - 1e-12
        else:  # PEAK_VALLEY：用户阈值即资格线（§12）
            charge_candidate = (charge_threshold > 0.0) & (price <= charge_threshold + 1e-12)
            discharge_candidate = (discharge_threshold > 0.0) & (
                price >= discharge_threshold - 1e-12
            )
        if charge_candidate.any() and discharge_candidate.any():
            plan_charge, plan_discharge = _plan_daily_windows(
                price=price,
                day_key=day_key,
                charge_candidate=charge_candidate,
                discharge_candidate=discharge_candidate,
                usable_discharge_ac=usable * eta_d,
                charge_energy_per_hour=max_charge_power * dt,
                discharge_energy_per_hour=max_discharge_power * dt,
                round_trip_efficiency=eta_c * eta_d,
            )

    export_allowed = float(np.max(export_price)) > 0.0
    current_soc = float(config.initial_soc)
    soc_min = float(config.soc_min)
    soc_max = float(config.soc_max)

    # ---- 性能（V2 §86、§87）：循环内只做 Python 标量运算 ----
    # 这里**故意**用逐时标量循环而非向量化：SOC 递推带上下限裁剪（§10.5、§11），
    # 本质是顺序状态机，无法无损向量化。因此把全部开销移到循环之外：
    #   1) 所有 ``config.*`` 字段预先取到局部变量 —— Pydantic 属性访问是 µs 级的，
    #      循环内每多访问一次，8760 点就多几毫秒；
    #   2) 输入数组转成 Python list，避免 ndarray 逐元素索引的固定开销；
    #   3) 输出先累加到 list，循环结束后一次性转 ndarray。
    # 复杂度 O(n)，实测 8760 点约 20~30 ms（优化前约 132 ms）。
    load_l = load.tolist()
    pv_l = pv.tolist()
    plan_charge_l = plan_charge.tolist()
    plan_discharge_l = plan_discharge.tolist()

    is_self_consumption = strategy is DispatchStrategy.PV_SELF_CONSUMPTION
    is_peak_valley = strategy is DispatchStrategy.PEAK_VALLEY
    allow_arbitrage = bool(config.allow_arbitrage)
    charge_from_pv = bool(config.charge_from_pv)
    grid_charge_on = bool(config.allow_grid_charge and config.charge_from_grid)
    export_storage_on = bool(config.allow_export)

    pv_to_load_l: list[float] = []
    pv_to_storage_l: list[float] = []
    pv_to_grid_l: list[float] = []
    pv_curtailed_l: list[float] = []
    grid_to_load_l: list[float] = []
    grid_to_storage_l: list[float] = []
    load_from_storage_l: list[float] = []
    storage_to_grid_l: list[float] = []
    charge_l: list[float] = []
    discharge_l: list[float] = []
    soc_start_l: list[float] = []
    soc_end_l: list[float] = []
    action_code_l: list[int] = []
    reason_code_l: list[int] = []

    code_of = _CODE
    charge_code = action_map[DispatchAction.CHARGE.value]
    discharge_code = action_map[DispatchAction.DISCHARGE.value]
    idle_code = action_map[DispatchAction.IDLE.value]

    for t in range(n):
        soc_start_v = current_soc

        p_load = load_l[t]
        p_pv = pv_l[t]

        # ---- 1. 光伏优先供负荷（§20）----
        direct = p_pv if p_pv < p_load else p_load
        surplus = p_pv - direct
        deficit = p_load - direct
        pv_to_load_v = direct

        charge = 0.0
        discharge = 0.0
        pv_to_storage_v = 0.0
        grid_to_storage_v = 0.0
        load_from_storage_v = 0.0
        storage_to_grid_v = 0.0
        pv_to_grid_v = 0.0
        pv_curtailed_v = 0.0
        grid_to_load_v = 0.0
        reason = R_IDLE_NO_SURPLUS_NOT_PEAK
        action_code = idle_code

        if not has_storage:
            reason = R_IDLE_NO_STORAGE
            if surplus > 1e-12:
                if export_allowed:
                    pv_to_grid_v = surplus
                else:
                    pv_curtailed_v = surplus
            grid_to_load_v = deficit
        else:
            # 充放电可用量用**标量**内联计算，等价于
            # ``storage_soc.max_charge_energy_ac`` / ``max_discharge_energy_ac``，
            # 但避免在 8760 次循环里反复构造临时数组（V2 §87）。
            #   充电：P×η_c×Δt ≤ (soc_max−soc)×capacity 且 P ≤ max_charge_power
            #   放电：P÷η_d×Δt ≤ (soc−soc_min)×capacity 且 P ≤ max_discharge_power
            room_ac = min(
                max_charge_power * dt,
                (soc_max - current_soc) * capacity / eta_c,
            )
            available_ac = min(
                max_discharge_power * dt,
                (current_soc - soc_min) * capacity * eta_d,
            )

            want_charge = False
            want_discharge = False
            charge_reason = R_CHARGE_PV_SURPLUS
            discharge_reason = R_DISCHARGE_PEAK

            if is_self_consumption:
                # §13：盈余充电；光伏不足时才由储能补负荷
                if surplus > 1e-12 and charge_from_pv:
                    want_charge, charge_reason = True, R_CHARGE_PV_SURPLUS
                elif deficit > 1e-12:
                    want_discharge, discharge_reason = True, R_DISCHARGE_DEFICIT
                else:
                    reason = R_IDLE_PV_DEFICIT_NOT_ALLOWED

            elif is_peak_valley:
                # §12：只有落在**当日价格排名窗口**内的小时才按价格充放电（先规划后执行）。
                # 窗口已保证"便宜优先充、贵优先放"，且谷段不会被傍晚平价充电挤掉。
                low_hit = plan_charge_l[t]
                high_hit = plan_discharge_l[t]
                if low_hit and allow_arbitrage:
                    want_charge, charge_reason = True, (
                        R_CHARGE_PV_SURPLUS if surplus > 1e-12 else R_CHARGE_LOW_PRICE
                    )
                elif high_hit and (deficit > 1e-12 or export_storage_on):
                    # 峰段放电：先补负荷缺口，余量在 allow_export 开启时向电网放电（§20）
                    want_discharge, discharge_reason = True, R_DISCHARGE_PEAK
                elif surplus > 1e-12 and charge_from_pv:
                    want_charge, charge_reason = True, R_CHARGE_PV_SURPLUS

            else:  # ECONOMIC_OPTIMIZATION
                # §14：当日分位阈值给出资格，价格排名给出窗口内的优先级
                if surplus > 1e-12 and charge_from_pv:
                    want_charge, charge_reason = True, R_CHARGE_PV_SURPLUS
                elif plan_charge_l[t] and allow_arbitrage:
                    want_charge, charge_reason = True, R_CHARGE_ECONOMIC_LOW
                elif plan_discharge_l[t] and (deficit > 1e-12 or export_storage_on):
                    want_discharge, discharge_reason = True, R_DISCHARGE_ECONOMIC_HIGH

            # ---- 充电 ----
            # §16 不变式：``action == CHARGE ⟺ charge > 0``。
            # 因此先把实际充入的电量算出来，再据其决定动作与原因；
            # "想充电但一度电都没充进去"必须记为 IDLE 并说明真正的阻塞原因。
            if want_charge and room_ac > 1e-12:
                from_pv = min(surplus, room_ac) if charge_from_pv else 0.0
                remaining_room = room_ac - from_pv
                grid_charge = 0.0
                if remaining_room > 1e-12 and grid_charge_on:
                    # 电网充电量受 SOC 与功率约束，二者已体现在 room_ac 内（V2 §11、§21）
                    grid_charge = remaining_room
                    if from_pv <= 1e-12:
                        charge_reason = R_CHARGE_GRID_LOW
                charge = from_pv + grid_charge
                pv_to_storage_v = from_pv
                grid_to_storage_v = grid_charge
                surplus -= from_pv
                if charge > 1e-12:
                    action_code = charge_code
                    reason = charge_reason
                else:
                    # 典型情形：峰谷套利在谷段想充电，但 allow_grid_charge=False
                    # 且本时段没有光伏盈余 —— 一度电也充不进去（§16 要求如实说明）。
                    action_code = idle_code
                    reason = R_IDLE_SOC_FULL if room_ac <= 1e-12 else R_IDLE_CHARGE_DISABLED
            elif want_charge:
                action_code = idle_code
                reason = R_IDLE_SOC_FULL

            # ---- 放电 ----
            if want_discharge and available_ac > 1e-12 and action_code == idle_code:
                to_load = min(deficit, available_ac)
                remaining = available_ac - to_load
                to_grid = remaining if (remaining > 1e-12 and export_storage_on) else 0.0
                discharge = to_load + to_grid
                load_from_storage_v = to_load
                storage_to_grid_v = to_grid
                deficit -= to_load
                if discharge > 1e-12:
                    action_code = discharge_code
                    reason = discharge_reason
                else:
                    # 峰段有放电意愿，但负荷无缺口且未开启储能上网 —— 无处可放
                    action_code = idle_code
                    reason = R_IDLE_NO_DISCHARGE_TARGET
            elif want_discharge and available_ac <= 1e-12 and action_code == idle_code:
                action_code = idle_code
                reason = R_IDLE_SOC_EMPTY

            # ---- 余电上网 / 弃光（§9.3、§20）----
            if surplus > 1e-12:
                if export_allowed:
                    pv_to_grid_v = surplus
                else:
                    pv_curtailed_v = surplus

            if deficit > 1e-12:
                grid_to_load_v = deficit

        pv_to_load_l.append(pv_to_load_v)
        pv_to_storage_l.append(pv_to_storage_v)
        pv_to_grid_l.append(pv_to_grid_v)
        pv_curtailed_l.append(pv_curtailed_v)
        grid_to_load_l.append(grid_to_load_v)
        grid_to_storage_l.append(grid_to_storage_v)
        load_from_storage_l.append(load_from_storage_v)
        storage_to_grid_l.append(storage_to_grid_v)
        charge_l.append(charge)
        discharge_l.append(discharge)

        # ---- SOC 递推（§10.5）----
        #   ΔE = charge×η_c − discharge÷η_d（与 storage_soc.internal_energy_delta 等价，标量内联）
        if has_storage and capacity > 0.0:
            delta = charge * eta_c - discharge / eta_d
            current_soc = min(max(current_soc + delta / capacity, soc_min), soc_max)
        soc_start_l.append(soc_start_v)
        soc_end_l.append(current_soc)

        action_code_l.append(action_code)
        reason_code_l.append(code_of[reason])

    pv_to_load = np.array(pv_to_load_l)
    pv_to_storage = np.array(pv_to_storage_l)
    pv_to_grid = np.array(pv_to_grid_l)
    pv_curtailed = np.array(pv_curtailed_l)
    grid_to_load = np.array(grid_to_load_l)
    grid_to_storage = np.array(grid_to_storage_l)
    load_from_storage = np.array(load_from_storage_l)
    storage_to_grid = np.array(storage_to_grid_l)
    charge_ac = np.array(charge_l)
    discharge_ac = np.array(discharge_l)
    soc_start = np.array(soc_start_l)
    soc_end = np.array(soc_end_l)
    action_code = np.array(action_code_l, dtype=np.int64)
    reason_code = np.array(reason_code_l, dtype=np.int64)

    # ---- 经济量（§29、§31）----
    grid_import = grid_to_load + grid_to_storage
    grid_export = pv_to_grid + storage_to_grid
    electricity_cost = grid_import * price
    export_revenue = grid_export * export_price

    # 储能收益严格按逐时实际充放电计价（V2 §29）：
    #   放电侧：供负荷部分替代的是购电价，上网部分按上网电价计收；
    #   充电侧：电网充电按购电价计成本，光伏充电按**上网电价的机会成本**计，
    #           否则会把"本该卖掉的电"算成零成本的免费能源。
    # 明确禁止简化为"放电量 × 峰谷价差"。
    charge_cost = grid_to_storage * price + pv_to_storage * export_price
    discharge_value = load_from_storage * price + storage_to_grid * export_price
    storage_revenue = discharge_value - charge_cost
    total_revenue = export_revenue + storage_revenue
    net_energy_cost = electricity_cost - export_revenue

    storage_series = StorageSeries(
        capacity_kwh=capacity,
        usable_energy_kwh=usable,
        soc_start=soc_start,
        soc_end=soc_end,
        charge_ac=charge_ac,
        discharge_ac=discharge_ac,
        grid_charge_ac=grid_to_storage.copy(),
        pv_charge_ac=pv_to_storage.copy(),
    )

    logger.debug(
        "调度完成：策略=%s 年序=%d 点数=%d 充电=%.1f kWh 放电=%.1f kWh 电网充电=%.1f kWh",
        config.strategy.value,
        year_index,
        n,
        float(charge_ac.sum()),
        float(discharge_ac.sum()),
        float(grid_to_storage.sum()),
    )

    return DispatchOutcome(
        storage=storage_series,
        load=np.asarray(load, dtype=float),
        pv=np.asarray(pv, dtype=float),
        pv_to_load=pv_to_load,
        pv_to_storage=pv_to_storage,
        pv_to_grid=pv_to_grid,
        pv_curtailed=pv_curtailed,
        grid_to_load=grid_to_load,
        grid_to_storage=grid_to_storage,
        load_from_storage=load_from_storage,
        storage_to_grid=storage_to_grid,
        grid_import=grid_import,
        grid_export=grid_export,
        electricity_cost=electricity_cost,
        export_revenue=export_revenue,
        storage_revenue=storage_revenue,
        total_revenue=total_revenue,
        net_energy_cost=net_energy_cost,
        price=price,
        export_price=export_price,
        action_codes=[action_names[a] for a in action_code.tolist()],
        reason_codes=[_CODE_LIST[c] for c in reason_code.tolist()],
        reasons=[REASON_LABELS[_CODE_LIST[c]] for c in reason_code.tolist()],
        strategy=config.strategy,
    )
