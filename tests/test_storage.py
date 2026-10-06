"""储能模块单元测试（规范 §39–§45、§116）。"""

from __future__ import annotations

import math

import pytest

from cenep.calculation.storage import (
    annual_charge_energy,
    annual_discharge_energy,
    available_energy_for_year,
    resolve_efficiencies,
    storage_duration_hours,
    storage_year_result,
)


class TestDuration:
    def test_duration_formula(self):
        """§39：StorageDuration = StorageEnergy / StoragePower。"""
        assert storage_duration_hours(4000.0, 2000.0) == pytest.approx(2.0)

    def test_zero_power_is_safe(self):
        assert storage_duration_hours(1000.0, 0.0) == 0.0


class TestEfficiencies:
    def test_round_trip_from_components(self):
        """§40：round_trip = charge × discharge。"""
        c, d, r = resolve_efficiencies(0.95, 0.95, None)
        assert c == pytest.approx(0.95)
        assert d == pytest.approx(0.95)
        assert r == pytest.approx(0.9025)

    def test_components_from_round_trip(self):
        """§40：只给往返效率时，charge = discharge = sqrt(round_trip)。"""
        c, d, r = resolve_efficiencies(None, None, 0.88)
        assert c == pytest.approx(math.sqrt(0.88))
        assert d == pytest.approx(math.sqrt(0.88))
        assert r == pytest.approx(0.88)
        assert c * d == pytest.approx(0.88)

    def test_missing_all_raises(self):
        with pytest.raises(ValueError):
            resolve_efficiencies(None, None, None)


class TestEnergy:
    def test_discharge_formula(self):
        """§41：Edis = Available × DoD × Cycles × η_dis。"""
        value = annual_discharge_energy(1000.0, 0.9, 330.0, 1.0)
        assert value == pytest.approx(297_000.0)

    def test_charge_formula(self):
        """§42：Echg = Edis / η_dis / η_chg。"""
        value = annual_charge_energy(297_000.0, 1.0, 1.0)
        assert value == pytest.approx(297_000.0)
        value2 = annual_charge_energy(278_610.696142, math.sqrt(0.88), math.sqrt(0.88))
        assert value2 == pytest.approx(316_603.063798, rel=1e-9)

    def test_charge_greater_than_discharge_when_lossy(self):
        discharge = annual_discharge_energy(1000.0, 0.9, 330.0, math.sqrt(0.88))
        charge = annual_charge_energy(discharge, math.sqrt(0.88), math.sqrt(0.88))
        assert charge > discharge
        assert charge / discharge == pytest.approx(1 / 0.88)

    def test_zero_efficiency_rejected(self):
        with pytest.raises(ValueError):
            annual_charge_energy(100.0, 0.0, 0.9)


class TestDegradation:
    def test_first_year_is_initial(self):
        """§45：Available_1 = Initial。"""
        available, replaced = available_energy_for_year(1000.0, 0.02, 1)
        assert available == pytest.approx(1000.0)
        assert replaced is False

    def test_degradation_formula(self):
        available, _ = available_energy_for_year(1000.0, 0.02, 3)
        assert available == pytest.approx(1000.0 * 0.98**2)

    def test_replacement_resets_capacity(self):
        """§38、§45：更换电芯当年容量恢复至初始值。"""
        available_before, _ = available_energy_for_year(1000.0, 0.02, 10)
        available_at, replaced = available_energy_for_year(1000.0, 0.02, 11, replacement_year=11)
        available_after, _ = available_energy_for_year(1000.0, 0.02, 12, replacement_year=11)
        assert available_before < 1000.0
        assert replaced is True
        assert available_at == pytest.approx(1000.0)
        assert available_after == pytest.approx(1000.0 * 0.98)

    def test_year_zero_rejected(self):
        with pytest.raises(ValueError):
            available_energy_for_year(1000.0, 0.02, 0)


class TestStorageYear:
    def test_full_pipeline(self):
        """§41 + §42 + §45 串联：黄金案例第 1 年。"""
        row = storage_year_result(
            year=1,
            initial_energy_kwh=1000.0,
            depth_of_discharge=0.9,
            annual_cycles=330.0,
            charge_efficiency=math.sqrt(0.88),
            discharge_efficiency=math.sqrt(0.88),
            annual_degradation_rate=0.02,
            replacement_year=None,
        )
        assert row.available_energy_kwh == pytest.approx(1000.0)
        assert row.discharge_energy_kwh == pytest.approx(278_610.696142, rel=1e-9)
        assert row.charge_energy_kwh == pytest.approx(316_603.063798, rel=1e-9)
        assert row.is_replacement_year is False

    def test_year_three_reflects_degradation(self):
        row = storage_year_result(3, 1000.0, 0.9, 330.0, 1.0, 1.0, 0.02)
        assert row.discharge_energy_kwh == pytest.approx(0.9 * 330.0 * 1000.0 * 0.98**2)
