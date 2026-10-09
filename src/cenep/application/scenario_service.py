"""光储四场景联动与收益去重的**应用服务**（V2.3 §3.5、§7.1、§7.5、§7.6、§10 阶段 6）。

分层职责（§1、§0.2）
------------------
* 本服务**只做编排与数据装配**：曲线解析调用既有的
  :class:`~cenep.application.load_profile_service.LoadProfileService` 与
  :mod:`cenep.calculation.pv_profile`，电价版本从
  :class:`~cenep.policy.tariff_plan_store.TariffPlanStore` 读取，
  四场景比较与收益去重调用
  :mod:`cenep.calculation.scenario_bill_engine`，
  财务接入调用既有的 :class:`~cenep.calculation.engine.CalculationEngine`。
* **界面不得实现任何公式**：阶段 7 的 UI 只允许调用本服务的公开方法（§0.2 红线）。
* 报错一律是**中文** :class:`~cenep.calculation.errors.ValidationError`（带字段名）。

本阶段交付的能力
--------------
1. 列出/选择参考场景（默认光储，仅光伏项目应改为仅光伏）；
2. 在**同一负荷、同一电价计划**下跑无光伏 / 仅光伏 / 仅储能 / 光储四场景（§7.1）；
3. 输出方案电费差额、需量变化与分项费用（§7.1、§7.5）；
4. 用**唯一去重后的年度节省额**构造财务输入并交给既有财务引擎（§3.5、§7.6）；
5. 报告用行数据（供阶段 7 的 Excel/PDF 直接使用）。

**不含**（属阶段 7）：UI 页面、Excel/PDF 新增工作表与章节、项目文件迁移。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ..calculation.errors import ValidationError
from ..calculation.scenario_bill_engine import (
    ScenarioConfig,
    build_finance_overrides,
    compare_four_scenarios,
    extract_government_fund_yuan_per_kwh,
    government_fund_included_in_tou_price,
)
from ..calculation.tariff_plan_engine import effective_period_prices
from ..domain.enums import DispatchStrategy, TariffPeriod
from ..domain.models import Project
from ..domain.scenario_bill import (
    SCENARIO_ORDER,
    SUPPORTED_SCENARIO_KINDS,
    ScenarioBillComparison,
    ScenarioBillLine,
    ScenarioBillSet,
    ScenarioDedupManifest,
    ScenarioKind,
)
from ..domain.timeseries import StorageDispatchConfig
from ..infrastructure.db import Database
from ..policy.tariff_plan_store import TariffPlanStore

logger = logging.getLogger(__name__)

__all__ = ["ScenarioBillService"]

#: 光储场景的默认参考口径（"光储"是优先业务范围，见规格书"优先业务范围"一节）
DEFAULT_REFERENCE_SCENARIO = ScenarioKind.PV_STORAGE


class ScenarioBillService:
    """四场景账单联动服务（V2.3 §7.1、§7.6）。

    :param project: 项目（负荷与光伏参数来自它；账单与电价计划另有来源，见下）
    :param db: SQLite 库；``None`` 时电价版本库只存在于内存（测试友好；正式使用必须持久化，§8.2）
    :param store: 电价计划版本库；``None`` 时按 ``db`` 新建
    :param load_profile_service: 负荷曲线服务；``None`` 时按需惰性新建

    说明（§0.2 最小侵入）：本项目**不新增任何项目文件字段**，
    因此"用哪份负荷数据集 / 用哪份电价计划"由调用方在方法参数上显式给出，
    不写进 ``Project``，避免静默改变既有字段语义。
    """

    def __init__(
        self,
        project: Project,
        *,
        db: Database | None = None,
        store: TariffPlanStore | None = None,
        load_profile_service=None,
    ) -> None:
        self.project = project
        self.store = store or TariffPlanStore(db)
        self._load_service = load_profile_service

    # ------------------------------------------------------------------ #
    # 依赖装配
    # ------------------------------------------------------------------ #
    @property
    def load_service(self):
        """负荷曲线服务（惰性构造，避免无负荷数据的项目被无关依赖拖慢）。"""
        if self._load_service is None:
            from .load_profile_service import LoadProfileService

            self._load_service = LoadProfileService(self.project)
        return self._load_service

    # ------------------------------------------------------------------ #
    # 选择与说明
    # ------------------------------------------------------------------ #
    def supported_scenarios(self) -> list[ScenarioKind]:
        """四类场景（固定顺序，§7.1）。"""
        return list(SUPPORTED_SCENARIO_KINDS)

    def describe_scenarios(self) -> str:
        """四场景的中文说明（界面"让用户选择并核对"用，§0.2 不替用户决定）。"""
        lines = ["规格书 §7.1 要求的四类场景（同一负荷、同一电价计划下比较）："]
        for kind in SCENARIO_ORDER:
            lines.append(f"· {kind.label}")
        lines.append(
            "说明：财务现金流默认取『光伏 + 储能』场景的唯一去重后年度收益；"
            "仅光伏项目请显式指定 reference_scenario=仅光伏（V2.3 §3.5）"
        )
        return "\n".join(lines)

    def list_tariff_plans(self, *, province: str = "湖北"):
        """可用的电价计划（版本化，含官方版与项目资料版并存，§4.1）。"""
        return self.store.list_plans(province=province)

    def describe_tariff_plan(self, tariff_plan_id: str) -> str:
        """电价计划的来源与适用性一行说明（报告必须能追溯到文号与生效期，§4.1）。"""
        plan = self.store.get_plan(tariff_plan_id)
        prices = effective_period_prices(plan)
        parts = [
            f"{plan.display_name}",
            f"状态：{plan.status.label}",
            f"适用范围：{'、'.join(plan.applicable_voltage_levels) or '未限定'}"
            f"/{'、'.join(plan.applicable_tariff_structures) or '未限定'}",
            "分时电价：" + "、".join(
                f"{period.label} {prices[period]:.6f}"
                for period in TariffPeriod
                if prices.get(period) is not None
            ),
            f"需量电价：{plan.demand_charge_yuan_per_kw_month} 元/千瓦·月"
            f"（计费方式：{plan.demand_billing_mode.label}）",
            f"政府性基金及附加：{extract_government_fund_yuan_per_kwh(plan)} 元/千瓦时"
            f"（已含在分时电价内：{government_fund_included_in_tou_price(plan)}）",
            f"来源：{plan.source_text}",
        ]
        return "；".join(parts)

    # ------------------------------------------------------------------ #
    # 四场景比较（§7.1、§7.5、§7.6）
    # ------------------------------------------------------------------ #
    def compare(
        self,
        tariff_plan_id: str,
        *,
        config: ScenarioConfig | None = None,
        dataset=None,
        pv_series: np.ndarray | None = None,
        dispatch_config: StorageDispatchConfig | None = None,
        reference_scenario: ScenarioKind = DEFAULT_REFERENCE_SCENARIO,
        require_verified: bool = True,
        basic_charge_yuan: float = 0.0,
        storage_capacity_revenue_yuan: float | None = None,
        storage_ancillary_revenue_yuan: float | None = None,
        storage_other_revenue_yuan: float | None = None,
    ) -> ScenarioBillSet:
        """在同一负荷、同一电价计划下生成四场景并完成收益去重（§7.1、§7.6）。

        数据装配口径（全部复用既有实现，**不新建第二套引擎**）：

        * **负荷**：``dataset`` 给定时用该数据集；否则用
          :meth:`~cenep.application.load_profile_service.LoadProfileService.active_dataset`
          当前激活的数据集（缺失点按数据集既定策略处理，§6.4）；
        * **光伏**：``pv_series`` 给定时直接用（例如由导入的光伏曲线换算，§6.4）；
          否则调用既有 :meth:`~cenep.application.load_profile_service.LoadProfileService.resolve_pv_series`
          在负荷时间轴上解析；
        * **电价**：由 ``tariff_plan_id`` 指定的**版本化电价计划**解析（§2.3、§4.2）；
        * **调度**：``dispatch_config`` 给定时用；否则用 ``project.timeseries.dispatch``
          （即用户在项目里配置的策略，§12–§14）。

        :param reference_scenario: 作为财务输入的参考场景（默认光储；仅光伏项目请传仅光伏）
        :param storage_capacity_revenue_yuan: 储能容量收益覆盖（元/年）；``None`` 时取项目参数
        :raises ValidationError: 无可用负荷数据集 / 光伏容量为 0 / 电价计划不可用（全部**中文**报错）
        """
        plan = self.store.get_plan(tariff_plan_id)
        cfg = config or ScenarioConfig()
        target = dataset or self.load_service.active_dataset()
        if target is None:
            raise ValidationError(
                "尚未选择负荷数据集，无法做四场景电费对比；请先导入高频负荷曲线，"
                "或基于月账单生成估算负荷曲线（V2.3 §7.1、V2.2 §6.3 A）",
                field="load.dataset_id",
            )

        axis = self.load_service.axis_of(target)
        load = self.load_service.load_series(target)
        if pv_series is None:
            pv_capacity = float(
                self.load_service.project_pv_config().capacity_kwp
                or self.project.pv.pv_capacity_kwp
                or 0.0
            )
            if pv_capacity <= 0.0:
                raise ValidationError(
                    "光伏装机容量为 0，无法生成『仅光伏』与『光储』场景；"
                    "请先在「光伏参数」里填写装机容量（V2.3 §7.1）",
                    field="pv.pv_capacity_kwp",
                )
            resolved_pv = self.load_service.resolve_pv_series(target)
        else:
            resolved_pv = np.asarray(pv_series, dtype=float)
            pv_capacity = float(
                self.load_service.project_pv_config().capacity_kwp
                or self.project.pv.pv_capacity_kwp
                or 0.0
            )
        if resolved_pv.size != axis.point_count:
            raise ValidationError(
                f"光伏曲线点数（{resolved_pv.size}）与负荷时间轴点数（{axis.point_count}）不一致；"
                "两者必须使用同一时间轴、同一时区、同时间隔（V2.3 §7.1、V2.2 §6.4）",
                field="scenario.pv",
            )

        resolved_dispatch = dispatch_config or self.project.timeseries.dispatch
        is_measured = bool(self.load_service.is_measured_dataset(target))
        load_source = f"{target.provenance_text}（{'实测' if is_measured else '估算'}）"

        storage_capacity_revenue = (
            float(storage_capacity_revenue_yuan)
            if storage_capacity_revenue_yuan is not None
            else float(self.project.storage.annual_capacity_revenue)
        )
        storage_ancillary_revenue = (
            float(storage_ancillary_revenue_yuan)
            if storage_ancillary_revenue_yuan is not None
            else float(self.project.storage.annual_ancillary_revenue)
        )
        storage_other_revenue = (
            float(storage_other_revenue_yuan)
            if storage_other_revenue_yuan is not None
            else float(self.project.storage.annual_other_revenue)
        )

        result = compare_four_scenarios(
            plan=plan,
            axis=axis,
            load=load,
            pv=resolved_pv,
            dispatch_config=resolved_dispatch,
            project_pv_capacity_kwp=pv_capacity,
            project_storage_power_kw=float(self.project.storage.storage_power_kw),
            project_storage_energy_kwh=float(self.project.storage.storage_energy_kwh),
            config=cfg,
            project=self.project,
            require_verified=require_verified,
            basic_charge_yuan=float(basic_charge_yuan),
            load_source=load_source,
            load_is_measured=is_measured,
            storage_capacity_revenue_yuan=storage_capacity_revenue,
            storage_ancillary_revenue_yuan=storage_ancillary_revenue,
            storage_other_revenue_yuan=storage_other_revenue,
            reference_scenario=reference_scenario,
        )
        logger.info(
            "四场景服务完成：计划=%s 数据集=%s 唯一去重收益=%.2f 元 去重=%s",
            tariff_plan_id,
            target.profile_id,
            result.unique_annual_benefit_yuan,
            result.dedup_verified,
        )
        return result

    # ------------------------------------------------------------------ #
    # 财务引擎接入（§3.5、§7.6 第 5 条）
    # ------------------------------------------------------------------ #
    def build_finance_input(
        self,
        result: ScenarioBillSet,
        *,
        dispatch_config: StorageDispatchConfig | None = None,
        years: int | None = None,
    ) -> dict[int, object]:
        """把**唯一去重后的年度运营收益**转成既有财务引擎的覆盖表（§3.5）。

        :raises ValidationError: 去重校验未通过（**中文**报错，禁止带病进入财务测算）
        """
        return build_finance_overrides(
            result,
            dispatch_config=dispatch_config or self.project.timeseries.dispatch,
            project=self.project,
            years=years,
        )

    def run_financials(
        self,
        result: ScenarioBillSet,
        *,
        dispatch_config: StorageDispatchConfig | None = None,
        years: int | None = None,
    ):
        """跑既有财务引擎（**不新建第二套财务实现**，§0.2、§3.5）。

        只把去重后的年度收益与同源电量通过 ``year_override`` 注入，
        其余（OPEX、折旧、税、融资、现金流、IRR/NPV）全部沿用既有
        :class:`~cenep.calculation.engine.CalculationEngine`。

        :returns: 既有 ``CalculationResult``
        """
        from ..calculation.engine import CalculationEngine

        overrides = self.build_finance_input(
            result, dispatch_config=dispatch_config, years=years
        )
        return CalculationEngine().calculate(self.project, year_override=overrides)

    def verify_finance_consistency(
        self,
        result: ScenarioBillSet,
        *,
        tolerance_yuan: float = 1e-6,
        dispatch_config: StorageDispatchConfig | None = None,
        years: int | None = None,
    ) -> tuple[bool, float, list[str]]:
        """校验"财务现金流里的运营收益"与"去重后的唯一收益"完全一致（§7.6 第 5 条）。

        这是 §10 阶段 6 验收标准"电费差额和财务现金流相互一致，不重复计算收益"
        的**可执行判据**。

        :returns: ``(是否一致, 偏差绝对值 元, 中文说明列表)``
        :raises ValidationError: 去重校验未通过（先修口径再谈一致性）
        """
        comparison = result.reference_comparison()
        if comparison is None:
            raise ValidationError(
                f"参考场景『{result.reference_scenario.label}』没有对比结果，无法核对财务一致性"
                "（V2.3 §3.5）",
                field="scenario.reference_scenario",
            )
        financials = self.run_financials(
            result, dispatch_config=dispatch_config, years=years
        )
        first_year = financials.annual_results[0]
        engine_revenue = float(first_year.total_revenue)
        expected = float(result.unique_annual_benefit_yuan)
        deviation = abs(engine_revenue - expected)
        ok = deviation <= tolerance_yuan
        messages = [
            f"去重后的唯一年度运营收益 = {expected:,.6f} 元（{result.reference_scenario.label}）",
            f"财务引擎首年 total_revenue = {engine_revenue:,.6f} 元",
            f"偏差 = {deviation:.6e} 元（容差 {tolerance_yuan:.6e} 元）→ "
            + ("一致，且不存在重复计算" if ok else "**不一致：存在重复计算或漏计**"),
        ]
        if not ok:
            messages.append(
                "常见原因：把分解口径（光伏自用节省 / 储能套利收益）又单独加了一次，"
                "或漏算了上网收入 / 储能容量收益（V2.3 §7.6）"
            )
        return ok, deviation, messages

    # ------------------------------------------------------------------ #
    # 报告用行数据（阶段 7 的 Excel/PDF 直接复用）
    # ------------------------------------------------------------------ #
    @staticmethod
    def scenario_rows(result: ScenarioBillSet) -> list[list[object]]:
        """四场景年度汇总表行（§7.7：输出年度汇总与关键假设）。"""
        rows: list[list[object]] = [
            [
                "场景",
                "光伏容量（kWp）",
                "储能功率（kW）",
                "储能容量（kWh）",
                "负荷电量（kWh）",
                "购电量（kWh）",
                "上网电量（kWh）",
                "电度电费（元）",
                "需量电费（元）",
                "力调电费（元）",
                "政府性基金及附加（元）",
                "其他未建模（元）",
                "电费成本合计（元）",
                "上网收入（元）",
                "净成本（元）",
                "综合单价（元/kWh）",
                "计费需量（kW）",
                "需量取值方法",
                "光伏自用率",
                "负荷覆盖率",
                "上网率",
                "电网依赖率",
            ]
        ]
        for line in result.scenarios:
            rows.append(
                [
                    line.label,
                    line.pv_capacity_kwp,
                    line.storage_power_kw,
                    line.storage_energy_kwh,
                    line.load_kwh,
                    line.grid_import_kwh,
                    line.grid_export_kwh,
                    line.energy_charge_yuan,
                    line.demand_charge_yuan,
                    line.power_factor_adjustment_yuan,
                    line.government_fund_yuan,
                    line.other_unmodeled_yuan,
                    line.total_cost_yuan,
                    line.export_revenue_yuan,
                    line.net_cost_yuan,
                    line.average_price_yuan_per_kwh,
                    line.peak_demand_kw,
                    line.demand_method_label,
                    line.self_consumption_rate,
                    line.load_coverage_rate,
                    line.export_rate,
                    line.grid_dependency_rate,
                ]
            )
        return rows

    @staticmethod
    def comparison_rows(result: ScenarioBillSet) -> list[list[object]]:
        """方案 vs 基准的差额表行（§7.1、§7.5：分项差必须分开列示）。"""
        rows: list[list[object]] = [
            [
                "方案",
                "账单节省（元）",
                "节省率",
                "净节省（元）",
                "电度电费差（元）",
                "需量电费差（元）",
                "容量电费差（元）",
                "力调差（元）",
                "政府性基金差（元）",
                "其他未建模差（元）",
                "基准需量（kW）",
                "方案需量（kW）",
                "需量削减（kW）",
                "上网收入差（元）",
                "唯一运营收益（元）",
            ]
        ]
        for item in result.comparisons:
            rows.append(
                [
                    item.scenario.label,
                    item.bill_saving_yuan,
                    item.bill_saving_rate,
                    item.net_saving_yuan,
                    item.energy_charge_delta_yuan,
                    item.demand_charge_delta_yuan,
                    item.capacity_charge_delta_yuan,
                    item.power_factor_delta_yuan,
                    item.government_fund_delta_yuan,
                    item.other_unmodeled_delta_yuan,
                    item.baseline_peak_demand_kw,
                    item.scenario_peak_demand_kw,
                    item.demand_reduction_kw,
                    item.export_revenue_delta_yuan,
                    item.dedup.unique_annual_benefit_yuan,
                ]
            )
        return rows

    @staticmethod
    def dedup_rows(result: ScenarioBillSet) -> list[list[object]]:
        """**收益去重清单**表行（§7.6 —— 报告必须原样呈现"哪些计了、哪些没计、为什么"）。"""
        rows: list[list[object]] = [
            [
                "方案",
                "收益项",
                "金额（元）",
                "是否计入现金流（且仅一次）",
                "角色",
                "不计入的理由",
                "计价口径",
            ]
        ]
        for item in result.comparisons:
            for entry in item.dedup.entries:
                rows.append(
                    [
                        item.scenario.label,
                        entry.label,
                        entry.amount_yuan,
                        "是" if entry.counted_in_unique_benefit else "否",
                        {
                            "unique": "现金流唯一来源",
                            "decomposition": "分解口径（仅展示）",
                            "external": "外部收入参数",
                        }.get(entry.entry_role, entry.entry_role),
                        entry.excluded_reason,
                        entry.caliber,
                    ]
                )
        return rows

    @staticmethod
    def monthly_rows(
        line: ScenarioBillLine, baseline: ScenarioBillLine | None = None
    ) -> list[list[object]]:
        """某场景的逐月费用表行（§7.7：输出每月费用拆分）。"""
        rows: list[list[object]] = [
            [
                "月份",
                "购电量（kWh）",
                "上网电量（kWh）",
                "光伏发电量（kWh）",
                "电度电费（元）",
                "需量电费（元）",
                "政府性基金及附加（元）",
                "上网收入（元）",
                "计费需量（kW）",
                "计量窗口需量（kW）",
            ]
        ]
        for row in line.monthly:
            rows.append(
                [
                    row.billing_month,
                    row.grid_import_kwh,
                    row.grid_export_kwh,
                    row.pv_generation_kwh,
                    row.energy_charge_yuan,
                    row.demand_charge_yuan,
                    row.government_fund_yuan,
                    row.export_revenue_yuan,
                    row.billable_demand_kw,
                    row.demand_measured_kw,
                ]
            )
        if baseline is not None:
            rows.append([])
            rows.append([f"基准场景逐月计费需量（{baseline.demand_method_label}）"])
            rows.append(["月份"] + [row.billing_month for row in baseline.monthly])
            rows.append(
                ["计费需量（kW）"] + [row.billable_demand_kw for row in baseline.monthly]
            )
        return rows

    @staticmethod
    def dedup_manifest_rows(manifest: ScenarioDedupManifest) -> list[list[object]]:
        """去重清单的文本行（供 PDF 章节与验收报告直接引用，§11）。"""
        rows: list[list[object]] = [["项目", "取值"]]
        rows.append(["去重策略", manifest.strategy])
        rows.append(["基准电费成本（元）", manifest.baseline_cost_yuan])
        rows.append(["账单节省（唯一来源，元）", manifest.bill_saving_yuan])
        rows.append(["上网收入（独立来源，元）", manifest.export_revenue_yuan])
        rows.append(["唯一去重后的年度运营收益（元）", manifest.unique_annual_benefit_yuan])
        rows.append(["分析口径合计（仅展示，元）", manifest.analysis_total_yuan])
        rows.append(["恒等式偏差（元）", manifest.identity_deviation_yuan])
        rows.append(["恒等式容差（元）", manifest.identity_tolerance_yuan])
        rows.append(["恒等式是否通过", "通过" if manifest.identity_passed else "未通过"])
        for equation in manifest.identity_equations:
            rows.append(["恒等式", equation])
        for note in manifest.duplicate_risk_notes:
            rows.append(["边界与风险说明", note])
        for message in manifest.messages:
            rows.append(["结论", message])
        return rows

    @staticmethod
    def default_dispatch_config(
        *,
        strategy: DispatchStrategy = DispatchStrategy.PV_SELF_CONSUMPTION,
        allow_grid_charge: bool = False,
        allow_export: bool = False,
    ) -> StorageDispatchConfig:
        """构造一份**默认不假设任何收益**的调度配置（§0.2：不预填未经核验的参数）。

        ``allow_grid_charge`` 与 ``allow_export`` 默认关闭：这两个开关会显著改变
        ``S_bill`` 与上网收入，必须由用户显式打开（§7.6 边界条件）。
        """
        return StorageDispatchConfig(
            strategy=strategy,
            charge_from_pv=True,
            charge_from_grid=allow_grid_charge,
            allow_grid_charge=allow_grid_charge,
            allow_export=allow_export,
            allow_arbitrage=True,
        )

    # ------------------------------------------------------------------ #
    # 持久化（§8.2：复用既有项目文件，不新增字段）
    # ------------------------------------------------------------------ #
    def save(self, path: str | Path) -> Path:
        """保存项目到 ``.nep``（复用既有实现；电价计划在 SQLite 版本库中，不写进项目文件）。"""
        from ..infrastructure.project_file import save_project

        return save_project(self.project, path)

    # ------------------------------------------------------------------ #
    # 便捷：把"仅光伏"项目的一行结论拼出来（供阶段 7 报告首段）
    # ------------------------------------------------------------------ #
    @staticmethod
    def headline(result: ScenarioBillSet, comparison: ScenarioBillComparison | None = None) -> str:
        """一行中文结论（含节省额、需量削减与唯一收益，并标注数据来源与置信度）。"""
        item = comparison or result.reference_comparison()
        if item is None:
            return "尚无方案对比结果。"
        confidence = (
            "负荷为实测高频曲线"
            if result.load_is_measured
            else "负荷为**估算曲线**，不得声称精确计算逐时消纳率或需量削减效果（§7.7）"
        )
        return (
            f"在电价计划『{result.tariff_plan_name}』下，{item.scenario.label} 相对基准"
            f"（无光伏、无储能）年电费节省 {item.bill_saving_yuan:,.2f} 元"
            + (f"（节省率 {item.bill_saving_rate:+.2%}）" if item.bill_saving_rate is not None else "")
            + f"，计费需量由 {item.baseline_peak_demand_kw:,.2f} kW 降至 "
            f"{item.scenario_peak_demand_kw:,.2f} kW（削减 {item.demand_reduction_kw:,.2f} kW）；"
            f"**唯一去重后的年度运营收益 {result.unique_annual_benefit_yuan:,.2f} 元**"
            f"（= 账单节省 + 上网收入 + 外部收益，不含任何分解口径的重复累加）；{confidence}。"
        )
