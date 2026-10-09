"""电价与基准账单复算的应用服务（V2.3 §7.1、§7.2、§7.3、§7.4）。

分层职责（§1、§0.2）
------------------
* 本服务**只做编排**：版本库存取调用 :class:`~cenep.policy.tariff_plan_store.TariffPlanStore`，
  时段/价格/复算调用 :mod:`cenep.calculation.tariff_plan_engine` 与
  :mod:`cenep.calculation.bill_recalculator`，账单事实读取复用
  :class:`~cenep.application.bill_service.BillService`。
* **界面不得实现任何电价或账单公式**：阶段 6 的 UI 只允许调用本服务的公开方法。
* 报错一律是**中文** :class:`~cenep.calculation.errors.ValidationError`（带字段名）。

本阶段（阶段 5）交付的能力
-----------------------
1. 列出/查询/校验/对比版本化电价计划（含官方 2026-01 版与项目资料版**并存**）；
2. 按日期与适用范围选取电价版本，并给出"能否用于正式复算"的中文结论；
3. 单张账单的基准复算与差异分解、多张账单的年度校准；
4. "**按用户账单反算/录入实际分时电价**"模式（§4.1 第 3 条优先于未验证模板）；
5. 报告用行数据（§8.1 Excel「电价版本与来源」表的最小数据源）。

**不包含**（属阶段 6）：光伏/储能方案对比、``BillSimulationResult``、财务收益去重。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..calculation.bill_recalculator import (
    calibrate_bills,
    energy_by_period_from_hourly,
    recompute_bill,
    recompute_bills,
)
from ..calculation.errors import ValidationError
from ..calculation.tariff_plan_engine import (
    base_price_yuan_per_kwh,
    compose_period_price,
    effective_period_prices,
    fixed_price_yuan_per_kwh,
    period_hours_of,
)
from ..domain.bill_models import BillAnnualSummary, BillTolerance, ElectricityBill
from ..domain.bill_recomputation import BillCalibrationSummary, BillRecomputation
from ..domain.enums import (
    DemandBillingMode,
    PriceBasis,
    TariffComponentType,
    TariffComponentUnit,
    TariffPeriod,
    TariffPlanStatus,
)
from ..domain.models import Project
from ..domain.tariff_models import (
    TariffPlan,
    TariffPlanComparison,
    TariffPlanValidation,
    TariffPriceComponent,
)
from ..infrastructure.db import Database
from ..policy.tariff_plan_store import TariffPlanStore

logger = logging.getLogger(__name__)

__all__ = ["TariffService"]


class TariffService:
    """电价计划与基准账单复算服务（V2.3 §7）。

    :param project: 项目（账单事实挂在 ``project.bills`` 上，与 V2.1 保持一致）。
    :param db: SQLite 库；``None`` 时版本库仅存在于内存（测试友好；正式使用必须持久化，§8.2）。
    :param tolerance: 差异容差；``None`` 时用 §2.1 默认 ``max(1 元, 0.5% × 账单总额)``。
    """

    def __init__(
        self,
        project: Project,
        *,
        db: Database | None = None,
        store: TariffPlanStore | None = None,
        tolerance: BillTolerance | None = None,
    ) -> None:
        self.project = project
        self.tolerance = tolerance or BillTolerance()
        self.store = store or TariffPlanStore(db)

    # ------------------------------------------------------------------ #
    # 账单事实（读）
    # ------------------------------------------------------------------ #
    @property
    def bills(self) -> list[ElectricityBill]:
        """项目内账单事实列表（副本）。"""
        return list(self.project.bills)

    def get_bill(self, bill_id: str) -> ElectricityBill:
        """按 ID 取账单事实。

        :raises ValidationError: 账单不存在（中文提示）
        """
        for bill in self.project.bills:
            if bill.bill_id == bill_id:
                return bill
        raise ValidationError(f"未找到账单：{bill_id}", field="bill_id")

    # ------------------------------------------------------------------ #
    # 电价计划（版本库）
    # ------------------------------------------------------------------ #
    def list_plans(
        self, *, province: str = "湖北", status: TariffPlanStatus | None = None
    ) -> list[TariffPlan]:
        """列出电价计划（默认湖北省，可按状态过滤）。"""
        return self.store.list_plans(province=province, status=status)

    def plan_summaries(self, *, province: str = "湖北") -> list[dict[str, object]]:
        """电价计划摘要列表（供界面/报告表格使用，含来源与适用范围的**中文**字段）。"""
        out: list[dict[str, object]] = []
        for plan in self.list_plans(province=province):
            prices = effective_period_prices(plan)
            out.append(
                {
                    "tariff_plan_id": plan.tariff_plan_id,
                    "名称": plan.name,
                    "展示名称": plan.display_name,
                    "状态": plan.status.label,
                    "购电模式": plan.market_mode.label,
                    "适用电压等级": "、".join(plan.applicable_voltage_levels) or "未限定",
                    "适用计费方式": "、".join(plan.applicable_tariff_structures) or "未限定",
                    "来源": plan.source_text,
                    "政策文号": plan.source_document_number or "未提供",
                    "基础电价定义": plan.base_price_definition or "未给出",
                    "基础电价_元每千瓦时": base_price_yuan_per_kwh(plan),
                    "固定分项_元每千瓦时": fixed_price_yuan_per_kwh(plan),
                    "尖峰_元每千瓦时": prices.get(TariffPeriod.SHARP_PEAK),
                    "高峰_元每千瓦时": prices.get(TariffPeriod.PEAK),
                    "平段_元每千瓦时": prices.get(TariffPeriod.FLAT),
                    "低谷_元每千瓦时": prices.get(TariffPeriod.VALLEY),
                    "最大需量_元每千瓦月": plan.demand_charge_yuan_per_kw_month,
                    "变压器容量_元每千伏安月": plan.capacity_charge_yuan_per_kva_month,
                    "时段划分": "；".join(plan.rule_summary()),
                    "时段小时数": period_hours_of(plan),
                    "备注": plan.notes,
                    "是否用户覆盖": plan.user_overridden,
                    "覆盖说明": plan.override_note or "",
                }
            )
        return out

    def get_plan(self, tariff_plan_id: str) -> TariffPlan:
        """按 ID 取电价计划（不存在时抛中文异常）。"""
        return self.store.get_plan(tariff_plan_id)

    def validate_plan(
        self,
        tariff_plan_id: str,
        *,
        as_of: date | None = None,
        require_verified: bool = True,
    ) -> TariffPlanValidation:
        """校验电价计划（§4.2、§7.4），返回中文结论与覆盖检测明细。"""
        return self.store.validate(tariff_plan_id, as_of=as_of, require_verified=require_verified)

    def validate_plan_for_bill(
        self, bill_id: str, tariff_plan_id: str, *, require_verified: bool = True
    ) -> TariffPlanValidation:
        """按**账单账期**校验某计划能否用于这张账单（§2.3：电价规则按计费日期生效）。"""
        bill = self.get_bill(bill_id)
        return self.store.validate(
            tariff_plan_id, as_of=bill.billing_period_start, require_verified=require_verified
        )

    def compare_plans(self, plan_a_id: str, plan_b_id: str) -> TariffPlanComparison:
        """逐项比较两套电价计划（时段 / 浮动系数 / 电度电价 / 容需量 / 范围 / 来源）。"""
        return self.store.compare(plan_a_id, plan_b_id)

    def compare_official_with_material(
        self, *, official_plan_id: str | None = None
    ) -> TariffPlanComparison:
        """**官方 2026-01 版 vs 项目资料版**的逐项差异（本阶段最重要的业务对照）。

        :param official_plan_id: 指定官方计划；``None`` 时取"两部制 110千伏"。
        """
        official_id = official_plan_id or "HUBEI_COMMERCIAL_2026_01_TWO_PART_KV110"
        return self.store.compare(official_id, "HUBEI_DFL_MATERIAL_TOU_RATIO")

    def select_plan(
        self,
        day: date,
        *,
        voltage_level: str | None = None,
        tariff_structure: str | None = None,
        require_verified: bool = True,
    ) -> TariffPlan:
        """按日期 + 适用范围选取电价版本（§2.3、§4.1 第 6、7 条）。"""
        return self.store.select(
            day,
            voltage_level=voltage_level,
            tariff_structure=tariff_structure,
            require_verified=require_verified,
        )

    # ------------------------------------------------------------------ #
    # 基准账单复算（§3.4、§7.4）
    # ------------------------------------------------------------------ #
    def recompute_bill(
        self,
        bill_id: str,
        tariff_plan_id: str,
        *,
        period_energies: Mapping[TariffPeriod, float] | None = None,
        hourly_energy_kwh: Sequence[float] | None = None,
        demand_mode: DemandBillingMode | None = None,
        require_verified: bool = True,
    ) -> BillRecomputation:
        """单张账单的基准复算与差异分解（V2.3 §3.4）。

        :param hourly_energy_kwh: 可选 24 点逐小时电量；提供时按**电价计划的时段规则**
            归并成尖峰/高峰/平段/低谷电量（§7.4 第 1 条），优先级高于账单分时电量字段。
        :param require_verified: 是否要求计划可用于正式复算（默认 ``True``，§4.2）。
        """
        bill = self.get_bill(bill_id)
        plan = self.get_plan(tariff_plan_id)
        energies = period_energies
        if energies is None and hourly_energy_kwh is not None:
            energies = energy_by_period_from_hourly(
                plan, bill.billing_period_start.month, hourly_energy_kwh
            )
        return recompute_bill(
            bill,
            plan,
            period_energies=energies,
            demand_mode=demand_mode,
            tolerance=self.tolerance,
            require_verified=require_verified,
        )

    def recompute_all(
        self,
        tariff_plan_id: str,
        *,
        year: int | None = None,
        require_verified: bool = True,
        period_energies_by_bill: Mapping[str, Mapping[TariffPeriod, float]] | None = None,
    ) -> BillCalibrationSummary:
        """年度账单校准汇总（V2.3 §7.4、§3.1）。

        :param period_energies_by_bill: 可选"账单编号 → 分时电量"；用于账单无分时电量字段、
            但用户已提供逐小时/高频负荷数据的场景（§7.4 第 1 条）。
        """
        plan = self.get_plan(tariff_plan_id)
        return calibrate_bills(
            self.project.bills,
            plan,
            year=year,
            tolerance=self.tolerance,
            require_verified=require_verified,
            period_energies_by_bill=period_energies_by_bill,
        )

    def recompute_bills_with_plan(
        self,
        bills: Sequence[ElectricityBill],
        plan: TariffPlan,
        *,
        require_verified: bool = True,
        period_energies_by_bill: Mapping[str, Mapping[TariffPeriod, float]] | None = None,
    ) -> list[BillRecomputation]:
        """用给定计划复算给定账单（供阶段 6 与校准脚本复用，不读取项目）。"""
        return recompute_bills(
            bills,
            plan,
            require_verified=require_verified,
            tolerance=self.tolerance,
            period_energies_by_bill=period_energies_by_bill,
        )

    def calibrate_bills_with_plan(
        self,
        bills: Sequence[ElectricityBill],
        plan: TariffPlan,
        *,
        year: int | None = None,
        require_verified: bool = True,
        period_energies_by_bill: Mapping[str, Mapping[TariffPeriod, float]] | None = None,
    ) -> BillCalibrationSummary:
        """用给定计划校准给定账单集合（供校准脚本与阶段 6 复用）。"""
        return calibrate_bills(
            bills,
            plan,
            year=year,
            tolerance=self.tolerance,
            require_verified=require_verified,
            period_energies_by_bill=period_energies_by_bill,
        )

    # ------------------------------------------------------------------ #
    # 按用户账单反算 / 录入实际分时电价（§4.1 第 3 条、§2.3、§4.2）
    # ------------------------------------------------------------------ #
    def build_plan_from_bill(
        self,
        bill_id: str,
        *,
        template_plan_id: str,
        component_values: Mapping[TariffComponentType | str, float] | None = None,
        period_base_price: Mapping[TariffPeriod, float] | None = None,
        period_unit_price: Mapping[TariffPeriod, float] | None = None,
        name: str | None = None,
        note: str = "",
        status: TariffPlanStatus = TariffPlanStatus.VERIFIED,
        save: bool = True,
    ) -> TariffPlan:
        """按**用户账单实际分项单价 / 实际分时单价**反算一份电价计划（最高优先级的来源）。

        两种价格口径（§4.1 第 3 条：用户当前实际账单上的**分时单价和费用**优先；
        §2.3：提供"按用户账单反算/录入实际分时电价"模式）：

        **口径一：直接录入实际分时单价（``period_unit_price``）**
            用户直接把账单上各时段的实际电度单价填进来，软件**原样采用**，不做任何浮动推导。
            适用于"账单已给出分时单价"或"市场化直购逐时结算"的用户。

        **口径二：由分项数值按政策浮动系数合成（``component_values``）**
            时段规则与浮动系数沿用 ``template_plan_id``（政策时段表不因账单改写），
            各时段直接单价 = ``基础电价 × 浮动系数 + 固定分项``；``period_base_price``
            可为个别时段单独指定基础电价（市场化用户各时段电能量价格不同时使用）。

        * 版本 ID = ``模板ID_FROM_<账单月份>``，与模板**并存**，模板不被修改（§4.2）；
        * 两种口径都要求填写 ``note``（覆盖说明），并写入 ``override_note`` 与 ``notes``。

        :raises ValidationError: 两种口径都没提供、分项键无法识别、账单/模板不存在
        """
        bill = self.get_bill(bill_id)
        template = self.get_plan(template_plan_id)
        if not component_values and not period_unit_price:
            raise ValidationError(
                "按账单反算电价必须提供『实际分时单价』（period_unit_price）或至少一个分项数值"
                "（component_values），否则无法确定电价",
                field="component_values",
            )

        def key_text(key: TariffComponentType | str) -> str:
            return key.value if isinstance(key, TariffComponentType) else str(key)

        wanted = {key_text(key): float(value) for key, value in (component_values or {}).items()}
        updated_components: list[TariffPriceComponent] = []
        applied: set[str] = set()
        for component in template.price_components:
            if component.unit is not TariffComponentUnit.YUAN_PER_KWH:
                updated_components.append(component.model_copy(deep=True))
                continue
            if not component.included_in_tou_price:
                updated_components.append(component.model_copy(deep=True))
                continue
            match = next(
                (
                    value
                    for key, value in wanted.items()
                    if key == component.component_type.value or component.name.startswith(key)
                ),
                None,
            )
            if match is None:
                updated_components.append(component.model_copy(deep=True))
                continue
            applied.add(component.component_type.value)
            updated_components.append(
                component.model_copy(
                    update={
                        "value": float(match),
                        "source_note": f"取自账单 {bill.bill_id}（{bill.billing_month}）的实际分项均价",
                    }
                )
            )
        unknown = sorted(set(wanted) - applied)
        if unknown:
            raise ValidationError(
                "以下分项未能匹配到模板中的任何电度电价分项：" + "、".join(unknown)
                + "；可选键为 " + "、".join(sorted({c.component_type.value for c in template.price_components})),
                field="component_values",
            )

        if period_unit_price:
            missing = [
                period.label
                for period in {rule.period for rule in template.time_period_rules}
                if period not in period_unit_price
            ]
            if missing:
                raise ValidationError(
                    "直接录入分时单价时必须覆盖模板声明过的全部时段，当前缺少："
                    + "、".join(missing)
                    + "（价格缺失不得按 0 或平段兜底，§4.2）",
                    field="period_unit_price",
                )
            for period, price in period_unit_price.items():
                if float(price) < 0.0:
                    raise ValidationError(
                        f"时段『{period.label}』的实际单价不能为负（当前 {price} 元/千瓦时），请核对账单",
                        field="period_unit_price",
                    )

        base = sum(
            float(component.value)
            for component in updated_components
            if component.unit is TariffComponentUnit.YUAN_PER_KWH
            and component.included_in_tou_price
            and component.adjustable_by_tou
            and component.value is not None
        )
        fixed = sum(
            float(component.value)
            for component in updated_components
            if component.unit is TariffComponentUnit.YUAN_PER_KWH
            and component.included_in_tou_price
            and not component.adjustable_by_tou
            and component.value is not None
        )
        rules = []
        for rule in template.time_period_rules:
            if period_unit_price and rule.period in period_unit_price:
                price = float(period_unit_price[rule.period])
                basis = PriceBasis.DIRECT_PRICE
                note_suffix = "；直接单价由用户按账单实际分时单价录入（原样采用）"
            else:
                multiplier = rule.price_multiplier if rule.price_multiplier is not None else 1.0
                base_for_rule = base
                if period_base_price and rule.period in period_base_price:
                    base_for_rule = float(period_base_price[rule.period])
                price = compose_period_price(
                    base_price_yuan_per_kwh=base_for_rule,
                    price_multiplier=float(multiplier),
                    fixed_price_yuan_per_kwh=fixed,
                )
                basis = PriceBasis.DIRECT_PRICE
                note_suffix = "；直接单价由用户账单实际分项按政策浮动系数合成"
            rules.append(
                rule.model_copy(
                    update={
                        "direct_price_yuan_per_kwh": price,
                        "price_basis": basis,
                        "note": rule.note + note_suffix,
                    }
                )
            )

        price_mode = (
            "直接录入的账单实际分时单价（原样采用）"
            if period_unit_price
            else f"账单实际分项按模板浮动系数合成（基础电价 {base:.6f}，固定分项 {fixed:.6f} 元/千瓦时）"
        )
        derived = template.model_copy(
            update={
                "tariff_plan_id": f"{template.tariff_plan_id}_FROM_{bill.billing_month}",
                "name": name
                or f"按账单反算电价（{bill.billing_month}，模板：{template.name}）",
                "effective_from": bill.billing_period_start,
                "effective_to": bill.billing_period_end,
                "source_name": f"用户账单 {bill.bill_id}（{bill.billing_month}）实际电价反算",
                "source_url": None,
                "source_document_number": template.source_document_number,
                "source_fetched_at": date.today(),
                "verified_at": None,
                "verified_by": "用户按账单实际值录入",
                "status": status,
                "time_period_rules": rules,
                "price_components": updated_components,
                "user_overridden": True,
                "override_note": note
                or f"价格取自账单 {bill.bill_id} 的实际电价（{price_mode}）；时段规则沿用模板 {template.tariff_plan_id}",
                "notes": "\n".join(
                    [
                        template.notes,
                        f"按账单反算：账单 {bill.bill_id}（{bill.billing_month}）实际电价作为价格来源，"
                        "优先级高于未核验模板（§4.1 第 3 条）。",
                        f"价格口径：{price_mode}。",
                        "时段规则沿用模板（政策时段表不因账单改写）；"
                        "如账单实际执行时段与模板不同，请另建用户覆盖版本并说明依据。",
                    ]
                ),
            }
        )
        if save:
            self.store.save_plan(derived)
        logger.info(
            "按账单反算电价计划：%s ← 账单 %s（模板 %s，口径：%s）",
            derived.tariff_plan_id,
            bill_id,
            template_plan_id,
            "分时单价" if period_unit_price else "分项合成",
        )
        return derived

    # ------------------------------------------------------------------ #
    # 报告用行数据（§8.1「电价版本与来源」表）
    # ------------------------------------------------------------------ #
    def plan_rows_for_report(self, tariff_plan_id: str) -> list[list[object]]:
        """单个计划的报告行：``[字段, 取值, 单位, 来源/口径]``（供 Excel/PDF 直接使用）。"""
        plan = self.get_plan(tariff_plan_id)
        prices = effective_period_prices(plan)
        rows: list[list[object]] = [
            ["电价计划编号", plan.tariff_plan_id, "", "—"],
            ["电价计划名称", plan.name, "", "—"],
            ["核验状态", plan.status.label, "", plan.source_text],
            ["购电模式", plan.market_mode.label, "", "§7.3 用户可配置项"],
            ["适用范围", "、".join(plan.applicable_voltage_levels) or "未限定", "", "§4.1 第 6 条"],
            ["适用计费方式", "、".join(plan.applicable_tariff_structures) or "未限定", "", "§4.1 第 6 条"],
            ["生效期间", f"{plan.effective_from} ~ {plan.effective_to or '未标注'}", "—", plan.source_text],
            ["政策文号", plan.source_document_number or "未提供", "", plan.source_text],
            ["来源链接", plan.source_url or "未提供", "", plan.source_text],
            ["数据抓取日期", plan.source_fetched_at or "未提供", "", plan.source_text],
            ["核验人/方式", plan.verified_by or "未提供", "", plan.source_text],
            ["基础电价口径", plan.base_price_definition or "未给出", "", "§4.2"],
            ["基础电价", base_price_yuan_per_kwh(plan), "元/千瓦时", "Σ 参与峰谷浮动的分项"],
            ["固定分项", fixed_price_yuan_per_kwh(plan), "元/千瓦时", "Σ 不参与浮动但已含在电度电价中的分项"],
        ]
        for period in TariffPeriod:
            price = prices.get(period)
            if price is None:
                continue
            rows.append([f"{period.label}电度电价", price, "元/千瓦时", f"{plan.name}（表列绝对电价）"])
        rows.extend(
            [
                ["最大需量电价", plan.demand_charge_yuan_per_kw_month, "元/千瓦·月", plan.source_text],
                ["变压器容量电价", plan.capacity_charge_yuan_per_kva_month, "元/千伏安·月", plan.source_text],
                ["基本电费计费方式", plan.demand_billing_mode.label, "", "§7.5 容量与需量默认互斥"],
            ]
        )
        for text in plan.rule_summary():
            rows.append(["时段规则", text, "", plan.source_document_number or plan.source_text])
        rows.append(["时段小时数（按月分组）", str(period_hours_of(plan)), "小时", "§4.2 24 小时覆盖检测"])
        rows.append(["备注", plan.notes, "", "—"])
        return rows

    # ------------------------------------------------------------------ #
    # 便捷：把项目账单导出为"账单事实 + 复算"对照表（阶段 7 报告复用）
    # ------------------------------------------------------------------ #
    def calibration_rows_for_report(self, summary: BillCalibrationSummary) -> list[list[object]]:
        """把年度校准结果整理成报告行（实际账单 / 软件复算必须分列，§8.1）。"""
        rows: list[list[object]] = [
            [
                "账单月份",
                "账单编号",
                "总电量（kWh）",
                "账单实录总额（元）",
                "软件复算合计（元，仅建模部分）",
                "未建模费用基准（元）",
                "复算总额（元）",
                "毛差异（元）",
                "净差异（元）",
                "差异率",
                "是否通过容差（净差异）",
            ]
        ]
        for item in summary.monthly:
            rows.append(
                [
                    item.billing_month,
                    item.bill_id,
                    item.energy_total_kwh,
                    item.bill_total_yuan,
                    item.recomputed_subtotal_yuan,
                    item.recomputed_unmodeled_baseline_yuan,
                    item.recomputed_total_with_baseline_yuan,
                    item.difference_yuan,
                    item.difference_after_baseline_yuan,
                    f"{item.difference_rate:+.2%}" if item.difference_rate is not None else "无法计算",
                    "通过" if item.passed_tolerance else "超出容差",
                ]
            )
        return rows

    # ------------------------------------------------------------------ #
    # 持久化（§8.2）
    # ------------------------------------------------------------------ #
    def save(self, path: str | Path) -> Path:
        """把项目（含账单）保存到 ``.nep``；电价计划本身在 SQLite 版本库中，不写进项目文件。"""
        from ..infrastructure.project_file import save_project

        saved = save_project(self.project, path)
        logger.info("保存项目（含 %d 条账单）：%s", len(self.project.bills), saved)
        return saved

    def annual_bill_basis(self, year: int | None = None) -> BillAnnualSummary:
        """年度账单基准数据（复用 V2.1 的既有实现，保持单一公式来源）。"""
        from ..calculation.bill_calculator import annual_bill_summary

        return annual_bill_summary(self.project.bills, year=year, tolerance=self.tolerance)

    @staticmethod
    def describe_plan_choice(plans: Iterable[TariffPlan]) -> str:
        """把候选电价版本渲染成一句中文选择提示（界面"让用户选择、核对、覆盖"用）。"""
        items = list(plans)
        if not items:
            return "当前没有可用的电价计划，请先载入内置计划或手工录入。"
        lines = [f"共 {len(items)} 套可选电价计划，请选择并核对（软件不替用户决定）："]
        for plan in items:
            lines.append(
                f"· {plan.name}｜状态：{plan.status.label}｜适用范围："
                f"{'、'.join(plan.applicable_voltage_levels) or '未限定'}"
                f"/{'、'.join(plan.applicable_tariff_structures) or '未限定'}｜"
                f"生效：{plan.effective_from} ~ {plan.effective_to or '未标注'}｜来源：{plan.source_text}"
            )
        return "\n".join(lines)
