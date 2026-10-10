"""账单应用服务：手动录入 / 编辑 / 复制 / 删除 / 导入 / 汇总 / 保存（V2.1 §5.1、§5.4、§5.5）。

分层职责（V2.1 §1、§0.2）
------------------------
* 本模块**只做编排**：校验调用 :mod:`cenep.calculation.bill_calculator`，
  导入调用 :mod:`cenep.data.bill_importer`，持久化调用
  :mod:`cenep.infrastructure.project_file`。界面（阶段 2）只与本服务交互，
  **不得**在 GUI 里重写任何公式或电价逻辑。
* 账单事实直接挂在 :class:`~cenep.domain.models.Project` 的 ``bills`` 段上，
  随 ``.nep`` 项目文件一起保存/重开（V2.1 §8.2），不额外造一套存储。
* 报错一律是**中文** :class:`~cenep.calculation.errors.ValidationError`
  （带字段名），不把 Pydantic 的英文异常抛给用户（V2.1 §0.2）。

``project_id`` 的口径（已知限制，见报告）
--------------------------------------
V2 的项目模型没有持久 UUID，只有 ``basic_info.project_name``。因此本服务的
``project_id`` 默认取项目名称（保存到 ``.nep`` 后可用文件名覆盖），
用于"项目 + 账期 + 计量点"的重复判定（§5.5）。这不是加密标识，仅作同一性判断。
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from ..calculation.bill_calculator import (
    annual_bill_summary,
    apply_quality,
    monthly_summary,
    reconcile_bill,
)
from ..calculation.bill_price_source import (
    CUSTOMER_KIND_MARKET_DIRECT,
    BillEnergyPriceResolution,
    resolve_bill_energy_price,
)
from ..calculation.errors import ValidationError
from ..data.bill_importer import (
    TEMPLATE_FILE_NAME,
    BillImportPreview,
    BillImportResult,
    apply_bill_import,
    bill_template_bytes,
    build_bill_template,
    parse_bill_source,
    parse_tariff_structure,
    preview_bill_import,
)
from ..data.quality import score_bill_quality
from ..domain.bill_models import (
    BillAnnualSummary,
    BillMonthlySummary,
    BillQualityScore,
    BillReconciliation,
    BillTolerance,
    BillValidationIssue,
    ElectricityBill,
    billing_month_of,
    duplicate_key_of,
    label_of,
    make_bill_id,
)
from ..domain.enums import BillQualityStatus, BillSourceType, DuplicateStrategy
from ..domain.models import Project
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.project_file import load_project, save_project

logger = get_logger()

__all__ = ["BillService", "SORT_FIELDS", "is_valid_month"]

#: ``list_bills`` 支持的排序字段
SORT_FIELDS: tuple[str, ...] = (
    "period_start",
    "billing_month",
    "bill_total_yuan",
    "energy_total_kwh",
    "created_at",
    "updated_at",
)


class BillService:
    """项目账单的录入、校验、导入与汇总服务（V2.1 §5.4、§5.5）。"""

    def __init__(
        self,
        project: Project,
        *,
        project_id: str | None = None,
        tolerance: BillTolerance | None = None,
    ) -> None:
        self.project = project
        self.tolerance = tolerance or BillTolerance()
        self._project_id = (project_id or "").strip() or self._default_project_id(project)

    # ------------------------------------------------------------------ #
    # 基本信息
    # ------------------------------------------------------------------ #
    @staticmethod
    def _default_project_id(project: Project) -> str:
        name = (project.basic_info.project_name or "").strip()
        return name or "未命名项目"

    @property
    def project_id(self) -> str:
        """账单归属标识（默认项目名称；见模块文档的已知限制）。"""
        return self._project_id

    @project_id.setter
    def project_id(self, value: str) -> None:
        text = (value or "").strip()
        if not text:
            raise ValidationError("项目标识不能为空", field="project_id")
        self._project_id = text

    @property
    def bills(self) -> list[ElectricityBill]:
        """当前项目的账单事实列表（副本，防止外部直接改内部列表）。"""
        return list(self.project.bills)

    def __len__(self) -> int:
        return len(self.project.bills)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def list_bills(
        self,
        *,
        month: str | None = None,
        sort_by: str = "period_start",
        descending: bool = False,
    ) -> list[ElectricityBill]:
        """按月份筛选、按账期等排序的账单列表（V2.1 §5.4）。

        :param month: ``YYYY-MM``；``None`` = 全部
        :param sort_by: 见 :data:`SORT_FIELDS`
        :raises ValidationError: 月份格式非法或排序字段不支持（中文提示）
        """
        if month is not None and not is_valid_month(month):
            raise ValidationError(
                f"月份筛选值『{month}』格式不正确，请使用 YYYY-MM（如 2026-01）", field="billing_month"
            )
        if sort_by not in SORT_FIELDS:
            raise ValidationError(
                f"不支持的排序字段『{sort_by}』，可选：{'、'.join(SORT_FIELDS)}", field="sort_by"
            )

        selected = [b for b in self.project.bills if month is None or b.billing_month == month]
        key = {
            "period_start": lambda b: (b.billing_period_start, b.billing_period_end),
            "billing_month": lambda b: b.billing_month,
            "bill_total_yuan": lambda b: b.bill_total_yuan,
            "energy_total_kwh": lambda b: b.energy_total_kwh,
            "created_at": lambda b: b.created_at,
            "updated_at": lambda b: b.updated_at,
        }[sort_by]
        # "未提供"的账单永远排在最后（正序倒序都一样），避免 None 参与比较或抢占首行
        missing = [b for b in selected if _is_missing(b, sort_by)]
        provided = [b for b in selected if not _is_missing(b, sort_by)]
        return sorted(provided, key=key, reverse=descending) + missing

    def get_bill(self, bill_id: str) -> ElectricityBill:
        """按 ID 取账单。

        :raises ValidationError: 账单不存在（中文提示）
        """
        for bill in self.project.bills:
            if bill.bill_id == bill_id:
                return bill
        raise ValidationError(f"未找到账单：{bill_id}", field="bill_id")

    def find_by_duplicate_key(self, bill: ElectricityBill) -> ElectricityBill | None:
        """按"项目 + 账期 + 计量点"查找已存在的疑似重复账单（V2.1 §5.5）。"""
        key = bill.duplicate_key
        for existing in self.project.bills:
            if existing.duplicate_key == key:
                return existing
        return None

    def next_bill_id(
        self, period_start: date, period_end: date, meter_id: str | None = None
    ) -> str:
        """生成不与现有账单冲突的账单编号（同一账期+计量点重复时追加 ``-2``、``-3``…）。"""
        taken = {b.bill_id for b in self.project.bills}
        base = make_bill_id(period_start, period_end, meter_id)
        if base not in taken:
            return base
        index = 2
        while f"{base}-{index}" in taken:
            index += 1
        return f"{base}-{index}"

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def validate_bill(self, bill: ElectricityBill) -> BillReconciliation:
        """核对单条账单的自洽性（§2.1、§3.1），返回差异明细。"""
        return reconcile_bill(bill, tolerance=self.tolerance)

    def reconcile_all(self) -> list[BillReconciliation]:
        """核对全部账单（顺序与项目内保存顺序一致）。"""
        return [reconcile_bill(b, tolerance=self.tolerance) for b in self.project.bills]

    def score_bill(self, bill_id: str) -> BillQualityScore:
        """账单数据质量评分（§5.5）。"""
        return score_bill_quality(self.get_bill(bill_id), tolerance=self.tolerance)

    def resolve_energy_price(
        self,
        bill_id: str,
        *,
        customer_kind: str = CUSTOMER_KIND_MARKET_DIRECT,
        include_line_loss: bool = False,
        gov_tou_prices: dict[object, float] | None = None,
        gov_tou_energy_by_period: dict[object, float] | None = None,
    ) -> BillEnergyPriceResolution:
        """按**写死的优先级**给出某张账单的替代电价口径（V2.5 §5）。

        本方法**只做转调**，不含任何公式（§0.2：核心规则在 ``calculation/``，应用层与界面
        不得再实现一套"取平均电价"的逻辑）。优先级与中文说明见
        :func:`cenep.calculation.bill_price_source.resolve_bill_energy_price`：
        ① 账单 24 小时电量电价表（逐时交易价格，**首选**）→ ② 账单平均综合电价（降级并披露）
        → ③ 政府峰谷分时系数（**仅代理购电客户**，市场化直购客户调用即报中文错）。

        ``include_line_loss`` 默认为 ``False``（需求方确认的"逐时交易价格"口径）；
        传 ``True`` 得到"直接交易价格 + 上网环节线损价格"的全额成本口径。

        :raises ValidationError: 账单不存在 / 无任何可用电价来源 / 客户类型非法
        """
        bill = self.get_bill(bill_id)
        return resolve_bill_energy_price(
            bill,
            customer_kind=customer_kind,
            include_line_loss=include_line_loss,
            gov_tou_prices=gov_tou_prices,
            gov_tou_energy_by_period=gov_tou_energy_by_period,
        )

    def refresh_quality(self) -> int:
        """重算并写回全部账单的质量状态与说明，返回状态发生变化的条数。

        只改 ``quality_status`` / ``quality_messages``（§2.1 规定的数据质量字段），
        **不改动任何金额、电量事实**（V2.1 §0.2：账单事实与计算结果分开保存）。
        """
        changed = 0
        refreshed: list[ElectricityBill] = []
        for bill in self.project.bills:
            updated = apply_quality(bill, tolerance=self.tolerance)
            if (
                updated.quality_status != bill.quality_status
                or updated.quality_messages != bill.quality_messages
            ):
                changed += 1
            refreshed.append(updated)
        self.project.bills = refreshed
        if changed:
            logger.info("重算账单质量：%d / %d 条状态或说明发生变化", changed, len(refreshed))
        return changed

    def _ensure_valid(self, bill: ElectricityBill) -> ElectricityBill:
        """字段级硬错误（ERROR）时抛出**中文**校验异常，绝不静默入库。"""
        outcome = reconcile_bill(bill, tolerance=self.tolerance)
        errors = [issue for issue in outcome.issues if issue.level == "ERROR"]
        if errors:
            detail = "；".join(_with_label(issue) for issue in errors)
            raise ValidationError(
                f"账单数据未通过校验，无法保存：{detail}", field=errors[0].field or "bill"
            )
        return bill.model_copy(
            update={
                "quality_status": outcome.quality_status,
                "quality_messages": list(outcome.messages),
            }
        )

    # ------------------------------------------------------------------ #
    # 手动录入 / 编辑 / 删除 / 复制（§5.4）
    # ------------------------------------------------------------------ #
    def create_bill(self, **fields: object) -> ElectricityBill:
        """手动录入一条账单（V2.1 §5.4）。

        用法::

            service.create_bill(
                billing_period_start=date(2026, 1, 1),
                billing_period_end=date(2026, 1, 31),
                energy_total_kwh=100000, bill_total_yuan=65000,
            )

        :raises ValidationError: 字段名不支持、账期缺失/非法、字段值为负等（全部为中文提示）
        """
        unknown = [name for name in fields if name not in ElectricityBill.model_fields]
        if unknown:
            raise ValidationError(
                f"不支持的账单字段：{'、'.join(sorted(unknown))}；"
                f"可用字段：{'、'.join(sorted(ElectricityBill.model_fields))}",
                field=sorted(unknown)[0],
            )

        payload = dict(fields)
        start = payload.pop("billing_period_start", None)
        end = payload.pop("billing_period_end", None)
        if start is None or end is None:
            raise ValidationError(
                "手动录入账单必须提供账期起始日与结束日（billing_period_start / billing_period_end）",
                field="billing_period_start" if start is None else "billing_period_end",
            )

        payload.setdefault("project_id", self.project_id)
        payload.setdefault("source_type", BillSourceType.MANUAL)
        payload.setdefault("meter_id", None)

        period_start = start if isinstance(start, date) else _coerce_date(start, "billing_period_start")
        period_end = end if isinstance(end, date) else _coerce_date(end, "billing_period_end")
        payload["billing_period_start"] = period_start
        payload["billing_period_end"] = period_end
        _coerce_enum_text(payload)
        if "bill_id" not in payload:
            payload["bill_id"] = self.next_bill_id(period_start, period_end, payload.get("meter_id"))

        try:
            bill = ElectricityBill(**payload)  # type: ignore[arg-type]
        except PydanticValidationError as exc:
            raise ValidationError(_translate_pydantic(exc), field="bill") from exc

        return self.add_bill(bill)

    def add_bill(
        self,
        bill: ElectricityBill,
        *,
        on_duplicate: DuplicateStrategy | None = None,
        validate: bool = True,
    ) -> ElectricityBill:
        """把账单写入项目（V2.1 §5.5）。

        :param on_duplicate: 遇到"项目 + 账期 + 计量点"相同的账单时怎么办；
            ``None``（默认）= **不擅自决定**，抛出中文异常提示用户选择
            跳过 / 替换 / 保留。
        :param validate: 是否做字段级校验（``True`` 时 ERROR 级问题会拒绝保存）
        :raises ValidationError: 校验未通过，或存在重复且未指定处理策略
        """
        prepared = self._ensure_valid(bill) if validate else apply_quality(
            bill, tolerance=self.tolerance
        )
        existing = self.find_by_duplicate_key(prepared)

        if existing is None:
            self.project.bills = [*self.project.bills, prepared]
            logger.info("新增账单：%s", prepared.describe())
            return prepared

        if on_duplicate is None:
            raise ValidationError(
                f"已存在同项目、同账期、同计量点的账单『{existing.bill_id}』"
                f"（{existing.billing_period_start:%Y-%m-%d} ~ {existing.billing_period_end:%Y-%m-%d}，"
                f"计量点 {existing.meter_id or '未填写'}）；"
                "请明确选择：跳过（skip）/ 替换（replace）/ 保留两条（keep_both）",
                field="bill_id",
            )

        if on_duplicate is DuplicateStrategy.SKIP:
            logger.info("账单重复，按策略跳过：%s", existing.bill_id)
            return existing

        if on_duplicate is DuplicateStrategy.REPLACE:
            self.project.bills = [
                prepared if item.duplicate_key == existing.duplicate_key else item
                for item in self.project.bills
            ]
            logger.info("账单重复，按策略替换：%s → %s", existing.bill_id, prepared.bill_id)
            return prepared

        # KEEP_BOTH：给新账单换一个不冲突的 ID 后追加
        unique = prepared.model_copy(
            update={"bill_id": self.next_bill_id(
                prepared.billing_period_start, prepared.billing_period_end, prepared.meter_id
            )}
        )
        self.project.bills = [*self.project.bills, unique]
        logger.info("账单重复，按策略保留两条：%s / %s", existing.bill_id, unique.bill_id)
        return unique

    def update_bill(
        self, target_id: str, changes: dict[str, object] | None = None, **fields: object
    ) -> ElectricityBill:
        """编辑一条账单并重新校验（V2.1 §5.4）。

        两种写法等价：``update_bill(bid, bill_total_yuan=100)`` 与
        ``update_bill(bid, {"bill_total_yuan": 100})``。后者用于需要传入
        ``bill_id`` 等保留字段名的场景——保证"不允许修改"是**中文校验错误**
        而不是 Python 参数冲突。

        :param target_id: 要修改的账单编号（**不是**新编号）
        :raises ValidationError: 账单不存在、字段名不支持、校验未通过、
            或修改后的账期/计量点与另一条账单重复
        """
        current = self.get_bill(target_id)
        merged: dict[str, object] = {**(changes or {}), **fields}
        if not merged:
            return current

        unknown = [name for name in merged if name not in ElectricityBill.model_fields]
        if unknown:
            raise ValidationError(
                f"不支持的账单字段：{'、'.join(sorted(unknown))}", field=sorted(unknown)[0]
            )
        protected = {"bill_id", "created_at"} & set(merged)
        if protected:
            raise ValidationError(
                f"字段 {'、'.join(sorted(protected))} 不允许修改（账单编号与创建时间固定）",
                field=sorted(protected)[0],
            )

        _coerce_enum_text(merged)
        payload = current.model_dump()
        payload.update(merged)
        try:
            updated = ElectricityBill.model_validate(payload)
        except PydanticValidationError as exc:
            raise ValidationError(_translate_pydantic(exc), field="bill") from exc

        prepared = self._ensure_valid(updated)
        prepared.touch()

        collision = self.find_by_duplicate_key(prepared)
        if collision is not None and collision.bill_id != target_id:
            raise ValidationError(
                f"修改后的账期/计量点与已有账单『{collision.bill_id}』重复"
                f"（{collision.billing_period_start:%Y-%m-%d} ~ {collision.billing_period_end:%Y-%m-%d}），"
                "请调整账期或先删除该账单",
                field="billing_period_start",
            )

        self.project.bills = [prepared if b.bill_id == target_id else b for b in self.project.bills]
        logger.info("修改账单：%s", prepared.describe())
        return prepared

    def delete_bill(self, bill_id: str) -> ElectricityBill:
        """删除一条账单（界面需自行二次确认，见 §5.4）。

        :raises ValidationError: 账单不存在
        """
        target = self.get_bill(bill_id)
        self.project.bills = [b for b in self.project.bills if b.bill_id != bill_id]
        logger.info("删除账单：%s", target.bill_id)
        return target

    def copy_bill(
        self,
        bill_id: str,
        *,
        target_period_start: date | None = None,
        target_period_end: date | None = None,
    ) -> ElectricityBill:
        """复制一条账单到下一个月（V2.1 §5.4"复制上月账单"）。

        默认把账期整体后移一个月（起始日 +1 月，账期天数保持不变），
        电量与费用**原样复制**供用户修改；``notes`` 记录来源，来源类型记为手动录入。
        复制不会触发重复检测失败——账期不同，重复键不同。
        """
        source = self.get_bill(bill_id)
        if target_period_start is None:
            target_period_start = _add_months(source.billing_period_start, 1)
        if target_period_end is None:
            target_period_end = _add_months(source.billing_period_end, 1)

        copied = source.model_copy(
            update={
                "bill_id": self.next_bill_id(target_period_start, target_period_end, source.meter_id),
                "billing_period_start": target_period_start,
                "billing_period_end": target_period_end,
                "billing_month": billing_month_of(target_period_start),
                "source_type": BillSourceType.MANUAL,
                "source_file_name": None,
                "source_row_number": None,
                "notes": f"（复制自 {source.bill_id}：{source.billing_month}）{source.notes or ''}".strip(),
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "quality_status": BillQualityStatus.VALID,
                "quality_messages": [],
            }
        )
        return self.add_bill(copied, on_duplicate=DuplicateStrategy.SKIP)

    def copy_previous_month(self, month: str) -> ElectricityBill:
        """按月份复制上一条账单（V2.1 §5.4"复制上月账单"）。

        :param month: 目标月份 ``YYYY-MM``；取该月之前**最近一条**账单作为模板
        :raises ValidationError: 月份格式非法，或没有可复制的历史账单 / 历史账单不唯一
        """
        if not is_valid_month(month):
            raise ValidationError(
                f"月份『{month}』格式不正确，请使用 YYYY-MM（如 2026-01）", field="billing_month"
            )
        candidates = [b for b in self.project.bills if b.billing_month < month]
        if not candidates:
            raise ValidationError(
                f"没有早于 {month} 的账单可复制，请先手动录入或导入历史账单", field="billing_month"
            )
        latest = max(candidates, key=lambda b: (b.billing_month, b.billing_period_start))
        same_month = [b for b in candidates if b.billing_month == latest.billing_month]
        if len(same_month) > 1:
            raise ValidationError(
                f"{latest.billing_month} 有 {len(same_month)} 条账单，无法确定复制哪一条；"
                "请直接对目标账单使用『复制』功能",
                field="billing_month",
            )

        year, month_number = (int(part) for part in month.split("-"))
        last_day = calendar.monthrange(year, month_number)[1]
        return self.copy_bill(
            latest.bill_id,
            target_period_start=date(year, month_number, 1),
            target_period_end=date(year, month_number, last_day),
        )

    def clear(self) -> int:
        """清空全部账单（危险操作，调用方必须二次确认），返回删除条数。"""
        count = len(self.project.bills)
        self.project.bills = []
        logger.warning("清空账单：%d 条", count)
        return count

    # ------------------------------------------------------------------ #
    # Excel 模板与导入（§5.3、§5.5）
    # ------------------------------------------------------------------ #
    def export_template(self, target: str | Path | None = None) -> Path:
        """生成账单导入模板 ``CENEP_电费账单导入模板.xlsx``（§5.3）。

        :param target: 目标文件或目录；``None`` = 当前工作目录下的默认文件名
        """
        path = Path(target) if target is not None else Path.cwd() / TEMPLATE_FILE_NAME
        saved = build_bill_template(path)
        logger.info("导出账单模板：%s", saved)
        return saved

    @staticmethod
    def template_bytes() -> bytes:
        """模板文件字节，供界面"下载模板"（阶段 2 使用）。"""
        return bill_template_bytes()

    def preview_import(
        self,
        path: str | Path,
        *,
        sheet: str | None = None,
        column_mapping: dict[str, str] | None = None,
        skip_example_rows: bool = True,
    ) -> BillImportPreview:
        """生成导入预览（列映射 + 逐行校验 + 重复识别），不写入项目（§5.3、§5.5）。"""
        return preview_bill_import(
            path,
            sheet=sheet,
            column_mapping=column_mapping,
            skip_example_rows=skip_example_rows,
            existing_bills=self.project.bills,
            project_id=self.project_id,
            tolerance=self.tolerance,
        )

    def import_bills(
        self,
        path: str | Path | None = None,
        *,
        preview: BillImportPreview | None = None,
        strategy: DuplicateStrategy = DuplicateStrategy.SKIP,
        sheet: str | None = None,
        column_mapping: dict[str, str] | None = None,
        skip_example_rows: bool = True,
    ) -> BillImportResult:
        """导入账单并写入项目（V2.1 §5.3、§5.5）。

        重复账单按 ``strategy`` 处理（跳过 / 替换 / 保留），**不会重复写入同一条账单**。
        无效行不会入库，其行号在 :attr:`BillImportResult.invalid_rows` 中列出。

        :raises ValidationError: 文件 / 工作表 / 必需列层面的致命错误（中文提示）
        """
        if preview is None:
            if path is None:
                raise ValidationError("导入账单必须提供文件路径或已生成的导入预览", field="path")
            preview = self.preview_import(
                path,
                sheet=sheet,
                column_mapping=column_mapping,
                skip_example_rows=skip_example_rows,
            )
        result = apply_bill_import(preview, strategy=strategy)

        merged = list(self.project.bills)
        for bill in (*result.added, *result.kept_both):
            merged.append(bill)
        # 替换：同一"项目+账期+计量点"就地替换；项目里已不存在该账单时补录，避免丢数据
        for bill in result.replaced:
            key = bill.duplicate_key
            for index, item in enumerate(merged):
                if item.duplicate_key == key:
                    merged[index] = bill
                    break
            else:
                merged.append(bill)
        self.project.bills = merged

        if result.skipped_ids:
            logger.info("导入时跳过重复账单 %d 条", len(result.skipped_ids))
        if result.invalid_rows:
            logger.warning("导入时忽略无效行：%s", result.invalid_rows)
        logger.info("账单导入完成：%s", result.summary_text())
        return result

    # ------------------------------------------------------------------ #
    # 汇总（供界面"月度趋势与基准电费"使用，§5.1）
    # ------------------------------------------------------------------ #
    def monthly_summary(self) -> list[BillMonthlySummary]:
        """按月份的账单汇总（含平均综合电价），供 UI 月度趋势使用（§3.1）。"""
        return monthly_summary(self.project.bills, tolerance=self.tolerance)

    def annual_summary(self, year: int | None = None) -> BillAnnualSummary:
        """年度账单基准数据（含覆盖率与可直接相加的判定，§3.1）。"""
        return annual_bill_summary(self.project.bills, year=year, tolerance=self.tolerance)

    def monthly_trend(self) -> list[tuple[str, float | None, float | None]]:
        """``[(月份, 总电量 kWh, 账单总额 元), …]``，电量/金额未提供时为 ``None``。"""
        return [
            (item.billing_month, item.energy_total_kwh, item.amount_total_yuan)
            for item in self.monthly_summary()
        ]

    def month_coverage(self) -> dict[str, int]:
        """``{月份: 账单条数}``，用于界面提示缺月与重复（§5.4）。"""
        counts: dict[str, int] = defaultdict(int)
        for bill in self.project.bills:
            counts[bill.billing_month] += 1
        return dict(sorted(counts.items()))

    # ------------------------------------------------------------------ #
    # 持久化（§8.2）
    # ------------------------------------------------------------------ #
    def save(self, path: str | Path) -> Path:
        """把项目（含账单）保存到 ``.nep``（V2.1 §8.2）。"""
        saved = save_project(self.project, path)
        logger.info("保存项目（含 %d 条账单）：%s", len(self.project.bills), saved)
        return saved

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        project_id: str | None = None,
        tolerance: BillTolerance | None = None,
    ) -> tuple[Project, BillService]:
        """打开 ``.nep`` 并返回 ``(项目, 账单服务)``；旧项目照常打开（V2.1 §8.2）。

        旧项目没有账单段时，服务返回空账单列表，**不会生成任何虚构账单**。
        """
        project = load_project(path)
        return project, cls(project, project_id=project_id, tolerance=tolerance)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def is_valid_month(text: str) -> bool:
    """``YYYY-MM`` 是否合法（供界面做输入校验，避免各页面各写一套判断）。"""
    parts = str(text).split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return False
    year, month = int(parts[0]), int(parts[1])
    return 1900 <= year <= 2200 and 1 <= month <= 12


def _coerce_enum_text(payload: dict[str, object]) -> None:
    """把用户填写的**中文标签**（如"单一制"）换成枚举成员，就地修改 ``payload``。

    界面下拉框显示中文，因此手动录入 API 必须同时接受中文标签与机器值
    （``single_part``）。解析口径与 Excel 导入完全一致，见
    :func:`cenep.data.bill_importer.parse_tariff_structure`。

    :raises ValidationError: 取值不在允许范围内（中文提示）
    """
    structure = payload.get("tariff_structure")
    if isinstance(structure, str) and structure.strip():
        parsed = parse_tariff_structure(structure)
        if parsed is None:
            raise ValidationError(
                f"计费方式『{structure}』不是有效取值，允许值：单一制、两部制、未知"
                "（或 single_part / two_part / unknown）",
                field="tariff_structure",
            )
        payload["tariff_structure"] = parsed

    source = payload.get("source_type")
    if isinstance(source, str) and source.strip():
        parsed_source = parse_bill_source(source)
        if parsed_source is None:
            raise ValidationError(
                f"数据来源『{source}』不是有效取值，允许值：手动录入、Excel导入、估算"
                "（或 manual / excel / estimated）",
                field="source_type",
            )
        payload["source_type"] = parsed_source


def _is_missing(bill: ElectricityBill, sort_by: str) -> bool:
    """该账单在排序字段上是否"未提供"（``None``），用于把这类记录固定排在末尾。"""
    if sort_by in ("bill_total_yuan", "energy_total_kwh"):
        return getattr(bill, sort_by) is None
    return False


def _with_label(issue: BillValidationIssue) -> str:
    """给校验问题加上字段中文名；消息里已经有该字段名时不重复添加。"""
    if not issue.field:
        return issue.message
    label = label_of(issue.field)
    return issue.message if issue.message.startswith(label) else f"{label}：{issue.message}"


def _coerce_date(value: object, field: str) -> date:
    """把用户输入（``date`` / ``datetime`` / ``YYYY-MM-DD`` 字符串）转成日期。"""
    from ..data.importer import parse_timestamp

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    stamp = parse_timestamp(value)
    if stamp is None:
        raise ValidationError(
            f"{label_of(field)}『{value}』不是有效日期，请使用 YYYY-MM-DD（如 2026-01-01）", field=field
        )
    return stamp.date()


def _add_months(value: date, months: int) -> date:
    """按月平移日期；目标月没有该日时取该月最后一天（如 1/31 + 1 月 → 2/28）。"""
    total = value.year * 12 + (value.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(value.day, last_day))


def _translate_pydantic(exc: PydanticValidationError) -> str:
    """把 Pydantic 校验异常翻成一条中文说明（保留字段名）。"""
    parts: list[str] = []
    for err in exc.errors():
        location = err.get("loc", ())
        field_name = str(location[0]) if location else ""
        message = str(err.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{label_of(field_name)}：{message}" if field_name else message)
    return "账单数据校验未通过：" + "；".join(parts)
