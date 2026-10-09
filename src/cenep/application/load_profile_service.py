"""负荷与消纳应用服务：数据集管理、月账单估算、光伏出力解析、消纳分析装配（V2.2 §6.2、§6.3、§6.4）。

分层职责（规格书 §1、§0.2）
--------------------------
* 本模块**只做编排**：估算调用 :mod:`cenep.calculation.load_estimate`，
  消纳调用 :mod:`cenep.calculation.self_consumption`，光伏出力调用
  :mod:`cenep.calculation.pv_profile`（**复用既有光伏引擎，不另写一套**），
  负荷画像调用 :mod:`cenep.calculation.load_portrait`，
  文件读取/列映射/质量检查调用 :mod:`cenep.data.load_profile_importer`
  （阶段 3 已交付，不重复实现）。本模块自身**不含任何业务公式**。
* 界面只与本服务交互，因此 ``tests/test_gui.py`` 的"界面不得引入计算逻辑"静态扫描
  仍然成立（界面不导入 ``cenep.calculation``，只导入本服务）。
* 负荷数据集挂在 :class:`~cenep.domain.models.Project` 的 ``load_datasets`` 段上，
  随 ``.nep`` 项目文件一起保存/重开；**切换数据集时保留旧数据集**并记录
  ``Project.active_load_dataset_id``（§6.4：「保留旧数据集并记录当前激活版本，
  方便复现历史方案」）。
* 报错一律是**中文** :class:`~cenep.calculation.errors.ValidationError`（带字段名）。

来源标签红线（§0.2）
------------------
* 实测曲线只能来自 :func:`~cenep.data.load_profile_importer.apply_load_import`
  （``source_type=high_frequency_import``）；
* 月账单估算曲线只能来自 :func:`~cenep.calculation.load_estimate.estimate_from_monthly_energy`
  （``source_type=monthly_bill_estimate``）；
* 二者在 :class:`~cenep.domain.load_data.HighFrequencyLoadDataset` 构造时即被强制自洽；
* :meth:`LoadProfileService.analyze` 把来源标签原样传给消纳结果，
  再由 :class:`~cenep.domain.self_consumption_result.SelfConsumptionResult`
  在模型层再拦一次——估算数据不可能被标成实测。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import numpy as np

from ..calculation.errors import ValidationError
from ..calculation.load_estimate import estimate_from_monthly_energy
from ..calculation.load_portrait import LoadPortrait, build_portrait
from ..calculation.pv_profile import resolve_pv_series
from ..calculation.self_consumption import analyze_dispatch_outcome, analyze_self_consumption
from ..calculation.timeseries_engine import (
    TimeAxis,
    axis_from_timestamps,
    build_time_axis,
    points_per_year,
)
from ..data.load_profile_importer import (
    LoadImportPreview,
    apply_load_import,
    list_load_blocks,
    list_sheets,
    preview_block_import,
    preview_load_import,
)
from ..domain.enums import (
    LoadEstimateSource,
    MissingDataPolicy,
    PVProfileMode,
    Resolution,
)
from ..domain.load_data import HighFrequencyLoadDataset, provides_measured_curve
from ..domain.load_estimate import (
    LoadEstimateParams,
    LoadEstimateResult,
    MonthlyLoadEnergy,
    TypicalLoadTemplate,
)
from ..domain.models import Project
from ..domain.self_consumption_result import SelfConsumptionResult
from ..domain.timeseries import PVProfile, PVProfileConfig, TimeSeriesPoint

logger = logging.getLogger(__name__)

__all__ = ["LoadProfileService"]

#: 光伏曲线来源为"模板/默认形状"的模式（此时光伏出力形状本身也是估算，须随结果标注，§0.2）
_TEMPLATE_PV_MODES: frozenset[PVProfileMode] = frozenset(
    {
        PVProfileMode.TYPICAL_DAY,
        PVProfileMode.MONTHLY_HOUR_FACTOR,
        PVProfileMode.EQUIVALENT_HOURS,
    }
)


class LoadProfileService:
    """一个项目的负荷数据集与消纳分析服务（V2.2 §6.2、§6.3）。"""

    def __init__(
        self,
        project: Project,
        *,
        project_id: str | None = None,
        bill_service=None,
    ) -> None:
        """
        :param project: 目标项目（本服务直接读写其 ``load_datasets`` 段）
        :param project_id: 数据集的项目标识；默认取项目名称（与 ``BillService`` 同口径）
        :param bill_service: 可选的 :class:`~cenep.application.bill_service.BillService`，
            用于把月度账单电量直接转成估算输入（``None`` 时按需自行装配）
        """
        self.project = project
        self._project_id = project_id
        self._bill_service = bill_service

    # ------------------------------------------------------------------ #
    # 基本属性
    # ------------------------------------------------------------------ #
    @property
    def project_id(self) -> str:
        if self._project_id:
            return self._project_id
        return self.project.basic_info.project_name or "未命名项目"

    @property
    def bill_service(self):
        """按需装配账单服务（只做装配，不复制任何账单逻辑）。"""
        if self._bill_service is None:
            from .bill_service import BillService

            self._bill_service = BillService(self.project, project_id=self.project_id)
        return self._bill_service

    # ------------------------------------------------------------------ #
    # 数据集管理（§6.4：保留旧数据集 + 记录当前激活版本）
    # ------------------------------------------------------------------ #
    def datasets(self) -> list[HighFrequencyLoadDataset]:
        """全部负荷数据集（按登记顺序；实测与估算并存）。"""
        return list(self.project.load_datasets)

    def measured_datasets(self) -> list[HighFrequencyLoadDataset]:
        """只返回可作为**实测**结论的数据集（§0.2 唯一判定入口）。"""
        return [item for item in self.project.load_datasets if provides_measured_curve(item)]

    def dataset_by_id(self, profile_id: str) -> HighFrequencyLoadDataset:
        """按 ``profile_id`` 取数据集。

        :raises ValidationError: 找不到（中文报错，附可用清单）
        """
        for dataset in self.project.load_datasets:
            if dataset.profile_id == profile_id:
                return dataset
        available = "、".join(item.profile_id for item in self.project.load_datasets) or "（空）"
        raise ValidationError(
            f"找不到负荷数据集「{profile_id}」；当前可用：{available}",
            field="load.dataset_id",
        )

    def active_dataset(self) -> HighFrequencyLoadDataset | None:
        """当前激活的负荷数据集；未选择时返回 ``None``（界面显示空状态，不臆造数据）。"""
        target = self.project.active_load_dataset_id
        if not target:
            return None
        for dataset in self.project.load_datasets:
            if dataset.profile_id == target:
                return dataset
        return None

    def register(
        self,
        dataset: HighFrequencyLoadDataset,
        *,
        activate: bool = True,
        replace_existing: bool = True,
    ) -> HighFrequencyLoadDataset:
        """登记一个负荷数据集，并按需设为当前激活版本。

        :param activate: 是否同时激活（§6.4 要求记录当前激活版本）
        :param replace_existing: 同 ``profile_id`` 已存在时是否覆盖；``False`` 时抛中文错误
        :raises ValidationError: ``replace_existing=False`` 且同名数据集已存在
        """
        existing = [item.profile_id for item in self.project.load_datasets]
        if dataset.profile_id in existing:
            if not replace_existing:
                raise ValidationError(
                    f"负荷数据集「{dataset.profile_id}」已存在；"
                    f"如需保留历史版本请修改数据集名称后再导入（V2.2 §6.4）",
                    field="load.dataset_id",
                )
            self.project.load_datasets[existing.index(dataset.profile_id)] = dataset
        else:
            self.project.load_datasets.append(dataset)
        if activate:
            self.project.active_load_dataset_id = dataset.profile_id
        logger.info(
            "登记负荷数据集：%s（来源=%s、估算=%s、激活=%s）",
            dataset.profile_id,
            dataset.source_type.label,
            dataset.estimated,
            self.project.active_load_dataset_id == dataset.profile_id,
        )
        return dataset

    def activate(self, profile_id: str) -> HighFrequencyLoadDataset:
        """切换当前激活的数据集（旧数据集**保留**在列表中，可随时切回，§6.4）。"""
        dataset = self.dataset_by_id(profile_id)
        self.project.active_load_dataset_id = dataset.profile_id
        return dataset

    def remove(self, profile_id: str) -> HighFrequencyLoadDataset:
        """删除一个数据集（仅在用户明确要求时调用；删除后激活项自动回落到最后一条）。"""
        dataset = self.dataset_by_id(profile_id)
        self.project.load_datasets = [
            item for item in self.project.load_datasets if item.profile_id != profile_id
        ]
        if self.project.active_load_dataset_id == profile_id:
            self.project.active_load_dataset_id = (
                self.project.load_datasets[-1].profile_id if self.project.load_datasets else ""
            )
        return dataset

    def clear(self) -> int:
        """清空全部负荷数据集；返回被删除的条数。"""
        count = len(self.project.load_datasets)
        self.project.load_datasets = []
        self.project.active_load_dataset_id = ""
        return count

    # ------------------------------------------------------------------ #
    # 月电量来源（§3.2、§6.5）
    # ------------------------------------------------------------------ #
    def monthly_energies_from_bills(self, year: int | None = None) -> list[MonthlyLoadEnergy]:
        """把项目已录入的月度账单电量转成估算输入（``source=BILL``）。

        只取 ``energy_total_kwh`` 非空的账单（分项电量不参与估算，避免口径不明）；
        同一个月有多张账单时**相加**并在备注里写明张数（§3.1：仅在周期完整且不重叠时才允许
        直接相加，这里不静默合并口径，而是把张数写进备注供用户核对）。
        """
        grouped: dict[str, list[float]] = {}
        for bill in self.bill_service.bills:
            if bill.energy_total_kwh is None:
                continue
            if year is not None and bill.billing_period_start.year != year:
                continue
            grouped.setdefault(bill.billing_month, []).append(float(bill.energy_total_kwh))

        energies: list[MonthlyLoadEnergy] = []
        for month_key in sorted(grouped):
            try:
                year_str, month_str = month_key.split("-")
                month_year, month = int(year_str), int(month_str)
            except (ValueError, AttributeError):
                raise ValidationError(
                    f"账单账期「{month_key}」不是 YYYY-MM 格式，无法用于月电量估算；"
                    f"请检查账单录入（V2.2 §6.5）",
                    field="load.monthly_energy.billing_month",
                ) from None
            values = grouped[month_key]
            total = float(sum(values))
            if total <= 0.0:
                continue
            energies.append(
                MonthlyLoadEnergy(
                    year=month_year,
                    month=month,
                    energy_kwh=total,
                    source=LoadEstimateSource.BILL,
                    note=(
                        f"来自 {len(values)} 张账单的总电量合计"
                        if len(values) > 1
                        else "来自账单总电量"
                    ),
                )
            )
        return energies

    def bill_month_coverage(self, year: int) -> dict[str, object]:
        """该年度账单电量对 12 个月的覆盖情况（供界面提示"缺哪几个月"）。"""
        energies = self.monthly_energies_from_bills(year)
        covered = sorted(item.month for item in energies)
        return {
            "year": year,
            "covered_months": covered,
            "missing_months": sorted(set(range(1, 13)) - set(covered)),
            "total_kwh": float(sum(item.energy_kwh for item in energies)),
            "count": len(energies),
        }

    # ------------------------------------------------------------------ #
    # 月账单估算（§3.2、§6.5）
    # ------------------------------------------------------------------ #
    def estimate_load(
        self,
        params: LoadEstimateParams,
        *,
        monthly: list[MonthlyLoadEnergy] | None = None,
        template: TypicalLoadTemplate | None = None,
        register: bool = True,
    ) -> LoadEstimateResult:
        """由"可编辑模板 + 月电量"生成估算曲线，并按需登记为当前数据集（§6.5）。

        :param monthly: 月电量；``None`` 时自动取项目账单电量（``source=BILL``）
        :param template: 自定义模板；``None`` 时按 ``params.template_id`` 取内置模板副本
        :param register: 是否登记进项目（``True`` 时同时激活，界面立即可用）
        :raises ValidationError: 参数或数据不自洽（中文报错）
        """
        energies = (
            list(monthly) if monthly is not None else self.monthly_energies_from_bills(params.year)
        )
        result = estimate_from_monthly_energy(
            energies, params, template=template, project_id=self.project_id
        )
        if register:
            self.register(result.dataset)
        return result

    # ------------------------------------------------------------------ #
    # 高频负荷导入（复用阶段 3 的导入器，§6.1、§6.4）
    # ------------------------------------------------------------------ #
    @staticmethod
    def list_sheets(path: str | Path) -> list[str]:
        """列出工作表（导入流程第 ② 步）。"""
        return list(list_sheets(path))

    @staticmethod
    def list_blocks(path: str | Path, sheet: str | None = None) -> dict[str, list[str]]:
        """列出宽表数据块（时刻行 × 日期列形态）。"""
        return list_load_blocks(path, sheet)

    @staticmethod
    def preview_import(path: str | Path, **kwargs) -> LoadImportPreview:
        """生成导入预览（长表优先，宽表自动回退）；全部口径与质量检查在 ``data`` 层。"""
        return preview_load_import(path, **kwargs)

    @staticmethod
    def preview_block_import(path: str | Path, **kwargs) -> LoadImportPreview:
        """按宽表数据块生成导入预览。"""
        return preview_block_import(path, **kwargs)

    def import_preview(
        self,
        preview: LoadImportPreview,
        *,
        activate: bool = True,
        allow_invalid: bool = False,
        resample_to_minutes: int | None = None,
    ) -> HighFrequencyLoadDataset:
        """确认导入：把预览落实为数据集并登记（第 ⑥ 步）。

        ``source_type`` 固定沿用预览（实测高频导入）；本方法**不会**把它改成估算，
        也不会把估算数据标成实测（§0.2 红线）。
        """
        dataset = apply_load_import(
            preview,
            project_id=self.project_id,
            allow_invalid=allow_invalid,
            resample_to_minutes=resample_to_minutes,
        )
        return self.register(dataset, activate=activate)

    def import_file(
        self,
        path: str | Path,
        *,
        activate: bool = True,
        allow_invalid: bool = False,
        **kwargs,
    ) -> HighFrequencyLoadDataset:
        """一步完成"读取 + 预览 + 导入"（供脚本与自动化测试使用）。

        界面仍应走"预览 → 用户确认 → 导入"的分步流程（§8.4）。
        """
        preview = self.preview_import(path, **kwargs)
        return self.import_preview(preview, activate=activate, allow_invalid=allow_invalid)

    # ------------------------------------------------------------------ #
    # 光伏出力（复用 pv_profile，§9、§6.4）
    # ------------------------------------------------------------------ #
    def axis_of(self, dataset: HighFrequencyLoadDataset) -> TimeAxis:
        """数据集的时间轴（与既有 :class:`TimeAxis` 同语义，供光伏/调度引擎复用）。"""
        resolution = Resolution.from_interval_minutes(dataset.interval_minutes)
        if resolution is None:
            raise ValidationError(
                f"负荷数据集间隔 {dataset.interval_minutes} 分钟没有对应的内部分辨率，"
                f"无法与光伏曲线对齐（V2.2 §6.4）",
                field="load.dataset_id",
            )
        stamps = [point.timestamp for point in dataset.points]
        # 完整年度的数据集直接用既有整年轴，保证与 V2 时序引擎逐位一致；
        # 部分区间（真实资料常见）用通用轴构造函数，**不补点、不裁剪**。
        first = stamps[0]
        expected = points_per_year(first.year, resolution)
        if (
            (first.month, first.day, first.hour, first.minute) == (1, 1, 0, 0)
            and dataset.point_count == expected
        ):
            return build_time_axis(first.year, resolution)
        return axis_from_timestamps(stamps, resolution)

    def project_pv_config(self) -> PVProfileConfig:
        """项目的光伏出力配置：优先用 ``timeseries.pv``；未配置时回落到 V1 年度参数。

        回落口径（必须明确）：``timeseries.pv.mode == EQUIVALENT_HOURS`` 且
        ``equivalent_hours <= 0`` 说明时序光伏从未配置过，此时改用 ``project.pv`` 的
        容量 / 等效小时 / 性能比（V1 口径），保证"只有年度参数的项目"也能做消纳分析。
        """
        config = self.project.timeseries.pv
        if config.mode is PVProfileMode.EQUIVALENT_HOURS and config.equivalent_hours <= 0.0:
            return PVProfileConfig(
                mode=PVProfileMode.EQUIVALENT_HOURS,
                equivalent_hours=float(self.project.pv.equivalent_hours),
                performance_ratio=float(self.project.pv.performance_ratio),
                capacity_kwp=float(self.project.pv.pv_capacity_kwp or 0.0) or None,
            )
        return config

    def resolve_pv_series(
        self,
        dataset: HighFrequencyLoadDataset,
        *,
        config: PVProfileConfig | None = None,
        capacity_kwp: float | None = None,
        performance_ratio: float | None = None,
        year_index: int = 1,
        degradation_rate: float | None = None,
    ) -> np.ndarray:
        """按项目光伏参数在负荷数据集的时间轴上解析逐间隔发电量（kWh），复用 §9 引擎。

        :raises ValidationError: 容量/等效小时等参数缺失或非法（中文报错）
        """
        axis = self.axis_of(dataset)
        pv_config = config if config is not None else self.project_pv_config()
        capacity = (
            float(capacity_kwp)
            if capacity_kwp is not None
            else float(pv_config.capacity_kwp or self.project.pv.pv_capacity_kwp or 0.0)
        )
        if capacity <= 0.0:
            raise ValidationError(
                "光伏装机容量为 0，无法进行消纳分析；请先在「光伏参数」里填写装机容量"
                "（V2.2 §6.3 C）",
                field="pv.pv_capacity_kwp",
            )
        if performance_ratio is not None:
            pv_config = pv_config.model_copy(
                update={"performance_ratio": float(performance_ratio)}
            )
        elif pv_config.performance_ratio == 1.0 and self.project.pv.performance_ratio:
            # timeseries.pv 未显式给出性能比（默认 1.0）时，采用 V1 的项目性能比
            pv_config = pv_config.model_copy(
                update={"performance_ratio": float(self.project.pv.performance_ratio)}
            )
        rate = (
            float(degradation_rate)
            if degradation_rate is not None
            else float(self.project.pv.annual_degradation_rate)
        )
        return resolve_pv_series(
            pv_config, axis, capacity, year_index=year_index, degradation_rate=rate
        )

    def pv_series_from_dataset(
        self,
        pv_dataset: HighFrequencyLoadDataset,
        load_dataset: HighFrequencyLoadDataset,
        *,
        curve_capacity_kwp: float | None = None,
        target_capacity_kwp: float | None = None,
        performance_ratio: float = 1.0,
    ) -> np.ndarray:
        """由**导入的光伏出力数据集**解析发电量，并对齐到负荷数据集的时间轴（§6.4、§9.1）。

        对齐与换算口径（两条分支，与 :func:`cenep.calculation.pv_profile.resolve_pv_series`
        的 HOURLY 分支**完全一致**，不另写一套）：

        * ``curve_capacity_kwp`` 与 ``target_capacity_kwp`` 都给定时：
          先按 ``P/Δt ÷ 曲线容量`` 归一化为出力系数，再乘 ``目标容量 × 性能比 × Δt``
          （即"按目标装机缩放"）；
        * 否则：把导入曲线视为**目标容量下的实际发电量**，只乘 ``performance_ratio``。

        :raises ValidationError: 两条曲线的时间轴不一致（中文报错，附缺失时间戳示例）
        """
        load_stamps = [point.timestamp for point in load_dataset.points]
        pv_map = {point.timestamp: float(point.load_kwh) for point in pv_dataset.points}
        missing = [stamp for stamp in load_stamps if stamp not in pv_map]
        if missing:
            sample = "、".join(f"{t:%Y-%m-%d %H:%M}" for t in missing[:3])
            raise ValidationError(
                f"光伏曲线与负荷曲线的时间轴不一致：负荷有 {len(load_stamps)} 点，"
                f"其中 {len(missing)} 点在光伏曲线中找不到（示例：{sample}）。"
                f"两者必须使用同一时间轴、同一时区、同时间隔；如粒度不同请先用重采样明确对齐"
                f"（V2.2 §6.4）",
                field="load.align.pv",
            )
        series = np.asarray([pv_map[stamp] for stamp in load_stamps], dtype=float)

        if curve_capacity_kwp and target_capacity_kwp:
            resolution = Resolution.from_interval_minutes(load_dataset.interval_minutes)
            config = PVProfileConfig(
                mode=PVProfileMode.HOURLY,
                hourly=PVProfile(
                    profile_id=pv_dataset.profile_id,
                    name=pv_dataset.name,
                    resolution=resolution or Resolution.HOURLY,
                    source=pv_dataset.source_file_name,
                    points=[
                        TimeSeriesPoint(timestamp=stamp, pv_generation_kwh=float(value))
                        for stamp, value in zip(load_stamps, series)
                    ],
                    capacity_kwp=float(curve_capacity_kwp),
                ),
                performance_ratio=float(performance_ratio),
                capacity_kwp=float(target_capacity_kwp),
            )
            return resolve_pv_series(
                config,
                self.axis_of(load_dataset),
                float(target_capacity_kwp),
                year_index=1,
                degradation_rate=0.0,
            )
        return series * float(performance_ratio)

    # ------------------------------------------------------------------ #
    # 消纳分析（§3.3、§6.3 C）
    # ------------------------------------------------------------------ #
    def load_series(self, dataset: HighFrequencyLoadDataset) -> np.ndarray:
        """负荷数据集的逐间隔电量（kWh）；缺失值（``NaN``）**原样保留**，不按 0 处理（§6.4）。"""
        return np.asarray(dataset.interval_energy_kwh, dtype=float)

    def portrait(self, dataset: HighFrequencyLoadDataset | None = None) -> LoadPortrait:
        """负荷画像（§6.3 B）。

        :raises ValidationError: 尚未选择数据集（中文报错）
        """
        target = dataset or self.active_dataset()
        if target is None:
            raise ValidationError(
                "尚未选择负荷数据集；请先导入高频负荷或生成月账单估算曲线（V2.2 §6.3 A）",
                field="load.dataset_id",
            )
        return build_portrait(target)

    def analyze(
        self,
        dataset: HighFrequencyLoadDataset | None = None,
        *,
        pv_series: np.ndarray | None = None,
        pv_config: PVProfileConfig | None = None,
        missing_policy: MissingDataPolicy = MissingDataPolicy.REJECT,
        tolerance_kwh: float = 1e-6,
        extra_assumptions: tuple[str, ...] = (),
    ) -> SelfConsumptionResult:
        """计算光伏消纳四项指标（§3.3），并携带来源标签与完整口径。

        :param dataset: 负荷数据集；``None`` 时取当前激活的数据集
        :param pv_series: 光伏逐间隔发电量 kWh；``None`` 时按项目光伏参数解析（§9）
        :param missing_policy: 缺失点处理策略（默认拒算，§6.4）
        :raises ValidationError: 无数据集、容量为 0、长度不一致（全部中文报错）
        """
        target = dataset or self.active_dataset()
        if target is None:
            raise ValidationError(
                "尚未选择负荷数据集，无法计算消纳率；请先导入高频负荷或生成月账单估算曲线"
                "（V2.2 §6.3 A）",
                field="load.dataset_id",
            )
        resolved_config = pv_config if pv_config is not None else self.project_pv_config()
        pv = (
            np.asarray(pv_series, dtype=float)
            if pv_series is not None
            else self.resolve_pv_series(target, config=resolved_config)
        )
        pv_is_estimate = (
            pv_series is None and resolved_config.mode in _TEMPLATE_PV_MODES
        )
        assumptions = list(target.assumptions) + list(extra_assumptions)
        return analyze_self_consumption(
            self.load_series(target),
            pv,
            interval_minutes=target.interval_minutes,
            timestamps=[point.timestamp for point in target.points],
            load_source_type=target.source_type,
            load_provenance_text=target.provenance_text,
            pv_provenance_text=self.pv_provenance_text(resolved_config, pv_is_estimate),
            pv_source_is_estimate=pv_is_estimate,
            coverage_ratio=target.coverage_ratio,
            data_quality_status=target.quality_status,
            missing_policy=missing_policy,
            tolerance_kwh=tolerance_kwh,
            extra_assumptions=assumptions,
        )

    def analyze_outcome(
        self,
        outcome,
        *,
        dataset: HighFrequencyLoadDataset | None = None,
        timestamps: list[datetime] | None = None,
        tolerance_kwh: float = 1e-6,
    ) -> SelfConsumptionResult:
        """由既有储能调度结果汇总消纳指标（§3.3、§6.6：复用 dispatch 与能量平衡引擎）。"""
        target = dataset or self.active_dataset()
        if target is None:
            raise ValidationError(
                "尚未选择负荷数据集，无法汇总含储能的消纳结果（V2.2 §6.3 A）",
                field="load.dataset_id",
            )
        stamps = timestamps or [point.timestamp for point in target.points]
        config = self.project_pv_config()
        return analyze_dispatch_outcome(
            outcome,
            interval_minutes=target.interval_minutes,
            timestamps=stamps,
            load_source_type=target.source_type,
            load_provenance_text=target.provenance_text,
            pv_provenance_text=self.pv_provenance_text(
                config, config.mode in _TEMPLATE_PV_MODES
            ),
            coverage_ratio=target.coverage_ratio,
            data_quality_status=target.quality_status,
            tolerance_kwh=tolerance_kwh,
            extra_assumptions=tuple(target.assumptions),
        )

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def pv_provenance_text(config: PVProfileConfig, is_estimate: bool) -> str:
        """光伏出力的来源说明（必须与负荷来源分列显示，§0.2、§8.1）。"""
        badge = "估算/模板出力（不是实测）" if is_estimate else "实测/导入出力"
        return (
            f"{config.mode.label}｜{badge}；容量 {config.capacity_kwp or 0.0:g} kWp、"
            f"等效小时 {config.equivalent_hours:g} h、性能比 {config.performance_ratio:.4f}"
        )

    def is_measured_dataset(self, dataset: HighFrequencyLoadDataset) -> bool:
        """该数据集能否作为**实测**结论（唯一判定入口，§0.2）。"""
        return provides_measured_curve(dataset)

    def dataset_source_label(self, dataset: HighFrequencyLoadDataset) -> str:
        """界面用来源标签文本（实测 / 估算一眼可辨，§6.3 A）。"""
        return dataset.provenance_text

    def measured_badge(self, dataset: HighFrequencyLoadDataset) -> str:
        """来源徽标：实测为绿色文字，估算为橙色警告文字（§6.3 A 的"一眼可辨"）。"""
        return "实测数据" if provides_measured_curve(dataset) else dataset.source_type.report_badge
