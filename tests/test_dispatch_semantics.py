"""V2 调度语义测试：动作/原因一致性、可解释性、性能基准（V2 §16、§86）。

本文件针对两个已修复的缺陷建立回归防线：

* **缺陷：原因与实际动作不一致**。曾经出现"原因说充电、实际充了 0 kWh"的小时，
  违反 §16「每一个小时必须能够解释为什么充电/放电/不动作」。现强制不变式：

  .. code-block:: text

      action == CHARGE     ⟺  charge_ac > 0
      action == DISCHARGE  ⟺  discharge_ac > 0
      否则                  action == IDLE

* **缺陷：逐时循环性能**。优化后单年 8760 点调度从约 132 ms 降到约 34 ms，
  优化前后结果必须**逐位一致**，因此这里锁定 Golden Case 的全部汇总量。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from cenep.calculation import dispatch_engine, load_profile, pv_profile, tariff_series
from cenep.calculation.dispatch_engine import REASON_LABELS, dispatch
from cenep.calculation.timeseries_engine import build_time_axis
from cenep.domain.enums import (
    DispatchAction,
    DispatchStrategy,
    LoadProfileMode,
    PVProfileMode,
    TariffPeriod,
)
from cenep.domain.timeseries import (
    LoadProfileConfig,
    PVProfileConfig,
    StorageDispatchConfig,
    TariffProfile,
    TariffSeriesConfig,
    TimePeriodRule,
)

#: 优化前记录的 Golden Case 基准（逐位一致回归用，rel=1e-12）
BASELINE = {
    "charge_sum": 959.4032236002463,
    "discharge_sum": 0.0,
    "grid_charge_sum": 0.0,
    "pv_charge_sum": 959.4032236002463,
    "grid_import_sum": 725820.0695879696,
    "grid_export_sum": 324860.6663643695,
    "electricity_cost_sum": 372515.0551577386,
    "storage_revenue_sum": -335.7911282600862,
    "export_revenue_sum": 113701.23322752933,
    "soc_end_last": 1.0,
    "soc_end_sum": 8687.57963922919,
}


def _rules() -> list[TimePeriodRule]:
    """谷段 00–06、峰段 10–12、其余平段。"""
    out: list[TimePeriodRule] = []
    for h in range(24):
        if h in (10, 11, 12):
            out.append(TimePeriodRule(period=TariffPeriod.PEAK, hours=[h]))
        elif h in range(7):
            out.append(TimePeriodRule(period=TariffPeriod.VALLEY, hours=[h]))
        else:
            out.append(TimePeriodRule(period=TariffPeriod.FLAT, hours=[h]))
    return out


def _profile() -> TariffProfile:
    return TariffProfile(
        peak_price=1.0, flat_price=0.65, valley_price=0.35, export_price=0.35,
        time_periods=_rules(),
    )


def _golden(strategy: DispatchStrategy, **cfg_kw):
    """V2 Golden Case（§85）：PV 1000 kWp / 1100 h，负荷 150 万 kWh，储能 500 kW/1000 kWh。"""
    axis = build_time_axis(2025)
    load = load_profile.resolve_load(
        LoadProfileConfig(mode=LoadProfileMode.ANNUAL_SIMPLE, annual_energy_kwh=1_500_000.0), axis
    )
    pv = pv_profile.resolve_pv(
        PVProfileConfig(mode=PVProfileMode.EQUIVALENT_HOURS, equivalent_hours=1100.0),
        axis, capacity_kwp=1000.0, degradation_rate=0.0,
    )
    tariff = tariff_series.resolve_tariff(TariffSeriesConfig(profile=_profile()), axis)
    cfg = StorageDispatchConfig(strategy=strategy, **cfg_kw)
    out = dispatch(
        load=load, pv=pv, tariff=tariff, axis=axis, config=cfg,
        storage_capacity_kwh=1000.0, storage_power_kw=500.0, storage_degradation_rate=0.0,
    )
    return axis, load, pv, tariff, out


def _violations(out) -> int:
    """统计违反 §16 不变式的小时数。"""
    actions = np.array(out.action_codes)
    charge = out.storage.charge_ac
    discharge = out.storage.discharge_ac
    bad_charge = (actions == DispatchAction.CHARGE.value) & ~(charge > 1e-12)
    bad_discharge = (actions == DispatchAction.DISCHARGE.value) & ~(discharge > 1e-12)
    bad_idle = (actions == DispatchAction.IDLE.value) & ((charge > 1e-12) | (discharge > 1e-12))
    return int((bad_charge | bad_discharge | bad_idle).sum())


# --------------------------------------------------------------------------- #
# §16 不变式：全年 8760 小时逐时成立
# --------------------------------------------------------------------------- #
class TestActionEnergyInvariant:
    @pytest.mark.parametrize("strategy", list(DispatchStrategy))
    @pytest.mark.parametrize("grid_charge", [False, True])
    def test_invariant_holds_all_8760_hours(self, strategy, grid_charge):
        """覆盖整年 8760 小时：动作与电量必须一致（§16）。"""
        _, _, _, _, out = _golden(
            strategy,
            allow_grid_charge=grid_charge,
            charge_from_grid=grid_charge,
            charge_price_threshold=0.40,
            discharge_price_threshold=0.90,
        )
        assert len(out.action_codes) == 8760
        assert _violations(out) == 0, "存在动作与电量不一致的小时"

    def test_invariant_with_storage_export_enabled(self):
        """开启储能上网后仍必须满足不变式。"""
        _, _, _, _, out = _golden(DispatchStrategy.PEAK_VALLEY, allow_export=True)
        assert _violations(out) == 0

    def test_invariant_without_storage(self):
        """无储能时全部为 IDLE（§20 无事可做）。"""
        axis = build_time_axis(2025)
        load = np.full(8760, 100.0)
        pv = np.full(8760, 50.0)
        tariff = tariff_series.resolve_tariff(TariffSeriesConfig(profile=_profile()), axis)
        out = dispatch(
            load=load, pv=pv, tariff=tariff, axis=axis,
            config=StorageDispatchConfig(strategy=DispatchStrategy.PV_SELF_CONSUMPTION),
            storage_capacity_kwh=0.0, storage_power_kw=0.0,
        )
        assert set(out.action_codes) == {DispatchAction.IDLE.value}
        assert _violations(out) == 0

    @pytest.mark.parametrize("strategy", list(DispatchStrategy))
    def test_reason_code_always_known(self, strategy):
        """每个小时的原因码都必须有中文说明（§16 可解释性）。"""
        _, _, _, _, out = _golden(strategy, charge_price_threshold=0.40, discharge_price_threshold=0.90)
        assert len(out.reason_codes) == 8760
        for code in out.reason_codes:
            assert code in REASON_LABELS
        for text in out.reasons:
            assert text and any("\u4e00" <= ch <= "\u9fff" for ch in text)


# --------------------------------------------------------------------------- #
# 缺陷回归：谷段想充电但电网充电关闭
# --------------------------------------------------------------------------- #
class TestChargeDisabledReporting:
    def test_valley_hour_reports_idle_not_charge(self):
        """回归：峰谷套利 + allow_grid_charge=False 的谷段，必须是 IDLE 且说明真实原因。

        修复前：该小时报 action=CHARGE、原因"谷段电价不高于充电阈值，储能充电"，
        但实际充电量 = 0 —— 违反 §16。
        """
        _, _, _, _, out = _golden(
            DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=0.90,
            discharge_price_threshold=0.95,
            allow_grid_charge=False,
        )
        bad = [
            i
            for i, a in enumerate(out.action_codes)
            if a == DispatchAction.CHARGE.value and out.storage.charge_ac[i] <= 1e-12
        ]
        assert bad == []
        # 电网充电关闭 ⇒ 不可能出现电网充电量（光伏盈余充电不受阈值影响，仍在继续）
        assert float(out.storage.grid_charge_ac.sum()) == pytest.approx(0.0)
        assert np.allclose(
            out.storage.charge_ac, out.storage.pv_charge_ac, rtol=0.0, atol=1e-12
        )

    def test_idle_charge_disabled_reason_is_used(self):
        """应出现 IDLE_CHARGE_DISABLED：谷段可充但总开关关闭。"""
        _, _, _, _, out = _golden(
            DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=0.90,
            discharge_price_threshold=0.95,
            allow_grid_charge=False,
        )
        codes = set(out.reason_codes)
        assert "IDLE_CHARGE_DISABLED" in codes
        assert "CHARGE_LOW_PRICE" not in codes

    def test_grid_charge_enabled_does_charge_at_valley(self):
        """打开电网充电后，谷段确实充电，并单独统计电网充电量（§21）。"""
        _, _, _, _, out = _golden(
            DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=0.40,
            discharge_price_threshold=0.90,
            allow_grid_charge=True,
            charge_from_grid=True,
        )
        assert float(out.storage.grid_charge_ac.sum()) > 0.0
        assert "CHARGE_GRID_LOW" in set(out.reason_codes)
        assert _violations(out) == 0

    def test_self_consumption_never_grid_charges(self):
        """§13：光伏自用优先策略禁止电网充电。"""
        _, _, _, _, out = _golden(DispatchStrategy.PV_SELF_CONSUMPTION)
        assert float(out.storage.grid_charge_ac.sum()) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 缺陷回归：性能优化不得改变结果
# --------------------------------------------------------------------------- #
class TestPerformanceOptimizationFidelity:
    def test_aggregates_match_prerefactor_baseline(self):
        """逐位一致：优化后的汇总量必须与优化前记录的基准完全相同（rel=1e-12）。"""
        _, _, _, _, out = _golden(
            DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=0.40,
            discharge_price_threshold=0.90,
        )
        s = out.storage
        assert float(s.charge_ac.sum()) == pytest.approx(BASELINE["charge_sum"], rel=1e-12)
        assert float(s.discharge_ac.sum()) == pytest.approx(BASELINE["discharge_sum"], rel=1e-12)
        assert float(s.grid_charge_ac.sum()) == pytest.approx(BASELINE["grid_charge_sum"], rel=1e-12)
        assert float(s.pv_charge_ac.sum()) == pytest.approx(BASELINE["pv_charge_sum"], rel=1e-12)
        assert float(out.grid_import.sum()) == pytest.approx(BASELINE["grid_import_sum"], rel=1e-12)
        assert float(out.grid_export.sum()) == pytest.approx(BASELINE["grid_export_sum"], rel=1e-12)
        assert float(out.electricity_cost.sum()) == pytest.approx(
            BASELINE["electricity_cost_sum"], rel=1e-12
        )
        assert float(out.storage_revenue.sum()) == pytest.approx(
            BASELINE["storage_revenue_sum"], rel=1e-12
        )
        assert float(out.export_revenue.sum()) == pytest.approx(
            BASELINE["export_revenue_sum"], rel=1e-12
        )

    def test_soc_series_matches_prerefactor_baseline(self):
        """逐时 SOC 序列的首/中/末与校验和必须一致（rel=1e-12）。"""
        _, _, _, _, out = _golden(
            DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=0.40,
            discharge_price_threshold=0.90,
        )
        soc = out.storage.soc_end
        assert float(soc[0]) == pytest.approx(0.1, rel=1e-12)
        assert float(soc[len(soc) // 2]) == pytest.approx(1.0, rel=1e-12)
        assert float(soc[-1]) == pytest.approx(BASELINE["soc_end_last"], rel=1e-12)
        assert float(soc.sum()) == pytest.approx(BASELINE["soc_end_sum"], rel=1e-12)

    def test_single_year_dispatch_within_budget(self):
        """性能：单年 8760 点调度应在 100 ms 内（§86 单年预算 2000 ms，留足余量）。"""
        axis = build_time_axis(2025)
        load = np.full(8760, 100.0)
        pv = np.full(8760, 50.0)
        tariff = tariff_series.resolve_tariff(TariffSeriesConfig(profile=_profile()), axis)
        cfg = StorageDispatchConfig(
            strategy=DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=0.40,
            discharge_price_threshold=0.90,
        )
        start = time.perf_counter()
        dispatch(
            load=load, pv=pv, tariff=tariff, axis=axis, config=cfg,
            storage_capacity_kwh=1000.0, storage_power_kw=500.0,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        assert elapsed_ms < 100.0, f"单年调度耗时 {elapsed_ms:.1f} ms，超出预算"


# --------------------------------------------------------------------------- #
# 语义说明：PEAK_VALLEY 与 allow_grid_charge=False 的组合
# --------------------------------------------------------------------------- #
class TestPeakValleySemantics:
    def test_grid_charge_off_degrades_to_pv_surplus_only(self):
        """该组合下储能只能靠**光伏盈余**充电，谷段电价再低也不会从电网充。

        这是刻意的语义（§21 默认禁止电网充电）：用户把充电阈值设得很宽也不会
        从电网买电充储能。测试把它显式固化下来，避免以后被误当成缺陷"修掉"。
        """
        _, _, pv, _, out = _golden(
            DispatchStrategy.PEAK_VALLEY,
            charge_price_threshold=0.98,   # 几乎全年都"够便宜"
            discharge_price_threshold=1.00,
            allow_grid_charge=False,
        )
        assert float(out.storage.grid_charge_ac.sum()) == pytest.approx(0.0)
        # 充入储能的只能是光伏盈余
        assert np.allclose(
            out.storage.charge_ac, out.storage.pv_charge_ac, rtol=0.0, atol=1e-12
        )
        assert float(out.storage.charge_ac.sum()) <= float(pv.sum())
