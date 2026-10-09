"""V2.2 阶段 4：负荷与消纳应用服务测试（规格书 §6.2、§6.3、§6.4、§0.2）。

覆盖：数据集登记/切换/保留历史、账单电量 → 估算输入、月账单估算落地、
负荷画像、光伏出力解析（复用 §9 引擎）、消纳分析装配与来源标签、
缺失策略透传、项目文件持久化回归。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest
from pydantic import ValidationError as PydanticValidationError

from cenep.application.bill_service import BillService
from cenep.application.load_profile_service import LoadProfileService
from cenep.application.project_service import ProjectService
from cenep.calculation.errors import ValidationError
from cenep.domain.enums import (
    LoadDataSourceType,
    LoadEstimateSource,
    MissingDataPolicy,
    PVProfileMode,
    ProjectType,
)
from cenep.domain.load_data import HighFrequencyLoadDataset
from cenep.domain.load_estimate import LoadEstimateParams, MonthlyLoadEnergy
from cenep.domain.models import BasicInfo, Project, PVConfig
from cenep.domain.timeseries import LoadProfileConfig, PVProfile, PVProfileConfig, TimeSeriesPoint
from cenep.infrastructure.project_file import load_project, save_project


def _project(**pv_overrides) -> Project:
    config = dict(
        pv_capacity_kwp=1000.0,
        equivalent_hours=1100.0,
        performance_ratio=1.0,
        annual_degradation_rate=0.0,
    )
    config.update(pv_overrides)
    return Project(
        basic_info=BasicInfo(project_name="负荷服务测试", province="湖北", project_type=ProjectType.COMMERCIAL_PV),
        pv=PVConfig(**config),
    )


def _service(project: Project | None = None) -> LoadProfileService:
    project = project or _project()
    return LoadProfileService(project, project_id="TEST-1", bill_service=BillService(project))


def _monthly(energy: float = 100_000.0) -> list[MonthlyLoadEnergy]:
    return [MonthlyLoadEnergy(year=2025, month=m, energy_kwh=energy) for m in range(1, 13)]


def _params(**overrides) -> LoadEstimateParams:
    base = dict(year=2025, interval_minutes=60, template_id="double_shift")
    base.update(overrides)
    return LoadEstimateParams(**base)


# --------------------------------------------------------------------------- #
# 数据集管理（§6.4）
# --------------------------------------------------------------------------- #
class TestDatasetRegistry:
    def test_empty_state(self):
        service = _service()
        assert service.datasets() == []
        assert service.active_dataset() is None

    def test_register_and_activate(self):
        service = _service()
        result = service.estimate_load(_params(), monthly=_monthly())
        assert service.project.active_load_dataset_id == result.dataset.profile_id
        assert len(service.datasets()) == 1

    def test_switching_keeps_history(self):
        """§6.4：切换负荷数据集时必须**保留旧数据集**并记录当前激活版本。"""
        service = _service()
        first = service.estimate_load(
            _params(template_id="single_shift"), monthly=_monthly()
        ).dataset
        second = service.estimate_load(
            _params(template_id="continuous"), monthly=_monthly(120_000.0)
        ).dataset
        assert len(service.datasets()) == 2
        assert service.project.active_load_dataset_id == second.profile_id

        service.activate(first.profile_id)
        assert service.project.active_load_dataset_id == first.profile_id
        assert {item.profile_id for item in service.datasets()} == {
            first.profile_id,
            second.profile_id,
        }

    def test_same_profile_id_replaces_or_reports(self):
        service = _service()
        dataset = service.estimate_load(_params(), monthly=_monthly()).dataset
        service.register(dataset)
        assert len(service.datasets()) == 1
        with pytest.raises(ValidationError) as exc:
            service.register(dataset, replace_existing=False)
        assert "已存在" in str(exc.value)

    def test_dataset_by_id_reports_chinese_error(self):
        service = _service()
        with pytest.raises(ValidationError) as exc:
            service.dataset_by_id("不存在")
        assert "找不到负荷数据集" in str(exc.value)

    def test_remove_falls_back_to_last(self):
        service = _service()
        first = service.estimate_load(_params(template_id="office"), monthly=_monthly()).dataset
        second = service.estimate_load(
            _params(template_id="continuous"), monthly=_monthly(90_000.0)
        ).dataset
        service.remove(second.profile_id)
        assert service.project.active_load_dataset_id == first.profile_id

    def test_clear(self):
        service = _service()
        service.estimate_load(_params(), monthly=_monthly())
        assert service.clear() == 1
        assert service.datasets() == []
        assert service.project.active_load_dataset_id == ""

    def test_measured_and_estimated_separation(self):
        service = _service()
        estimated = service.estimate_load(_params(), monthly=_monthly()).dataset
        assert service.is_measured_dataset(estimated) is False
        assert service.measured_badge(estimated) == "估算数据（不是实测）"
        assert service.measured_datasets() == []
        assert "估算" in service.dataset_source_label(estimated)


# --------------------------------------------------------------------------- #
# 账单电量 → 估算输入（§3.1、§6.5）
# --------------------------------------------------------------------------- #
class TestBillsAsEstimateInput:
    @staticmethod
    def _project_with_bills() -> Project:
        project = _project()
        service = BillService(project, project_id="TEST-1")
        for month in range(1, 7):
            service.create_bill(
                billing_period_start=datetime(2025, month, 1).date(),
                billing_period_end=datetime(2025, month, 28).date(),
                energy_total_kwh=50_000.0 + month,
            )
        # 一条没有总电量的账单：不得参与估算，也不得被当成 0
        service.create_bill(
            billing_period_start=datetime(2025, 7, 1).date(),
            billing_period_end=datetime(2025, 7, 31).date(),
            energy_total_kwh=None,
        )
        return project

    def test_energies_take_total_only_and_label_bill(self):
        project = self._project_with_bills()
        service = LoadProfileService(project, project_id="TEST-1", bill_service=BillService(project))
        energies = service.monthly_energies_from_bills(2025)
        assert [item.month for item in energies] == [1, 2, 3, 4, 5, 6]
        assert all(item.source is LoadEstimateSource.BILL for item in energies)
        assert energies[0].energy_kwh == pytest.approx(50_001.0)
        assert energies[0].month_key == "2025-01"

    def test_year_filter(self):
        project = self._project_with_bills()
        service = LoadProfileService(project, bill_service=BillService(project))
        assert service.monthly_energies_from_bills(2024) == []

    def test_coverage_reports_missing_months(self):
        project = self._project_with_bills()
        service = LoadProfileService(project, bill_service=BillService(project))
        coverage = service.bill_month_coverage(2025)
        assert coverage["covered_months"] == [1, 2, 3, 4, 5, 6]
        assert coverage["missing_months"] == [7, 8, 9, 10, 11, 12]
        assert coverage["count"] == 6
        assert coverage["total_kwh"] == pytest.approx(sum(50_000.0 + m for m in range(1, 7)))

    def test_estimate_from_bills_uses_bill_energies(self):
        project = self._project_with_bills()
        service = LoadProfileService(project, bill_service=BillService(project))
        result = service.estimate_load(
            _params(annual_energy_kwh=600_000.0), monthly=None
        )
        sources = {row.month: row.source for row in result.monthly}
        for month in range(1, 7):
            assert sources[month] is LoadEstimateSource.BILL
        for month in range(7, 13):
            assert sources[month] is LoadEstimateSource.UNIFORM_DEFAULT
        assert result.all_months_regressed is True


# --------------------------------------------------------------------------- #
# 时间轴与光伏出力（§6.4、§9）
# --------------------------------------------------------------------------- #
class TestAxisAndPV:
    def test_full_year_dataset_uses_engine_axis(self):
        service = _service()
        dataset = service.estimate_load(_params(interval_minutes=60), monthly=_monthly()).dataset
        axis = service.axis_of(dataset)
        assert axis.point_count == dataset.point_count
        assert axis.resolution.value == "HOURLY"
        assert axis.timestamps[0] == datetime(2025, 1, 1, 0)

    def test_partial_dataset_uses_generic_axis(self):
        service = _service()
        points = [
            TimeSeriesPoint(timestamp=datetime(2025, 3, 1, hour), load_kwh=10.0)
            for hour in range(6)
        ]
        dataset = HighFrequencyLoadDataset(
            profile_id="partial",
            interval_minutes=60,
            points=points,
            period_start=points[0].timestamp,
            period_end=points[-1].timestamp,
        )
        axis = service.axis_of(dataset)
        assert axis.point_count == 6
        assert axis.month[0] == 3

    def test_zero_capacity_reports_chinese_error(self):
        project = _project(pv_capacity_kwp=0.0)
        service = LoadProfileService(project)
        dataset = service.estimate_load(_params(), monthly=_monthly()).dataset
        with pytest.raises(ValidationError) as exc:
            service.resolve_pv_series(dataset)
        assert "光伏装机容量" in str(exc.value)

    def test_resolve_pv_series_equivalent_hours(self):
        service = _service()
        dataset = service.estimate_load(_params(), monthly=_monthly()).dataset
        series = service.resolve_pv_series(dataset)
        assert series.size == dataset.point_count
        # 年发电量 = 容量 × 等效小时 × PR = 1000 × 1100 × 1.0
        assert float(series.sum()) == pytest.approx(1_100_000.0, rel=1e-6)

    def test_performance_ratio_falls_back_to_project(self):
        project = _project(performance_ratio=0.8)
        service = LoadProfileService(project)
        dataset = service.estimate_load(_params(), monthly=_monthly()).dataset
        series = service.resolve_pv_series(dataset)
        assert float(series.sum()) == pytest.approx(1000.0 * 1100.0 * 0.8, rel=1e-6)

    def test_timeseries_pv_config_preferred(self):
        project = _project()
        project.timeseries.pv = PVProfileConfig(
            mode=PVProfileMode.EQUIVALENT_HOURS,
            equivalent_hours=900.0,
            performance_ratio=0.9,
            capacity_kwp=500.0,
        )
        service = LoadProfileService(project)
        dataset = service.estimate_load(_params(), monthly=_monthly()).dataset
        series = service.resolve_pv_series(dataset)
        assert float(series.sum()) == pytest.approx(500.0 * 900.0 * 0.9, rel=1e-6)

    def test_pv_series_from_dataset_alignment_error(self):
        service = _service()
        load = service.estimate_load(_params(), monthly=_monthly()).dataset
        pv_points = [
            TimeSeriesPoint(timestamp=datetime(2025, 5, 1, hour), load_kwh=1.0) for hour in range(3)
        ]
        pv_dataset = HighFrequencyLoadDataset(
            profile_id="pv",
            interval_minutes=60,
            points=pv_points,
            period_start=pv_points[0].timestamp,
            period_end=pv_points[-1].timestamp,
        )
        with pytest.raises(ValidationError) as exc:
            service.pv_series_from_dataset(pv_dataset, load)
        assert "时间轴不一致" in str(exc.value)

    def test_pv_series_from_dataset_aligned_and_scaled(self):
        service = _service()
        dataset = service.estimate_load(_params(interval_minutes=60), monthly=_monthly()).dataset
        pv_dataset = HighFrequencyLoadDataset(
            profile_id="pv-measured",
            interval_minutes=60,
            points=[
                TimeSeriesPoint(timestamp=point.timestamp, load_kwh=1.0)
                for point in dataset.points
            ],
            period_start=dataset.points[0].timestamp,
            period_end=dataset.points[-1].timestamp,
        )
        scaled = service.pv_series_from_dataset(
            pv_dataset,
            dataset,
            curve_capacity_kwp=1000.0,
            target_capacity_kwp=2000.0,
            performance_ratio=1.0,
        )
        # 1000 kWp 的 1 kWh/点归一化后按 2000 kWp 缩放 → 2 kWh/点
        assert scaled[0] == pytest.approx(2.0)

    def test_pv_provenance_text_flags_template_output(self):
        service = _service()
        text = service.pv_provenance_text(service.project_pv_config(), True)
        assert "估算/模板出力（不是实测）" in text


# --------------------------------------------------------------------------- #
# 消纳分析装配（§6.3 C）
# --------------------------------------------------------------------------- #
class TestAnalyzeAssembly:
    def test_analyze_uses_active_dataset(self):
        service = _service()
        service.estimate_load(_params(), monthly=_monthly())
        result = service.analyze()
        assert result.point_count == 8760
        assert result.load_source_type is LoadDataSourceType.MONTHLY_BILL_ESTIMATE
        assert result.is_based_on_estimate is True
        assert result.energy_balance_error_kwh == pytest.approx(0.0, abs=1e-6)
        assert len(result.monthly) == 12
        assert len(result.calibers) == 4

    def test_analyze_without_dataset_reports_chinese_error(self):
        service = _service()
        with pytest.raises(ValidationError) as exc:
            service.analyze()
        assert "尚未选择负荷数据集" in str(exc.value)

    def test_analyze_accepts_explicit_pv_series(self):
        service = _service()
        dataset = service.estimate_load(_params(), monthly=_monthly()).dataset
        pv = np.full(dataset.point_count, 100.0)
        result = service.analyze(pv_series=pv)
        assert result.pv_generation_kwh == pytest.approx(100.0 * dataset.point_count)

    def test_missing_policy_forwarded(self):
        service = _service()
        dataset = service.estimate_load(_params(), monthly=_monthly()).dataset
        # 直接在该数据集上制造一个缺失点（NaN），验证缺失策略确实被透传到计算层（§6.4）
        target = dataset.points[10]
        dataset.points[10] = TimeSeriesPoint(
            timestamp=target.timestamp, load_kwh=float("nan")
        )
        with pytest.raises(ValidationError):
            service.analyze(pv_series=np.full(dataset.point_count, 50.0))
        result = service.analyze(
            pv_series=np.full(dataset.point_count, 50.0),
            missing_policy=MissingDataPolicy.FORWARD_FILL,
        )
        assert result.interpolated_intervals == 1

    def test_assumptions_carry_dataset_assumptions(self):
        service = _service()
        result_dataset = service.estimate_load(_params(), monthly=_monthly()).dataset
        result = service.analyze()
        assert set(result_dataset.assumptions).issubset(set(result.assumptions))

    def test_portrait_rows_and_typical_days(self):
        service = _service()
        service.estimate_load(_params(interval_minutes=60), monthly=_monthly())
        portrait = service.portrait()
        assert portrait.point_count == 8760
        assert len(portrait.monthly) == 12
        assert [curve.day_type for curve in portrait.typical_days] == ["WORKDAY", "WEEKEND", "ALL"]
        assert len(portrait.typical_days[0].values_kwh) == 24
        assert portrait.estimated is True
        keys = [name for name, _ in portrait.portrait_rows()]
        assert "时间覆盖率" in keys and "负荷率" in keys

    def test_portrait_without_dataset_reports_chinese_error(self):
        service = _service()
        with pytest.raises(ValidationError):
            service.portrait()


# --------------------------------------------------------------------------- #
# 装配与持久化
# --------------------------------------------------------------------------- #
class TestAssemblyAndPersistence:
    def test_project_service_assembles_service(self, tmp_path):
        service = ProjectService(db=None)
        project = _project()
        assembled = service.load_profile_service(project, project_id="A")
        assert isinstance(assembled, LoadProfileService)
        assert assembled.project is project
        assert assembled.project_id == "A"

    def test_datasets_survive_save_and_reopen(self, tmp_path):
        """§8.2：新数据必须随项目文件保存/重开，且旧项目仍可打开。"""
        project = _project()
        service = LoadProfileService(project, bill_service=BillService(project))
        service.estimate_load(_params(interval_minutes=60), monthly=_monthly())
        path = save_project(project, tmp_path / "load.nep")
        reopened = load_project(path)
        assert len(reopened.load_datasets) == 1
        assert reopened.active_load_dataset_id == project.active_load_dataset_id
        dataset = reopened.load_datasets[0]
        assert dataset.source_type is LoadDataSourceType.MONTHLY_BILL_ESTIMATE
        assert dataset.estimated is True
        assert dataset.point_count == 8760

    def test_old_project_without_load_fields_opens(self):
        """旧项目（没有 load_datasets 字段）反序列化后为空列表，不生成虚构数据。"""
        project = Project.model_validate(
            {
                "basic_info": {"project_name": "旧项目", "province": "湖北"},
            }
        )
        assert project.load_datasets == []
        assert project.active_load_dataset_id == ""

    def test_time_series_load_config_unchanged(self):
        """既有字段语义不得改变：``timeseries.load`` 仍是 V2 的 LoadProfileConfig。"""
        project = _project()
        service = LoadProfileService(project, bill_service=BillService(project))
        service.estimate_load(_params(), monthly=_monthly())
        assert isinstance(project.timeseries.load, LoadProfileConfig)
        assert project.timeseries.load.hourly is None


# --------------------------------------------------------------------------- #
# 高频导入复用（§6.1、§6.4）
# --------------------------------------------------------------------------- #
class TestHighFrequencyImportReuse:
    def test_import_file_registers_measured_dataset(self, tmp_path):
        import csv

        path = tmp_path / "load.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["时间", "有功功率(kW)"])
            for hour in range(48):
                day = 1 + hour // 24
                writer.writerow([f"2025-01-{day:02d} {hour % 24:02d}:00", 100.0 + hour])
        service = _service()
        dataset = service.import_file(path)
        assert dataset.source_type is LoadDataSourceType.HIGH_FREQUENCY_IMPORT
        assert dataset.estimated is False
        assert service.is_measured_dataset(dataset) is True
        assert service.measured_badge(dataset) == "实测数据"
        assert service.measured_datasets() == [dataset]

    def test_result_from_measured_dataset_has_no_estimate_flag(self, tmp_path):
        import csv

        path = tmp_path / "load.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["时间", "有功功率(kW)"])
            for hour in range(48):
                day = 1 + hour // 24
                writer.writerow([f"2025-01-{day:02d} {hour % 24:02d}:00", 100.0 + hour])
        service = _service()
        service.import_file(path)
        result = service.analyze()
        assert result.is_based_on_estimate is False
        assert result.load_source_type is LoadDataSourceType.HIGH_FREQUENCY_IMPORT
