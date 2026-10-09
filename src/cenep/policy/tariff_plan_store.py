"""电价计划版本库：内置核验版本 + 用户覆盖版本的存取与选取（V2.3 §4.1、§4.2、§7.3）。

与 :mod:`cenep.policy.store` 的关系（**概念分离，不复制第二套同类引擎**）
--------------------------------------------------------------------
* :class:`~cenep.policy.store.PolicyStore` 管理**新能源上网电价**政策模板与版本
  （``PolicyTemplate`` / ``PolicyProfile`` / ``policy_profiles`` 表）；
* 本模块管理**用户侧工商业购电**电价计划
  （``TariffPlan`` / ``tariff_plans`` 表）。

两者是"发电侧上网电价"与"用电侧购电电价"两件事（§2.3 明确要求不得互相复用），
因此各自一张表、各自一套存取；**不存在第二套同类引擎**——时段/价格的全部计算仍只在
:mod:`cenep.calculation.tariff_plan_engine` 与 :mod:`cenep.calculation.bill_recalculator` 里。

版本化铁律（§4.1、§4.2）
-----------------------
1. 内置版本**可以共存**：官方 2026-01 版 7 个电压等级 + 项目资料版 1 个，一直保留；
2. 用户覆盖**不覆盖内置**：用户修改产生的版本以新 ``tariff_plan_id``（后缀 ``_USER``）
   写入，内置版本保持可追溯（与"政策旧版本只新增不覆盖"同一原则，见 ``HUBEI_POLICY_MODEL.md`` 第 5 节）；
3. 计划按**计费日期 + 适用范围**选取（:func:`cenep.calculation.tariff_plan_engine.select_tariff_plan`），
   不允许"用一套计划静默替换另一套"。
"""

from __future__ import annotations

import logging
from datetime import date

from ..calculation.tariff_plan_engine import (
    compare_tariff_plans,
    select_tariff_plan,
    validate_tariff_plan,
)
from ..calculation.errors import ValidationError
from ..domain.enums import MarketMode, TariffPlanStatus, TariffStructure
from ..domain.tariff_models import TariffPlan, TariffPlanComparison, TariffPlanValidation
from ..infrastructure.db import Database
from .hubei_commercial import DFL_MATERIAL, OFFICIAL_2026_01, builtin_tariff_plans

logger = logging.getLogger(__name__)

__all__ = ["TariffPlanStore", "USER_PLAN_ID_SUFFIX"]

#: 用户覆盖版本的 ID 后缀（保证内置版本永不被改写，可追溯）
USER_PLAN_ID_SUFFIX = "_USER"


class TariffPlanStore:
    """电价计划版本库（内置 + 用户）。

    :param db: SQLite 库；``None`` 时**只在内存中**工作（便于测试与单次分析，
        但按 §8.2 要求，正式使用必须传入 :class:`~cenep.infrastructure.db.Database` 以持久化）。
    :param seed_builtin: 是否把内置计划（官方 2026-01 版 + 项目资料版）写入库（幂等）。
    """

    def __init__(self, db: Database | None = None, *, seed_builtin: bool = True) -> None:
        self.db = db
        self._memory: dict[str, TariffPlan] = {}
        if seed_builtin:
            self.seed_builtin_plans()

    # ------------------------------------------------------------------ #
    # 内置计划
    # ------------------------------------------------------------------ #
    def seed_builtin_plans(self) -> int:
        """写入内置计划（幂等：已存在的 ``tariff_plan_id`` **不覆盖**，避免覆盖用户修改）。

        :return: 本次实际新增的计划数量。
        """
        builtin_ids = {plan.tariff_plan_id for plan in builtin_tariff_plans()}
        existing = {plan.tariff_plan_id for plan in self.list_plans()}
        added = 0
        for plan in builtin_tariff_plans():
            if plan.tariff_plan_id in existing:
                continue
            self.save_plan(plan)
            added += 1
        if added:
            logger.info("写入内置电价计划 %d 个（内置共 %d 个）", added, len(builtin_ids))
        return added

    # ------------------------------------------------------------------ #
    # 存取
    # ------------------------------------------------------------------ #
    def save_plan(self, plan: TariffPlan) -> TariffPlan:
        """保存/更新一份电价计划（返回入参本身，便于链式调用）。"""
        if not plan.tariff_plan_id:
            raise ValidationError("电价计划必须提供 tariff_plan_id", field="tariff_plan_id")
        if self.db is not None:
            self.db.save_tariff_plan(plan)
        else:
            self._memory[plan.tariff_plan_id] = plan.model_copy(deep=True)
        logger.debug("保存电价计划：%s（%s）", plan.tariff_plan_id, plan.name)
        return plan

    def get_plan(self, tariff_plan_id: str) -> TariffPlan:
        """按 ID 取电价计划。

        :raises ValidationError: 不存在（**中文**提示，并给出可用的 ID 列表）
        """
        plan = self.find_plan(tariff_plan_id)
        if plan is None:
            available = "、".join(sorted(item.tariff_plan_id for item in self.list_plans()))
            raise ValidationError(
                f"未找到电价计划：{tariff_plan_id}；可用计划：{available or '（库为空）'}",
                field="tariff_plan_id",
            )
        return plan

    def find_plan(self, tariff_plan_id: str) -> TariffPlan | None:
        """按 ID 查电价计划，不存在时返回 ``None``。"""
        if self.db is not None:
            return self.db.load_tariff_plan(tariff_plan_id)
        plan = self._memory.get(tariff_plan_id)
        return plan.model_copy(deep=True) if plan is not None else None

    def list_plans(
        self, *, province: str | None = "湖北", status: TariffPlanStatus | None = None
    ) -> list[TariffPlan]:
        """列出电价计划（可按省份与状态过滤），按生效日期排序。"""
        if self.db is not None:
            plans = self.db.list_tariff_plans(province)
        else:
            plans = [plan.model_copy(deep=True) for plan in self._memory.values()]
            if province:
                plans = [plan for plan in plans if plan.province == province]
        if status is not None:
            plans = [plan for plan in plans if plan.status is status]
        return sorted(plans, key=lambda plan: (plan.effective_from, plan.tariff_plan_id))

    def delete_plan(self, tariff_plan_id: str) -> int:
        """删除一份计划（危险操作，调用方须二次确认），返回删除条数。"""
        if self.db is not None:
            removed = self.db.delete_tariff_plan(tariff_plan_id)
        else:
            removed = 1 if self._memory.pop(tariff_plan_id, None) is not None else 0
        if removed:
            logger.warning("删除电价计划：%s", tariff_plan_id)
        return removed

    # ------------------------------------------------------------------ #
    # 选取与校验
    # ------------------------------------------------------------------ #
    def select(
        self,
        day: date,
        *,
        voltage_level: str | None = None,
        tariff_structure: TariffStructure | str | None = None,
        province: str = "湖北",
        require_verified: bool = True,
    ) -> TariffPlan:
        """按计费日期 + 适用范围选取电价版本（§2.3、§4.1 第 6、7 条）。"""
        return select_tariff_plan(
            self.list_plans(province=province),
            day,
            voltage_level=voltage_level,
            tariff_structure=tariff_structure,
            require_verified=require_verified,
        )

    def validate(
        self, tariff_plan_id: str, *, as_of: date | None = None, require_verified: bool = True
    ) -> TariffPlanValidation:
        """校验指定计划（§4.2、§7.4）。"""
        return validate_tariff_plan(
            self.get_plan(tariff_plan_id), as_of=as_of, require_verified=require_verified
        )

    def compare(self, plan_a_id: str, plan_b_id: str) -> TariffPlanComparison:
        """逐项比较两套计划（**并存**要求：官方版 vs 项目资料版，§4.2、§7.3）。"""
        return compare_tariff_plans(self.get_plan(plan_a_id), self.get_plan(plan_b_id))

    def builtin_comparison(self) -> TariffPlanComparison:
        """内置"官方 2026-01（两部制 110千伏）"与"项目资料版"的逐项差异。

        这是本阶段最重要的业务对照：东风本田资料口径 vs 官方 2026-01 政策。
        """
        official = next(
            plan for plan in OFFICIAL_2026_01 if plan.tariff_plan_id.endswith("TWO_PART_KV110")
        )
        return compare_tariff_plans(official, DFL_MATERIAL)

    # ------------------------------------------------------------------ #
    # 用户覆盖（§4.1、§4.2：用户可覆盖政策默认值，必须留痕）
    # ------------------------------------------------------------------ #
    def save_user_override(
        self,
        plan: TariffPlan,
        *,
        override_note: str,
        status: TariffPlanStatus = TariffPlanStatus.VERIFIED,
        verified_by: str | None = None,
    ) -> TariffPlan:
        """保存**用户覆盖版本**（新 ID = 原 ID + ``_USER``，内置版本保持不变）。

        :param override_note: 覆盖说明（改了什么、依据是什么）。**必填**——
            §7.4 第 4 条禁止"为了匹配总金额而无解释地修改各时段电价"，
            因此覆盖必须留下可追溯的说明，否则拒绝保存。
        :raises ValidationError: 覆盖说明为空
        """
        note = (override_note or "").strip()
        if not note:
            raise ValidationError(
                "保存用户覆盖版本必须填写『覆盖说明』（改动了什么、依据是什么）；"
                "不允许无解释地修改电价（§7.4 第 4 条）",
                field="override_note",
            )
        new_id = plan.tariff_plan_id
        if not new_id.endswith(USER_PLAN_ID_SUFFIX):
            new_id = f"{new_id}{USER_PLAN_ID_SUFFIX}"
        source_plan = self.find_plan(plan.tariff_plan_id)
        note_parts = [f"用户覆盖：{note}"]
        if source_plan is not None:
            note_parts.append(f"覆盖自：{source_plan.name}（{source_plan.tariff_plan_id}）")
        override = plan.model_copy(
            update={
                "tariff_plan_id": new_id,
                "status": status,
                "user_overridden": True,
                "override_note": note,
                "verified_by": verified_by or plan.verified_by or "用户核验",
                "notes": "\n".join([plan.notes, *note_parts]).strip(),
            }
        )
        self.save_plan(override)
        logger.info("保存用户覆盖电价计划：%s ← %s", new_id, plan.tariff_plan_id)
        return override

    def market_direct_variant(
        self,
        base_plan_id: str,
        *,
        market_energy_price_yuan_per_kwh: float,
        line_loss_price_yuan_per_kwh: float | None = None,
        override_note: str = "",
        status: TariffPlanStatus = TariffPlanStatus.VERIFIED,
    ) -> TariffPlan:
        """由代理购电计划派生**市场化直购**变体（§4.1 第 3、6 条、§7.3）。

        只替换"电能量"分项（代理购电价 → 用户实际市场电价），其余分项（输配电价、系统运行费折价、
        政府性基金及附加）沿用官方表；各时段直接单价按
        ``基础电价 × 政策浮动系数 + 固定分项`` 重算，其中基础电价 = 市场电价 + 线损折价。

        .. note::
           这是"**用户实际价格优先于未验证模板**"（§4.1 第 3 条）的落地入口：
           市场电价必须由用户从账单/合同录入，软件**不预填**任何市场电价数值。
        """
        source = self.get_plan(base_plan_id)
        if market_energy_price_yuan_per_kwh is None or float(market_energy_price_yuan_per_kwh) <= 0:
            raise ValidationError(
                "市场化直购变体必须提供大于 0 的市场电能量电价（元/千瓦时），"
                "该数值应由用户从账单或购电合同录入，软件不预填（§4.1 第 3 条）",
                field="market_energy_price_yuan_per_kwh",
            )
        line_loss = (
            float(line_loss_price_yuan_per_kwh)
            if line_loss_price_yuan_per_kwh is not None
            else next(
                (
                    float(component.value)
                    for component in source.price_components
                    if component.name.startswith("代理工商业上网环节线损")
                    and component.value is not None
                ),
                0.0,
            )
        )
        base = float(market_energy_price_yuan_per_kwh) + line_loss
        fixed = sum(
            float(component.value or 0.0)
            for component in source.price_components
            if component.included_in_tou_price and not component.adjustable_by_tou
        )
        rules = []
        for rule in source.time_period_rules:
            multiplier = rule.price_multiplier if rule.price_multiplier is not None else 1.0
            rules.append(
                rule.model_copy(
                    update={
                        "direct_price_yuan_per_kwh": base * float(multiplier) + fixed,
                        "price_basis": rule.price_basis,
                        "note": rule.note + "；电能量分项已替换为市场化直购价格（用户录入）",
                    }
                )
            )
        components = []
        for component in source.price_components:
            if component.name.startswith("代理购电价"):
                components.append(
                    component.model_copy(
                        update={
                            "name": "市场化直购电能量电价（用户录入）",
                            "component_type": component.component_type,
                            "value": float(market_energy_price_yuan_per_kwh),
                            "source_note": "用户从账单/购电合同录入，非软件预填（§4.1 第 3 条）",
                        }
                    )
                )
            elif component.name.startswith("代理工商业上网环节线损"):
                components.append(
                    component.model_copy(
                        update={
                            "value": line_loss,
                            "source_note": (
                                "用户录入的实际线损折价"
                                if line_loss_price_yuan_per_kwh is not None
                                else "沿用官方表一列 3 的 0.013643 元/千瓦时"
                            ),
                        }
                    )
                )
            else:
                components.append(component.model_copy(deep=True))
        variant = source.model_copy(
            update={
                "tariff_plan_id": f"{source.tariff_plan_id}_MARKET",
                "name": f"{source.name}（市场化直购变体）",
                "market_mode": MarketMode.RETAIL_MARKET,
                "status": status,
                "time_period_rules": rules,
                "price_components": components,
                "user_overridden": True,
                "override_note": override_note
                or f"由 {source.tariff_plan_id} 派生：代理购电价替换为 {market_energy_price_yuan_per_kwh} 元/千瓦时",
                "notes": "\n".join(
                    [
                        source.notes,
                        f"市场化直购变体：基础电价 = 市场电价 {market_energy_price_yuan_per_kwh} + "
                        f"线损折价 {line_loss} = {base:.6f} 元/千瓦时；固定分项 {fixed:.6f} 元/千瓦时；",
                        "注意：市场化直购用户的电能量部分按市场逐时价格结算，本变体用**单一市场电价**"
                        "近似为分时口径，只适用于口径对照；逐时结算请使用高频/逐小时价格数据（§7.4）。",
                    ]
                ),
            }
        )
        self.save_plan(variant)
        logger.info("派生市场化直购电价计划：%s ← %s", variant.tariff_plan_id, source.tariff_plan_id)
        return variant
