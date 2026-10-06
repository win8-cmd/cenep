"""V2 储能调度与能量平衡测试（V2 §16、§19、§20、§21、§73–§81）。

覆盖规范列为 P0 的强制测试项：能量守恒（§73）、SOC 边界（§74）、
充放电功率上限（§75）、效率方向（§76）、无储能退化（§77）、无光伏退化（§78）、
PV 与负荷的三种相对关系（§79）、盈余/满/空四种工况（§80）、峰谷套利（§81），
以及 §16 的"每小时必须可解释"与 §21 的"电网充电单独统计"。

另含 **逐日价格排序窗口调度**的缺陷回归（§12、§14）：修复前 ``PEAK_VALLEY`` 是逐时贪心，
会在傍晚平价时段先把电池充满，导致次日凌晨谷段一度电都充不进（全年谷段充电量
仅占 0.14%），全年套利收益被低估约 60%。该缺陷的全部回归断言见
``TestDailyPriceWindowPlanning``。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from cenep.calculation import dispatch_engine, energy_balance, tariff_series
from cenep.calculation.dispatch_engine import REASON_LABELS
from cenep.calculation.errors import ValidationError
from cenep.calculation.tariff_series import TariffSeries
from cenep.calculation.timeseries_engine import build_time_axis
from cenep.domain.enums import DispatchAction, DispatchStrategy, TariffPeriod
from cenep.domain.timeseries import (
    StorageDispatchConfig,
    TariffProfile,
    TariffSeriesConfig,
    TimePeriodRule,
)
from cenep.domain.timeseries import LoadProfile, LoadProfileConfig, TimeSeriesPoint
from cenep.domain.enums import LoadProfileMode

YEAR = 2025


def _axis():
    return build_time_axis(YEAR)


def _tariff(peak: float = 1.0, flat: float = 0.65, valley: float = 0.35, export: float = 0.35):
    """谷 00–07、平 08–10/15–17/22–23、峰 11–14/18–21 的简单 TOU。"""
    from cenep.calculation import tariff_series

    rules = [
        TimePeriodRule(period=TariffPeriod.VALLEY, hours=list(range(0, 8))),
        TimePeriodRule(period=TariffPeriod.FLAT, hours=[8, 9, 10, 15, 16, 17, 22, 23]),
        TimePeriodRule(period=TariffPeriod.PEAK, hours=[11, 12, 13, 14, 18, 19, 20, 21]),
    ]
    profile = TariffProfile(
        peak_price=peak, flat_price=flat, valley_price=valley, export_price=export,
        time_periods=rules,
    )
    return tariff_series.resolve_tariff(TariffSeriesConfig(profile=profile), _axis(), 1)


def _flat_arrays(axis, load_value: float, pv_value: float):
    n = len(axis.timestamps)
    return np.full(n, load_value, dtype=float), np.full(n, pv_value, dtype=float)


def _run(load, pv, cfg, tariff=None, capacity=1000.0, power=500.0):
    axis = _axis()
    out = dispatch_engine.dispatch(
        load=load, pv=pv, tariff=tariff or _tariff(), axis=axis, config=cfg,
        storage_capacity_kwh=capacity, storage_power_kw=power,
    )
    return axis, out


def _cfg(strategy=DispatchStrategy.PV_SELF_CONSUMPTION, **kw) -> StorageDispatchConfig:
    return StorageDispatchConfig(strategy=strategy, **kw)


# --------------------------------------------------------------------------- #
# V2 §19、§73 能量守恒（最高级别测试条件）
# --------------------------------------------------------------------------- #
class TestEnergyBalance:
    @pytest.mark.parametrize("strategy", list(DispatchStrategy))
    def test_balance_within_tolerance(self, strategy):
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 150.0)
        _, out = _run(load, pv, _cfg(strategy))
        b = energy_balance.check_balance(out, 1e-6)
        assert b.is_balanced is True
        assert b.max_hourly_error < 1e-6

    def test_balance_identity_terms(self):
        """逐项校验 §19 的等式两侧。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 300.0, 250.0)
        _, out = _run(load, pv, _cfg())
        supply = energy_balance.supply_array(out)
        demand = energy_balance.demand_array(out)
        assert np.allclose(supply, demand, atol=1e-9)
        assert np.allclose(out.pv, out.pv_to_load + out.pv_to_storage + out.pv_to_grid + out.pv_curtailed)
        assert np.allclose(out.load, out.pv_to_load + out.grid_to_load + out.load_from_storage)
        assert np.allclose(out.grid_import, out.grid_to_load + out.grid_to_storage)
        assert np.allclose(out.grid_export, out.pv_to_grid + out.storage_to_grid)

    def test_randomized_balance_100_cases(self):
        """§73：随机 100 组 PV/负荷/储能/电价组合，误差必须 < 1e-6。"""
        rng = np.random.default_rng(20251006)
        axis = _axis()
        n = len(axis.timestamps)
        for _ in range(100):
            load = rng.uniform(0.0, 400.0, size=n)
            pv = rng.uniform(0.0, 500.0, size=n)
            capacity = float(rng.uniform(0.0, 3000.0))
            power = float(rng.uniform(0.0, 1000.0))
            allow_grid_charge = bool(rng.integers(0, 2))
            cfg = StorageDispatchConfig(
                strategy=DispatchStrategy(rng.choice(list(DispatchStrategy))),
                allow_grid_charge=allow_grid_charge,
                charge_from_grid=allow_grid_charge,
                allow_export=bool(rng.integers(0, 2)),
                soc_min=float(rng.uniform(0.0, 0.3)),
                soc_max=float(rng.uniform(0.7, 1.0)),
                initial_soc=0.5,
            )
            out = dispatch_engine.dispatch(
                load=load, pv=pv, tariff=_tariff(), axis=axis, config=cfg,
                storage_capacity_kwh=capacity, storage_power_kw=power,
            )
            supply = energy_balance.supply_array(out)
            demand = energy_balance.demand_array(out)
            err = float(np.max(np.abs(supply - demand)))
            assert err < 1e-6, f"随机工况能量不平衡：{err}"

    def test_check_balance_raises_on_violation(self):
        """人为制造不平衡：校验必须判定失败，而不是放过。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 100.0, 100.0)
        _, out = _run(load, pv, _cfg())
        out.pv_curtailed = out.pv_curtailed + 1.0  # 破坏守恒
        with pytest.raises(ValidationError, match="能量平衡校验失败"):
            energy_balance.check_balance(out)


# --------------------------------------------------------------------------- #
# V2 §74、§75、§76 SOC 与功率、效率
# --------------------------------------------------------------------------- #
class TestSocAndPowerLimits:
    @pytest.mark.parametrize("strategy", list(DispatchStrategy))
    def test_soc_within_bounds(self, strategy):
        """§74：SOC 必须始终落在 [soc_min, soc_max]。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 250.0, 200.0)
        cfg = _cfg(strategy, soc_min=0.2, soc_max=0.9, initial_soc=0.5)
        _, out = _run(load, pv, cfg)
        assert out.storage.soc_end.min() >= 0.2 - 1e-9
        assert out.storage.soc_end.max() <= 0.9 + 1e-9

    def test_power_limits(self):
        """§75：充放电功率不得超过上限。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 400.0, 300.0)
        _, out = _run(load, pv, _cfg(), power=120.0)
        assert out.storage.charge_ac.max() <= 120.0 + 1e-9
        assert out.storage.discharge_ac.max() <= 120.0 + 1e-9

    def test_charge_efficiency_direction(self):
        """§76：充电 100 kWh(AC) 时，SOC 上升应等于 100×η_capacity 的换算值。"""
        axis = _axis()
        n = len(axis.timestamps)
        load = np.zeros(n)
        pv = np.full(n, 100.0)  # 全部盈余用于充电
        cfg = _cfg(
            DispatchStrategy.PV_SELF_CONSUMPTION,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            initial_soc=0.0,
            soc_min=0.0,
            soc_max=1.0,
        )
        _, out = _run(load, pv, cfg, capacity=1000.0, power=100.0)
        charged = float(out.storage.charge_ac[0])
        # 首小时：SOC 从 0 上升，受 max_charge_power=100 kW × 1h 限制
        assert charged == pytest.approx(100.0, rel=1e-9)
        assert out.storage.soc_end[0] == pytest.approx(100.0 * 0.9 / 1000.0, rel=1e-9)

    def test_discharge_efficiency_direction(self):
        """放电方向：AC 放电 100 kWh 需消耗 100÷η_d 的内部电量。"""
        axis = _axis()
        n = len(axis.timestamps)
        load = np.full(n, 100.0)
        pv = np.zeros(n)
        cfg = _cfg(
            DispatchStrategy.PV_SELF_CONSUMPTION,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            initial_soc=1.0,
            soc_min=0.0,
            soc_max=1.0,
        )
        _, out = _run(load, pv, cfg, capacity=1000.0, power=100.0)
        discharge = float(out.storage.discharge_ac[0])
        assert discharge == pytest.approx(100.0, rel=1e-9)
        assert out.storage.soc_end[0] == pytest.approx(1.0 - 100.0 / 0.9 / 1000.0, rel=1e-9)


# --------------------------------------------------------------------------- #
# V2 §77、§78 退化场景
# --------------------------------------------------------------------------- #
class TestDegenerateCases:
    def test_no_storage_degenerates(self):
        """§77：储能容量为 0 时，全部负荷由电网 + 光伏承担，不得凭空产生储能电量。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 150.0)
        _, out = _run(load, pv, _cfg(), capacity=0.0, power=0.0)
        assert out.storage.charge_ac.sum() == 0.0
        assert out.storage.discharge_ac.sum() == 0.0
        assert np.allclose(out.grid_to_load, load - pv)
        assert out.reason_codes[0] == "IDLE_NO_STORAGE"

    def test_no_pv_degenerates_to_grid_only(self):
        """§78：无光伏时退化为纯电网供电。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 0.0)
        _, out = _run(load, pv, _cfg())
        assert np.allclose(out.grid_to_load, load)
        assert out.pv_to_load.sum() == 0.0
        assert out.grid_export.sum() == 0.0
        b = energy_balance.check_balance(out)
        assert b.pv_generation == 0.0

    def test_no_pv_and_no_storage(self):
        axis = _axis()
        load, pv = _flat_arrays(axis, 150.0, 0.0)
        _, out = _run(load, pv, _cfg(), capacity=0.0, power=0.0)
        assert np.allclose(out.grid_import, load)


# --------------------------------------------------------------------------- #
# V2 §79、§80 PV 与负荷的相对关系、储能工况
# --------------------------------------------------------------------------- #
class TestPvLoadRelations:
    def test_pv_greater_than_load(self):
        """§79：PV > Load → 负荷全由光伏覆盖，盈余充电/上网。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 50.0, 100.0)
        cfg = _cfg(DispatchStrategy.PV_SELF_CONSUMPTION, initial_soc=0.0, soc_min=0.0)
        _, out = _run(load, pv, cfg, capacity=2000.0, power=500.0)
        assert np.allclose(out.pv_to_load, load)
        assert out.grid_to_load.sum() == 0.0
        assert out.pv_to_storage.sum() > 0.0

    def test_pv_less_than_load(self):
        """§79：PV < Load → 缺口由储能/电网补。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 300.0, 100.0)
        cfg = _cfg(DispatchStrategy.PV_SELF_CONSUMPTION, initial_soc=0.8)
        _, out = _run(load, pv, cfg, capacity=2000.0, power=500.0)
        assert np.allclose(out.pv_to_load, pv)
        assert out.load_from_storage.sum() > 0.0
        assert np.allclose(out.grid_to_load + out.load_from_storage, load - pv)

    def test_pv_equals_load(self):
        """§79：PV = Load → 无盈余、无缺口，储能不动作。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 150.0, 150.0)
        _, out = _run(load, pv, _cfg(DispatchStrategy.PV_SELF_CONSUMPTION))
        assert np.allclose(out.pv_to_load, load)
        assert out.storage.charge_ac.sum() == 0.0
        assert out.storage.discharge_ac.sum() == 0.0
        assert out.grid_import.sum() == 0.0

    def test_storage_full_stops_charging(self):
        """§80：SOC 满 → 停止充电并给出可解释原因。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 0.0, 100.0)
        cfg = _cfg(DispatchStrategy.PV_SELF_CONSUMPTION, initial_soc=1.0, soc_max=1.0, soc_min=0.0)
        _, out = _run(load, pv, cfg, capacity=50.0, power=100.0)
        assert out.storage.charge_ac.sum() == pytest.approx(0.0, abs=1e-9)
        assert "IDLE_SOC_FULL" in out.reason_codes

    def test_storage_empty_stops_discharging(self):
        """§80：SOC 空 → 停止放电并给出可解释原因。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 100.0, 0.0)
        cfg = _cfg(
            DispatchStrategy.PV_SELF_CONSUMPTION, initial_soc=0.0, soc_min=0.0, soc_max=1.0
        )
        _, out = _run(load, pv, cfg, capacity=100.0, power=100.0)
        assert out.storage.discharge_ac.sum() == pytest.approx(0.0, abs=1e-9)
        assert "IDLE_SOC_EMPTY" in out.reason_codes
        assert np.allclose(out.grid_to_load, load)


# --------------------------------------------------------------------------- #
# V2 §81 峰谷套利
# --------------------------------------------------------------------------- #
class TestPeakValleyArbitrage:
    def test_valley_charge_peak_discharge(self):
        """§81：谷价 0.35 < 阈值 0.40 → 充电；峰价 1.00 > 阈值 0.90 → 放电。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 0.0)
        cfg = _cfg(
            DispatchStrategy.PEAK_VALLEY,
            allow_grid_charge=True,
            charge_from_grid=True,
            charge_price_threshold=0.40,
            discharge_price_threshold=0.90,
            initial_soc=0.5,
            soc_min=0.0,
        )
        _, out = _run(load, pv, cfg, capacity=1000.0, power=200.0)

        valley_hours = set(range(0, 8))
        peak_hours = {11, 12, 13, 14, 18, 19, 20, 21}
        charge_in_valley = sum(
            out.storage.charge_ac[i]
            for i, ts in enumerate(axis.timestamps)
            if ts.hour in valley_hours
        )
        discharge_in_peak = sum(
            out.storage.discharge_ac[i]
            for i, ts in enumerate(axis.timestamps)
            if ts.hour in peak_hours
        )
        assert charge_in_valley > 0.0
        assert discharge_in_peak > 0.0
        assert charge_in_valley == pytest.approx(out.storage.charge_ac.sum(), rel=1e-9)
        assert discharge_in_peak == pytest.approx(out.storage.discharge_ac.sum(), rel=1e-9)

    def test_thresholds_gate_action(self):
        """阈值设到不可能命中时，储能不应动作。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 0.0)
        cfg = _cfg(
            DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=0.01,
            discharge_price_threshold=99.0,
            charge_from_pv=False,
            allow_grid_charge=False,
        )
        _, out = _run(load, pv, cfg)
        assert out.storage.charge_ac.sum() == 0.0
        assert out.storage.discharge_ac.sum() == 0.0

    def test_storage_revenue_uses_hourly_prices(self):
        """§29：套利收益必须按逐时充放电计价，不能简化成"放电量 × 峰谷价差"。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 0.0)
        cfg = _cfg(
            DispatchStrategy.PEAK_VALLEY, allow_grid_charge=True, charge_from_grid=True,
            charge_price_threshold=0.40, discharge_price_threshold=0.90, initial_soc=0.5,
        )
        _, out = _run(load, pv, cfg)
        manual = (
            out.load_from_storage * out.price
            + out.storage_to_grid * out.export_price
            - out.grid_to_storage * out.price
            - out.pv_to_storage * out.export_price
        )
        assert np.allclose(out.storage_revenue, manual, atol=1e-9)


# --------------------------------------------------------------------------- #
# V2 §16、§20、§21 可解释性与开关
# --------------------------------------------------------------------------- #
class TestExplainabilityAndSwitches:
    @pytest.mark.parametrize("strategy", list(DispatchStrategy))
    def test_every_hour_has_reason(self, strategy):
        """§16：每一个小时都必须能解释为什么充电/放电/不动作。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 150.0)
        _, out = _run(load, pv, _cfg(strategy))
        assert len(out.reasons) == len(axis.timestamps)
        assert len(out.action_codes) == len(axis.timestamps)
        for reason, code in zip(out.reasons, out.reason_codes):
            assert code in REASON_LABELS
            assert reason and reason == REASON_LABELS[code]

    def test_decisions_carry_action_and_energy(self):
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 150.0)
        _, out = _run(load, pv, _cfg(DispatchStrategy.PV_SELF_CONSUMPTION, initial_soc=0.8))
        decisions = out.decisions(axis.timestamps[:24])
        assert len(decisions) == 24
        assert all(isinstance(d.action, DispatchAction) for d in decisions)
        assert any(d.discharge_energy > 0 for d in decisions)

    def test_storage_export_blocked_by_default(self):
        """§20：默认禁止储能上网。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 0.0, 0.0)
        cfg = _cfg(DispatchStrategy.PEAK_VALLEY, allow_export=False, initial_soc=1.0,
                   charge_price_threshold=0.40, discharge_price_threshold=0.90)
        _, out = _run(load, pv, cfg)
        assert out.storage_to_grid.sum() == 0.0

    def test_storage_export_allowed_when_enabled(self):
        """§20：显式开启后才允许储能上网。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 0.0, 0.0)
        cfg = _cfg(
            DispatchStrategy.PEAK_VALLEY, allow_export=True, initial_soc=1.0,
            charge_price_threshold=0.40, discharge_price_threshold=0.90, soc_min=0.0,
        )
        _, out = _run(load, pv, cfg)
        assert out.storage_to_grid.sum() > 0.0

    def test_grid_charge_counted_separately(self):
        """§21：电网充电必须单独统计，不能混入光伏储能。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 300.0, 50.0)
        cfg = _cfg(
            DispatchStrategy.PEAK_VALLEY, allow_grid_charge=True, charge_from_grid=True,
            charge_price_threshold=0.40, discharge_price_threshold=0.90, initial_soc=0.5,
        )
        _, out = _run(load, pv, cfg, capacity=2000.0, power=500.0)
        grid_charge = float(out.grid_to_storage.sum())
        pv_charge = float(out.pv_to_storage.sum())
        assert grid_charge > 0.0
        assert out.storage.grid_charge_ac.sum() == pytest.approx(grid_charge, rel=1e-9)
        assert out.storage.pv_charge_ac.sum() == pytest.approx(pv_charge, rel=1e-9)
        assert out.storage.charge_ac.sum() == pytest.approx(grid_charge + pv_charge, rel=1e-9)

    def test_curtailment_when_export_not_possible(self):
        """§9.3：上网电价为 0 时，光伏盈余应计为弃光而不是上网。"""
        axis = _axis()
        load, pv = _flat_arrays(axis, 0.0, 200.0)
        tariff = _tariff(export=0.0)
        _, out = _run(load, pv, _cfg(), tariff=tariff, capacity=0.0, power=0.0)
        assert out.grid_export.sum() == 0.0
        assert out.pv_curtailed.sum() == pytest.approx(out.pv.sum(), rel=1e-9)

    def test_deterministic(self):
        axis = _axis()
        load, pv = _flat_arrays(axis, 220.0, 180.0)
        cfg = _cfg(DispatchStrategy.ECONOMIC_OPTIMIZATION)
        _, a = _run(load, pv, cfg)
        _, b = _run(load, pv, cfg)
        assert np.array_equal(a.storage.soc_end, b.storage.soc_end)
        assert a.reason_codes == b.reason_codes


# --------------------------------------------------------------------------- #
# V2 §25、§26 储能衰减与更换
# --------------------------------------------------------------------------- #
class TestStorageDegradation:
    def test_degradation_reduces_capacity(self):
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 150.0)
        _, y1 = _run(load, pv, _cfg(), capacity=1000.0)
        out2 = dispatch_engine.dispatch(
            load=load, pv=pv, tariff=_tariff(), axis=axis, config=_cfg(),
            storage_capacity_kwh=1000.0, storage_power_kw=500.0,
            storage_degradation_rate=0.02, year_index=5,
        )
        assert out2.storage.capacity_kwh < y1.storage.capacity_kwh

    def test_replacement_restores_capacity(self):
        axis = _axis()
        load, pv = _flat_arrays(axis, 200.0, 150.0)
        out = dispatch_engine.dispatch(
            load=load, pv=pv, tariff=_tariff(), axis=axis, config=_cfg(),
            storage_capacity_kwh=1000.0, storage_power_kw=500.0,
            storage_degradation_rate=0.02, replacement_year=10, year_index=10,
        )
        assert out.storage.capacity_kwh == pytest.approx(1000.0, rel=1e-9)


# --------------------------------------------------------------------------- #
# V2 §12、§14 逐日价格排序窗口调度（重大经济缺陷回归）
#
# 缺陷（修复前）：``PEAK_VALLEY`` 逐时贪心 —— 只要电价 ≤ 充电阈值就充电。傍晚 19–20 点
# （平价 0.689）就把电池充满，次日凌晨 0–7 点（谷价 0.30）SOC 已经是 100%，
# 最便宜的充电机会被自己挤掉。广东式三电价实测（谷 0.30 / 平 0.689 / 峰 0.921，
# 1 MWh / 500 kW / DoD 90% / 往返效率 0.88）：
#
#   修复前：年充电 701,324 kWh（谷段仅 959 kWh，平价 700,364 kWh），套利 84,792 元
#   修复后：年充电 700,364 kWh（谷段 350,182 kWh —— 与平价各占一半），套利 221,301 元
#
# 谷段优先级：谷段电价最低 ⇒ 每天的价格排名窗口**先**把谷段用满（一充一放时谷段占 100%），
# 平价只在"当日第二个循环"里出现。受功率约束（500 kW × 2 h = 959 kWh ≈ 一个满循环），
# 两充两放时谷段与平价各承担一个循环，谷段占比的物理上限就是 50% —— 因此本组测试
# 对"谷段绝对多数（≥60%）"的断言放在**只允许谷充**的配置上（那里占 100%），
# 两充两放配置则断言"谷段不被平价挤占 + 谷段按功率用满 + 平价只出现在第二循环"。
# --------------------------------------------------------------------------- #
class TestDailyPriceWindowPlanning:
    """广东式三电价场景：谷段优先、收益量级、两充两放优于一充一放。"""

    VALLEY_PRICE = 0.30
    FLAT_PRICE = 0.689
    PEAK_PRICE = 0.921
    CAPACITY_KWH = 1000.0
    POWER_KW = 500.0
    DOD = 0.90
    ROUND_TRIP = 0.88
    #: 平段与谷段都在充电阈值以下；放电阈值略低于峰价
    CHARGE_THRESHOLD = 0.703
    DISCHARGE_THRESHOLD = 0.911
    #: 谷段 0–7/23、峰段 10–11/14–18、其余平价（广东式双峰）
    _VALLEY_HOURS = [0, 1, 2, 3, 4, 5, 6, 7, 23]
    _PEAK_HOURS = [10, 11, 14, 15, 16, 17, 18]
    _FLAT_HOURS = [8, 9, 12, 13, 19, 20, 21, 22]
    #: 工厂典型日负荷：峰段 800 kW、平价 210~790 kW、谷段 200 kW，保证放电能被完全吸纳
    _WORKDAY = [
        200.0, 200.0, 200.0, 200.0, 200.0, 200.0, 200.0, 200.0,   # 0-7   谷
        760.0, 780.0,                                             # 8-9   平
        800.0, 800.0,                                             # 10-11 峰
        790.0, 780.0,                                             # 12-13 平
        800.0, 800.0, 800.0, 800.0, 800.0,                        # 14-18 峰
        500.0, 300.0, 220.0, 210.0,                               # 19-22 平
        200.0,                                                    # 23    谷
    ]

    def _tariff(self, axis):
        rules = [
            TimePeriodRule(period=TariffPeriod.VALLEY, hours=self._VALLEY_HOURS),
            TimePeriodRule(period=TariffPeriod.PEAK, hours=self._PEAK_HOURS),
            TimePeriodRule(period=TariffPeriod.FLAT, hours=self._FLAT_HOURS),
        ]
        profile = TariffProfile(
            peak_price=self.PEAK_PRICE,
            flat_price=self.FLAT_PRICE,
            valley_price=self.VALLEY_PRICE,
            export_price=0.0,
            time_periods=rules,
        )
        return tariff_series.resolve_tariff(TariffSeriesConfig(profile=profile), axis, 1)

    def _run(self, charge_threshold: float, axis=None, tariff=None, strategy=None):
        """跑一年广东场景；``charge_threshold`` 决定平价是否进入充电候选。"""
        axis = axis or _axis()
        n = len(axis.timestamps)
        load = np.tile(np.array(self._WORKDAY, dtype=float), n // 24)
        pv = np.zeros(n)
        tariff = tariff if tariff is not None else self._tariff(axis)
        cfg = StorageDispatchConfig(
            strategy=strategy or DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=charge_threshold,
            discharge_price_threshold=self.DISCHARGE_THRESHOLD,
            allow_grid_charge=True,
            charge_from_grid=True,
            allow_export=False,
            soc_min=1.0 - self.DOD,
            soc_max=1.0,
            initial_soc=1.0 - self.DOD,
            charge_efficiency=math.sqrt(self.ROUND_TRIP),
            discharge_efficiency=math.sqrt(self.ROUND_TRIP),
        )
        out = dispatch_engine.dispatch(
            load=load, pv=pv, tariff=tariff, axis=axis, config=cfg,
            storage_capacity_kwh=self.CAPACITY_KWH, storage_power_kw=self.POWER_KW,
        )
        return axis, out

    @staticmethod
    def _by_price(price: np.ndarray, charge: np.ndarray) -> dict[float, float]:
        """按电价分组统计充电量（场景里只有三档电价）。"""
        return {
            round(float(p), 6): float(charge[np.isclose(price, p)].sum())
            for p in np.unique(np.round(price, 6))
        }

    # ---- 1. 谷段优先（核心回归）----
    def test_valley_charge_is_maximised_and_not_crowded_out(self):
        """谷段必须先用满：谷段充电量按功率/容量上限取满，且不少于平价充电量。"""
        _, out = self._run(self.CHARGE_THRESHOLD)
        price = np.asarray(out.price, dtype=float)
        charge = np.asarray(out.storage.charge_ac, dtype=float)
        by_price = self._by_price(price, charge)
        valley, flat = by_price[self.VALLEY_PRICE], by_price[self.FLAT_PRICE]
        total = float(charge.sum())

        # 修复前：谷段只有 959 kWh（0.14%），平价 700,364 kWh —— 谷段被彻底挤占
        assert valley >= 0.45 * total, f"谷段充电占比仅 {valley / total:.1%}，谷段优先级退化"
        assert valley >= flat - 1e-6, "平价充电量超过谷段，谷段被傍晚平价充电挤占"
        assert flat > 0.0, "两充两放场景应出现第二循环（平价充电）"

        # 谷段"用满"：一天只有一次谷段充电机会，其电量恰好是一个满循环
        #   一个循环的交流充电量 = 可用容量 ÷ η_charge
        usable_kwh = self.CAPACITY_KWH * self.DOD
        per_cycle_ac = usable_kwh / math.sqrt(self.ROUND_TRIP)
        assert valley == pytest.approx(per_cycle_ac * 365.0, rel=0.01), (
            f"谷段年充电 {valley:,.0f} kWh 未按功率上限取满（理论 {per_cycle_ac * 365.0:,.0f} kWh）"
        )

    def test_valley_share_exceeds_60_percent_when_flat_is_excluded(self):
        """只允许谷充（一充一放）时，谷段必须占全年充电量的绝对多数（≥60%）。"""
        _, out = self._run(self.VALLEY_PRICE + 0.001)
        price = np.asarray(out.price, dtype=float)
        charge = np.asarray(out.storage.charge_ac, dtype=float)
        valley = float(charge[price <= self.VALLEY_PRICE + 1e-9].sum())
        total = float(charge.sum())
        assert total > 0.0
        assert valley / total >= 0.60, f"谷段占全年充电量仅 {valley / total:.1%}"
        assert valley == pytest.approx(total, rel=1e-9), "平价不应出现在充电窗口内"

    # ---- 2. 收益量级 ----
    def test_annual_arbitrage_revenue_magnitude(self):
        """全年套利收益必须落在经济合理量级：修复前 84,792 元 → 修复后 >15 万元。"""
        _, out = self._run(self.CHARGE_THRESHOLD)
        # PV=0、上网电价为 0 ⇒ 储能收益就是套利收益（§29 逐时计价口径）
        revenue = float(out.storage_revenue.sum())
        assert revenue > 150_000.0, f"年套利收益仅 {revenue:,.0f} 元，谷段优先未生效"
        # 物理上界：两充两放全部按"峰价 − 谷价/η"与"峰价 − 平价/η"计价
        usable_ac = self.CAPACITY_KWH * self.DOD * math.sqrt(self.ROUND_TRIP)
        upper = usable_ac * 365.0 * (
            (self.PEAK_PRICE - self.VALLEY_PRICE / self.ROUND_TRIP)
            + (self.PEAK_PRICE - self.FLAT_PRICE / self.ROUND_TRIP)
        )
        assert revenue == pytest.approx(upper, rel=0.02), (
            f"年套利收益 {revenue:,.0f} 元与理论最优 {upper:,.0f} 元偏差过大"
        )

    # ---- 3. 两充两放严格优于一充一放 ----
    def test_two_cycles_beats_valley_only(self):
        """允许平价充电（两充两放）必须**严格**优于只允许谷充（一充一放）。

        修复前的方向是反的：84,792 元（两充两放）< 178,611 元（只允许谷充）——
        平价充电把谷段挤掉，越"允许"越亏。这是最能抓住回归的断言。
        """
        _, two_cycle = self._run(self.CHARGE_THRESHOLD)
        _, valley_only = self._run(self.VALLEY_PRICE + 0.001)
        two = float(two_cycle.storage_revenue.sum())
        one = float(valley_only.storage_revenue.sum())
        assert two > one, f"两充两放 {two:,.0f} 元 未超过 一充一放 {one:,.0f} 元"
        # 第二循环的净差价 = 峰价 − 平价/η_往返 = 0.138 元/kWh，年增量约 20%~30%
        assert (two - one) / one > 0.15

    # ---- 4. 不得为充而充 ----
    def test_no_charge_on_days_without_cheap_hour(self):
        """某天电价全部高于充电阈值时，该天充电量必须为 0（规划不得凭空造窗口）。"""
        axis = _axis()
        n = len(axis.timestamps)
        assert int(axis.hour[0]) == 0, "本测试假定时间轴从 0 点开始、每天 24 点"
        base = self._tariff(axis)
        price = np.asarray(base.price, dtype=float).copy()
        # 每 5 天把第 4 天整天的电价抬到 0.85：高于充电阈值 0.703、低于放电阈值 0.911
        expensive_day = ((np.arange(n) // 24) % 5) == 3
        price[expensive_day] = 0.85
        custom = TariffSeries(
            price=price,
            export_price=np.asarray(base.export_price, dtype=float),
            period_code=np.asarray(base.period_code),
        )
        _, out = self._run(self.CHARGE_THRESHOLD, axis=axis, tariff=custom)
        charge = np.asarray(out.storage.charge_ac, dtype=float)

        for day in range(n // 24):
            block = charge[day * 24 : (day + 1) * 24]
            if expensive_day[day * 24] or all(
                p > self.CHARGE_THRESHOLD for p in price[day * 24 : (day + 1) * 24]
            ):
                assert float(block.sum()) == 0.0, f"第 {day + 1} 天全天电价高于阈值却充了电"
            else:
                assert float(block.sum()) > 0.0, f"第 {day + 1} 天有低价窗口却未充电"

    # ---- 5. 平价充电只能服务于"当日第二循环"----
    def test_flat_charge_only_serves_a_second_cycle(self):
        """平价充电必须发生在"当日已经放过电、且其后还有高价窗口"的第二循环内。

        修复前 19–20 点的平价充电发生在当日最后一个高价窗口之后，只会挤掉次日谷段。
        """
        axis, out = self._run(self.CHARGE_THRESHOLD)
        price = np.asarray(out.price, dtype=float)
        charge = np.asarray(out.storage.charge_ac, dtype=float)
        discharge = np.asarray(out.storage.discharge_ac, dtype=float)
        flat_charging = np.flatnonzero((charge > 1e-9) & (price > self.VALLEY_PRICE + 1e-9))
        assert flat_charging.size > 0
        for t in flat_charging.tolist():
            day_start = t - int(axis.hour[t])
            day_end = day_start + 24
            assert (discharge[day_start:t] > 1e-9).any(), (
                f"第 {t // 24 + 1} 天 {axis.hour[t]} 时的平价充电之前当日没有放电"
                "（不是第二循环，属于为充而充）"
            )
            assert (discharge[t:day_end] > 1e-9).any(), (
                f"第 {t // 24 + 1} 天 {axis.hour[t]} 时的平价充电之后当日没有放电目标"
            )

    # ---- 6. SOC 与功率不变式 ----
    def test_soc_and_power_invariants_hold_all_year(self):
        """修复后仍满足 §74、§75：SOC ∈ [下限, 上限]、充放电 ≤ 功率 × Δt。"""
        axis = _axis()
        dt = axis.delta_hours
        for threshold in (self.CHARGE_THRESHOLD, self.VALLEY_PRICE + 0.001):
            _, out = self._run(threshold)
            soc = np.asarray(out.storage.soc_end, dtype=float)
            charge = np.asarray(out.storage.charge_ac, dtype=float)
            discharge = np.asarray(out.storage.discharge_ac, dtype=float)
            assert float(soc.min()) >= (1.0 - self.DOD) - 1e-9
            assert float(soc.max()) <= 1.0 + 1e-9
            assert float(charge.max()) <= self.POWER_KW * dt + 1e-9
            assert float(discharge.max()) <= self.POWER_KW * dt + 1e-9
            # 窗口调度同样不得储能上网（allow_export=False）
            assert float(out.storage_to_grid.sum()) == 0.0

    # ---- 7. 动作与电量、原因逐时一致（§16）----
    def test_action_energy_and_reason_consistency_all_8760_hours(self):
        """全年 8760 小时逐时满足 ``action == CHARGE ⟺ charge > 0``（§16）。"""
        axis, out = self._run(self.CHARGE_THRESHOLD)
        assert len(out.action_codes) == 8760
        actions = np.array(out.action_codes)
        charge = np.asarray(out.storage.charge_ac, dtype=float)
        discharge = np.asarray(out.storage.discharge_ac, dtype=float)
        charging = charge > 1e-12
        discharging = discharge > 1e-12

        assert np.array_equal(actions == DispatchAction.CHARGE.value, charging)
        assert np.array_equal(actions == DispatchAction.DISCHARGE.value, discharging)
        assert np.array_equal(
            actions == DispatchAction.IDLE.value, ~(charging | discharging)
        )
        for code, text in zip(out.reason_codes, out.reasons):
            assert code in REASON_LABELS
            assert text == REASON_LABELS[code]
        # 谷段充电必须给出可解释的中文原因
        assert "CHARGE_GRID_LOW" in set(out.reason_codes)

    # ---- 8. ECONOMIC_OPTIMIZATION 同样改为价格排名窗口 ----
    def test_economic_optimization_also_ranks_price_windows(self):
        """经济优化（§14）若仍是逐时贪心，同样会把谷段挤掉，故一并纳入回归。"""
        _, out = self._run(
            self.CHARGE_THRESHOLD, strategy=DispatchStrategy.ECONOMIC_OPTIMIZATION
        )
        price = np.asarray(out.price, dtype=float)
        charge = np.asarray(out.storage.charge_ac, dtype=float)
        # 当日 25% 分位 = 谷价 0.30 ⇒ 只有谷段有充电资格
        valley = float(charge[price <= self.VALLEY_PRICE + 1e-9].sum())
        total = float(charge.sum())
        assert total > 0.0
        assert valley == pytest.approx(total, rel=1e-9), "经济优化未把充电集中到最低价窗口"
        assert float(out.storage_revenue.sum()) > 150_000.0
