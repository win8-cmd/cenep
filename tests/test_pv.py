"""光伏模块单元测试（规范 §19、§20、§22、§24、§25、§113、§116）。"""

from __future__ import annotations

import pytest

from cenep.calculation.pv import (
    allocate_pv_energy,
    degradation_factor,
    first_year_generation,
    generation_for_year,
    resolve_pv_capacity,
)


class TestPvCapacity:
    def test_direct_input_wins(self):
        capacity, basis = resolve_pv_capacity(1000.0, 6500.0, 6.0)
        assert capacity == pytest.approx(1000.0)
        assert basis == "INPUT"

    def test_area_based(self):
        """§19：PVCapacity = UsableRoofArea / AreaPerKWp。"""
        capacity, basis = resolve_pv_capacity(None, 6500.0, 6.5)
        assert capacity == pytest.approx(1000.0)
        assert basis == "AREA"

    def test_missing_returns_zero(self):
        capacity, basis = resolve_pv_capacity(None, 0.0, 6.0)
        assert capacity == 0.0
        assert basis == "NONE"


class TestGeneration:
    def test_first_year_formula(self):
        """§20：G1 = Ppv × H × PR × (1 - C)。"""
        g1 = first_year_generation(1000.0, 1100.0, 1.0, 0.0)
        assert g1 == pytest.approx(1_100_000.0)

    def test_curtailment_reduces_generation(self):
        g1 = first_year_generation(1000.0, 1100.0, 1.0, 0.10)
        assert g1 == pytest.approx(990_000.0)

    def test_performance_ratio_1_means_no_double_deduction(self):
        """§21：等效小时已是最终可利用小时时 PR=1，不得重复扣减。"""
        assert first_year_generation(1.0, 1100.0, 1.0, 0.0) == pytest.approx(1100.0)

    def test_degradation_first_year_is_one(self):
        assert degradation_factor(0.005, 1) == pytest.approx(1.0)

    def test_degradation_formula(self):
        """§22：Gn = G1 × (1-d)^(n-1)。"""
        assert degradation_factor(0.005, 2) == pytest.approx(0.995)
        assert degradation_factor(0.005, 3) == pytest.approx(0.995**2)
        assert generation_for_year(1_100_000.0, 0.005, 2) == pytest.approx(1_100_000.0 * 0.995)

    def test_degradation_requires_year_from_one(self):
        with pytest.raises(ValueError):
            degradation_factor(0.005, 0)


class TestAllocation:
    def test_self_use_capped_by_load(self):
        """§24：PVSelfUse = min(Generation × ratio, Load)。"""
        alloc = allocate_pv_energy(generation=1_000_000.0, load=500_000.0, self_consumption_ratio=0.8)
        assert alloc.direct_use == pytest.approx(500_000.0)
        assert alloc.export == pytest.approx(500_000.0)

    def test_self_use_capped_by_ratio(self):
        alloc = allocate_pv_energy(generation=1_000_000.0, load=900_000.0, self_consumption_ratio=0.8)
        assert alloc.direct_use == pytest.approx(800_000.0)
        assert alloc.export == pytest.approx(200_000.0)

    def test_storage_takes_priority_over_export(self):
        """§23、§46：分配顺序为 直接自用 → 储能 → 余电上网。"""
        alloc = allocate_pv_energy(
            generation=1_000_000.0,
            load=800_000.0,
            self_consumption_ratio=0.8,
            storage_charge_headroom=150_000.0,
        )
        assert alloc.direct_use == pytest.approx(800_000.0)
        assert alloc.to_storage == pytest.approx(150_000.0)
        assert alloc.export == pytest.approx(50_000.0)

    def test_storage_headroom_limited_by_remaining(self):
        alloc = allocate_pv_energy(
            generation=1_000_000.0,
            load=800_000.0,
            self_consumption_ratio=0.8,
            storage_charge_headroom=999_999.0,
        )
        assert alloc.to_storage == pytest.approx(200_000.0)
        assert alloc.export == pytest.approx(0.0)

    def test_loss_ratio_reduces_usable_energy(self):
        alloc = allocate_pv_energy(
            generation=1_000_000.0, load=0.0, self_consumption_ratio=0.0, loss_ratio=0.02
        )
        assert alloc.loss == pytest.approx(20_000.0)
        assert alloc.export == pytest.approx(980_000.0)

    @pytest.mark.parametrize(
        "generation,load,ratio,headroom,loss",
        [
            (1_100_000.0, 1_200_000.0, 0.8, 0.0, 0.0),
            (1_100_000.0, 1_200_000.0, 0.8, 316_603.064, 0.0),
            (1_100_000.0, 100_000.0, 0.9, 50_000.0, 0.03),
            (0.0, 500_000.0, 0.8, 10_000.0, 0.0),
            (1_100_000.0, 0.0, 1.0, 0.0, 0.0),
        ],
    )
    def test_energy_conservation(self, generation, load, ratio, headroom, loss):
        """§113：PVGeneration = PVDirectUse + PVToStorage + PVExport + PVLoss。"""
        alloc = allocate_pv_energy(generation, load, ratio, headroom, loss)
        assert alloc.is_balanced()
        assert abs(alloc.balance_error) <= 1e-6

    def test_no_double_counting(self):
        """§47：进入储能的电量不计自用收益。"""
        alloc = allocate_pv_energy(
            generation=1_000_000.0, load=1_000_000.0, self_consumption_ratio=1.0, storage_charge_headroom=100_000.0
        )
        # 自用已达上限，储能只能拿到 0（因为 100% 电量都用于自用）
        assert alloc.direct_use == pytest.approx(1_000_000.0)
        assert alloc.to_storage == pytest.approx(0.0)
