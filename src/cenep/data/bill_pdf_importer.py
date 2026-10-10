"""电网电费账单 PDF 解析器（国网湖北版式，V2.5 新增；V2.5 修复为全量解析）。

背景
----
``bill_importer`` 只认 Excel / CSV：它经 :func:`~cenep.data.importer.read_table_from_sheet`
用 openpyxl 逐单元格取值。但实际收资里大量账单是电网下发的 **PDF**，本模块把这类 PDF
解析成"和 Excel 表完全同构的行字典"，交给 ``bill_importer`` 的既有链路
（列映射 → 预览 → 校验 → 重复识别 → 入库）复用，**不在本模块做任何计算或写库**。

设计边界
--------
* 本模块**只负责"读 PDF → 行字典 + 结构化账单事实"**，不接触项目对象、不写库、
  不做电价计算（V2.1 §0.2）。
* 解析失败 / 字段缺失一律返回 ``None`` 或写入 ``messages``（中文），**不抛裸异常**（§0.2）；
  只有"文件根本不是 PDF / 无法打开"才抛 :class:`ValidationError`。
* 每个字段都记录来源提示（来自哪一章、哪一行），供预览界面展示"为什么是这个值"。
* 输出行字典的键**直接用 ``BILL_COLUMNS`` 的 ``field`` 名**（如 ``energy_sharp_kwh``）。

账单实际包含的五个章节（V2.5，以国网湖北 2025-10 东本三厂账单核验）
------------------------------------------------------------------
1. **账单概况**（第 1 页）：本期电量 / 本期电费 / 费用组成（5 项）/ 峰谷比例 /
   平均电价 / 功率因数调整电费 / 需（容）量电费 / 工商业电费小计 / 增值税发票金额。
2. **电量明细 + 电费明细**（第 2、3 页）：按**电能表分组**逐块给出
   ``示数类型｜上期示数｜本期示数｜倍率｜抄见电量｜变损｜线损｜加减｜计费电量``，
   紧接该表的电费明细 ``费用类别｜费用组成｜分时时段｜计费电量｜计费标准｜电费``。
   分组头形如 ``电能表编号：…，电价：…``；**定比分表**的组头含"定比"且带
   ``上级电能表编号``。
3. **第 4 页**：延续第 3 页的电费明细，然后是「输配容（需）量电费 / 功率因数调整电费」
   两张并列子表（需量值、需量电价、功率因数实际值 / 标准 / 调整系数、
   月每千伏安用电量、合同容量、参与调整电费金额）+ **24 小时电量电价表**。
4. **第 5 页**：市场化运营费明细（B 增加支出 / C 降低支出 / 虚拟电厂调峰 / 调频）。

取数原则（V2.5 §5）
------------------
* 第 1 页的"峰谷比例"是**汇总口径**（本月写作 尖峰0% / 峰0% / 平100% / 谷0%），
  而第 2/3 页的**电量明细里有真正的分时电量**。两者冲突时**一律以明细为准**，
  并在 ``messages`` / ``assumptions`` 中说明取数来源与差异（不丢明细的分时电量）。
* 总电量取第 1 页「本期电量」（= 各电能表计费电量之和），分时电量取各表明细之和。
* 24 小时电价表只覆盖参与市场化交易的电能表，其电量合计**不必**等于全站总电量；
  差额在提示里如实给出，不判错、不臆造。

已知版式（国网湖北，同一户逐月导出，非固定模板）
------------------------------------------------
1. **表格版**（2024-01 ~ 2024-08）：分时电量在"尖峰时段(kW·h)"四标签块里，按坐标就近取值。
2. **电量明细版**（2024-09 ~ 2024-12）：出现"正向有功（尖峰/峰/平/谷/总）"抄表行，
   且存在 **定比（0.03）分表**，必须主表 + 定比相加才是总电量。
3. **账单概况版**（2025 起）：电量集中在"账单概况"块，且自 2025-01 起概况页写作
   **平段单一电价**（峰谷比例 尖峰0% / 峰0% / 平100% / 谷0%），
   **但第 2/3 页电量明细里仍有真实的尖峰 / 峰 / 谷 / 平分时电量**（V2.5 修复点）。

三大勾稽（与 ``bill_calculator.reconcile_bill`` 口径一致，本模块只做"辅助核对"，不判废）
------------------------------------------------------------------------------------
* 分时电量合计 = 总购电量（取明细之和与第 1 页总电量相比）
* 电度电费合计 = 代理购电 + 输配 + 线损 + 系统运行 + 政府性基金及附加
* 账单总额 = 电度电费合计 + 基本电费 + 功率因数调整电费
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..calculation.errors import ValidationError
from ..domain.bill_models import (
    HOURLY_PRICE_HOURS,
    HourlyEnergyPricePoint,
    OperationFeeDetail,
    OperationFeeItem,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PDF_SUFFIXES",
    "is_pdf",
    "parse_bill_pdf",
    "parse_bill_pdf_full",
    "pdf_row_dict",
    "pdf_row_payload",
]

#: 本模块处理的 PDF 后缀
PDF_SUFFIXES: tuple[str, ...] = (".pdf",)

# --------------------------------------------------------------------------- #
# 字段口径：解析结果键 → BILL_COLUMNS 的 field
# --------------------------------------------------------------------------- #
#: 解析器内部键 → 模板字段名。左列是本模块 ``parse_bill_pdf`` 返回的键，
#: 右列是 ``bill_importer.BILL_COLUMNS`` 里的 ``field``。
_FIELD_MAP: dict[str, str] = {
    "period_start": "billing_period_start",
    "period_end": "billing_period_end",
    "account": "meter_id",
    "customer_name": "customer_name",
    "voltage": "voltage_level",
    "tariff": "tariff_structure",
    "capacity": "contract_capacity_kva",
    "demand": "billing_demand_kw",
    "energy_total": "energy_total_kwh",
    "sharp": "energy_sharp_kwh",
    "peak": "energy_peak_kwh",
    "flat": "energy_flat_kwh",
    "valley": "energy_valley_kwh",
    "offpeak": "energy_offpeak_kwh",
    "energy_charge": "energy_charge_yuan",
    "market": "market_purchase_charge_yuan",
    "trans": "transmission_distribution_charge_yuan",
    "loss": "line_loss_charge_yuan",
    "sysop": "system_operation_charge_yuan",
    "gov": "government_fund_charge_yuan",
    "basic_capacity": "basic_capacity_charge_yuan",
    "basic_demand": "demand_charge_yuan",
    "pf": "power_factor_adjustment_yuan",
    "other": "other_charge_yuan",
    "adjustment": "adjustment_charge_yuan",
    "bill_total": "bill_total_yuan",
    # ---- V2.5 追加：需量电价与功率因数（标量账单事实）----
    "demand_rate": "demand_rate_yuan_per_kw_month",
    "pf_actual": "power_factor_actual",
    "pf_standard": "power_factor_standard",
    "pf_factor": "power_factor_adjustment_factor",
    "pf_participating": "power_factor_participating_charge_yuan",
    "energy_per_kva": "energy_per_kva_kwh",
}

#: V2.5 §3 **结构化**账单事实：值不是标量，不能放进"一行表格"的行字典。
#: 它们由 :func:`pdf_row_payload` 单独返回，再由
#: :func:`cenep.data.bill_importer.build_bill_from_row` 的 ``bill_extras`` 参数写入账单。
_EXTRA_KEYS: tuple[str, ...] = ("hourly_energy_tariff", "operation_fee_detail")

#: 不作为账单事实字段的解析键（仅内部记录，避免污染模板口径）
_SKIP_KEYS: frozenset[str] = frozenset({"_vat_invoice", "vat", "_pv_pct", "_pv_pct_source"})

#: 数值字段（供上层把空值规范成 None）
_NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        "contract_capacity_kva",
        "billing_demand_kw",
        "energy_total_kwh",
        "energy_sharp_kwh",
        "energy_peak_kwh",
        "energy_flat_kwh",
        "energy_valley_kwh",
        "energy_offpeak_kwh",
        "energy_charge_yuan",
        "market_purchase_charge_yuan",
        "transmission_distribution_charge_yuan",
        "line_loss_charge_yuan",
        "system_operation_charge_yuan",
        "government_fund_charge_yuan",
        "basic_capacity_charge_yuan",
        "demand_charge_yuan",
        "power_factor_adjustment_yuan",
        "other_charge_yuan",
        "vat_yuan",
        "adjustment_charge_yuan",
        "bill_total_yuan",
        "demand_rate_yuan_per_kw_month",
        "power_factor_actual",
        "power_factor_standard",
        "power_factor_adjustment_factor",
        "power_factor_participating_charge_yuan",
        "energy_per_kva_kwh",
    }
)

#: 分时时段字段（尖峰 / 峰 / 平 / 谷 / 深谷）→ 中文名
_PERIOD_LABELS: dict[str, str] = {
    "sharp": "尖峰",
    "peak": "高峰",
    "flat": "平段",
    "valley": "低谷",
    "offpeak": "深谷",
}

#: 分时时段字段的求和顺序（与 bill_models.BILL_ENERGY_FIELDS 的时段口径一致）
_PERIOD_KEYS: tuple[str, ...] = ("sharp", "peak", "flat", "valley", "offpeak")


def is_pdf(path: str | Path) -> bool:
    """判断路径是否为 PDF（按后缀，不读文件）。"""
    return Path(path).suffix.lower() in PDF_SUFFIXES


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
_MISSING_TOKENS = ("", "-", "—", "--", "/", "－", "None")


def _num(value: Any) -> float | None:
    """把单元格 / 文本转成 float；空、``-``、``—`` 返回 ``None``（= 未提供，不是 0）。"""
    if value is None:
        return None
    text = (
        str(value)
        .replace(",", "")
        .replace("￥", "")
        .replace("¥", "")
        .replace("\u3000", "")
        .strip()
    )
    if text in _MISSING_TOKENS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _words(page) -> list[tuple[float, float, str]]:
    """返回 ``[(x0, y0, text)]``（PyMuPDF ``get_text('words')`` 的坐标版）。

    坐标必须保留：电网账单是多列表格，纯文本抽取会把阅读顺序打乱，
    同一行的"标签"与"数值"会错位（这也是本模块存在的根本原因）。
    """
    return [(round(w[0], 1), round(w[1], 1), w[4]) for w in page.get_text("words")]


def _open_document(path: str | Path):
    """打开 PDF；失败统一翻译成中文 ``ValidationError``（§0.2、§9.1）。"""
    target = Path(path)
    if not target.exists():
        raise ValidationError(f"账单文件不存在：{target}", field="bill.pdf.missing")
    try:
        import pymupdf  # 延迟导入：Excel 路径不需要 PDF 库
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        try:
            import fitz as pymupdf  # type: ignore[no-redef]
        except ImportError:
            raise ValidationError(
                "解析 PDF 账单需要 PyMuPDF，请先安装该依赖（pip install pymupdf）",
                field="bill.pdf.dependency",
            ) from exc
    try:
        return pymupdf.open(str(target)), pymupdf
    except Exception as exc:
        raise ValidationError(
            f"无法打开 PDF 账单「{target.name}」：{exc}", field="bill.pdf.open"
        ) from exc


def _number_tokens(
    words: list[tuple[float, float, str]], x_min: float, x_max: float, y: float, y_tol: float
) -> list[tuple[float, float, Any]]:
    """取出 ``y`` 附近 ``[x_min, x_max)`` 区间内的数值 token：``[(x, y, 数值)]``。"""
    out: list[tuple[float, float, Any]] = []
    for x, token_y, text in words:
        if not (x_min <= x < x_max) or abs(token_y - y) > y_tol:
            continue
        value = _num(text)
        if value is not None:
            out.append((x, token_y, value))
    return out


def _join_line(words: list[tuple[float, float, str]], y: float, y_tol: float = 1.0) -> str:
    """把同一 y 带的词拼成一行文本（用于"含某关键字"这类整行判断）。"""
    return "".join(
        text for _, token_y, text in sorted(words, key=lambda w: w[0]) if abs(token_y - y) <= y_tol
    )


# --------------------------------------------------------------------------- #
# 表格行重建：把 word 坐标聚合成"逻辑行"
# --------------------------------------------------------------------------- #
def _page_rows(page) -> list[tuple[float, list[tuple[float, str]]]]:
    """把一页的 word 聚合成 ``[(y, [(x, text), …])]``（按 y 升序）。

    聚合粒度 0.5pt：PDF 里同一条表格行的文字基线会有零点几磅的抖动，
    不聚到一起就会把一行拆成好几行，导致"标签在上一行、数值在下一行"。
    """
    buckets: dict[float, list[tuple[float, str]]] = {}
    for x, y, text in _words(page):
        key = round(y * 2) / 2
        buckets.setdefault(key, []).append((x, text))
    return [(y, sorted(cells)) for y, cells in sorted(buckets.items())]


def _text_of(cells: list[tuple[float, str]]) -> str:
    """把逻辑行的单元格拼成文本（不加空格：中文标签与数字之间没有分隔符）。"""
    return "".join(text for _, text in cells)


_LABEL_TOKEN = re.compile(r"^[^\d\s]+$")


def _looks_like_label_row(cells: list[tuple[float, str]]) -> bool:
    """该逻辑行是否"只有标签、没有数值"（用于判定"标签行 + 数值行"的排版拆行）。"""
    if not cells:
        return False
    for _, text in cells:
        stripped = text.strip()
        if _num(stripped) is not None:
            return False
        if not _LABEL_TOKEN.match(stripped):
            return False
    return True


def _pair_rows(
    rows: list[tuple[float, list[tuple[float, str]]]], *, max_gap: float = 13.0
) -> list[tuple[float, list[tuple[float, str]]]]:
    """把"标签行 + 数值行"配成一条逻辑行（**不合并**单元格，保留列结构）。

    电网账单的表格里，标签与数值的基线常常差 ~7pt（真实账单）甚至 ~12pt
    （测试里的合成 PDF），直接按 y 分组会得到"标签在上一行、数值在下一行"。
    配对规则（保守）：

    * 当前行**只含标签**（无任何数值 token）时，暂存为"待配对标签行"；
    * 紧随其后的行若含数值且与前一行相距 ``≤ max_gap``（默认 13pt），
      则把标签行的单元格**原样前置**，得到"标签 + 数值"的一条逻辑行
      （数值仍各占各的 x，列结构不被破坏）；
    * 标签行若没有等到数值行（例如 24 小时电价表的表头），原样保留。

    为什么默认 13pt：真实账单的行距约 14~18pt，而"标签行 + 数值行"的间距约 7pt；
    阈值一旦放大到行距，就会把相邻条目的数值并进上一条标签，语义会被破坏。
    """
    paired: list[tuple[float, list[tuple[float, str]]]] = []
    pending: tuple[float, list[tuple[float, str]]] | None = None
    for y, cells in rows:
        has_values = any(_num(text) is not None for _, text in cells)
        if pending is not None and has_values and (y - pending[0]) <= max_gap:
            paired.append((y, [*pending[1], *cells]))
            pending = None
            continue
        if pending is not None:
            paired.append(pending)
            pending = None
        if not has_values and _looks_like_label_row(cells):
            pending = (y, cells)
            continue
        paired.append((y, cells))
    if pending is not None:
        paired.append(pending)
    return paired


def _iter_paired_rows(doc):
    """按页产出"已配对"的逻辑行：``(页码, [(y, 单元格)])``。"""
    for page_index, page in enumerate(doc, start=1):
        yield page_index, _pair_rows(_page_rows(page))


def _segment_by_header(
    rows: list[tuple[float, list[tuple[float, str]]]], header_text: str
) -> list[list[tuple[float, list[tuple[float, str]]]]]:
    """按"表头行文本"把一页切成若干段（每个表头开启一段，段到下一个表头结束）。

    保留为公共小工具：解析"电量明细 / 电费明细"等重复出现的同构表时都用它。
    """
    segments: list[list[tuple[float, list[tuple[float, str]]]]] = []
    current: list[tuple[float, list[tuple[float, str]]]] | None = None
    for y, cells in rows:
        if header_text in _text_of(cells):
            if current:
                segments.append(current)
            current = []
            continue
        if current is not None:
            current.append((y, cells))
    if current:
        segments.append(current)
    return segments


# --------------------------------------------------------------------------- #
# 章节 1：账单概况（第 1 页）
# --------------------------------------------------------------------------- #
def _parse_header_text(doc) -> dict[str, Any]:
    """三种版式通用的账单头（文本正则，容错多写法）。"""
    text = "\n".join(page.get_text() for page in doc)
    result: dict[str, Any] = {}

    match = re.search(r"账单周期[^\n]*\n\s*(\d{4}-\d{2}-\d{2})", text)
    if match is None:
        match = re.search(r"账单周期\s*\n\s*(\d{4}-\d{2}-\d{2})", text)
    if match:
        result["period_start"] = match.group(1)

    match = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:\n\s*)?(?:用电地址|管理单位|户名)", text)
    if match:
        result["period_end"] = match.group(1)
    else:
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
        if len(dates) >= 2:
            result["period_end"] = dates[-1]

    match = re.search(r"户号[：:\s]*(\d{13})", text)
    if match:
        result["account"] = match.group(1)
    match = re.search(r"电压等级[：:\s]*(交流\d+kV)", text)
    if match:
        result["voltage"] = match.group(1)
    match = re.search(r"户名[：:\s]*([^\n]{2,40})", text)
    if match:
        result["customer_name"] = match.group(1).strip()

    match = re.search(r"本期电量\s*\n?\s*(\d{6,8})\s*(?:kW·h|千瓦时)", text)
    if match is None:
        match = re.search(r"(\d{7,8})kW·h\s*\n本期电量", text)
    if match:
        result["energy_total"] = _num(match.group(1))

    match = re.search(r"本期电费\s*\n?\s*([\d]+\.\d+)\s*元", text)
    if match:
        result["bill_total"] = _num(match.group(1))
    if "bill_total" not in result:
        match = re.search(r"总电费\s*\n(\d+)\.\s*\n(\d+)", text)
        if match:
            result["bill_total"] = _num(match.group(1) + "." + match.group(2))

    match = re.search(r"其中增值税专用发票金额[：\s]*([\d.]+)元", text)
    if match:
        # 注意：这是**价内税**（发票金额，已含在账单总额里），
        # 不是"另加"的增值税。若映射到 vat_yuan，会让账单总额勾稽凭空多出这一笔。
        result["_vat_invoice"] = _num(match.group(1))

    # 峰谷比例：用于判定 2025 起的"平段单一电价"（尖峰0%、峰0%、平100%、谷0%）
    match = re.search(
        r"尖峰(\d+(?:\.\d+)?)%\s*、\s*峰\s*(\d+(?:\.\d+)?)%\s*、"
        r"平\s*(\d+(?:\.\d+)?)%\s*、\s*谷\s*(\d+(?:\.\d+)?)%",
        text,
    )
    if match:
        result["_pv_pct"] = [float(match.group(i)) for i in range(1, 5)]
    return result


def _parse_header_pos(doc) -> dict[str, Any]:
    """版式 1（表格版）的账单头：按坐标读户号 / 电压 / 账期。"""
    result: dict[str, Any] = {}
    ws = _words(doc[0])
    for _, _, text in ws:
        if re.fullmatch(r"\d{13}", text):
            result["account"] = text
            break
    for _, _, text in ws:
        if re.fullmatch(r"交流\d+kV", text):
            result["voltage"] = text
            break
    dates = sorted(
        (y, text)
        for x, y, text in ws
        if x < 100 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)
    )
    if len(dates) >= 2:
        result["period_start"] = dates[0][1]
        result["period_end"] = dates[-1][1]
    return result


# --------------------------------------------------------------------------- #
# 章节 2：电量明细（按电能表分组）+ 电费明细
# --------------------------------------------------------------------------- #
@dataclass
class _MeterGroup:
    """账单详情页里的一个计量分组（`电能表编号：…，电价：…` 开启的一块）。

    一个受电点下可能有多个分组；**定比分表**（组头含"定比"、带"上级电能表编号"）
    的电量必须与主表相加才是账单总电量（V2.5 §1）。
    """

    meters: list[str] = field(default_factory=list)
    tariff: str = ""
    is_ratio_submeter: bool = False
    parent_meters: list[str] = field(default_factory=list)
    #: 计费电量（按示数类型）：total / sharp / peak / flat / valley / offpeak
    energy: dict[str, float] = field(default_factory=dict)
    #: 该组的电费明细行：``[(费用类别, 费用组成, 分时时段, 计费电量, 计费标准, 电费)]``
    charges: list[dict[str, Any]] = field(default_factory=list)
    #: 该组的中文提示（如"分时之和与组合计不一致"），由解析器写入 ``messages``
    notes: list[str] = field(default_factory=list)

    def energy_of(self, key: str) -> float | None:
        return self.energy.get(key)

    @property
    def total_kwh(self) -> float | None:
        return self.energy.get("total")

    def describe(self) -> str:
        """一行中文摘要（日志用）。"""
        meters = "、".join(self.meters) if self.meters else "（未识别表号）"
        kind = "定比分表" if self.is_ratio_submeter else "电能表"
        total = "未识别" if self.total_kwh is None else f"{self.total_kwh:,.0f} kWh"
        return f"{kind} {meters}：电价 {self.tariff or '未识别'}，计费电量合计 {total}"


def _parse_meter_number_header(line: str) -> tuple[list[str], str, bool, list[str]]:
    """解析分组头 ``电能表编号：A、B，电价：X``（可能跨行拼接），返回四个元组。

    :return: ``(表号列表, 电价文本, 是否定比分表, 上级表号列表)``
    """
    cleaned = (
        line.replace("\u3000", "")
        .replace(" ", "")
        .replace("电能表编号:", "电能表编号：")
        .replace("电价:", "电价：")
        .replace("上级电能表编号:", "上级电能表编号：")
    )
    meters: list[str] = []
    tariff = ""
    parent: list[str] = []
    match = re.search(r"电能表编号[：:]([^，,]*)，?电价[：:]([^，,]*)", cleaned)
    if match:
        numbers = match.group(1)
        meters = re.findall(r"[0-9A-Za-z*\-]+", numbers)
        tariff = match.group(2).strip()
    else:
        match = re.search(r"电能表编号[：:]([^，,]*)", cleaned)
        if match:
            meters = re.findall(r"[0-9A-Za-z*\-]+", match.group(1))
        match = re.search(r"电价[：:]([^，,]*)", cleaned)
        if match:
            tariff = match.group(1).strip()
    match = re.search(r"上级电能表编号[：:](.*)$", cleaned)
    if match:
        parent = re.findall(r"\d{13,}", match.group(1))
    is_ratio = "定比" in cleaned
    if is_ratio and not meters:
        # 定比组的"表号"就是定比标记本身（如 定比0.03），如实保留
        ratio = re.search(r"定比([\d.]+)", cleaned)
        if ratio:
            meters = [f"定比{ratio.group(1)}"]
    return meters, tariff, is_ratio, parent


def _energy_tag_of(line: str) -> str | None:
    """把电量明细的示数类型翻译成内部时段键；不是电量行则返回 ``None``。

    全角括号 ``正向有功（尖峰）`` 与半角 ``正向有功(尖峰)`` 都要认
    （真实账单里主表用全角、定比分表用半角）。
    """
    if "正向有功" not in line:
        return None
    body = line.replace("（", "(").replace("）", ")")
    # 判定顺序很关键：「谷」不得先于「峰」之外的任何键匹配，
    # 而「峰」必须在「谷」之后判断（否则「尖峰」会先撞上「峰」）。
    for keyword, key in (
        ("尖峰", "sharp"),
        ("谷", "valley"),
        ("平", "flat"),
        ("峰", "peak"),
        ("总", "total"),
    ):
        if keyword in body:
            return key
    return None


def _charge_column_x(rows: list[tuple[float, list[tuple[float, str]]]], end: int) -> float | None:
    """在本组之前（含本组内）找最近的「计费电量」列标题 x。

    账单有时把电量明细的**表头只打印一次**（在第一个分组之前），后面的分组没有表头；
    因此不能只在"本组之内"找表头，而要向前回溯到本组的起点。
    """
    for _, cells in reversed(rows[:end]):
        line = _text_of(cells)
        if "计费电量" in line and "示数类型" in line:
            xs = [x for x, text in cells if "计费电量" in text]
            if xs:
                return min(xs)
    return None


def _parse_meter_group_block(
    segment: list[tuple[float, list[tuple[float, str]]]],
    *,
    charge_x: float | None = None,
) -> tuple[dict[str, float], list[dict[str, Any]], list[str]]:
    """解析一个计量分组：返回 ``(计费电量字典, 电费明细行, 中文提示)``。

    :param charge_x: 本组「计费电量」列标题的 x（由调用方按页回溯得到）；
        ``None`` 时退回经验列位 530pt。

    计费电量的取法：

    1. 「计费电量」列的数据右对齐，列锚点由**表头列标题 + 向前回溯**确定；
    2. 每个时段行只在 ``[charge_x-14, charge_x+14]`` 内、且**与标签同一逻辑行**上
       取整数（计费电量一律整数，右邻"计费标准"带小数，整数约束挡掉价格）；
    3. 本行没印（账单把多行合并成一个单元格）时**不借相邻行**——
       那会把另一个时段甚至"正向无功/最大需量"的电量搬过来；
    4. 旧版式把数值印在标签行**上方**约 7~15pt：此时才按"独立数值行"借用一次
       （见 :func:`_borrow_quantity_above`），且借到的值会被标记、不会重复使用。
    """
    energy: dict[str, float] = {}
    group_notes: list[str] = []
    if charge_x is None:
        header_rows = [
            cells
            for _, cells in segment
            if "计费电量" in _text_of(cells) and "示数类型" in _text_of(cells)
        ]
        charge_xs = [x for cells in header_rows for x, text in cells if "计费电量" in text]
        charge_x = min(charge_xs) if charge_xs else None
    column = (charge_x, 14.0) if charge_x is not None else (530.0, 14.0)

    rows = [(y, cells) for y, cells in segment]
    energy_rows = [
        (y, cells) for y, cells in rows if _energy_tag_of(_text_of(cells)) is not None
    ]
    claimed: set[tuple[float, float]] = set()
    for y, cells in energy_rows:
        tag = _energy_tag_of(_text_of(cells))
        if tag is None or tag in energy:
            continue
        found = _quantity_in_rows(rows, y, column)
        if found is None:
            # 旧版式：计费电量印在**标签行上方约 7~15pt**（数值行不重复标签文字）。
            # 仅在"本行没有印"时才借用，且借用一次就标记，避免两个时段抢同一个值。
            found = _borrow_quantity_above(rows, y, column, claimed)
        if found is not None:
            energy[tag] = found

    # 组内自校：分组打印的「总」计费电量是该组的权威合计。分组表里某些时段行可能
    # **根本不打印**计费电量（多行共用一个合并单元格），或因排版变化读偏；
    # 一旦"逐时段之和"与组合计对不上，就保留组合计，并把差异记入分组备注，
    # 由上层写成中文提示——绝不静默丢弃明细，也不臆造缺失的时段值。
    printed_total = energy.get("total")
    if printed_total is not None:
        parts = {key: value for key, value in energy.items() if key != "total"}
        part_sum = sum(parts.values())
        if parts and abs(part_sum - printed_total) > max(1.0, abs(printed_total) * 0.002):
            group_notes.append(
                f"电量明细：该组逐时段计费电量之和 {part_sum:,.0f} kWh 与组内合计 "
                f"{printed_total:,.0f} kWh 不一致（差 {part_sum - printed_total:+,.0f} kWh），"
                "已以组内合计为准，分时电量请核对账单原件"
            )

    charges = _parse_charge_rows(segment)
    return energy, charges, group_notes


def _borrow_quantity_above(
    rows: list[tuple[float, list[tuple[float, str]]]],
    y: float,
    column: tuple[float, float],
    claimed: set[tuple[float, float]],
) -> float | None:
    """旧版式：把"印在标签行上方一点"的计费电量借给本时段行。

    只认**独立的数值行**（该行含数值但**不含任何时段标签**），且距标签行 4~16pt；
    借到的值会登记进 ``claimed``，因此不会有两个时段共用同一个电量。
    这样既补上旧版式漏读的电量，又不会在"多行共用合并单元格"的新版式里
    （那时本行上方没有任何数值行）误借。
    """
    charge_x, tolerance = column
    candidates: list[tuple[float, float, tuple[float, float]]] = []
    for row_y, row_cells in rows:
        gap = y - row_y
        if not 4.0 <= gap <= 16.0:
            continue
        if _energy_tag_of(_text_of(row_cells)) is not None:
            continue
        for x, text in row_cells:
            if abs(x - charge_x) > tolerance:
                continue
            if not re.fullmatch(r"-?\d+(\.0+)?", text):
                continue
            number = _num(text)
            if number is None:
                continue
            key = (row_y, x)
            if key in claimed:
                continue
            candidates.append((gap, -x, number, key))  # type: ignore[arg-type]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    best = candidates[0]
    claimed.add(best[3])  # type: ignore[arg-type]
    return best[2]  # type: ignore[return-value]


def _quantity_in_rows(
    rows: list[tuple[float, list[tuple[float, str]]]],
    y: float,
    column: tuple[float, float],
) -> float | None:
    """取**本行**「计费电量」列的整数（列标题 x + 经验容差）。

    只认"与标签同一逻辑行"的数值（``|Δy| ≤ 2pt``），**不跨行借用**。这一点很关键：
    真实账单里有些时段行**根本不打印**计费电量（多行共用一个合并单元格），
    若跨行取数就会把"另一个时段的电量"（甚至"正向无功/最大需量"的电量）
    搬过来，导致分时合计凭空多出几十个百分点。

    计费电量一律是整数，右邻的"计费标准"列带小数，因此整数约束本身就能挡掉价格；
    列容差 ±14pt 足以覆盖同一列左右 ≤6pt 的抖动。
    """
    charge_x, tolerance = column
    same_row: list[tuple[float, float]] = []
    for row_y, row_cells in rows:
        if abs(row_y - y) > 30.0:
            continue
        for x, text in row_cells:
            if not re.fullmatch(r"-?\d+(\.0+)?", text):
                continue
            number = _num(text)
            if number is None:
                continue
            if abs(x - charge_x) <= tolerance:
                same_row.append((abs(row_y - y), number))
    same_row = [item for item in same_row if item[0] <= 2.0]
    if not same_row:
        # 本行没有印计费电量（多行共用合并单元格）时**不借相邻行**：那会把另一个时段
        # 甚至"正向无功/最大需量"的电量搬过来，让分时合计凭空多出几十个百分点。
        return None
    same_row.sort(key=lambda item: item[0])
    return same_row[0][1]


def _text_in_band(
    rows: list[tuple[float, list[tuple[float, str]]]],
    y: float,
    x_min: float,
    x_max: float,
    *,
    first_only: bool = True,
) -> str:
    """取 ``y±18pt`` 带内、``[x_min, x_max)`` 列区间的文本（默认只取最上面的一个）。

    用于读表格里的**标签**单元格：账单中标签与数值的基线可能相差 ~7pt，
    只按当前行取会漏掉标签。
    """
    collected: list[tuple[float, float, str]] = []
    for row_y, cells in rows:
        if abs(row_y - y) > 18.0:
            continue
        for x, text in cells:
            if x_min <= x < x_max and text.strip():
                collected.append((row_y, x, text.strip()))
    if not collected:
        return ""
    collected.sort(key=lambda item: (item[0], item[1]))
    if first_only:
        return collected[0][2]
    return "".join(item[2] for item in collected)


def _field_average(
    rows: list[tuple[float, list[tuple[float, str]]]],
    y: float,
    x_min: float,
    x_max: float,
    *,
    digit_only: bool = False,
) -> float | None:
    """取 ``y±18pt`` 带内某列的平均值（无值时返回 ``None``）。

    为什么是"平均"：账单表头是**合并单元格**，列标题（如 ``电费``）打印在该列
    居中处，因此标题的 x 与数据列的左边界不同；同一列的多个标题取平均后，
    落点与数据列最接近。``digit_only`` 用于剔除 ``(1)`` ``(2)`` 这类序号token。
    """
    collected: list[tuple[float, float]] = []
    for row_y, cells in rows:
        if abs(row_y - y) > 18.0:
            continue
        for x, text in cells:
            if not (x_min <= x < x_max):
                continue
            if digit_only and _num(text) is not None:
                continue
            if text.strip():
                collected.append((row_y, x))
    if not collected:
        return None
    return sum(x for _, x in collected) / len(collected)


def _column_bounds(
    rows: list[tuple[float, list[tuple[float, str]]]], header_label: str
) -> tuple[float, float] | None:
    """按"列标题在某列居中"这一点，推出该数据列的 ``[左边界, 右边界)``。

    做法：把页面上**所有数值 token 的 x** 排序，标题 x 落在哪两个相邻 x 之间，
    就取这两个相邻 x 作为列边界。对 ``计费电量`` 这类"标题居中于列"的表格很稳，
    且不写死任何页面的绝对坐标。
    """
    xs = sorted({x for _, cells in rows for x, text in cells if _num(text) is not None})
    if len(xs) < 2:
        return None
    label_x: float | None = None
    for row_y, cells in rows:
        for x, text in cells:
            if text.strip() == header_label:
                label_x = x
                break
        if label_x is not None:
            break
    if label_x is None:
        return None
    left = max((x for x in xs if x <= label_x + 4), default=xs[0])
    right = min((x for x in xs if x > left + 4), default=xs[-1] + 30)
    return (left, right)


def _parse_charge_rows(
    segment: list[tuple[float, list[tuple[float, str]]]],
) -> list[dict[str, Any]]:
    """解析一段电费明细，返回逐行 ``{类别, 组成, 时段, 计费电量, 计费标准, 电费}``。

    表格列位（真实账单 pdf 坐标，单位 pt）：费用类别 x≈35、费用组成 x≈139、
    分时时段 x≈347、计费电量 x≈381、计费标准 x≈468、电费 x≈519~560。

    取数方式：以每个**标签行**的 y 为锚，在其下方 ±18pt 的带内按列取数
    （账单里标签与数值的基线差约 7~17pt，右侧的"电费"列常常比标签低一行）：

    * 计费电量：``[计费电量列-12, 计费标准列)`` 内**最靠上**的数值；
    * 计费标准：``[计费标准列-12, 电费列)`` 内**最靠上**的数值（``-`` 表示无标准）；
    * 电费：``[电费列-10, +∞)`` 内**最靠上**的数值。
    """
    rows = list(segment)
    bounds_quantity = _column_bounds(rows, "计费电量")
    bounds_rate = _column_bounds(rows, "计费标准")
    bounds_fee = _column_bounds(rows, "电费")
    charges: list[dict[str, Any]] = []
    section_name = ""
    for y, cells in rows:
        line = _text_of(cells)
        category_match = re.match(r"^\((\d+)\)", line)
        if category_match and _field_average(rows, y, 0.0, 130.0, digit_only=True) is None:
            detail = _text_in_band(rows, y, 30.0, 130.0)
            section_name = f"({category_match.group(1)}){detail}".strip()
        if "费用类别" in line and "费用组成" in line:
            continue  # 表头行
        if "小计" in line:
            continue
        if not section_name:
            continue

        numbers = [
            (row_y, x, _num(text))
            for row_y, row_cells in rows
            if abs(row_y - y) <= 18.0
            for x, text in row_cells
            if _num(text) is not None
        ]
        x_quantity = bounds_quantity[0] - 12 if bounds_quantity else 360.0
        x_rate = bounds_rate[0] - 12 if bounds_rate else 458.0
        x_fee = bounds_fee[0] - 10 if bounds_fee else 512.0
        quantity = _pick(numbers, x_quantity, x_rate)
        rate = _pick(numbers, x_rate, x_fee)
        fee = _pick(numbers, x_fee, 10_000.0)
        if quantity is None and rate is None and fee is None:
            continue

        period = _text_in_band(rows, y, 330.0, 372.0)
        if period not in ("总", "尖峰", "高峰", "平段", "低谷", "深谷"):
            period = ""
        composed = _text_in_band(rows, y, 130.0, 330.0)

        charges.append(
            {
                "category": section_name,
                "component": composed,
                "period": period,
                "quantity_kwh": quantity,
                "rate_yuan_per_kwh": rate,
                "fee_yuan": fee,
                "_line": line,
            }
        )
    return charges


def _pick(
    numbers: list[tuple[float, float, float | None]], x_min: float, x_max: float
) -> float | None:
    """在列区间内取 y 最小、其次 x 最大的数值（兼容"数值比标签低一行"的排版）。"""
    candidates = [
        (row_y, x, value)
        for row_y, x, value in numbers
        if value is not None and x_min <= x < x_max
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]))
    return candidates[0][2]


def _parse_energy_rows(doc) -> dict[str, float]:
    """从"正向有功（尖峰/峰/平/谷/总）"抄表行读计费电量（**不依赖计量分组**）。

    真实账单（第 2/3 页）里计费电量固定在最后一列（x≈530）；旧版式与测试里的
    合成 PDF 列位不同，因此取"该行 / 相邻行内 x 最大的整数"（计费电量一律是整数，
    而右侧的价格列带小数，用整数约束就不会把价格误读成电量）。

    这是分组解析的**兼容回退**：分组解析已经取到时以分组结果为准（分组结果还能
    区分主表与定比分表），只有在分组解析缺某个时段时才用本函数补齐。
    """
    result: dict[str, float] = {}
    for page in doc:
        rows = _pair_rows(_page_rows(page))
        for y, cells in rows:
            tag = _energy_tag_of(_text_of(cells))
            if tag is None or tag in result:
                continue
            numbers: list[tuple[float, float, float]] = []
            for row_y, row_cells in rows:
                if abs(row_y - y) > 30.0:
                    continue
                for x, text in row_cells:
                    if x <= 300 or not re.fullmatch(r"-?\d+(\.0+)?", text):
                        continue
                    value = _num(text)
                    if value is not None:
                        numbers.append((x, abs(row_y - y), value))
            if not numbers:
                continue
            # 先取最右列（计费电量列），同列内取**行距最近**的那一行：
            # 列位相同时若按"先来后到"取，会把上一条目的数值读成本条目的。
            numbers.sort(key=lambda item: (-item[0], item[1]))
            result[tag] = numbers[0][2]
    return result


def _parse_meter_groups(doc) -> list[_MeterGroup]:
    """把第 2~4 页的"电能表分组 → 电量明细 → 电费明细"整块读出来。

    分组头的识别：``电能表编号：…，电价：…``（定比组还带 ``上级电能表编号:…``）。
    某些排版会把分组头拆成两行，因此这里**跨页**把"以 ``电能表编号：`` 开头、
    紧跟的表号续行"拼起来；`电价` 可能在下一行，所以再往后看一行。
    """
    groups: list[_MeterGroup] = []
    header_pattern = re.compile(r"电能表编号[：:]")
    for page_index, rows in _iter_paired_rows(doc):
        starts = [
            index
            for index, (_, cells) in enumerate(rows)
            if header_pattern.search(_text_of(cells))
        ]
        if not starts:
            continue
        for order, start in enumerate(starts):
            end = starts[order + 1] if order + 1 < len(starts) else len(rows)
            # 分组头可能跨行：把后续"纯表号续行"一并拼进来（最多再看 2 行）
            header = _text_of(rows[start][1])
            if not re.search(r"电价[：:]", header):
                for offset in (1, 2):
                    if start + offset >= len(rows):
                        break
                    extra = _text_of(rows[start + offset][1])
                    if not re.fullmatch(r"[0-9A-Za-z、，,\s]+", extra):
                        break
                    header += extra
            meters, tariff, is_ratio, parent = _parse_meter_number_header(header)
            segment = rows[start + 1 : end]
            energy, charges, group_notes = _parse_meter_group_block(
                segment, charge_x=_charge_column_x(rows, start + 1)
            )
            groups.append(
                _MeterGroup(
                    meters=meters,
                    tariff=tariff,
                    is_ratio_submeter=is_ratio,
                    parent_meters=parent,
                    energy=energy,
                    charges=charges,
                    notes=group_notes,
                )
            )
            logger.debug("PDF 账单第 %d 页计量分组：%s", page_index, groups[-1].describe())
    return groups


def _parse_charges(doc, groups: list[_MeterGroup]) -> dict[str, Any]:
    """分项费用：优先用**电费明细**按分组求和，退回"账单概况块"文本正则。

    为什么要按明细求和：第 1 页概况里的分项是**全户合计**，而电费明细是按计量点
    逐条给出的；两条口径必须能对上（对不上就说明解析漏了某一组，必须提示）。
    """
    text = "\n".join(page.get_text() for page in doc)
    result: dict[str, Any] = {}

    def grab(pattern: str) -> float | None:
        match = re.search(pattern, text)
        return _num(match.group(1)) if match else None

    result["market"] = grab(r"市场化购电电费元\s+([\d.]+)") or grab(
        r"\(1\)市场化购电电费\s+([\d.]+)"
    )
    result["trans"] = grab(r"输配电费元\s+([\d.]+)") or grab(r"\(3\)输配电费\s+([\d.]+)")
    result["loss"] = grab(
        r"上网环节线损折价\s*\n?元/kWh\s+电费元\s+([\d.]+)"
    ) or grab(r"\(2\)上网环节线损费用\s+([\d.]+)")
    result["gov"] = grab(r"政府性基金及附加元\s+([\d.]+)") or grab(
        r"\(5\)政府性基金及附加\s+([\d.]+)"
    )
    result["sysop"] = grab(r"\(4\)系统运行费\s+([\d.]+)")
    if result["sysop"] is None:
        result["sysop"] = grab(r"电价标准\s*\n?元/kWh\s+电费元\s+([\d.]+)")

    # 基本电费（按容量 / 按需量）。**优先用账单第 4 页的需量块**：
    # 「需量电价 × 计费需量 = 需量电费」，比概况页的相邻数字可靠得多
    # （概况页「输配需量电费」一行的两个数字是"计费需量"与"需量电费"，
    #   单凭文本正则会把计费需量误当成电费）。
    match = re.search(r"基本电费元\s+(\d+)\s*\n(?:基本电费|需量基本费)\s+(\d+)\s+([\d.]+)", text)
    if match:
        result["basic_demand"] = _num(match.group(1))
        result["demand"] = _num(match.group(2))
    else:
        match = re.search(r"输配需量电费\s+(\d+)\s+(\d+)", text)
        if match:
            result["demand"] = _num(match.group(1))
            result["basic_demand"] = _num(match.group(2))

    match = re.search(r"功率因数调整电费元\s+(-?[\d.]+)", text)
    if match is None:
        match = re.search(r"功率因数调整电费\s+(-?[\d.]+)", text)
    if match:
        result["pf"] = _num(match.group(1))

    # 输配电费中的"电量电费"部分：用于把需量电费从输配里剥离，归入基本电费
    match = re.search(r"电量电费\s+(\d+(?:\.\d+)?)", text)
    if match:
        result["_trans_energy"] = _num(match.group(1))

    # ---- V2.5：明细求和（比文本正则更可靠，且能发现"漏了某个计量点"）----
    detail_totals = _charge_totals_of_groups(groups)
    if detail_totals:
        result["_detail_totals"] = detail_totals
        result["_group_energy_sum"] = sum(group.total_kwh or 0.0 for group in groups)
        # 输配电量电费取自明细（概况页的"输配电费"把需量电费也算进去了）
        if detail_totals.get("trans") is not None:
            result["_trans_energy"] = detail_totals["trans"]
            block_demand = result.get("_demand_charge_from_block")
            if block_demand is not None:
                result["basic_demand"] = block_demand
            elif detail_totals.get("demand_charge") is not None:
                result["basic_demand"] = detail_totals["demand_charge"]
    return result


#: 电费明细「费用类别」→ 账单分项字段
_CHARGE_CATEGORY_MAP: tuple[tuple[str, str], ...] = (
    ("市场化购电电费", "market"),
    ("上网环节线损费用", "loss"),
    ("输配电量电费", "trans"),
    ("输配电费", "trans"),
    ("输配需量电费", "demand_charge"),
    ("系统运行费用", "sysop"),
    ("政府性基金及附加", "gov"),
    ("目录电费", "catalog"),
)


def _charge_totals_of_groups(groups: list[_MeterGroup]) -> dict[str, float]:
    """按电费明细的「费用类别」汇总各计量分组的电费，返回 ``{内部键: 金额合计}``。

    只累加**计费电费**（第 6 列），不碰计费电量与计费标准；
    「目录电费」「辅助服务费用」等非账单分项也会如实保留（供核对，不进账单字段）。
    """
    totals: dict[str, float] = {}
    for group in groups:
        for row in group.charges:
            category = str(row.get("category") or "")
            if not category:
                continue
            fee = row.get("fee_yuan")
            if not isinstance(fee, (int, float)):
                continue
            for keyword, key in _CHARGE_CATEGORY_MAP:
                if keyword in category:
                    totals[key] = round(totals.get(key, 0.0) + float(fee), 2)
                    break
    return totals


# --------------------------------------------------------------------------- #
# 章节 4：输配容（需）量电费 / 功率因数调整电费
# --------------------------------------------------------------------------- #
#: 「输配容（需）量电费 / 功率因数调整电费」子表的"列带"（真实账单实测）。
#:
#: 子表把标签与数值排成两条错位的网格：数值在 x ≈ 104 / 180 / 261 / 331 / 388 /
#: 452 / 532，标签在 x ≈ 144 / 216 / 315 / 414 / 496（标签通常比数值低 0~15pt）。
#: 下表的"列带"以 5pt 为粒度，覆盖每个字段的标签与数值，但**不覆盖任何别的字段**——
#: 这一条约束正好把"需量电费（按实际）1108107"（距 需量值 标签 68pt）挡在外面，
#: 避免读到回退值。所有区间都是实测坐标，未做任何写死的"金额猜测"。
_DEMAND_FIELD_ZONES: dict[str, tuple[float, float]] = {
    "demand": (140.0, 200.0),
    "demand_rate": (210.0, 305.0),
    "pf_actual": (310.0, 383.0),
    "pf_standard": (410.0, 470.0),
    "pf_factor": (492.0, 536.0),
    "pf_participating": (310.0, 400.0),
    # 左列（标签 x≈35、数值 x≈104）：月每千伏安用电量 / 需量电费（按实际）
    "energy_per_kva": (30.0, 200.0),
    "demand_charge": (30.0, 200.0),
}

#: 子表里"左列"字段的标签（标签在 x≈35，数值在 x≈104，两者行距可达 ~30pt）
_LEFT_COLUMN_LABELS: frozenset[str] = frozenset(
    {"月每千伏安用电量", "需量电费（按实际）"}
)

#: 子表字段 → 账单上的中文标签
_DEMAND_LABELS: dict[str, str] = {
    "demand": "需量值",
    "demand_rate": "需量电价",
    "pf_actual": "功率因数实际值",
    "pf_standard": "功率因数标准",
    "pf_factor": "调整系数",
    "energy_per_kva": "月每千伏安用电量",
    "pf_participating": "参与调整电费金额",
    "demand_charge": "需量电费（按实际）",
}


def _block_field_value(
    block: list[tuple[float, list[tuple[float, str]]]],
    label: str,
    x_min: float,
    x_max: float,
) -> float | None:
    """取"标签所在列带"内距标签行最近的值（标签与数值可能不在同一行）。

    只在该字段自己的列带里配对，避免把相邻列（如"折扣需量电费 0"）的值错配进来；
    带内没有数值、或最近值距离超过两个列宽时返回 ``None``（= 账单未提供，不是 0）。
    """
    labels: list[tuple[float, float]] = []
    values: list[tuple[float, float, float]] = []
    for row_y, cells in block:
        for x, text in cells:
            if not (x_min <= x < x_max):
                continue
            stripped = text.strip()
            if stripped == label or stripped.startswith(label):
                labels.append((row_y, x))
                continue
            number = _num(stripped)
            if number is not None:
                values.append((row_y, x, number))
    if not labels or not values:
        return None
    best: tuple[float, float] | None = None
    for label_y, label_x in labels:
        for row_y, x, number in values:
            distance = abs(row_y - label_y) + 0.5 * abs(x - label_x)
            if best is None or distance < best[0]:
                best = (distance, number)
    if best is None or best[0] > 100.0:
        return None
    return best[1]


def _parse_demand_and_pf(doc) -> dict[str, Any]:
    """解析"输配容（需）量电费 / 功率因数调整电费"两张并列子表。

    这两张子表**每个计量点一组**（真实账单第 4 页共 3 组），列位固定为
    ``需量值 x≈104~181、需量电价 x≈262、功率因数实际值 x≈378、功率因数标准 x≈467、
    调整系数 x≈532、月每千伏安用电量 x≈104、合同容量 x≈190~230、
    参与调整电费金额 x≈388``；标签与数值在**同一列**上配对（数值常印得比标签高）。

    取数口径：

    * ``demand`` / ``demand_rate`` / ``basic_demand`` 取**需量块**：
      「需量电价 × 计费需量 = 需量电费」能对上的那一组优先，
      否则取需量值最大的那一组（按需量计费时实际最大需量才是计费依据）；
    * ``pf_actual`` / ``pf_factor`` / ``pf_participating`` / ``energy_per_kva``
      取各组的**非零最大值**：真实账单里第一组是"力调考核组"（实际值 0.98、
      调整系数 -0.0075、参与调整金额 5,031,440.65），第二组只有需量电价与标准，
      因此"逐字段取最完整的一组"比"整组二选一"更贴近账单事实；
    * ``pf_standard`` 取最小的非空值（标准是门槛值，多个标准时保守取最小）。
    """
    blocks: list[list[tuple[float, list[tuple[float, str]]]]] = []
    for page in doc:
        rows = _pair_rows(_page_rows(page))
        current: list[tuple[float, list[tuple[float, str]]]] | None = None
        for y, cells in rows:
            line = _text_of(cells)
            if (
                line.startswith("输配容")
                and "功率因数调整电费" in line
                and "需量电费" not in line
                and "容量电费" not in line
            ):
                # 只认**并列子表的表头行**：「输配容（需）量电费 ｜ 功率因数调整电费」。
                # 数据行里也会出现"输配容量电费""功率因数调整电费"这些标签，
                # 不能被当成新块的表头（否则每块会被切碎，字段全部读不到）。
                current = []
                blocks.append(current)
                continue
            if current is None:
                continue
            if "24小时" in line or "市场化运营费" in line:
                current = None
                continue
            current.append((y, cells))

    parsed: list[dict[str, Any]] = []
    for block in blocks:
        values: dict[str, Any] = {}
        for key, (x_min, x_max) in _DEMAND_FIELD_ZONES.items():
            found = _block_field_value(block, _DEMAND_LABELS[key], x_min, x_max)
            if found is not None:
                values[key] = found
        if values:
            parsed.append(values)
    blocks = parsed
    if not blocks:
        return {}

    def demand_score(block: dict[str, Any]) -> tuple[int, float]:
        demand = block.get("demand")
        rate = block.get("demand_rate")
        charge = block.get("demand_charge")
        if demand is not None and rate is not None and charge is not None:
            if abs(demand * rate - charge) <= max(1.0, abs(charge) * 0.001):
                return (2, float(demand))
        if demand is not None:
            return (1, float(demand))
        return (0, 0.0)

    best = max(blocks, key=demand_score)

    def max_of(key: str) -> float | None:
        values = [block[key] for block in blocks if block.get(key) is not None]
        return max(values) if values else None

    def min_of(key: str) -> float | None:
        values = [block[key] for block in blocks if block.get(key) is not None]
        return min(values) if values else None

    def nonzero_max_of(key: str) -> float | None:
        """取非零最大值：``0`` 在真实账单里表示"该组未考核"，不是有效取值。"""
        values = [
            block[key] for block in blocks if block.get(key) not in (None, 0.0, 0)
        ]
        return max(values) if values else max_of(key)

    result: dict[str, Any] = {}
    for key, value in (
        ("demand_rate", best.get("demand_rate")),
        ("demand", best.get("demand")),
        ("pf_actual", nonzero_max_of("pf_actual")),
        ("pf_standard", min_of("pf_standard")),
        ("pf_factor", nonzero_max_of("pf_factor")),
        ("pf_participating", nonzero_max_of("pf_participating")),
        ("energy_per_kva", max_of("energy_per_kva")),
        ("capacity", max_of("capacity")),
    ):
        if value is not None:
            result[key] = value
    demand = best.get("demand")
    rate = best.get("demand_rate")
    charge = best.get("demand_charge")
    if charge is not None:
        result["_demand_charge_from_block"] = charge
    elif demand is not None and rate is not None:
        # 账单子表未直接给出需量电费时，用"需量电价 × 计费需量"这一账单自身的乘式求值
        result["_demand_charge_from_block"] = round(demand * rate, 2)
    return result


# --------------------------------------------------------------------------- #
# 章节 4：24 小时电量电价表
# --------------------------------------------------------------------------- #
def _parse_hourly_energy_tariff(doc) -> list[HourlyEnergyPricePoint]:
    """解析「24 小时电量电价」表（小时 / 电量 / 直接交易价格 / 上网环节线损价格）。

    数据形态：**小时列与其余三列可能不在同一 y 带**（真实账单里小时印在下边框、
    数值印在上边框，基线差约 3pt；部分账单差到 25pt），因此以"小时 token"所在行为锚，
    在 ``±_HOUR_BAND`` 带内**按距离就近**取三列数值：

    * 列锚点优先取表头（``小时 / 电量 / 直接交易价格 / 上网环节线损价格``）的 x；
    * 取不到表头时退回真实账单的固定列位（133 / 233 / 372 / 523 pt）。

    取数只用 `5xx` 价格列与 `2xx` 电量列的**最近行**，因此不会把相邻小时的数值错配
    过来（真实账单里小时编号与数值的最大基线差约 25pt，小于行距 14pt 的 2 倍）。
    价格小数位原样保留（不做四舍五入）。
    """
    for page in doc:
        rows = _pair_rows(_page_rows(page))
        anchors: dict[str, float] = {}
        points: dict[int, HourlyEnergyPricePoint] = {}
        for y, cells in rows:
            line = _text_of(cells)
            if "直接交易价格" in line and "电量" in line:
                found: dict[str, float] = {}
                for x, text in cells:
                    stripped = text.strip()
                    if stripped == "小时":
                        found["hour"] = x
                    elif stripped == "电量":
                        found.setdefault("energy", x)
                    elif "直接交易价格" in stripped:
                        found["trade"] = x
                    elif "线损价格" in stripped or "上网环节" in stripped:
                        found["loss"] = x
                if len(found) >= 3:
                    anchors = found
                continue
            if not anchors:
                continue
            hour = _hour_of_row(cells, anchors)
            if hour is None or hour in points:
                continue
            values = _hour_values_near(rows, y, anchors)
            if values is None:
                continue
            points[hour] = HourlyEnergyPricePoint(hour=hour, **values)
        if len(points) >= 2:
            return [points[hour] for hour in sorted(points)]
    return []


def _hour_of_row(cells: list[tuple[float, str]], anchors: dict[str, float]) -> int | None:
    """取本行"小时"列的整数（1~24）；不是数据行时返回 ``None``。"""
    hour_anchor = anchors.get("hour")
    for x, text in cells:
        if hour_anchor is not None and abs(x - hour_anchor) > 30.0:
            continue
        value = _num(text)
        if value is None or value != int(value) or not 1 <= value <= 24:
            continue
        return int(value)
    return None


def _hour_values_near(
    rows: list[tuple[float, list[tuple[float, str]]]],
    y: float,
    anchors: dict[str, float],
) -> dict[str, float | None] | None:
    """取 ``y`` 附近三列（电量 / 直接交易价格 / 上网环节线损价格）的数值。

    对每一列，在 ``±_HOUR_BAND`` 内选**行距最近**的候选（同距取更靠近列锚点的 x），
    因此"小时与数值不同行"也能取到，而不会跨到相邻小时。
    """
    targets = (
        ("energy_kwh", anchors.get("energy", 233.0)),
        ("direct_trade_price_yuan_per_kwh", anchors.get("trade", 372.0)),
        ("line_loss_price_yuan_per_kwh", anchors.get("loss", 523.0)),
    )
    values: dict[str, float | None] = {}
    for key, target_x in targets:
        best: tuple[float, float, float] | None = None
        for row_y, row_cells in rows:
            gap = abs(row_y - y)
            if gap > _HOUR_BAND:
                continue
            for x, text in row_cells:
                if abs(x - target_x) > 40.0:
                    continue
                number = _num(text)
                if number is None:
                    continue
                candidate = (gap, abs(x - target_x), number)
                if best is None or candidate < best:
                    best = candidate
        values[key] = best[2] if best else None
    if all(value is None for value in values.values()):
        return None
    return values


#: 24 小时电价表里"小时 token"与其余三列数值的最大允许行距（真实账单实测 ≤25pt）
_HOUR_BAND = 30.0


# --------------------------------------------------------------------------- #
# 章节 5：市场化运营费明细
# --------------------------------------------------------------------------- #
def _parse_operation_fees(doc) -> OperationFeeDetail | None:
    """解析第 5 页「市场化运营费明细」：``运营费用名称｜金额`` 两列。

    条目归类：``一、B…`` 之后的 ``B1…B6`` 归 B 类，``二、C…`` 之后的 ``C1…C6`` 归 C 类，
    三 / 四 两条是独立字段。金额**保持账单符号**（C 类为负，不取绝对值）。
    """
    for page in doc:
        text = page.get_text()
        if "运营费用名称" not in text or "总合计" not in text:
            continue
        rows = _pair_rows(_page_rows(page))
        detail = OperationFeeDetail()
        section = ""
        for _, cells in rows:
            line = _text_of(cells)
            if "运营费用名称" in line and _num(cells[-1][1]) is None:
                continue
            numbers = [(x, _num(text)) for x, text in cells if _num(text) is not None]
            if not numbers:
                continue
            amount = numbers[-1][1]
            if amount is None:
                continue
            name = ""
            for x, text in cells:
                stripped = text.strip()
                if not stripped or "运营费用名称" in stripped or _num(stripped) is not None:
                    continue
                # 金额列（x≈518~560）里的文本不可能是条目名
                if x >= 460.0:
                    continue
                name = stripped
                break
            if "总合计" in line:
                detail.total_yuan = amount
                continue
            if re.match(r"^一、B", name):
                section = "B"
                detail.b_increase_total_yuan = amount
                continue
            if re.match(r"^二、C", name):
                section = "C"
                detail.c_decrease_total_yuan = amount
                continue
            if "虚拟电厂辅助服务调峰电费" in name:
                detail.virtual_plant_peak_shaving_yuan = amount
                continue
            if "调频辅助服务市场结算费用" in name:
                detail.frequency_regulation_yuan = amount
                continue
            item_name = re.sub(r"^其中[：:]", "", name).strip()
            if not item_name:
                continue
            if section == "B" and re.match(r"^B\d", item_name):
                detail.b_increase_items.append(OperationFeeItem(name=item_name, amount_yuan=amount))
            elif section == "C" and re.match(r"^C\d", item_name):
                detail.c_decrease_items.append(OperationFeeItem(name=item_name, amount_yuan=amount))
        if (
            detail.total_yuan is None
            and not detail.b_increase_items
            and not detail.c_decrease_items
        ):
            continue
        return detail
    return None


# --------------------------------------------------------------------------- #
# 版式 1 兼容：尖峰时段四标签块
# --------------------------------------------------------------------------- #
def _parse_peak_valley_blocks(doc) -> dict[str, float]:
    """版式 1：从"尖峰时段(kW·h)"四标签块按列坐标取电量。

    同一 y 带出现 4 个时段标签，取标签下方 0~60px 内、水平最靠近该列的
    4~8 位整数作为该时段电量。
    """
    for page in doc:
        ws = _words(page)
        kind: list[tuple[float, float, str]] = []
        for x, y, text in ws:
            if "尖峰" in text and "时段" in text:
                kind.append((x, y, "sharp"))
            elif "高峰" in text and "时段" in text:
                kind.append((x, y, "peak"))
            elif "平时段" in text:
                kind.append((x, y, "flat"))
            elif "低谷" in text and "时段" in text:
                kind.append((x, y, "valley"))
        if len({k[2] for k in kind}) < 4:
            continue
        label_y = min(k[1] for k in kind)
        candidates = [
            (x, y, text)
            for x, y, text in ws
            if label_y - 3 < y < label_y + 60 and re.fullmatch(r"\d{4,8}", text)
        ]
        values: dict[str, tuple[float, float | None]] = {}
        for x, y, text in candidates:
            nearest = min(kind, key=lambda k: abs(k[0] - x))
            if nearest[2] not in values or y < values[nearest[2]][0]:
                values[nearest[2]] = (y, _num(text))
        if len(values) == 4:
            return {key: float(val) for key, (_, val) in values.items() if val is not None}
    return {}


# --------------------------------------------------------------------------- #
# 归一化：把"原始解析结果"整理成模板口径
# --------------------------------------------------------------------------- #
def _normalize(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """把原始解析结果整理成模板口径。

    :return: ``(归一化结果, 中文提示列表, 口径说明列表)``。

    归一化是**必要的**，因为电网账单的"基本电费"在后期版式里被打包进"输配电费"中：
    若直接录用会让"输配电费"虚高、"基本电费-按需量"缺失，破坏
    ``账单总额 = 电度电费合计 + 基本电费 + 力调`` 的勾稽口径。
    """
    notes: list[str] = []
    assumptions: list[str] = []
    out = dict(raw)

    # ① 峰谷比例 vs 电量明细（判断优先级由需求方口径决定，V2.5 §5）：
    #    ① 明细里有分时电量 → 一律用明细（这是账单事实）；
    #    ② 明细里没有分时电量 → 允许"全部为平段、其余为零"，并给**中性说明**；
    #    ③ 两种口径冲突 → 以明细为准并披露差异（不静默取一个）。
    pct = raw.get("_pv_pct")
    detail_energy = {
        key: raw.get(key) for key in _PERIOD_KEYS if raw.get(key) is not None
    }
    detail_sum = sum(value for value in detail_energy.values() if value is not None)
    non_flat = sum(
        detail_energy.get(key) or 0.0 for key in _PERIOD_KEYS if key != "flat"
    )
    has_detail = bool(detail_energy) and (detail_sum > 0 or non_flat > 0)
    if pct == [0.0, 0.0, 100.0, 0.0] and not has_detail:
        # 明细里确实没有分时电量：这是**合法的"平段用电"账单**，不是缺陷。
        # 尖峰 / 峰 / 谷为 0、平段 = 总电量，就是账单的真实情况（需求方口径）。
        out["sharp"] = 0.0
        out["peak"] = 0.0
        out["valley"] = 0.0
        if raw.get("energy_total") is not None:
            out["flat"] = raw["energy_total"]
        flat = out.get("flat")
        flat_text = "未提供" if flat is None else f"{float(flat):,.0f}"
        notes.append(
            "账单各时段电量：尖峰 0 / 峰 0 / 平 "
            f"{flat_text} / 谷 0 kWh（全部为平段用电，账单按平段单一电价计费）"
        )
        assumptions.append(
            "账单概况页峰谷比例写作「尖峰0%/峰0%/平100%/谷0%」，且第 2/3 页电量明细未给出"
            "分时电量：按账单事实把全部电量计入平段、其余时段为 0——这是**正常的平段用电**"
            "口径，不是数据缺陷"
        )
    elif pct == [0.0, 0.0, 100.0, 0.0] and has_detail:
        notes.append(
            "账单概况页的峰谷比例写作「尖峰0%/峰0%/平100%/谷0%」，"
            "但第 2/3 页电量明细给出了真实的分时电量"
            f"（尖峰 {raw.get('sharp', 0) or 0:,.0f}、峰 {raw.get('peak', 0) or 0:,.0f}、"
            f"平 {raw.get('flat', 0) or 0:,.0f}、谷 {raw.get('valley', 0) or 0:,.0f} kWh）："
            "按「明细优先」口径采用**电量明细**的取数，概况页比例仅作参考"
        )
        assumptions.append(
            "分时电量取「电量明细」逐电能表计费电量之和（含定比分表），"
            "未采用账单概况页的峰谷比例汇总口径（V2.5 §5：冲突时以明细为准）"
        )
    elif pct == [0.0, 0.0, 0.0, 0.0] and not has_detail:
        # 概况页自己也写"全 0%"（既不是平段 100%、也没有明细）：不做任何时段归集，
        # 只留说明，避免把"没读到的电量"悄悄塞进某个时段。
        notes.append(
            "账单概况页峰谷比例写作全 0%，且电量明细未给出分时电量："
            "各时段电量均按未提供处理（不臆造平段电量）"
        )
    elif pct is not None:
        assumptions.append(
            "账单概况页给出峰谷比例 尖峰{:.2f}%/峰{:.2f}%/平{:.2f}%/谷{:.2f}%，"
            "分时电量优先取电量明细；明细缺失的时段才按该比例理解".format(*pct)
        )

    # ② 基本电费口径归一：从"输配电费"里剥离需量电费
    trans = out.get("trans")
    basic_demand = out.get("basic_demand")
    trans_energy = out.get("_trans_energy")
    if (
        trans is not None
        and basic_demand
        and trans_energy is not None
        and abs((trans_energy + basic_demand) - trans) < 2.0
    ):
        out["trans"] = trans_energy
        notes.append(
            f"账单的输配电费含需量电费：已拆分为输配电费 {trans_energy:.2f} 元"
            f" + 基本电费-按需量 {basic_demand:.2f} 元"
        )

    # ③ 电度电费合计（= 5 项明细之和）；账单未给明细时留空，不臆造
    parts = [out.get(k) for k in ("market", "trans", "loss", "sysop", "gov")]
    if any(p is not None for p in parts):
        out["energy_charge"] = round(sum(p or 0.0 for p in parts), 2)

    # ④ 电压等级去掉"交流"前缀（模板口径如 110kV）
    voltage = out.get("voltage")
    if isinstance(voltage, str):
        out["voltage"] = voltage.replace("交流", "")

    # ⑤ 缺账期终止日时按起始日推导月末
    if out.get("period_start") and not out.get("period_end"):
        notes.append("账单未提供账期终止日，请在预览中手工补填后再导入")
    return out, notes, assumptions


def _check_reconciliations(
    row: dict[str, Any], raw: dict[str, Any], extras: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """三大勾稽 + V2.5 新增勾稽的**辅助核对**（只提示，不判废）。

    :return: ``(中文提示列表, 口径说明列表)``
    """
    messages: list[str] = []
    assumptions: list[str] = []

    total = row.get("energy_total_kwh")
    parts = [row.get(f"energy_{k}") for k in ("sharp", "peak", "flat", "valley", "offpeak")]
    if total is not None and any(p is not None for p in parts):
        s = sum(p or 0.0 for p in parts)
        if abs(s - total) > max(1.0, total * 0.001):
            messages.append(f"分时电量合计 {s:.0f} 与总购电量 {total:.0f} 不一致（差 {s - total:+.0f} kWh）")
        else:
            messages.append(
                f"分时电量合计 {s:,.0f} kWh 与账单总购电量 {total:,.0f} kWh 一致"
                "（取数口径：电量明细逐电能表计费电量之和）"
            )

    charge = row.get("energy_charge_yuan")
    detail = [
        row.get(f"{k}_yuan")
        for k in (
            "market_purchase_charge",
            "transmission_distribution_charge",
            "line_loss_charge",
            "system_operation_charge",
            "government_fund_charge",
        )
    ]
    if charge is not None and any(d is not None for d in detail):
        s = sum(d or 0.0 for d in detail)
        if abs(s - charge) > max(1.0, abs(charge) * 0.001):
            messages.append(f"电度电费明细合计 {s:.2f} 与电度电费合计 {charge:.2f} 不一致")

    bill_total = row.get("bill_total_yuan")
    if bill_total is not None and charge is not None:
        base = charge
        for key in ("demand_charge_yuan", "basic_capacity_charge_yuan", "power_factor_adjustment_yuan"):
            base += row.get(key) or 0.0
        if abs(base - bill_total) > max(2.0, abs(bill_total) * 0.002):
            messages.append(f"账单总额 {bill_total:.2f} 与（电度+基本+力调）{base:.2f} 不一致")

    # ---- V2.5：电费明细逐计量点求和 vs 账单概况分项 ----
    detail_totals: dict[str, float] = raw.get("_detail_totals") or {}
    for internal_key, field in (
        ("market", "market_purchase_charge_yuan"),
        ("loss", "line_loss_charge_yuan"),
        ("trans", "transmission_distribution_charge_yuan"),
        ("sysop", "system_operation_charge_yuan"),
        ("gov", "government_fund_charge_yuan"),
    ):
        declared = row.get(field)
        summed = detail_totals.get(internal_key)
        if declared is None or summed is None:
            continue
        if abs(declared - summed) > max(1.0, abs(declared) * 0.001):
            messages.append(
                f"电费明细逐计量点求和得到的「{_charge_label(internal_key)}」{summed:,.2f} 元"
                f"与账单概况页的 {declared:,.2f} 元不一致（差 {summed - declared:+,.2f} 元），"
                "请核对是否有计量点未被识别"
            )

    # ---- V2.5：需量电价 × 计费需量 = 需量电费 ----
    rate = row.get("demand_rate_yuan_per_kw_month")
    demand = row.get("billing_demand_kw")
    demand_charge = row.get("demand_charge_yuan")
    if rate is not None and demand is not None and demand_charge is not None:
        expected = rate * demand
        if abs(expected - demand_charge) > max(1.0, abs(demand_charge) * 0.002):
            messages.append(
                f"需量电价 {rate:g} 元/kW·月 × 账单计费需量 {demand:g} kW = {expected:,.2f} 元，"
                f"与账单的需量电费 {demand_charge:,.2f} 元不一致，请核对"
            )
        else:
            messages.append(
                f"需量电费核对：需量电价 {rate:g} 元/kW·月 × 计费需量 {demand:g} kW "
                f"= {demand_charge:,.2f} 元，与账单一致"
            )

    # ---- V2.5：24 小时电价表 ----
    hourly: list[HourlyEnergyPricePoint] = extras.get("hourly_energy_tariff") or []
    if hourly:
        hours = [point.hour for point in hourly]
        energy_sum = sum(point.energy_kwh or 0.0 for point in hourly)
        prices = [
            point.direct_trade_price_yuan_per_kwh
            for point in hourly
            if point.direct_trade_price_yuan_per_kwh is not None
        ]
        loss_prices = [
            point.line_loss_price_yuan_per_kwh
            for point in hourly
            if point.line_loss_price_yuan_per_kwh is not None
        ]
        assumptions.append(
            "24 小时电量电价表为账单事实（逐时电量 / 直接交易价格 / 上网环节线损价格），"
            "来自账单第 4 页；消纳率电价计算应直接取用该表，不得用模拟电价替换（§0.2）"
        )
        messages.append(
            f"已读取 24 小时电量电价表 {len(hourly)} 条"
            f"（小时编号 {min(hours)}~{max(hours)}），逐时电量合计 {energy_sum:,.0f} kWh"
        )
        if len(hourly) != len(HOURLY_PRICE_HOURS):
            messages.append(
                f"警告：24 小时电价表只有 {len(hourly)} 条，应为 24 条（1~24 时），"
                "请核对账单是否被截断"
            )
        if prices and not all(0.0 < price < 2.0 for price in prices):
            messages.append(
                "警告：24 小时电价表存在超出合理区间（0~2 元/kWh）的直接交易价格，请核对："
                + "、".join(
                    f"{point.hour} 时 {point.direct_trade_price_yuan_per_kwh}"
                    for point in hourly
                    if point.direct_trade_price_yuan_per_kwh is not None
                    and not 0.0 < point.direct_trade_price_yuan_per_kwh < 2.0
                )
            )
        if loss_prices and not all(0.0 <= price < 0.5 for price in loss_prices):
            messages.append(
                "警告：24 小时电价表存在超出合理区间（0~0.5 元/kWh）的上网环节线损价格，请核对"
            )
        # 24 小时表只覆盖参与市场化交易的电能表（定比分表电量不在其中）
        if row.get("energy_total_kwh"):
            gap = energy_sum - float(row["energy_total_kwh"])
            tolerance = max(1.0, abs(float(row["energy_total_kwh"])) * 0.02)
            if abs(gap) > tolerance:
                messages.append(
                    f"24 小时电量合计 {energy_sum:,.0f} kWh 与账单总电量 "
                    f"{float(row['energy_total_kwh']):,.0f} kWh 相差 {gap:+,.0f} kWh"
                    "（24 小时表只覆盖参与市场化交易的电能表，定比分表电量不在其中，"
                    "两者本就不必相等，此处仅作提示）"
                )
                assumptions.append(
                    "24 小时电量电价表的电量合计不等于账单总电量：该表只统计参与市场化交易的"
                    "电能表，定比（分表）电量不参与逐时电价，差额已在提示中如实给出"
                )

    # ---- V2.5：市场化运营费用明细 ----
    operation: OperationFeeDetail | None = extras.get("operation_fee_detail")
    if operation is not None:
        messages.append(operation.describe())
        assumptions.append(
            "市场化运营费用（B 增加支出 / C 降低支出 / 虚拟电厂调峰 / 调频）为账单事实，"
            "已按账单原文与符号保存，不做任何收益或回收计算（§0.2）"
        )
        if (
            operation.total_yuan is not None
            and operation.b_increase_total_yuan is not None
            and operation.c_decrease_total_yuan is not None
        ):
            composed = (
                operation.b_increase_total_yuan
                + operation.c_decrease_total_yuan
                + (operation.virtual_plant_peak_shaving_yuan or 0.0)
                + (operation.frequency_regulation_yuan or 0.0)
            )
            if abs(composed - operation.total_yuan) > max(1.0, abs(operation.total_yuan) * 0.001):
                messages.append(
                    f"市场化运营费用四类合计 {composed:,.2f} 元与账单总合计 "
                    f"{operation.total_yuan:,.2f} 元不一致，请核对"
                )

    return messages, assumptions


def _charge_label(internal_key: str) -> str:
    """内部费用键 → 账单上的中文费用类别名（仅用于提示文案）。"""
    return {
        "market": "市场化购电电费",
        "loss": "上网环节线损费用",
        "trans": "输配电量电费",
        "sysop": "系统运行费用",
        "gov": "政府性基金及附加",
        "catalog": "目录电费",
    }.get(internal_key, internal_key)


# --------------------------------------------------------------------------- #
# 对外主函数
# --------------------------------------------------------------------------- #
@dataclass
class _ParseOutcome:
    """一次完整解析的结果（行字典 + 中文提示 + 结构化账单事实）。"""

    row: dict[str, Any]
    notes: list[str]
    extras: dict[str, Any]


def _parse_document(doc) -> _ParseOutcome:
    """把已打开的 PDF 解析成 ``_ParseOutcome``（供 ``parse_bill_pdf`` 等复用）。"""
    raw: dict[str, Any] = {}
    raw.update(_parse_header_text(doc))
    for key, value in _parse_header_pos(doc).items():
        if value:
            raw[key] = value

    groups = _parse_meter_groups(doc)
    raw.update(_parse_charges(doc, groups))
    raw.update(_parse_demand_and_pf(doc))

    # 分时电量：明细优先 → 抄表行（不依赖分组）→ 四标签块（旧版式）
    detail = _energy_from_groups(groups)
    for key, value in detail.items():
        raw[key] = value
    if not all(detail.get(key) is not None for key in ("sharp", "peak", "flat", "valley")):
        # 没有"电能表编号"分组头的版式（或分组解析缺时段）：用抄表行回退补齐
        for key, value in _parse_energy_rows(doc).items():
            raw.setdefault(key, value)
    if not detail:
        peak_valley = _parse_peak_valley_blocks(doc)
        for key in ("sharp", "peak", "flat", "valley"):
            if peak_valley.get(key) is not None:
                raw.setdefault(key, peak_valley[key])

    raw["_meter_groups"] = [
        {
            "meters": group.meters,
            "tariff": group.tariff,
            "is_ratio_submeter": group.is_ratio_submeter,
            "energy": dict(group.energy),
        }
        for group in groups
    ]

    extras: dict[str, Any] = {}
    hourly = _parse_hourly_energy_tariff(doc)
    if hourly:
        extras["hourly_energy_tariff"] = hourly
    operation = _parse_operation_fees(doc)
    if operation is not None:
        extras["operation_fee_detail"] = operation
    # 逐电能表分组的计费电量（结构化账单事实：供界面/测试核对"多块表是否正确合并"）
    extras["meter_groups"] = raw["_meter_groups"]

    normalized, notes, assumptions = _normalize(raw)

    row: dict[str, Any] = {}
    for source_key, field in _FIELD_MAP.items():
        row[field] = normalized.get(source_key)
    # 计费方式：账单未明写时按"有基本电费"推断为两部制，否则未知
    if not row.get("tariff_structure"):
        if normalized.get("basic_demand") or normalized.get("basic_capacity") or normalized.get("demand"):
            row["tariff_structure"] = "两部制"
            notes.append("账单未明写计费方式，依据存在基本电费/计费需量推断为『两部制』")
        else:
            row["tariff_structure"] = "未知"

    notes.extend(_missing_field_notes(row))
    for group in groups:
        for note in group.notes:
            meters = "、".join(group.meters) if group.meters else "（未识别表号）"
            notes.append(f"{meters}：{note}")
    recon_messages, recon_assumptions = _check_reconciliations(row, raw, extras)
    notes.extend(recon_messages)
    for line in (*assumptions, *recon_assumptions):
        if line not in notes:
            notes.append(line)

    logger.info(
        "PDF 账单解析：账期 %s ~ %s，识别字段 %d 个，计量分组 %d 个，"
        "24 小时电价 %d 条，运营费用 %s",
        row.get("billing_period_start"),
        row.get("billing_period_end"),
        sum(1 for value in row.values() if value not in (None, "")),
        len(groups),
        len(extras.get("hourly_energy_tariff") or []),
        "有" if extras.get("operation_fee_detail") else "无",
    )
    return _ParseOutcome(row=row, notes=notes, extras=extras)


def _energy_from_groups(groups: list[_MeterGroup]) -> dict[str, float]:
    """把各计量分组的计费电量按时段相加（**明细优先**口径，V2.5 §5）。

    主表与定比分表都要计入：真实账单里主表总电量 7,255,282 kWh，
    定比分表 224,136 kWh，两者相加正是账单概况的 7,479,418 kWh。
    """
    totals: dict[str, float] = {}
    for group in groups:
        for key, value in group.energy.items():
            if value is None:
                continue
            totals[key] = round(totals.get(key, 0.0) + float(value), 4)
    return totals


def _missing_field_notes(row: dict[str, Any]) -> list[str]:
    """把"未能识别的关键字段"写成一句中文提示（不臆造数值）。"""
    labels = (
        ("energy_total_kwh", "总购电量"),
        ("energy_charge_yuan", "电度电费合计"),
        ("bill_total_yuan", "账单总额"),
    )
    missing = [label for field, label in labels if row.get(field) is None]
    if not missing:
        return []
    return ["以下字段未能从 PDF 中识别，已留空（= 未提供，不是 0）：" + "、".join(missing)]


def parse_bill_pdf(path: str | Path) -> tuple[dict[str, Any], list[str]]:
    """解析一份 PDF 账单，返回 ``(模板字段行, 中文提示列表)``。

    * 行字典的键是 ``BILL_COLUMNS`` 的 ``field`` 名（如 ``energy_sharp_kwh``）；
    * 无法解析的字段为 ``None``（= 未提供，**不是 0**）；
    * 提示列表说明"哪些字段没提取到 / 做了哪些归一化 / 勾稽是否相符"；
    * 文件不是 PDF / 打不开 → 抛 :class:`ValidationError`（中文）。

    需要**结构化**账单事实（24 小时电量电价表、市场化运营费用明细）请用
    :func:`parse_bill_pdf_full` 或 :func:`pdf_row_payload`。
    """
    outcome = parse_bill_pdf_full(path)
    return outcome[0], outcome[1]


def parse_bill_pdf_full(
    path: str | Path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """解析一份 PDF 账单，返回 ``(模板字段行, 中文提示列表, 结构化账单事实)``。

    第三个元素（``extras``）目前含：

    * ``hourly_energy_tariff``：``list[HourlyEnergyPricePoint]``——24 小时电量电价表；
    * ``operation_fee_detail``：``OperationFeeDetail``——市场化运营费用明细。

    这些是**账单事实**，不是模拟结果（V2.1 §0.2）；它们不是标量，
    因此不放进"一行表格"的行字典，而由
    :func:`cenep.data.bill_importer.build_bill_from_row` 的 ``bill_extras`` 参数写入账单。
    """
    doc, _ = _open_document(path)
    try:
        outcome = _parse_document(doc)
    finally:
        doc.close()
    logger.debug("PDF 账单结构化事实：%s", sorted(outcome.extras))
    return outcome.row, outcome.notes, outcome.extras


def pdf_row_payload(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 ``(行字典, 结构化账单事实)``，供 ``bill_importer`` 消费（V2.5）。

    行字典只输出 ``field`` 名（不重复输出中文表头），原因是 ``resolve_bill_columns``
    会把"未被映射的表头"列进 ``unmapped_headers`` 并在预览里提示"未识别的列"。
    """
    row, notes, extras = parse_bill_pdf_full(path)
    from .bill_importer import BILL_COLUMNS

    valid_fields = {column.field for column in BILL_COLUMNS if column.field}
    # 「数据标记」用模板的**中文表头**输出（该列在 BILL_COLUMNS 里 field 为空，
    # 只有表头），这样它会被 resolve_bill_columns 正常命中，而不是落进"未识别列"。
    marker_header = next(
        (column.header for column in BILL_COLUMNS if not column.field), "数据来源"
    )
    enriched: dict[str, Any] = {
        field: row.get(field) for field in valid_fields if field in row
    }
    enriched[marker_header] = "pdf"  # V2.5：PDF 账单的来源就是 PDF，不再谎报 excel
    enriched["_pdf_notes"] = notes
    return enriched, extras


def pdf_row_dict(path: str | Path) -> dict[str, Any]:
    """返回**单行**行字典（键 = ``BILL_COLUMNS`` 的 ``field`` 名），供 ``bill_importer`` 消费。

    这是 :func:`pdf_row_payload` 的第一项，保留给只关心行字典的调用方与既有测试。
    """
    row, _extras = pdf_row_payload(path)
    return row
