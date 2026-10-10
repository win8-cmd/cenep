"""黄金样本**字段级自动比对框架**（V2.5 缺口③）。

目标
----
把"真实账单 PDF + 人工核对后的期望 JSON"自动比出**逐字段差异报告**：

    字段｜期望｜实际｜绝对差｜相对差｜通过/失败

供测试（回归）与界面/报告（"为什么这个值和账单不一样"）共用同一套逻辑，
避免"测试里一套容差、界面里另一套口径"。

数据来源与口径
--------------
* **期望值**来自人工核对后的 JSON。仓库里已有的
  ``tests/data/dongfeng_2025_bill_facts.json``（某大工业用户 2025 年 12 个月账单，
  户名/户号属于业务敏感信息，仅在该数据文件内保留，**本模块代码不复制这些信息**）
  即本框架的**首个期望值来源**，其中 ``months[].billing_month`` 为账期键。
* **实际值**来自 :func:`cenep.data.bill_pdf_importer.parse_bill_pdf_full` 的行字典
  （键 = ``bill_importer.BILL_COLUMNS`` 的 ``field``）。
* **容差**：金额类 0.01 元、电量类 0.5 kWh、比率类相对 1e-4。
  选择依据是账单本身的有效位数（金额两位小数、电量整数、单价 6 位小数），
  不是"为了让它通过"而放宽。
* **等价值**：同一物理量在不同来源可能写法不同（期望 JSON 写 ``110千伏``，
  解析器写 ``110kV``）。这类差异由 :func:`normalize_voltage_kv` 之类的
  **显式归一化函数**处理，并在标注规范里写明——**不允许**用容差去掩盖语义差异。

重要约束
--------
* 本模块**不产生期望值**，也不修改任何账单数据；它只做比对与报告。
* **未比对到的字段一律标 ``unverified``（未验证）**，绝不写成"通过"。
* 若真实账单 PDF 缺失，:func:`compare_pdf_to_golden` 会抛出
  :class:`GoldenSampleMissingError`（中文），由测试决定 skip 还是 fail，
  避免"悄悄跳过"被当成"已验收"。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_KNOWN_GAPS",
    "FIELD_SPECS",
    "GoldenComparisonReport",
    "GoldenSampleMissingError",
    "Tolerance",
    "FieldSpec",
    "FieldComparison",
    "compare_month_facts",
    "compare_pdf_to_golden",
    "date_of",
    "find_month_facts",
    "list_golden_months",
    "load_golden_facts",
    "normalize_voltage_kv",
    "render_report_text",
    "render_report_markdown",
    "write_report",
]

#: 比对结果状态（不写中文，便于程序判断；报告里再翻译成中文）
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_MISSING_ACTUAL = "missing_actual"
STATUS_UNEXPECTED_ACTUAL = "unexpected_actual"
STATUS_NOT_PROVIDED = "not_provided"
STATUS_UNVERIFIED = "unverified"

_STATUS_TEXT = {
    STATUS_PASS: "通过",
    STATUS_FAIL: "失败（超差）",
    STATUS_MISSING_ACTUAL: "失败（实际缺失）",
    STATUS_UNEXPECTED_ACTUAL: "失败（期望缺失但实际有值）",
    STATUS_NOT_PROVIDED: "通过（双方均未提供）",
    STATUS_UNVERIFIED: "未验证（无法比对）",
}

#: 视为"失败"的状态
FAILING_STATUSES = frozenset({STATUS_FAIL, STATUS_MISSING_ACTUAL, STATUS_UNEXPECTED_ACTUAL})


class GoldenSampleMissingError(FileNotFoundError):
    """真实账单样本缺失（中文提示；由调用方决定 skip 或 fail）。"""


# --------------------------------------------------------------------------- #
# 容差与字段规格
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tolerance:
    """一个字段的判定容差：``|差| <= abs_tol + rel_tol * |期望|`` 即通过。"""

    abs_tol: float = 0.0
    rel_tol: float = 0.0

    def accepts(self, expected: float, actual: float) -> bool:
        """判断实际值是否落在容差内。"""
        return abs(actual - expected) <= self.abs_tol + self.rel_tol * abs(expected)

    def text(self) -> str:
        """人类可读的容差说明。"""
        parts = []
        if self.abs_tol:
            parts.append(f"绝对 ≤ {self.abs_tol:g}")
        if self.rel_tol:
            parts.append(f"相对 ≤ {self.rel_tol:g}")
        return "、".join(parts) if parts else "必须完全相等"


#: 货币（元）：账单保留两位小数
TOL_MONEY = Tolerance(abs_tol=0.01)
#: 电量（kWh）：账单为整数
TOL_ENERGY = Tolerance(abs_tol=0.5)
#: 功率/需量（kW、kVA）：账单为整数或有小数
TOL_POWER = Tolerance(abs_tol=0.5)
#: 单价（元/kWh）：账单 6~7 位小数，取相对容差
TOL_PRICE = Tolerance(rel_tol=1e-4)
#: 无量纲比率（功率因数等）
TOL_RATIO = Tolerance(abs_tol=1e-4)


def normalize_voltage_kv(value: Any) -> float | None:
    """把电压等级文本归一成 **千伏数值**。

    ``110千伏`` / ``交流110kV`` / ``110 kV`` → ``110.0``；无法识别返回 ``None``。

    这是**显式等价值**：期望 JSON 与解析器的写法不同，但物理量相同。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"(\d+(?:\.\d+)?)", str(value))
    return float(match.group(1)) if match else None


def date_of(value: Any) -> date | None:
    """把 ``YYYY-MM-DD`` / ``date`` 归一成 :class:`datetime.date`；无法识别返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


@dataclass(frozen=True)
class FieldSpec:
    """一条"期望 JSON 字段 ↔ 解析行字典字段"的比对规格。"""

    #: 期望 JSON 中的字段名（``.`` 表示层级，如 ``period_energy_kwh.VALLEY``）
    key: str
    #: 中文名（报告展示用）
    label: str
    #: 解析行字典中的键；为 ``None`` 时必须提供 ``derive``
    row_field: str | None = None
    #: 值类型：``number`` / ``text`` / ``date`` / ``voltage``
    kind: str = "number"
    tolerance: Tolerance = field(default_factory=Tolerance)
    #: 由行字典现算派生值（用于期望 JSON 里有、行字典里没有的字段，如分项单价）
    derive: Callable[[dict[str, Any]], Any] | None = None
    #: 归一化函数（对期望值与实际值都生效）
    normalize: Callable[[Any], Any] | None = None
    #: 派生值说明（写进报告，避免"这个数哪来的"说不清）
    derive_note: str = ""

    def actual_of(self, row: dict[str, Any]) -> Any:
        """从行字典取出/算出实际值。"""
        if self.derive is not None:
            return self.derive(row)
        if self.row_field is None:
            return None
        return row.get(self.row_field)


def _ratio(numerator_field: str, denominator_field: str = "energy_total_kwh"):
    """构造"分项金额 ÷ 总电量 = 分项单价"的派生函数。"""

    def derive(row: dict[str, Any]) -> float | None:
        numerator = row.get(numerator_field)
        denominator = row.get(denominator_field)
        if numerator is None or denominator in (None, 0):
            return None
        return float(numerator) / float(denominator)

    return derive


def _avg_price(row: dict[str, Any]) -> float | None:
    """账单平均电价 = 账单总额 ÷ 总电量（与账单自述的平均电价口径一致）。"""
    total = row.get("bill_total_yuan")
    energy = row.get("energy_total_kwh")
    if total is None or energy in (None, 0):
        return None
    return float(total) / float(energy)


#: 期望 JSON（``dongfeng_2025_bill_facts.json`` 结构）→ 解析行字典的字段规格。
#: 顺序即报告顺序；新增样本时优先扩充本表，而不是另写一套比对代码。
FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("billing_period_start", "账期起", "billing_period_start", "date"),
    FieldSpec("billing_period_end", "账期止", "billing_period_end", "date"),
    FieldSpec("customer_no", "户号", "meter_id", "text"),
    FieldSpec(
        "voltage_level",
        "电压等级",
        "voltage_level",
        "voltage",
        TOL_POWER,
        normalize=normalize_voltage_kv,
    ),
    FieldSpec("energy_total_kwh", "总购电量", "energy_total_kwh", "number", TOL_ENERGY),
    FieldSpec("energy_charge_yuan", "电度电费合计", "energy_charge_yuan", "number", TOL_MONEY),
    FieldSpec("demand_kw", "计费需量", "billing_demand_kw", "number", TOL_POWER),
    FieldSpec("demand_charge_yuan", "需量电费", "demand_charge_yuan", "number", TOL_MONEY),
    FieldSpec(
        "power_factor_yuan",
        "功率因数调整电费",
        "power_factor_adjustment_yuan",
        "number",
        TOL_MONEY,
    ),
    FieldSpec("bill_total_yuan", "账单总额", "bill_total_yuan", "number", TOL_MONEY),
    FieldSpec(
        "avg_price_declared",
        "平均电价（账单自述）",
        None,
        "number",
        TOL_PRICE,
        derive=_avg_price,
        derive_note="由 账单总额 ÷ 总购电量 现算（行字典无该字段）",
    ),
    FieldSpec("period_energy_kwh.SHARP_PEAK", "尖峰电量", "energy_sharp_kwh", "number", TOL_ENERGY),
    FieldSpec("period_energy_kwh.PEAK", "高峰电量", "energy_peak_kwh", "number", TOL_ENERGY),
    FieldSpec("period_energy_kwh.FLAT", "平段电量", "energy_flat_kwh", "number", TOL_ENERGY),
    FieldSpec("period_energy_kwh.VALLEY", "低谷电量", "energy_valley_kwh", "number", TOL_ENERGY),
    FieldSpec(
        "component_unit_price_yuan_per_kwh.market_energy",
        "市场化购电单价（派生）",
        None,
        "number",
        TOL_PRICE,
        derive=_ratio("market_purchase_charge_yuan"),
        derive_note="由 市场化购电电费 ÷ 总购电量 现算",
    ),
    FieldSpec(
        "component_unit_price_yuan_per_kwh.line_loss",
        "上网环节线损单价（派生）",
        None,
        "number",
        TOL_PRICE,
        derive=_ratio("line_loss_charge_yuan"),
        derive_note="由 上网环节线损费用 ÷ 总购电量 现算",
    ),
    FieldSpec(
        "component_unit_price_yuan_per_kwh.transmission_distribution",
        "输配电量电价（派生）",
        None,
        "number",
        TOL_PRICE,
        derive=_ratio("transmission_distribution_charge_yuan"),
        derive_note="由 输配电量电费 ÷ 总购电量 现算",
    ),
    FieldSpec(
        "component_unit_price_yuan_per_kwh.system_operation",
        "系统运行费单价（派生）",
        None,
        "number",
        TOL_PRICE,
        derive=_ratio("system_operation_charge_yuan"),
        derive_note="由 系统运行费用 ÷ 总购电量 现算",
    ),
    FieldSpec(
        "component_unit_price_yuan_per_kwh.government_fund",
        "政府性基金单价（派生）",
        None,
        "number",
        TOL_PRICE,
        derive=_ratio("government_fund_charge_yuan"),
        derive_note="由 政府性基金及附加 ÷ 总购电量 现算",
    ),
)

#: **已知缺口**（键 → 原因）。键可以是
#: ``"<账期>:<字段键>"``（只对该账期生效，推荐）或 ``"<字段键>"``（对所有账期生效）。
#: 这些字段的失败会被单独归类，使"真实存在的缺陷"与"意外回归"在报告里分得开。
#:
#: 规范要求：**不得**为了让报告好看而把失败塞进这里；每条都要有可复现的证据。
#: 已修复的缺口必须从这里移除（移出后若又失败，就会作为「意外失败」被测试拦住）。
#:
#: 修复记录（**不再登记为缺口**）：
#:   * ``2025-09:demand_kw`` —— 计费需量曾误读为 66,223 kW（实为 30,844 kW）。
#:     根因：需量子表「需量值」单元格被排版拆成 `30842.` / `066223` 两行，
#:     且 `30842.` 与表头行同处一个逻辑行被整行丢弃。已修复于
#:     ``bill_pdf_importer._parse_demand_and_pf``（保留表头行的非表头单元格）
#:     与 ``bill_pdf_importer._block_field_value``（拼接拆行单元格），
#:     回归测试见 ``tests/test_v25_pdf_demand_regression.py``。
DEFAULT_KNOWN_GAPS: dict[str, str] = {
    "2025-09:period_energy_kwh.VALLEY": (
        "2025-09 低谷电量差 534 kWh：解析得 1,864,348 kWh，期望 JSON 写 1,864,882 kWh。"
        "账单全文检索 1864882 无命中，该值应为「总电量 − 其余三时段」倒算所得；"
        "而账单第 2/3 页第 2 计量分组（…7778、…3848）的 534 kWh 因表格行跨页未被解析"
        "（该组 TOU 行在账单上全部印为 0，534 只出现在总行，故归属时段无法从文本层唯一确定）。"
        "两边孰对未定，标为待复核。见 taiqu-storage/golden_candidate_9月.md 缺陷 G2。"
    ),
}


# --------------------------------------------------------------------------- #
# 期望值装载
# --------------------------------------------------------------------------- #
def load_golden_facts(path: str | Path) -> dict[str, Any]:
    """读取期望值 JSON；文件缺失或格式不对时抛中文异常。"""
    target = Path(path)
    if not target.exists():
        raise GoldenSampleMissingError(
            f"黄金样本期望值文件不存在：{target}。"
            "请确认 tests/data 下的期望 JSON 是否随仓库完整检出。"
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GoldenSampleMissingError(
            f"黄金样本期望值文件不是合法 JSON：{target}（{exc}）。"
        ) from exc
    if not isinstance(data, dict) or "months" not in data:
        raise GoldenSampleMissingError(
            f"黄金样本期望值文件结构不符：{target} 缺少顶层 'months' 数组。"
            "期望结构见 tests/data/golden_bills/README.md 的标注规范。"
        )
    return data


def list_golden_months(facts: dict[str, Any]) -> list[str]:
    """列出期望值文件里所有账期键（如 ``["2025-09", "2025-10"]``）。"""
    return [
        str(month.get("billing_month"))
        for month in facts.get("months", [])
        if month.get("billing_month")
    ]


def find_month_facts(facts: dict[str, Any], billing_month: str) -> dict[str, Any]:
    """取出指定账期的期望值；找不到时抛中文 :class:`GoldenSampleMissingError`。"""
    for month in facts.get("months", []):
        if str(month.get("billing_month")) == str(billing_month):
            return month
    available = "、".join(list_golden_months(facts)) or "（无）"
    raise GoldenSampleMissingError(
        f"期望值中没有账期「{billing_month}」。该文件现有账期：{available}。"
    )


def _dig(data: dict[str, Any], key: str) -> Any:
    """按键的 ``.`` 层级取值；任一层缺失返回 ``None``。"""
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


# --------------------------------------------------------------------------- #
# 比对
# --------------------------------------------------------------------------- #
@dataclass
class FieldComparison:
    """单个字段的比对结果。"""

    field: str
    label: str
    expected: Any
    actual: Any
    status: str
    abs_diff: float | None = None
    rel_diff: float | None = None
    tolerance: Tolerance = field(default_factory=Tolerance)
    note: str = ""

    @property
    def passed(self) -> bool:
        """是否视为通过（含"双方均未提供"与"未验证"）。"""
        return self.status not in FAILING_STATUSES

    @property
    def failing(self) -> bool:
        """是否为失败（超差/缺失）。"""
        return self.status in FAILING_STATUSES

    @property
    def status_text(self) -> str:
        """状态的中文说明。"""
        return _STATUS_TEXT.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "field": self.field,
            "label": self.label,
            "expected": _jsonable(self.expected),
            "actual": _jsonable(self.actual),
            "abs_diff": self.abs_diff,
            "rel_diff": self.rel_diff,
            "status": self.status,
            "status_text": self.status_text,
            "tolerance": self.tolerance.text(),
            "note": self.note,
        }


def _jsonable(value: Any) -> Any:
    """把 ``date`` 等类型转成可 JSON 序列化的形式。"""
    if isinstance(value, date):
        return value.isoformat()
    return value


def _format_value(value: Any) -> str:
    """报告里的数值格式化（保持与账单一致的可读精度）。"""
    if value is None:
        return "—"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return f"{value:,.0f}"
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _compare_one(spec: FieldSpec, month_facts: dict[str, Any], row: dict[str, Any]) -> FieldComparison:
    """比对一个字段。"""
    expected_raw = _dig(month_facts, spec.key)
    actual_raw = spec.actual_of(row)
    note = spec.derive_note

    if spec.normalize is not None:
        expected = spec.normalize(expected_raw)
        actual = spec.normalize(actual_raw)
    else:
        expected = expected_raw
        actual = actual_raw

    if spec.kind == "date":
        expected = date_of(expected_raw)
        actual = date_of(actual_raw)

    # ---- 缺失语义（规范 §4 规则 1：缺失 ≠ 0） ---- #
    if expected is None and actual is None:
        return FieldComparison(
            spec.key, spec.label, expected, actual, STATUS_NOT_PROVIDED, tolerance=spec.tolerance,
            note=(note or "") + ("；" if note else "") + "期望与实际均未提供（缺失，不是 0）",
        )
    if expected is None:
        return FieldComparison(
            spec.key, spec.label, expected, actual, STATUS_UNEXPECTED_ACTUAL,
            tolerance=spec.tolerance,
            note=(note or "") + ("；" if note else "") + "期望 JSON 未提供该字段，但解析结果有值，请人工确认",
        )
    if actual is None:
        return FieldComparison(
            spec.key, spec.label, expected, actual, STATUS_MISSING_ACTUAL,
            tolerance=spec.tolerance,
            note=(note or "") + ("；" if note else "") + "解析结果未取到该字段（未提供，不是 0）",
        )

    # ---- 文本 / 日期：必须相等 ---- #
    if spec.kind in ("text", "date"):
        status = STATUS_PASS if expected == actual else STATUS_FAIL
        return FieldComparison(
            spec.key, spec.label, expected, actual, status, tolerance=spec.tolerance, note=note
        )

    # ---- 数值 / 电压：按容差 ---- #
    try:
        expected_number = float(expected)
        actual_number = float(actual)
    except (TypeError, ValueError):
        status = STATUS_PASS if expected == actual else STATUS_FAIL
        return FieldComparison(
            spec.key, spec.label, expected, actual, status, tolerance=spec.tolerance, note=note
        )

    abs_diff = abs(actual_number - expected_number)
    rel_diff = abs_diff / abs(expected_number) if expected_number else None
    status = STATUS_PASS if spec.tolerance.accepts(expected_number, actual_number) else STATUS_FAIL
    return FieldComparison(
        spec.key,
        spec.label,
        expected_number,
        actual_number,
        status,
        abs_diff=round(abs_diff, 6),
        rel_diff=None if rel_diff is None else round(rel_diff, 9),
        tolerance=spec.tolerance,
        note=note,
    )


@dataclass
class GoldenComparisonReport:
    """一份账单的字段级比对报告。"""

    bill_name: str
    billing_month: str
    source_pdf: str
    expected_source: str
    comparisons: list[FieldComparison] = field(default_factory=list)
    #: 已知缺口（字段键 → 原因）
    known_gaps: dict[str, str] = field(default_factory=dict)
    #: 附加说明（如"该月账单文件缺失，仅比对了可派生字段"）
    notes: list[str] = field(default_factory=list)

    # ---- 统计 ---- #
    @property
    def compared(self) -> int:
        """实际完成比对的字段数（不含 unverified）。"""
        return sum(1 for item in self.comparisons if item.status != STATUS_UNVERIFIED)

    @property
    def passed(self) -> int:
        """通过的字段数。"""
        return sum(1 for item in self.comparisons if item.status == STATUS_PASS)

    @property
    def not_provided(self) -> int:
        """双方均未提供的字段数。"""
        return sum(1 for item in self.comparisons if item.status == STATUS_NOT_PROVIDED)

    @property
    def failed(self) -> list[FieldComparison]:
        """全部失败项。"""
        return [item for item in self.comparisons if item.failing]

    def gap_reason(self, field_key: str) -> str | None:
        """返回该字段在本账期命中的已知缺口原因；未登记返回 ``None``。

        先查账期专属键 ``"<账期>:<字段>"``，再查全局键 ``"<字段>"``。
        """
        scoped = self.known_gaps.get(f"{self.billing_month}:{field_key}")
        if scoped is not None:
            return scoped
        return self.known_gaps.get(field_key)

    @property
    def known_failures(self) -> list[FieldComparison]:
        """命中已知缺口的失败项。"""
        return [item for item in self.failed if self.gap_reason(item.field) is not None]

    @property
    def unexpected_failures(self) -> list[FieldComparison]:
        """**不在**已知缺口里的失败项（回归信号）。"""
        return [item for item in self.failed if self.gap_reason(item.field) is None]

    @property
    def stale_known_gaps(self) -> list[str]:
        """登记为已知缺口、但本次**已经通过**的字段（提示可从缺口清单移除）。"""
        passing = {item.field for item in self.comparisons if item.status == STATUS_PASS}
        scoped = {
            key.split(":", 1)[1]
            for key in self.known_gaps
            if key.startswith(f"{self.billing_month}:")
        }
        candidates = scoped | set(self.known_gaps)
        return sorted(key for key in candidates if key in passing and ":" not in key)

    @property
    def ok(self) -> bool:
        """是否无"意外失败"。"""
        return not self.unexpected_failures

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "bill_name": self.bill_name,
            "billing_month": self.billing_month,
            "source_pdf": self.source_pdf,
            "expected_source": self.expected_source,
            "compared": self.compared,
            "passed": self.passed,
            "not_provided": self.not_provided,
            "failed": len(self.failed),
            "known_failures": [item.field for item in self.known_failures],
            "unexpected_failures": [item.field for item in self.unexpected_failures],
            "stale_known_gaps": self.stale_known_gaps,
            "notes": list(self.notes),
            "comparisons": [item.to_dict() for item in self.comparisons],
        }


def compare_month_facts(
    row: dict[str, Any],
    month_facts: dict[str, Any],
    *,
    specs: Sequence[FieldSpec] = FIELD_SPECS,
    known_gaps: dict[str, str] | None = None,
    source_pdf: str = "",
    expected_source: str = "",
    bill_name: str = "",
    notes: Iterable[str] = (),
) -> GoldenComparisonReport:
    """把**解析行字典**与**某月期望值**逐字段比对，返回报告。

    参数
    ----
    row:
        :func:`cenep.data.bill_pdf_importer.parse_bill_pdf_full` 的行字典。
    month_facts:
        期望值 JSON 中该账期的对象（含 ``billing_month`` / ``period_energy_kwh`` 等）。
    specs:
        比对规格；默认 :data:`FIELD_SPECS`。
    known_gaps:
        已知缺口字典；``None`` 时用 :data:`DEFAULT_KNOWN_GAPS`。
    """
    billing_month = str(month_facts.get("billing_month", ""))
    report = GoldenComparisonReport(
        bill_name=bill_name or str(month_facts.get("file_name", "")),
        billing_month=billing_month,
        source_pdf=source_pdf,
        expected_source=expected_source,
        known_gaps=dict(DEFAULT_KNOWN_GAPS if known_gaps is None else known_gaps),
        notes=list(notes),
    )
    for spec in specs:
        report.comparisons.append(_compare_one(spec, month_facts, row))
    logger.info(
        "黄金样本比对：账期 %s，比对 %d 项，通过 %d 项，失败 %d 项（其中已知缺口 %d 项）",
        billing_month,
        report.compared,
        report.passed,
        len(report.failed),
        len(report.known_failures),
    )
    return report


def compare_pdf_to_golden(
    pdf_path: str | Path,
    facts_path: str | Path,
    billing_month: str,
    *,
    specs: Sequence[FieldSpec] = FIELD_SPECS,
    known_gaps: dict[str, str] | None = None,
    parser: Callable[[str | Path], tuple[dict[str, Any], list[str], dict[str, Any]]] | None = None,
) -> GoldenComparisonReport:
    """解析真实 PDF 并与期望值比对，返回报告。

    参数
    ----
    pdf_path:
        真实账单 PDF 路径。**缺失时抛中文** :class:`GoldenSampleMissingError`
        （由测试决定 skip 还是 fail，避免"静默跳过"被误解为"已验收"）。
    facts_path:
        期望值 JSON 路径（如 ``tests/data/dongfeng_2025_bill_facts.json``）。
    billing_month:
        账期键，如 ``"2025-09"``。
    parser:
        解析函数，默认 :func:`cenep.data.bill_pdf_importer.parse_bill_pdf_full`；
        注入点便于测试替换。
    """
    target = Path(pdf_path)
    if not target.exists():
        raise GoldenSampleMissingError(
            f"真实账单样本缺失，无法比对：{target}。"
            "真实账单属于业务资料，不入仓库；请从资料来源目录取得后重跑。"
        )
    facts = load_golden_facts(facts_path)
    month_facts = find_month_facts(facts, billing_month)

    # 期望值里的文件名与本机文件名不一致时给出提示（不阻断）
    expected_name = str(month_facts.get("file_name", ""))
    notes: list[str] = []
    if expected_name and expected_name != target.name:
        notes.append(
            f"注意：期望值记录的样本文件名为「{expected_name}」，"
            f"本次比对使用「{target.name}」，请确认是否为同一份账单。"
        )

    if parser is None:
        from .bill_pdf_importer import parse_bill_pdf_full

        parser = parse_bill_pdf_full

    row, parser_notes, _extras = parser(target)
    notes.append(f"解析器提示 {len(parser_notes)} 条（明细见解析结果，本报告只做字段级比对）")
    return compare_month_facts(
        row,
        month_facts,
        specs=specs,
        known_gaps=known_gaps,
        source_pdf=str(target),
        expected_source=str(facts_path),
        bill_name=str(month_facts.get("file_name", target.name)),
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# 报告渲染
# --------------------------------------------------------------------------- #
def render_report_text(report: GoldenComparisonReport, *, show_passing: bool = True) -> str:
    """把报告渲染成**中文差异报告文本**（字段｜期望｜实际｜绝对差｜相对差｜结果）。"""
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append(f"黄金样本字段级比对报告 —— {report.bill_name}（账期 {report.billing_month}）")
    lines.append("=" * 100)
    lines.append(f"样本 PDF：{report.source_pdf}")
    lines.append(f"期望来源：{report.expected_source}")
    lines.append(
        f"统计：比对 {report.compared} 项｜通过 {report.passed} 项｜"
        f"双方均未提供 {report.not_provided} 项｜失败 {len(report.failed)} 项"
        f"（已知缺口 {len(report.known_failures)} 项，意外失败 {len(report.unexpected_failures)} 项）"
    )
    for note in report.notes:
        lines.append(f"说明：{note}")
    lines.append("")
    header = f"{'字段':<34}{'期望':>18}{'实际':>18}{'绝对差':>14}{'相对差':>13}  结果"
    lines.append(header)
    lines.append("-" * 100)
    for item in report.comparisons:
        if not show_passing and not item.failing:
            continue
        abs_text = "—" if item.abs_diff is None else f"{item.abs_diff:,.6g}"
        rel_text = "—" if item.rel_diff is None else f"{item.rel_diff:.3e}"
        marker = "✔" if item.passed else "✘"
        if item.failing and report.gap_reason(item.field):
            gap = "（已知缺口）"
        elif item.status == STATUS_PASS and report.gap_reason(item.field):
            gap = "（原已知缺口，本次通过）"
        else:
            gap = ""
        lines.append(
            f"{item.label:<34}{_format_value(item.expected):>18}"
            f"{_format_value(item.actual):>18}{abs_text:>14}{rel_text:>13}"
            f"  {marker} {item.status_text}{gap}"
        )
    if report.failed:
        lines.append("")
        lines.append("失败明细与依据：")
        for item in report.failed:
            lines.append(f"  · {item.label}（{item.field}）")
            reason = report.gap_reason(item.field)
            if reason:
                lines.append(f"    已知缺口：{reason}")
            lines.append(f"    容差：{item.tolerance.text()}；备注：{item.note or '（无）'}")
    if report.stale_known_gaps:
        lines.append("")
        lines.append(
            "提示：以下字段已登记为已知缺口，但本次比对**已通过**，"
            "请复核后从缺口清单移除：" + "、".join(report.stale_known_gaps)
        )
    lines.append("")
    lines.append(
        "注：本报告只反映**本次实际比对**的结果；未列入规格的字段一律视为未验证，"
        "不得据此声称「已验收」。"
    )
    return "\n".join(lines)


def render_report_markdown(report: GoldenComparisonReport) -> str:
    """把报告渲染成 Markdown 表格（便于粘进验收文档）。"""
    lines = [
        f"### 字段级比对：{report.bill_name}（账期 {report.billing_month}）",
        "",
        f"- 样本 PDF：`{report.source_pdf}`",
        f"- 期望来源：`{report.expected_source}`",
        f"- 统计：比对 {report.compared} 项 / 通过 {report.passed} 项 / "
        f"双方均未提供 {report.not_provided} 项 / 失败 {len(report.failed)} 项"
        f"（已知缺口 {len(report.known_failures)}，意外失败 {len(report.unexpected_failures)}）",
        "",
        "| 字段 | 期望 | 实际 | 绝对差 | 相对差 | 结果 |",
        "|---|---|---|---|---|---|",
    ]
    for item in report.comparisons:
        abs_text = "—" if item.abs_diff is None else f"{item.abs_diff:,.6g}"
        rel_text = "—" if item.rel_diff is None else f"{item.rel_diff:.3e}"
        lines.append(
            f"| {item.label} | {_format_value(item.expected)} | {_format_value(item.actual)} "
            f"| {abs_text} | {rel_text} | {item.status_text} |"
        )
    return "\n".join(lines)


def write_report(report: GoldenComparisonReport, path: str | Path) -> Path:
    """把报告写成文件（``.md`` → Markdown；其它 → 纯文本）。返回写入路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = (
        render_report_markdown(report)
        if target.suffix.lower() in (".md", ".markdown")
        else render_report_text(report)
    )
    target.write_text(text, encoding="utf-8")
    return target
