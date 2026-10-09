"""账单应用服务测试（V2.1 §5.1、§5.4、§5.5、§8.2）。

覆盖阶段 1 的验收标准：账单可手动录入、Excel 导入、保存、重开、复核；旧项目可打开。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from cenep.application.bill_service import BillService, is_valid_month
from cenep.application.project_service import ProjectService
from cenep.calculation.errors import ValidationError
from cenep.data.bill_importer import BILL_COLUMNS, SHEET_BILLS
from cenep.domain.bill_models import BillTolerance
from cenep.domain.enums import BillQualityStatus, BillSourceType, DuplicateStrategy
from cenep.infrastructure.project_file import FILE_FORMAT, load_project

HEADERS = [column.header for column in BILL_COLUMNS]


def _row(**fields) -> list:
    return [fields.get(column.field, "") for column in BILL_COLUMNS]


def _bill_workbook(path: Path, rows: list[list]) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = SHEET_BILLS
    for row in rows:
        worksheet.append(row)
    workbook.save(path)
    return path


def _data_row(**overrides) -> list:
    values = {
        "billing_period_start": "2026-01-01",
        "billing_period_end": "2026-01-31",
        "meter_id": "M001",
        "tariff_structure": "单一制",
        "energy_total_kwh": 100_000,
        "energy_sharp_kwh": 10_000,
        "energy_peak_kwh": 30_000,
        "energy_flat_kwh": 40_000,
        "energy_valley_kwh": 20_000,
        "energy_charge_yuan": 65_000,
        "bill_total_yuan": 65_000,
        "voltage_level": "10kV",
    }
    values.update(overrides)
    return _row(**values)


@pytest.fixture
def project(golden_pv):
    golden_pv.basic_info.project_name = "湖北某工商业厂房"
    return golden_pv


@pytest.fixture
def service(project):
    return BillService(project)


class TestManualEntry:
    """§5.4：手动录入、编辑、复制、删除。"""

    def test_create_bill(self, service):
        bill = service.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            meter_id="M001",
            voltage_level="10kV",
            energy_total_kwh=100_000,
            energy_charge_yuan=65_000,
            bill_total_yuan=65_000,
        )
        assert bill.billing_month == "2026-01"
        assert bill.source_type is BillSourceType.MANUAL
        assert bill.project_id == "湖北某工商业厂房"
        assert bill.quality_status is BillQualityStatus.VALID
        assert len(service) == 1
        assert service.bills[0].bill_id == bill.bill_id

    def test_create_bill_from_string_dates(self, service):
        bill = service.create_bill(
            billing_period_start="2026-03-01", billing_period_end="2026/3/31"
        )
        assert bill.billing_period_start == date(2026, 3, 1)
        assert bill.billing_period_end == date(2026, 3, 31)
        assert bill.billing_month == "2026-03"

    def test_minimal_bill_is_allowed(self, service):
        """§2.1：金额/电量都可以不提供（留空 = 未提供，不是 0）。"""
        bill = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        assert bill.bill_total_yuan is None
        assert bill.energy_total_kwh is None

    def test_unknown_field_rejected(self, service):
        with pytest.raises(ValidationError) as exc:
            service.create_bill(
                billing_period_start=date(2026, 1, 1),
                billing_period_end=date(2026, 1, 31),
                不存在的字段=1,
            )
        assert "不支持的账单字段" in exc.value.message

    def test_missing_period_rejected(self, service):
        with pytest.raises(ValidationError, match="必须提供账期"):
            service.create_bill(energy_total_kwh=1000)

    def test_invalid_date_rejected(self, service):
        with pytest.raises(ValidationError, match="不是有效日期"):
            service.create_bill(billing_period_start="前天", billing_period_end="2026-01-31")

    def test_reversed_period_rejected(self, service):
        with pytest.raises(ValidationError, match="不能早于"):
            service.create_bill(
                billing_period_start=date(2026, 2, 1), billing_period_end=date(2026, 1, 31)
            )

    def test_negative_value_rejected_with_chinese_message(self, service):
        with pytest.raises(ValidationError) as exc:
            service.create_bill(
                billing_period_start=date(2026, 1, 1),
                billing_period_end=date(2026, 1, 31),
                energy_total_kwh=-5,
            )
        assert "不能为负" in exc.value.message
        assert exc.value.field == "energy_total_kwh"

    def test_negative_value_never_stored(self, service):
        with pytest.raises(ValidationError):
            service.create_bill(
                billing_period_start=date(2026, 1, 1),
                billing_period_end=date(2026, 1, 31),
                bill_total_yuan=-1,
            )
        assert len(service) == 0

    def test_warning_bill_is_stored_with_status(self, service):
        bill = service.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            energy_total_kwh=100_000,
            energy_peak_kwh=10_000,
        )
        assert bill.quality_status is BillQualityStatus.WARNING
        assert bill.quality_messages

    def test_get_bill_missing(self, service):
        with pytest.raises(ValidationError, match="未找到账单"):
            service.get_bill("不存在")

    def test_update_bill(self, service):
        created = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        updated = service.update_bill(created.bill_id, bill_total_yuan=1234.0, notes="补录")
        assert updated.bill_total_yuan == pytest.approx(1234.0)
        assert updated.notes == "补录"
        assert service.get_bill(created.bill_id).bill_total_yuan == pytest.approx(1234.0)

    def test_update_bill_rejects_invalid_change(self, service):
        created = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        with pytest.raises(ValidationError, match="不能为负"):
            service.update_bill(created.bill_id, energy_total_kwh=-1)
        assert service.get_bill(created.bill_id).energy_total_kwh is None

    def test_update_bill_protects_identity(self, service):
        created = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        with pytest.raises(ValidationError, match="不允许修改"):
            service.update_bill(created.bill_id, {"bill_id": "新编号"})
        assert service.get_bill(created.bill_id).bill_id == created.bill_id

    def test_update_bill_unknown_field(self, service):
        created = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        with pytest.raises(ValidationError, match="不支持的账单字段"):
            service.update_bill(created.bill_id, 乱七八糟=1)

    def test_update_bill_no_change_returns_same(self, service):
        created = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        assert service.update_bill(created.bill_id).bill_id == created.bill_id

    def test_delete_bill(self, service):
        created = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        removed = service.delete_bill(created.bill_id)
        assert removed.bill_id == created.bill_id
        assert len(service) == 0

    def test_delete_missing_raises(self, service):
        with pytest.raises(ValidationError):
            service.delete_bill("没有这条")

    def test_clear(self, service):
        for month in (1, 2):
            service.create_bill(
                billing_period_start=date(2026, month, 1),
                billing_period_end=date(2026, month, 28),
            )
        assert service.clear() == 2
        assert len(service) == 0


class TestDuplicateHandling:
    """§5.5：以项目 + 账期 + 计量点识别重复，由用户选择 跳过 / 替换 / 保留。"""

    def _create(self, service, month: int = 1, **overrides):
        payload = {
            "billing_period_start": date(2026, month, 1),
            "billing_period_end": date(2026, month, 28),
            "meter_id": "M001",
        }
        payload.update(overrides)
        return service.create_bill(**payload)

    def test_duplicate_without_strategy_asks_user(self, service):
        first = self._create(service)
        with pytest.raises(ValidationError) as exc:
            self._create(service, bill_total_yuan=100.0)
        assert "跳过" in exc.value.message and "替换" in exc.value.message and "保留" in exc.value.message
        assert len(service) == 1
        assert service.get_bill(first.bill_id).bill_total_yuan is None

    def test_skip_returns_existing(self, service):
        first = self._create(service)
        result = service.add_bill(
            first.model_copy(update={"bill_total_yuan": 100.0}),
            on_duplicate=DuplicateStrategy.SKIP,
        )
        assert result.bill_id == first.bill_id
        assert len(service) == 1
        assert service.get_bill(first.bill_id).bill_total_yuan is None

    def test_replace_keeps_count_and_position(self, service):
        first = self._create(service)
        self._create(service, month=2)
        replacement = first.model_copy(update={"bill_total_yuan": 999.0})
        result = service.add_bill(replacement, on_duplicate=DuplicateStrategy.REPLACE)
        assert len(service) == 2
        assert result.bill_total_yuan == pytest.approx(999.0)
        assert service.list_bills()[0].bill_total_yuan == pytest.approx(999.0)

    def test_keep_both_adds_suffix(self, service):
        first = self._create(service)
        result = service.add_bill(
            first.model_copy(update={"bill_total_yuan": 888.0}),
            on_duplicate=DuplicateStrategy.KEEP_BOTH,
        )
        assert len(service) == 2
        assert result.bill_id != first.bill_id
        assert result.bill_id.startswith(first.bill_id)

    def test_different_meter_is_not_duplicate(self, service):
        self._create(service, meter_id="M001")
        self._create(service, meter_id="M002")
        assert len(service) == 2

    def test_update_into_collision_rejected(self, service):
        first = self._create(service, month=1)
        self._create(service, month=2)
        with pytest.raises(ValidationError, match="重复"):
            service.update_bill(
                service.list_bills()[1].bill_id,
                billing_period_start=date(2026, 1, 1),
                billing_period_end=date(2026, 1, 28),
                billing_month="2026-01",
            )
        assert service.get_bill(first.bill_id).billing_month == "2026-01"
        assert len(service) == 2


class TestCopy:
    def test_copy_bill_shifts_one_month(self, service):
        source = service.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            meter_id="M1",
            energy_total_kwh=100_000,
            bill_total_yuan=65_000,
        )
        copied = service.copy_bill(source.bill_id)
        assert copied.billing_month == "2026-02"
        assert copied.billing_period_start == date(2026, 2, 1)
        assert copied.billing_period_end == date(2026, 2, 28)  # 2026-02 只有 28 天
        assert copied.energy_total_kwh == pytest.approx(100_000.0)
        assert copied.source_type is BillSourceType.MANUAL
        assert source.bill_id in (copied.notes or "")
        assert len(service) == 2

    def test_copy_bill_explicit_target(self, service):
        source = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        copied = service.copy_bill(
            source.bill_id,
            target_period_start=date(2026, 6, 1),
            target_period_end=date(2026, 6, 30),
        )
        assert copied.billing_month == "2026-06"

    def test_copy_bill_clamps_end_of_month(self, service):
        source = service.create_bill(
            billing_period_start=date(2026, 1, 31), billing_period_end=date(2026, 1, 31)
        )
        copied = service.copy_bill(source.bill_id)
        assert copied.billing_period_start == date(2026, 2, 28)

    def test_copy_previous_month(self, service):
        service.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            energy_total_kwh=100_000,
            bill_total_yuan=65_000,
        )
        copied = service.copy_previous_month("2026-02")
        assert copied.billing_month == "2026-02"
        assert copied.billing_period_start == date(2026, 2, 1)

    def test_copy_previous_month_picks_latest(self, service):
        service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31),
            bill_total_yuan=1.0,
        )
        service.create_bill(
            billing_period_start=date(2026, 2, 1), billing_period_end=date(2026, 2, 28),
            bill_total_yuan=2.0,
        )
        copied = service.copy_previous_month("2026-03")
        assert copied.bill_total_yuan == pytest.approx(2.0)

    def test_copy_previous_month_requires_history(self, service):
        with pytest.raises(ValidationError, match="没有早于"):
            service.copy_previous_month("2026-02")

    def test_copy_previous_month_invalid_month(self, service):
        with pytest.raises(ValidationError, match="格式不正确"):
            service.copy_previous_month("2026-13")

    def test_copy_previous_month_ambiguous(self, service):
        service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31), meter_id="A"
        )
        service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31), meter_id="B"
        )
        with pytest.raises(ValidationError, match="无法确定"):
            service.copy_previous_month("2026-02")


class TestQueries:
    def _fill(self, service):
        service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31),
            meter_id="M1", bill_total_yuan=100.0,
        )
        service.create_bill(
            billing_period_start=date(2026, 2, 1), billing_period_end=date(2026, 2, 28),
            meter_id="M1", bill_total_yuan=None,
        )
        service.create_bill(
            billing_period_start=date(2026, 3, 1), billing_period_end=date(2026, 3, 31),
            meter_id="M1", bill_total_yuan=300.0,
        )

    def test_filter_by_month(self, service):
        self._fill(service)
        assert [b.billing_month for b in service.list_bills(month="2026-02")] == ["2026-02"]

    def test_invalid_month_filter(self, service):
        with pytest.raises(ValidationError, match="格式不正确"):
            service.list_bills(month="2026/02")

    def test_invalid_sort_field(self, service):
        with pytest.raises(ValidationError, match="不支持的排序字段"):
            service.list_bills(sort_by="随便")

    def test_default_sort_is_by_period(self, service):
        self._fill(service)
        months = [b.billing_month for b in service.list_bills()]
        assert months == ["2026-01", "2026-02", "2026-03"]

    def test_descending(self, service):
        self._fill(service)
        months = [b.billing_month for b in service.list_bills(descending=True)]
        assert months == ["2026-03", "2026-02", "2026-01"]

    def test_sort_with_missing_amount_goes_last(self, service):
        self._fill(service)
        amounts = [b.bill_total_yuan for b in service.list_bills(sort_by="bill_total_yuan")]
        assert amounts[0] == pytest.approx(100.0)
        assert amounts[-1] is None

    def test_month_coverage(self, service):
        self._fill(service)
        assert service.month_coverage() == {"2026-01": 1, "2026-02": 1, "2026-03": 1}

    def test_is_valid_month_helper(self):
        assert is_valid_month("2026-01")
        assert not is_valid_month("2026-13")
        assert not is_valid_month("26-01")
        assert not is_valid_month("")


class TestQuality:
    def test_validate_bill(self, service):
        bill = service.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            energy_total_kwh=100_000,
            energy_peak_kwh=10_000,
            bill_total_yuan=999.0,
        )
        outcome = service.validate_bill(bill)
        assert outcome.energy_consistent is False
        assert outcome.quality_status is BillQualityStatus.WARNING
        assert outcome.issues

    def test_reconcile_all(self, service):
        service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        assert len(service.reconcile_all()) == 1

    def test_score_bill(self, service):
        bill = service.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            voltage_level="10kV",
            energy_total_kwh=100_000,
            energy_charge_yuan=65_000,
            bill_total_yuan=65_000,
            tariff_structure="单一制",
        )
        score = service.score_bill(bill.bill_id)
        assert 0 <= score.score <= 100
        assert score.status is BillQualityStatus.VALID

    def test_refresh_quality_does_not_touch_facts(self, service):
        bill = service.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            bill_total_yuan=500.0,
        )
        before = service.get_bill(bill.bill_id).model_dump()
        # 人为破坏质量标记，再重算应恢复
        service.project.bills[0].quality_status = BillQualityStatus.INVALID
        service.refresh_quality()
        after = service.get_bill(bill.bill_id)
        assert after.quality_status is not BillQualityStatus.INVALID
        for field in ("bill_total_yuan", "energy_total_kwh", "billing_period_start"):
            assert after.model_dump()[field] == before[field]


class TestSummaries:
    def _fill_full_year(self, service):
        import calendar

        for month in range(1, 13):
            last_day = calendar.monthrange(2026, month)[1]
            service.create_bill(
                billing_period_start=date(2026, month, 1),
                billing_period_end=date(2026, month, last_day),
                meter_id="M1",
                energy_total_kwh=10_000.0,
                energy_charge_yuan=7_000.0,
                bill_total_yuan=7_000.0,
                voltage_level="10kV",
                tariff_structure="单一制",
                energy_sharp_kwh=1_000.0,
                energy_peak_kwh=3_000.0,
                energy_flat_kwh=4_000.0,
                energy_valley_kwh=2_000.0,
            )

    def test_monthly_summary_and_trend(self, service):
        self._fill_full_year(service)
        items = service.monthly_summary()
        assert len(items) == 12
        assert service.monthly_trend()[0] == ("2026-01", pytest.approx(10_000.0), pytest.approx(7_000.0))

    def test_annual_summary(self, service):
        self._fill_full_year(service)
        outcome = service.annual_summary(2026)
        assert outcome.coverage_ratio == pytest.approx(1.0)
        assert outcome.can_sum_directly is True
        assert outcome.total_energy_kwh == pytest.approx(120_000.0)
        assert outcome.average_price_yuan_per_kwh == pytest.approx(0.7)

    def test_annual_summary_default_year(self, service):
        self._fill_full_year(service)
        assert service.annual_summary().year == 2026

    def test_tolerance_is_applied(self, service):
        strict = BillService(service.project, tolerance=BillTolerance(amount_relative=0.0, amount_absolute_yuan=0.0))
        strict.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            energy_charge_yuan=1000.0,
            bill_total_yuan=1000.5,
        )
        assert strict.reconcile_all()[0].amount_consistent is False
        assert service.reconcile_all()[0].amount_consistent is True


class TestImportIntegration:
    """§5.3、§5.5：模板 → 预览 → 导入 → 项目。"""

    def _file(self, tmp_path, name: str = "账单.xlsx", **overrides) -> Path:
        return _bill_workbook(tmp_path / name, [HEADERS, _data_row(**overrides)])

    def test_import_adds_bill(self, service, tmp_path):
        result = service.import_bills(self._file(tmp_path))
        assert result.added_count == 1
        assert len(service) == 1
        bill = service.list_bills()[0]
        assert bill.source_type is BillSourceType.EXCEL
        assert bill.project_id == service.project_id
        assert bill.energy_total_kwh == pytest.approx(100_000.0)

    def test_preview_then_import(self, service, tmp_path):
        preview = service.preview_import(self._file(tmp_path))
        assert preview.valid_count == 1
        assert len(service) == 0  # 预览不写入
        result = service.import_bills(preview=preview)
        assert result.added_count == 1
        assert len(service) == 1

    def test_reimport_skips_duplicates(self, service, tmp_path):
        path = self._file(tmp_path)
        service.import_bills(path)
        result = service.import_bills(path, strategy=DuplicateStrategy.SKIP)
        assert result.added_count == 0
        assert result.skipped_ids
        assert len(service) == 1

    def test_reimport_replace_updates(self, service, tmp_path):
        service.import_bills(self._file(tmp_path))
        other = self._file(tmp_path, name="改过.xlsx", bill_total_yuan=70_000)
        service.import_bills(other, strategy=DuplicateStrategy.REPLACE)
        assert len(service) == 1
        assert service.list_bills()[0].bill_total_yuan == pytest.approx(70_000.0)

    def test_reimport_keep_both(self, service, tmp_path):
        path = self._file(tmp_path)
        service.import_bills(path)
        service.import_bills(path, strategy=DuplicateStrategy.KEEP_BOTH)
        assert len(service) == 2
        ids = {b.bill_id for b in service.bills}
        assert len(ids) == 2

    def test_invalid_rows_are_not_imported(self, service, tmp_path):
        path = _bill_workbook(
            tmp_path / "账单.xlsx",
            [HEADERS, _data_row(), _data_row(billing_period_start="坏日期")],
        )
        result = service.import_bills(path)
        assert result.added_count == 1
        assert result.invalid_rows == [3]
        assert len(service) == 1

    def test_fatal_error_propagates_as_chinese(self, service, tmp_path):
        with pytest.raises(ValidationError, match="文件不存在"):
            service.import_bills(tmp_path / "没有.xlsx")

    def test_import_without_path_or_preview(self, service):
        with pytest.raises(ValidationError, match="必须提供文件路径"):
            service.import_bills()

    def test_export_template(self, service, tmp_path):
        saved = service.export_template(tmp_path)
        assert saved.exists()
        assert saved.name.endswith(".xlsx")
        assert len(service.template_bytes()) > 0

    def test_imported_then_manually_edited(self, service, tmp_path):
        service.import_bills(self._file(tmp_path))
        bill = service.list_bills()[0]
        updated = service.update_bill(bill.bill_id, notes="人工复核后补充")
        assert updated.notes == "人工复核后补充"
        assert updated.source_type is BillSourceType.EXCEL


class TestPersistenceAndCompatibility:
    """§8.2：账单随项目保存/重开；旧项目照常打开。"""

    def test_save_and_reopen(self, service, tmp_path):
        service.create_bill(
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
            meter_id="M1",
            energy_total_kwh=100_000,
            bill_total_yuan=65_000,
            notes="复核通过",
        )
        saved = service.save(tmp_path / "带账单")
        project, reopened = BillService.open(saved)
        assert len(reopened) == 1
        bill = reopened.list_bills()[0]
        assert bill.energy_total_kwh == pytest.approx(100_000.0)
        assert bill.bill_total_yuan == pytest.approx(65_000.0)
        assert bill.notes == "复核通过"
        assert bill.quality_messages  # 质量说明一并保存

    def test_summary_after_reopen(self, service, tmp_path):
        for month in (1, 2):
            service.create_bill(
                billing_period_start=date(2026, month, 1),
                billing_period_end=date(2026, month, 28),
                energy_total_kwh=10_000,
                bill_total_yuan=7_000,
            )
        saved = service.save(tmp_path / "汇总")
        _, reopened = BillService.open(saved)
        assert len(reopened.monthly_summary()) == 2
        assert reopened.annual_summary(2026).total_energy_kwh == pytest.approx(20_000.0)

    def test_old_project_opens_with_empty_bills(self, golden_pv, tmp_path):
        """§8.2、§9.1：旧项目打开后账单为空，既有参数不被改动。"""
        payload = json.loads(golden_pv.model_dump_json())
        for key in ("bills", "timeseries", "migration_notes"):
            payload.pop(key, None)
        payload["schema_version"] = "1.0"
        path = tmp_path / "旧项目.nep"
        path.write_text(
            json.dumps(
                {
                    "format": FILE_FORMAT,
                    "schema_version": "1.0",
                    "app_version": "1.0.0",
                    "saved_at": "2026-01-01T00:00:00",
                    "project": payload,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        project, service = BillService.open(path)
        assert service.bills == []
        assert service.monthly_summary() == []
        assert service.annual_summary(2026).total_energy_kwh is None
        assert project.load.annual_load_kwh == golden_pv.load.annual_load_kwh
        assert project.pv.self_consumption_ratio == golden_pv.pv.self_consumption_ratio
        assert project.tariff.peak_price == golden_pv.tariff.peak_price

    def test_saving_old_project_does_not_invent_bills(self, golden_pv, tmp_path):
        payload = json.loads(golden_pv.model_dump_json())
        payload.pop("bills", None)
        path = tmp_path / "旧.nep"
        path.write_text(
            json.dumps(
                {
                    "format": FILE_FORMAT,
                    "schema_version": "2.0",
                    "app_version": "2.0.0",
                    "saved_at": "2026-01-01T00:00:00",
                    "project": payload,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = load_project(path)
        saved = BillService(loaded).save(tmp_path / "存回")
        assert load_project(saved).bills == []

    def test_project_id_defaults_to_project_name(self, project):
        assert BillService(project).project_id == project.basic_info.project_name

    def test_project_id_can_be_overridden(self, project):
        service = BillService(project, project_id="nep文件标识")
        assert service.project_id == "nep文件标识"
        bill = service.create_bill(
            billing_period_start=date(2026, 1, 1), billing_period_end=date(2026, 1, 31)
        )
        assert bill.project_id == "nep文件标识"

    def test_project_id_rejects_blank(self, service):
        with pytest.raises(ValidationError, match="不能为空"):
            service.project_id = "   "

    def test_project_service_exposes_bill_service(self, project):
        """§5.2：界面通过 ProjectService 拿到账单服务，不自行拼装。"""
        service = ProjectService().bill_service(project)
        assert isinstance(service, BillService)
        assert service.project is project
