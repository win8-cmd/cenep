"""校验与错误处理测试（规范 §112、§113、§114、§132）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from cenep.calculation.errors import EnergyBalanceError, ValidationError
from cenep.calculation.validator import (
    validate_energy_balance,
    validate_project,
    validate_storage_balance,
)
from cenep.domain.models import StorageConfig, TariffConfig
from cenep.domain.results import AnnualResult


class TestFriendlyErrors:
    def test_missing_pv_capacity_message_is_chinese_and_points_to_field(self, golden_pv):
        """§132：必须告诉用户哪个参数有问题。"""
        project = golden_pv.model_copy(deep=True)
        project.pv.pv_capacity_kwp = None
        project.pv.usable_roof_area_m2 = 0.0
        with pytest.raises(ValidationError) as exc:
            validate_project(project)
        assert "光伏装机容量必须大于 0" in str(exc.value)
        assert exc.value.field == "pv.pv_capacity_kwp"

    def test_zero_storage_energy_rejected(self, golden_storage):
        project = golden_storage.model_copy(deep=True)
        project.storage.storage_energy_kwh = 0.0
        with pytest.raises(ValidationError) as exc:
            validate_project(project)
        assert "储能容量必须大于 0" in str(exc.value)
        assert exc.value.field == "storage.storage_energy_kwh"

    def test_zero_storage_power_rejected(self, golden_storage):
        project = golden_storage.model_copy(deep=True)
        project.storage.storage_power_kw = 0.0
        with pytest.raises(ValidationError):
            validate_project(project)

    def test_negative_load_rejected(self, golden_pv):
        """Pydantic 赋值校验是第一道防线；validator 是第二道（用 model_copy 绕过第一道来验证第二道）。"""
        broken = golden_pv.model_copy(
            update={"load": golden_pv.load.model_copy(update={"annual_load_kwh": -1.0})}
        )
        with pytest.raises(ValidationError) as exc:
            validate_project(broken)
        assert "年用电量不能为负数" in str(exc.value)
        assert exc.value.field == "load.annual_load_kwh"

    def test_assignment_is_guarded_by_pydantic(self, golden_pv):
        with pytest.raises(PydanticValidationError):
            golden_pv.load.annual_load_kwh = -1.0

    def test_grace_period_not_less_than_term(self, golden_pv):
        broken = golden_pv.model_copy(
            update={
                "financing": golden_pv.financing.model_copy(
                    update={"grace_period": golden_pv.financing.loan_term}
                )
            }
        )
        with pytest.raises(ValidationError) as exc:
            validate_project(broken)
        assert "宽限期必须小于贷款期限" in str(exc.value)
        assert exc.value.field == "financing.grace_period"

    def test_replacement_year_beyond_life(self, golden_storage):
        project = golden_storage.model_copy(deep=True)
        project.storage.replacement_year = project.analysis_period + 1
        with pytest.raises(ValidationError) as exc:
            validate_project(project)
        assert exc.value.field == "storage.replacement_year"

    def test_custom_tariff_requires_both_prices(self, golden_pv):
        from cenep.domain.enums import TariffMode

        project = golden_pv.model_copy(deep=True)
        project.tariff.tariff_mode = TariffMode.CUSTOM
        with pytest.raises(ValidationError) as exc:
            validate_project(project)
        assert "自定义电价" in str(exc.value)


class TestModelBackstop:
    """Pydantic 是第二道防线（规范 §112），本项目的中文报错由 validator 提供。"""

    def test_efficiency_above_one_rejected(self):
        with pytest.raises(PydanticValidationError):
            StorageConfig(charge_efficiency=1.5)

    def test_tou_ratios_must_sum_to_one(self):
        with pytest.raises(PydanticValidationError):
            TariffConfig(peak_ratio=0.5, flat_ratio=0.4, valley_ratio=0.3)

    def test_financing_ratios_must_sum_to_one(self):
        from cenep.domain.models import FinancingConfig

        with pytest.raises(PydanticValidationError):
            FinancingConfig(debt_ratio=0.6, equity_ratio=0.6)

    def test_extra_field_rejected(self):
        with pytest.raises(PydanticValidationError):
            StorageConfig(storage_energy_kwh=1.0, unknown_field=1)  # type: ignore[call-arg]


class TestEnergyBalanceValidation:
    def test_balanced_row_passes(self):
        row = AnnualResult(
            year=1,
            pv_generation_kwh=1_100_000.0,
            pv_self_use_kwh=880_000.0,
            pv_to_storage_kwh=220_000.0,
            pv_export_kwh=0.0,
            pv_loss_kwh=0.0,
        )
        validate_energy_balance([row])

    def test_unbalanced_row_raises(self):
        """§113：误差 > 1e-6 必须报 CalculationError。"""
        row = AnnualResult(
            year=3,
            pv_generation_kwh=1_000_000.0,
            pv_self_use_kwh=800_000.0,
            pv_to_storage_kwh=0.0,
            pv_export_kwh=0.0,
            pv_loss_kwh=0.0,
        )
        with pytest.raises(EnergyBalanceError) as exc:
            validate_energy_balance([row])
        assert "第 3 年光伏电量不守恒" in str(exc.value)

    def test_tolerance_boundary(self):
        row = AnnualResult(
            year=1,
            pv_generation_kwh=1.0,
            pv_self_use_kwh=1.0 - 1e-7,
            pv_export_kwh=0.0,
        )
        validate_energy_balance([row])

    def test_storage_charge_smaller_than_pv_transfer_raises(self):
        row = AnnualResult(year=1, pv_to_storage_kwh=100.0, storage_charge_kwh=90.0)
        with pytest.raises(EnergyBalanceError):
            validate_storage_balance([row])
