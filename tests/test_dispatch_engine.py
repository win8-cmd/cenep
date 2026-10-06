"""V2 储能调度与能量平衡测试（V2 §16、§19、§20、§21、§73–§81）。

覆盖规范列为 P0 的强制测试项：能量守恒（§73）、SOC 边界（§74）、
充放电功率上限（§75）、效率方向（§76）、无储能退化（§77）、无光伏退化（§78）、
PV 与负荷的三种相对关系（§79）、盈余/满/空四种工况（§80）、峰谷套利（§81），
以及 §16 的"每小时必须可解释"与 §21 的"电网充电单独统计"。
"""

from __future__ import annotations

import numpy as np
import pytest

from cenep.calculation import dispatch_engine, energy_balance
from cenep.calculation.dispatch_engine import REASON_LABELS
from cenep.calculation.errors import ValidationError
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
