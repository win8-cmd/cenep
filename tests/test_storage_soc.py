"""储能 SOC 模型测试（V2 §10、§11、§24、§25、§26、§76）。

规范 §76 明确要求「构造 100 kWh charge，验证 SOC increase 与效率一致」。
本文件把这条要求做成**方向性断言**：充电乘效率、放电除效率，
并验证充放电一个往返后 SOC 精确回到起点（往返效率 0.88）。
方向写反会让往返效率变成 ``1/η²`` 量级，因此这几项断言是 V2 最关键的防线之一。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from cenep.calculation.storage_soc import (
    StorageSeries,
    capacity_for_year,
    clip_soc,
    empty_series,
    equivalent_cycles,
    internal_energy_delta,
    max_charge_energy_ac,
    max_discharge_energy_ac,
    soc_after,
    usable_energy,
)

ETA = math.sqrt(0.88)  # 0.938083151964686，与 StorageDispatchConfig 默认值一致
CAPACITY = 1000.0
SOC_MIN = 0.10
SOC_MAX = 1.00


class TestUsableEnergy:
    def test_formula(self):
        """§10.2：E_usable = E_rated × (SOC_max − SOC_min)。"""
        assert usable_energy(1000.0, 0.10, 1.00) == pytest.approx(900.0)
        assert usable_energy(500.0, 0.0, 0.9) == pytest.approx(450.0)

    def test_zero_when_bounds_equal(self):
        assert usable_energy(1000.0, 0.5, 0.5) == pytest.approx(0.0)


class TestEfficiencyDirection:
    """§76：效率方向必须与规范一致（充电乘、放电除）。"""

    def test_charge_multiplies_efficiency(self):
        """充 100 kWh(AC) → 电池内部只增加 100 × η。"""
        soc_end = soc_after(
            np.array([0.10]), np.array([100.0]), np.array([0.0]),
            capacity_kwh=CAPACITY, eta_charge=ETA, eta_discharge=ETA,
        )
        expected_rise = 100.0 * ETA / CAPACITY
        assert soc_end[0] == pytest.approx(0.10 + expected_rise)

    def test_discharge_divides_efficiency(self):
        """放 100 kWh(AC) → 电池内部要减少 100 / η（比 100 多）。"""
        soc_end = soc_after(
            np.array([0.50]), np.array([0.0]), np.array([100.0]),
            capacity_kwh=CAPACITY, eta_charge=ETA, eta_discharge=ETA,
        )
        expected_drop = 100.0 / ETA / CAPACITY
        assert soc_end[0] == pytest.approx(0.50 - expected_drop)
        assert expected_drop > 100.0 / CAPACITY, "放电的内部减少量必须大于 AC 侧放出量"

    def test_round_trip_returns_to_start(self):
        """往返闭合：充 100 kWh(AC) 后，只需放 88 kWh(AC) 即回到起点（0.88 往返效率）。"""
        start = np.array([0.10])
        after_charge = soc_after(
            start, np.array([100.0]), np.array([0.0]),
            capacity_kwh=CAPACITY, eta_charge=ETA, eta_discharge=ETA,
        )
        after_round_trip = soc_after(
            after_charge, np.array([0.0]), np.array([100.0 * 0.88]),
            capacity_kwh=CAPACITY, eta_charge=ETA, eta_discharge=ETA,
        )
        assert after_round_trip[0] == pytest.approx(start[0], abs=1e-12)

    def test_internal_delta_matches_round_trip(self):
        delta = internal_energy_delta(
            np.array([100.0]), np.array([88.0]), eta_charge=ETA, eta_discharge=ETA
        )
        assert delta[0] == pytest.approx(0.0, abs=1e-12)

    def test_internal_delta_positive_on_net_charge(self):
        delta = internal_energy_delta(
            np.array([50.0]), np.array([10.0]), eta_charge=ETA, eta_discharge=ETA
        )
        assert delta[0] == pytest.approx(50.0 * ETA - 10.0 / ETA)


class TestSocRecurrence:
    def test_vectorised_over_many_periods(self):
        """§10.5：递推必须支持整条 8760 序列（向量化，不逐点循环）。"""
        n = 8760
        soc_start = np.full(n, 0.5)
        charge = np.full(n, 10.0)
        discharge = np.full(n, 0.0)
        soc_end = soc_after(
            soc_start, charge, discharge,
            capacity_kwh=CAPACITY, eta_charge=ETA, eta_discharge=ETA,
        )
        assert soc_end.shape == (n,)
        assert np.allclose(soc_end, 0.5 + 10.0 * ETA / CAPACITY)

    def test_zero_capacity_is_safe(self):
        soc_end = soc_after(
            np.array([0.5]), np.array([10.0]), np.array([0.0]),
            capacity_kwh=0.0, eta_charge=ETA, eta_discharge=ETA,
        )
        assert soc_end[0] == pytest.approx(0.5)

    def test_clip_soc_bounds(self):
        """§10.5：SOC 必须夹在 [SOC_min, SOC_max]。"""
        clipped = clip_soc(np.array([-0.2, 0.5, 1.3]), SOC_MIN, SOC_MAX)
        assert clipped.tolist() == pytest.approx([SOC_MIN, 0.5, SOC_MAX])


class TestPowerAndSocLimits:
    """§11：充放电能量同时受功率上限与 SOC 边界限制。"""

    def test_charge_limited_by_power(self):
        out = max_charge_energy_ac(
            np.array([0.10]), capacity_kwh=CAPACITY, soc_max=SOC_MAX,
            eta_charge=ETA, max_charge_power_kw=500.0, delta_hours=1.0,
        )
        # SOC 空间足够（0.9×1000/η ≈ 959 kWh），因此受 500 kWh 功率上限约束
        assert out[0] == pytest.approx(500.0)

    def test_charge_limited_by_soc_room(self):
        out = max_charge_energy_ac(
            np.array([0.95]), capacity_kwh=CAPACITY, soc_max=SOC_MAX,
            eta_charge=ETA, max_charge_power_kw=500.0, delta_hours=1.0,
        )
        expected = 0.05 * CAPACITY / ETA  # 只剩 5% 空间
        assert out[0] == pytest.approx(expected)
        assert out[0] < 500.0

    def test_charge_zero_when_full(self):
        out = max_charge_energy_ac(
            np.array([1.0]), capacity_kwh=CAPACITY, soc_max=SOC_MAX,
            eta_charge=ETA, max_charge_power_kw=500.0, delta_hours=1.0,
        )
        assert out[0] == pytest.approx(0.0)

    def test_discharge_limited_by_power(self):
        out = max_discharge_energy_ac(
            np.array([0.90]), capacity_kwh=CAPACITY, soc_min=SOC_MIN,
            eta_discharge=ETA, max_discharge_power_kw=300.0, delta_hours=1.0,
        )
        assert out[0] == pytest.approx(300.0)

    def test_discharge_limited_by_soc_room(self):
        out = max_discharge_energy_ac(
            np.array([0.12]), capacity_kwh=CAPACITY, soc_min=SOC_MIN,
            eta_discharge=ETA, max_discharge_power_kw=500.0, delta_hours=1.0,
        )
        expected = 0.02 * CAPACITY * ETA
        assert out[0] == pytest.approx(expected)
        assert out[0] < 500.0

    def test_discharge_zero_when_empty(self):
        out = max_discharge_energy_ac(
            np.array([0.10]), capacity_kwh=CAPACITY, soc_min=SOC_MIN,
            eta_discharge=ETA, max_discharge_power_kw=500.0, delta_hours=1.0,
        )
        assert out[0] == pytest.approx(0.0)

    def test_half_hour_resolution_scales_power_limit(self):
        """§3 P0.1：15 分钟粒度下，功率上限要按 Δt 缩放。"""
        out = max_charge_energy_ac(
            np.array([0.10]), capacity_kwh=CAPACITY, soc_max=SOC_MAX,
            eta_charge=ETA, max_charge_power_kw=400.0, delta_hours=0.25,
        )
        assert out[0] == pytest.approx(100.0)

    def test_power_limits_are_arrays(self):
        soc = np.linspace(SOC_MIN, SOC_MAX, 100)
        charge = max_charge_energy_ac(
            soc, capacity_kwh=CAPACITY, soc_max=SOC_MAX,
            eta_charge=ETA, max_charge_power_kw=500.0, delta_hours=1.0,
        )
        discharge = max_discharge_energy_ac(
            soc, capacity_kwh=CAPACITY, soc_min=SOC_MIN,
            eta_discharge=ETA, max_discharge_power_kw=500.0, delta_hours=1.0,
        )
        assert charge.shape == discharge.shape == (100,)
        assert np.all(charge >= 0) and np.all(discharge >= 0)
        # SOC 越高越难充、越容易放
        assert charge[0] >= charge[-1]
        assert discharge[0] <= discharge[-1]


class TestDegradationAndReplacement:
    """§25 衰减、§26 更换。"""

    def test_first_year_is_initial_capacity(self):
        assert capacity_for_year(1000.0, 0.02, 1) == pytest.approx(1000.0)

    def test_compounding_degradation(self):
        assert capacity_for_year(1000.0, 0.02, 3) == pytest.approx(1000.0 * 0.98**2)

    def test_replacement_resets_capacity(self):
        """§26：更换年份起容量复位为初始值。"""
        assert capacity_for_year(1000.0, 0.02, 9, replacement_year=10) == pytest.approx(1000.0 * 0.98**8)
        assert capacity_for_year(1000.0, 0.02, 10, replacement_year=10) == pytest.approx(1000.0)
        assert capacity_for_year(1000.0, 0.02, 11, replacement_year=10) == pytest.approx(1000.0)

    def test_replacement_with_new_capacity(self):
        assert capacity_for_year(
            1000.0, 0.02, 10, replacement_year=10, replacement_capacity_kwh=1200.0
        ) == pytest.approx(1200.0)

    def test_no_replacement_keeps_degrading(self):
        assert capacity_for_year(1000.0, 0.02, 25) == pytest.approx(1000.0 * 0.98**24)


class TestEquivalentCycles:
    """§24：必须能区分「配置循环次数」与「实际等效循环次数」。"""

    def test_formula(self):
        assert equivalent_cycles(900_000.0, 900.0) == pytest.approx(1000.0)

    def test_zero_usable_returns_zero(self):
        assert equivalent_cycles(1234.0, 0.0) == 0.0

    def test_lower_than_configured_when_underused(self):
        configured = 330.0
        actual = equivalent_cycles(180_000.0, 900.0)  # 200 次
        assert actual == pytest.approx(200.0)
        assert actual < configured


class TestStorageSeries:
    def test_totals(self):
        series = StorageSeries(
            capacity_kwh=CAPACITY,
            usable_energy_kwh=900.0,
            soc_start=np.array([0.1, 0.5]),
            soc_end=np.array([0.5, 0.1]),
            charge_ac=np.array([100.0, 0.0]),
            discharge_ac=np.array([0.0, 88.0]),
            grid_charge_ac=np.array([40.0, 0.0]),
            pv_charge_ac=np.array([60.0, 0.0]),
        )
        assert series.total_charge == pytest.approx(100.0)
        assert series.total_discharge == pytest.approx(88.0)
        assert series.total_grid_charge == pytest.approx(40.0)
        assert series.total_pv_charge == pytest.approx(60.0)

    def test_grid_charge_tracked_separately(self):
        """§21：电网充电必须单独统计，不能混进"光伏储能"。"""
        series = StorageSeries(
            capacity_kwh=CAPACITY, usable_energy_kwh=900.0,
            soc_start=np.zeros(1), soc_end=np.zeros(1),
            charge_ac=np.array([100.0]), discharge_ac=np.zeros(1),
            grid_charge_ac=np.array([100.0]), pv_charge_ac=np.zeros(1),
        )
        assert series.total_grid_charge == series.total_charge
        assert series.total_pv_charge == 0.0

    def test_equivalent_cycles_from_series(self):
        series = StorageSeries(
            capacity_kwh=CAPACITY, usable_energy_kwh=900.0,
            soc_start=np.zeros(1), soc_end=np.zeros(1),
            charge_ac=np.zeros(1), discharge_ac=np.array([180_000.0]),
            grid_charge_ac=np.zeros(1), pv_charge_ac=np.zeros(1),
        )
        assert series.equivalent_cycles == pytest.approx(200.0)

    def test_soc_extremes(self):
        series = StorageSeries(
            capacity_kwh=CAPACITY, usable_energy_kwh=900.0,
            soc_start=np.array([0.1, 0.9]), soc_end=np.array([0.12, 0.87]),
            charge_ac=np.zeros(2), discharge_ac=np.zeros(2),
            grid_charge_ac=np.zeros(2), pv_charge_ac=np.zeros(2),
        )
        assert series.soc_min_actual() == pytest.approx(0.12)
        assert series.soc_max_actual() == pytest.approx(0.87)

    def test_empty_series_shapes(self):
        series = empty_series(8760)
        assert series.charge_ac.shape == (8760,)
        assert series.total_charge == 0.0
        assert series.equivalent_cycles == 0.0


class TestFullYearSimulation:
    def test_daily_cycle_stays_within_bounds(self):
        """§74：整年模拟后 SOC 必须始终落在 [SOC_min, SOC_max] 内。"""
        n = 8760
        charge = np.zeros(n)
        discharge = np.zeros(n)
        # 每天 22:00 充 500 kWh(AC)，次日 10:00 放 400 kWh(AC)
        for day in range(365):
            charge[day * 24 + 22] = 500.0
            discharge[day * 24 + 10] = 400.0

        soc = np.empty(n, dtype=float)
        current = SOC_MIN
        for i in range(n):
            room = max_charge_energy_ac(
                np.array([current]), capacity_kwh=CAPACITY, soc_max=SOC_MAX,
                eta_charge=ETA, max_charge_power_kw=1000.0, delta_hours=1.0,
            )[0]
            c = min(charge[i], room)
            avail = max_discharge_energy_ac(
                np.array([current]), capacity_kwh=CAPACITY, soc_min=SOC_MIN,
                eta_discharge=ETA, max_discharge_power_kw=1000.0, delta_hours=1.0,
            )[0]
            d = min(discharge[i], avail)
            charge[i], discharge[i] = c, d
            # 关键：把上一周期的 SOC 传递到本周期（soc_after 返回的是期末 SOC）
            current = soc_after(
                np.array([current]), np.array([c]), np.array([d]),
                capacity_kwh=CAPACITY, eta_charge=ETA, eta_discharge=ETA,
            )[0]
            soc[i] = current

        assert soc.min() >= SOC_MIN - 1e-12
        assert soc.max() <= SOC_MAX + 1e-12
        assert charge.sum() > 0 and discharge.sum() > 0
        # 一年下来必须真的充放过电，且 SOC 被推到过上限附近
        assert soc.max() > 0.5
