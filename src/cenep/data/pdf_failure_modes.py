"""PDF 账单导入的**失败模式分类**（V2.5 缺口①：可解释的中文失败状态）。

背景
----
``bill_pdf_importer`` 面向的是**版式正确的国网湖北账单**；一旦输入不是它能处理的 PDF
（扫描件、加密、损坏、空文件、根本不含账单特征），旧链路只会得到「所有字段都是 None」
的空结果，用户看到的是"什么都没识别出来"，**无法判断到底是文件坏了、要密码、
还是需要 OCR**。本模块专门把这类情形翻译成**有错误码、有中文原因、有可操作建议**
的失败状态，供界面与报告直接展示。

设计边界
--------
* 本模块**只做"能不能读、读出来像不像账单"的判定**，不解析任何账单字段、
  不做任何计算、不写库（与 ``bill_pdf_importer`` 的职责严格分开）。
* **绝不裸抛异常**：:func:`probe_pdf` / :func:`probe_bill_pdf` 对任何输入都返回
  结果对象；只有显式调用 :func:`raise_for_bill_pdf` 才会把失败转成中文
  :class:`~cenep.calculation.errors.ValidationError`。
* PyMuPDF **延迟导入**：Excel 导入链路不需要 PDF 库，绝不能因缺库而崩。
* 分不清「空白页」与「扫描件」时，用**页内是否含位图**区分（实测依据见下）。

实测依据（PyMuPDF 1.28.2，本机实测，非推测）
--------------------------------------------
============================  ==========================================  ==========================
构造样本                        PyMuPDF 实际行为                             本模块判定
============================  ==========================================  ==========================
3 页空白 PDF（无文本无图）      ``open`` 成功，``page_count=3``，chars=0        :attr:`PdfFailureCode.BLANK_PAGES`
每页 1 张位图、无文本对象       ``open`` 成功，chars=0，``images=1``/页         :attr:`PdfFailureCode.SCANNED_NO_TEXT`
AES-256 加密、需用户口令        ``open`` 成功，``needs_pass=1``；              :attr:`PdfFailureCode.ENCRYPTED`
                              ``load_page()`` 抛
                              ``ValueError: document closed or encrypted``
正常 PDF 截断到 40% 字节         ``open`` 抛 ``FileDataError``（文件头仍是 ``%PDF-``）  :attr:`PdfFailureCode.CORRUPTED`
``%PDF-1.7`` + 全 0 字节         ``open`` 抛 ``FileDataError``                 :attr:`PdfFailureCode.CORRUPTED`
无 ``%PDF-`` 头的任意垃圾字节     ``open`` 抛 ``FileDataError``                 :attr:`PdfFailureCode.NOT_A_PDF`
0 字节文件                      ``open`` 抛 ``EmptyFileError``                :attr:`PdfFailureCode.EMPTY_FILE`
只有 1 个字符文本层的 PDF        ``open`` 成功，chars=1（每页 1 字）             :attr:`PdfFailureCode.TEXT_LAYER_TOO_SPARSE`
有文本、但无任何账单特征词        ``open`` 成功，chars 正常                      :attr:`PdfFailureCode.NOT_A_BILL`
============================  ==========================================  ==========================

因此"文件头对不对"是区分**非 PDF** 与**损坏 PDF** 的唯一可靠线索：
PyMuPDF 两者都抛同一个 ``FileDataError``。

与其他模块的关系
----------------
* :mod:`cenep.data.pdf_ocr` 依赖本模块做预检，并按 OCR 后端可用性**细化**扫描件的提示文案；
  本模块**不**依赖 ``pdf_ocr``（避免循环依赖），只做一次函数内延迟导入以取提示文案。
* :mod:`cenep.data.bill_pdf_importer` 在文件末尾**追加**了两个薄封装
  （:func:`probe_bill_pdf` / :func:`raise_for_bill_pdf` 的转发），未改动任何既有函数。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BILL_FEATURE_TOKENS",
    "BINARY_DOCUMENT_SIGNATURES",
    "MAX_EXPECTED_PAGES",
    "MIN_BILL_TOKEN_HITS",
    "MIN_TEXT_CHARS_PER_PAGE",
    "PdfFailure",
    "PdfFailureCode",
    "PdfProbe",
    "SCANNED_PDF_OCR_HINT",
    "bill_feature_hits",
    "describe_probe",
    "failure_to_extras",
    "looks_like_pdf_header",
    "probe_bill_pdf",
    "probe_pdf",
    "raise_for_bill_pdf",
    "read_file_header",
    "validation_error_of",
]

# --------------------------------------------------------------------------- #
# 判定阈值（全部集中在此，便于复核；数值依据见模块文档的实测表）
# --------------------------------------------------------------------------- #

#: 每页平均文本字符数低于此值 → 认为**没有可用文本层**（真实国网账单实测约 1,300~1,500 字/页）
MIN_TEXT_CHARS_PER_PAGE: int = 20

#: 页数超过此值 → 页数异常（真实电费账单为 4~5 页；过大通常是选错了文件）
MAX_EXPECTED_PAGES: int = 60

#: 判定"是电费账单"所需命中的**核心特征词**最少个数
MIN_BILL_TOKEN_HITS: int = 2

#: 电费账单核心特征词（出现在国网湖北账单的全部版式上）
BILL_FEATURE_TOKENS: tuple[str, ...] = (
    "电费",
    "电量",
    "账单",
    "户号",
    "电能表",
    "计费",
    "功率因数",
    "千伏",
    "电价",
    "受电点",
    "抄见",
    "峰谷",
)

#: 非 PDF 的常见二进制文件头（用于给出"这看起来是什么格式"的可操作提示）
BINARY_DOCUMENT_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "ZIP/Office 文档（.xlsx/.docx/.zip）"),
    (b"\xd0\xcf\x11\xe0", "旧版 Office 二进制文档（.xls/.doc）"),
    (b"\x89PNG", "PNG 图片"),
    (b"\xff\xd8\xff", "JPEG 图片"),
    (b"GIF8", "GIF 图片"),
    (b"BM", "BMP 图片"),
    (b"%!PS", "PostScript 文件"),
    (b"{\\rtf", "RTF 文档"),
)

#: 扫描件（无文本层）默认提示。``pdf_ocr`` 可用时会替换为更具体的文案。
SCANNED_PDF_OCR_HINT: str = (
    "本版本未启用 OCR：如需识别扫描件，请安装可选 OCR 后端（Tesseract 及其中文语言包）"
    "后重试，或先用其他工具把扫描件转成带文本层的 PDF。"
)

#: PDF 文件头（允许前面有少量垃圾字节，与 PDF 规范一致）
_PDF_HEADER = re.compile(rb"%PDF-\d\.\d")
_PDF_HEADER_WINDOW = 1024


class PdfFailureCode(str, Enum):
    """PDF 账单导入的失败分类（值即稳定的错误码，供界面与报告使用）。"""

    OK = "pdf.ok"
    """文件可读、含文本层、且具备账单特征。"""

    FILE_MISSING = "pdf.failure.file_missing"
    """路径不存在或不是普通文件。"""

    EMPTY_FILE = "pdf.failure.empty_file"
    """文件长度为 0 字节。"""

    NOT_A_PDF = "pdf.failure.not_pdf"
    """缺少 ``%PDF-`` 文件头：根本不是 PDF（可能是 Excel/图片/文本）。"""

    CORRUPTED = "pdf.failure.corrupted"
    """文件头是 PDF，但结构损坏/被截断，无法打开。"""

    ENCRYPTED = "pdf.failure.encrypted"
    """PDF 受口令保护，需要密码才能读取内容。"""

    NO_PAGES = "pdf.failure.no_pages"
    """能打开但页面数为 0。"""

    ABNORMAL_PAGE_COUNT = "pdf.failure.abnormal_page_count"
    """页数远超电费账单的正常范围（可能是选错了文件）。"""

    SCANNED_NO_TEXT = "pdf.failure.scanned_no_text"
    """有页面、页内含位图，但几乎没有文本层 → 疑似扫描件，需要 OCR。"""

    BLANK_PAGES = "pdf.failure.blank_pages"
    """有页面，但既无文本也无位图 → 空白页/空文档。"""

    TEXT_LAYER_TOO_SPARSE = "pdf.failure.text_layer_too_sparse"
    """有少量文本但远低于账单应有的密度 → 文本层不可用（常见于"图片+水印"导出）。"""

    NOT_A_BILL = "pdf.failure.not_a_bill"
    """能正常读取文本，但未命中任何电费账单特征 → 未识别到账单特征。"""


@dataclass(frozen=True)
class PdfFailure:
    """一次失败的结构化描述（中文、可操作）。"""

    code: PdfFailureCode
    #: 一句话说明"发生了什么"（面向用户，中文）
    summary: str
    #: 判定依据（把可复核的证据写出来）
    detail: str
    #: 可操作建议（面向用户，中文）
    action: str
    #: 是否阻断导入（False 表示"能读但需人工确认"）
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（供报表与界面展示）。"""
        return {
            "code": self.code.value,
            "summary": self.summary,
            "detail": self.detail,
            "action": self.action,
            "blocking": self.blocking,
        }

    def text(self) -> str:
        """单行中文描述：``[错误码] 说明；依据；建议``。"""
        return f"[{self.code.value}] {self.summary}（依据：{self.detail}）处理建议：{self.action}"


@dataclass
class PdfProbe:
    """一次 PDF 预检的完整结果（**永不携带异常**，失败信息在 :attr:`failure` 里）。"""

    path: str
    file_name: str
    file_size: int
    #: 文件头是否为合法 ``%PDF-``
    has_pdf_header: bool
    #: 打开是否成功
    opened: bool
    #: 页面数（未打开或加密不可读时为 0）
    page_count: int
    #: 各页可见文本字符数（按页序）
    page_text_chars: list[int] = field(default_factory=list)
    #: 各页位图数量（按页序）
    page_image_counts: list[int] = field(default_factory=list)
    #: 是否为 PDF（PyMuPDF ``is_pdf``；能打开其它格式时可能为 False）
    is_pdf: bool = False
    is_encrypted: bool = False
    needs_password: bool = False
    #: PyMuPDF 是否对损坏结构做了修复
    was_repaired: bool = False
    #: 命中的账单特征词
    bill_traits_hit: list[str] = field(default_factory=list)
    #: 未命中的账单特征词
    bill_traits_missing: list[str] = field(default_factory=list)
    #: 文本开头片段（便于界面显示"读到的是什么"）
    text_head: str = ""
    #: 检测到的其它文件格式说明（非 PDF 时）
    detected_format: str | None = None
    #: 扫描件的 OCR 提示（仅 :attr:`PdfFailureCode.SCANNED_NO_TEXT` 时非空）
    ocr_hint: str | None = None
    #: 失败描述；``None`` 表示通过预检
    failure: PdfFailure | None = None
    #: 预检过程中的内部异常文本（不面向用户，便于排障）
    internal_error: str | None = None

    # ---- 便捷属性 ---- #
    @property
    def ok(self) -> bool:
        """是否通过预检（``failure is None`` 且无阻断项）。"""
        return self.failure is None

    @property
    def code(self) -> PdfFailureCode:
        """当前分类码（通过时为 :attr:`PdfFailureCode.OK`）。"""
        return self.failure.code if self.failure else PdfFailureCode.OK

    @property
    def text_chars(self) -> int:
        """全部页面的可见文本字符数合计。"""
        return sum(self.page_text_chars)

    @property
    def image_count(self) -> int:
        """全部页面的位图数量合计。"""
        return sum(self.page_image_counts)

    @property
    def chars_per_page(self) -> float:
        """每页平均文本字符数（页数为 0 时返回 0.0）。"""
        return self.text_chars / self.page_count if self.page_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（供报表、界面与测试断言使用）。"""
        return {
            "path": self.path,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "ok": self.ok,
            "code": self.code.value,
            "has_pdf_header": self.has_pdf_header,
            "opened": self.opened,
            "page_count": self.page_count,
            "text_chars": self.text_chars,
            "chars_per_page": round(self.chars_per_page, 3),
            "image_count": self.image_count,
            "page_text_chars": list(self.page_text_chars),
            "page_image_counts": list(self.page_image_counts),
            "is_pdf": self.is_pdf,
            "is_encrypted": self.is_encrypted,
            "needs_password": self.needs_password,
            "was_repaired": self.was_repaired,
            "bill_traits_hit": list(self.bill_traits_hit),
            "bill_traits_missing": list(self.bill_traits_missing),
            "text_head": self.text_head,
            "detected_format": self.detected_format,
            "ocr_hint": self.ocr_hint,
            "failure": self.failure.to_dict() if self.failure else None,
        }


# --------------------------------------------------------------------------- #
# 底层工具
# --------------------------------------------------------------------------- #
def read_file_header(path: str | Path, size: int = _PDF_HEADER_WINDOW) -> bytes:
    """读取文件头 ``size`` 字节；读取失败返回 ``b""``（**不抛异常**）。"""
    try:
        with Path(path).open("rb") as handle:
            return handle.read(size)
    except OSError as exc:  # pragma: no cover - 取决于文件系统权限
        logger.warning("读取文件头失败：%s（%s）", path, exc)
        return b""


def looks_like_pdf_header(data: bytes) -> bool:
    """判断字节流是否含合法 ``%PDF-x.y`` 文件头（允许前置少量垃圾字节）。"""
    return bool(_PDF_HEADER.search(data))


def _detect_other_format(header: bytes) -> str | None:
    """从文件头猜测"这其实是什么格式"（用于给出可操作提示）。"""
    for signature, name in BINARY_DOCUMENT_SIGNATURES:
        if header.startswith(signature):
            return name
    if header and all(byte in b"\t\n\r" or 32 <= byte < 127 for byte in header[:64]):
        return "纯文本文件"
    return None


def _import_pymupdf():  # noqa: ANN202 - 返回模块对象，类型依运行环境而定
    """延迟导入 PyMuPDF（``pymupdf`` 优先，回退 ``fitz``）。失败返回 ``None``。"""
    try:
        import pymupdf  # type: ignore[import-not-found]

        return pymupdf
    except ImportError:  # pragma: no cover - 取决于运行环境
        try:
            import fitz  # type: ignore[import-not-found]

            return fitz
        except ImportError:
            return None


def bill_feature_hits(text: str) -> tuple[list[str], list[str]]:
    """返回 ``(命中的账单特征词, 未命中的账单特征词)``。"""
    hit = [token for token in BILL_FEATURE_TOKENS if token in text]
    missing = [token for token in BILL_FEATURE_TOKENS if token not in text]
    return hit, missing


def _failure(
    code: PdfFailureCode,
    summary: str,
    detail: str,
    action: str,
    *,
    blocking: bool = True,
) -> PdfFailure:
    """构造 :class:`PdfFailure` 的内部便捷函数。"""
    return PdfFailure(code=code, summary=summary, detail=detail, action=action, blocking=blocking)


# --------------------------------------------------------------------------- #
# 主流程：预检
# --------------------------------------------------------------------------- #
def probe_pdf(path: str | Path) -> PdfProbe:
    """预检一个文件是否为**可读的 PDF**，返回 :class:`PdfProbe`（**永不抛异常**）。

    判定顺序（先排除"根本读不了"，再看"读出来像不像账单"）：

    1. 文件是否存在、是否为空文件；
    2. 文件头是否为 ``%PDF-``（决定：非 PDF / 损坏 PDF）；
    3. 能否打开；是否加密需要口令；
    4. 页数是否为 0、是否异常偏大；
    5. 文本层密度（无文本 + 有位图 → 扫描件；无文本 + 无位图 → 空白页；
       文本过稀 → 文本层不可用）；
    6. 账单特征词（由 :func:`probe_bill_pdf` 追加）。
    """
    target = Path(path)
    probe = PdfProbe(
        path=str(target),
        file_name=target.name,
        file_size=0,
        has_pdf_header=False,
        opened=False,
        page_count=0,
    )

    # ---- 1. 存在性与空文件 ---- #
    if not target.exists() or not target.is_file():
        probe.failure = _failure(
            PdfFailureCode.FILE_MISSING,
            f"账单文件不存在：{target}",
            "目标路径不存在或不是普通文件。",
            "请确认文件是否已被移动/删除，或重新选择账单文件。",
        )
        return probe

    probe.file_size = target.stat().st_size
    if probe.file_size == 0:
        probe.failure = _failure(
            PdfFailureCode.EMPTY_FILE,
            f"账单文件「{target.name}」是 0 字节的空文件。",
            "文件长度为 0 字节。",
            "该文件没有内容，通常是下载中断或复制失败所致，请重新获取原始账单。",
        )
        return probe

    # ---- 2. 文件头 ---- #
    header = read_file_header(target)
    probe.has_pdf_header = looks_like_pdf_header(header)
    if not probe.has_pdf_header:
        probe.detected_format = _detect_other_format(header)
        hint = f"文件头看起来是：{probe.detected_format}。" if probe.detected_format else ""
        probe.failure = _failure(
            PdfFailureCode.NOT_A_PDF,
            f"「{target.name}」不是 PDF 文件（缺少 %PDF- 文件头）。",
            f"文件前 1024 字节中未找到 %PDF-x.y 文件头；文件大小 {probe.file_size} 字节。{hint}",
            "请改用该账单对应的 PDF 原件；若手上是 Excel/图片，请先另存为 PDF 再导入。",
        )
        return probe

    # ---- 3. 打开与加密 ---- #
    pymupdf = _import_pymupdf()
    if pymupdf is None:  # pragma: no cover - 取决于运行环境
        probe.failure = _failure(
            PdfFailureCode.CORRUPTED,
            "解析 PDF 账单需要 PyMuPDF，当前环境未安装该依赖。",
            "import pymupdf / fitz 均失败。",
            "请安装依赖：pip install pymupdf（>=1.24）。",
        )
        return probe

    try:
        document = pymupdf.open(str(target))
    except Exception as exc:  # noqa: BLE001 - PyMuPDF 抛 FileDataError / EmptyFileError 等多种异常
        probe.internal_error = f"{type(exc).__name__}: {exc}"
        logger.info("PDF 打开失败：%s（%s）", target.name, probe.internal_error)
        probe.failure = _failure(
            PdfFailureCode.CORRUPTED,
            f"账单文件「{target.name}」已损坏或不完整，无法打开。",
            f"文件含 %PDF- 文件头，但 PyMuPDF 报错：{probe.internal_error}。"
            "常见原因是下载/复制被截断，或文件被其它程序占用后写出不完整。",
            "请重新下载或重新复制该账单原件；若原件本身如此，请联系供电单位重新出账。",
        )
        return probe

    try:
        probe.opened = True
        probe.is_pdf = bool(getattr(document, "is_pdf", False))
        probe.is_encrypted = bool(getattr(document, "is_encrypted", False))
        probe.needs_password = bool(getattr(document, "needs_pass", 0))
        probe.was_repaired = bool(getattr(document, "is_repaired", False))

        # ---- 3a. 加密 ---- #
        if probe.needs_password or probe.is_encrypted:
            probe.failure = _failure(
                PdfFailureCode.ENCRYPTED,
                f"账单文件「{target.name}」受口令保护（加密 PDF），需要密码才能读取。",
                f"PyMuPDF 报告 is_encrypted={probe.is_encrypted}、needs_pass={probe.needs_password}；"
                "页面内容在提供口令前不可访问。",
                "请向出账单位索要未加密的账单 PDF，或提供该 PDF 的打开密码。"
                "本软件不保存任何账单口令。",
            )
            return probe

        # ---- 3b. 非 PDF 但能打开（例如被当成纯文本读入） ---- #
        if not probe.is_pdf:
            probe.failure = _failure(
                PdfFailureCode.NOT_A_PDF,
                f"「{target.name}」虽然能打开，但并不是 PDF 文档。",
                f"PyMuPDF 报告 is_pdf=False（实际识别格式可能为纯文本/其它）。",
                "请确认所选文件确为 PDF 账单原件。",
            )
            return probe

        # ---- 4. 页数 ---- #
        try:
            probe.page_count = int(document.page_count)
        except Exception as exc:  # noqa: BLE001
            probe.internal_error = f"{type(exc).__name__}: {exc}"
            probe.page_count = 0

        if probe.page_count <= 0:
            probe.failure = _failure(
                PdfFailureCode.NO_PAGES,
                f"账单文件「{target.name}」不含任何可读页面（空文档，或文件已被截断/损坏）。",
                "PyMuPDF 报告 page_count=0。文件虽含 %PDF- 文件头并能打开，"
                "但页数为 0——实测把真实账单截断到 40% 字节即会落在此分支"
                "（另一种截断表现是 open 直接抛错，归入 pdf.failure.corrupted）。",
                "请重新下载或重新复制该账单原件；若原件本身如此，"
                "请联系供电单位重新出账。",
            )
            return probe

        # ---- 5. 逐页文本/位图统计（逐页 try：加密或损坏页不能拖垮整个预检） ---- #
        text_chunks: list[str] = []
        for index in range(probe.page_count):
            chars = 0
            images = 0
            try:
                page = document.load_page(index)
            except Exception as exc:  # noqa: BLE001 - 实测加密文档会在此抛 ValueError
                probe.internal_error = f"{type(exc).__name__}: {exc}"
                probe.page_text_chars.append(0)
                probe.page_image_counts.append(0)
                continue
            try:
                text = page.get_text("text") or ""
                chars = len(text.strip())
                text_chunks.append(text)
            except Exception as exc:  # noqa: BLE001
                probe.internal_error = f"{type(exc).__name__}: {exc}"
            try:
                images = len(page.get_images(full=True) or ())
            except Exception as exc:  # noqa: BLE001
                probe.internal_error = f"{type(exc).__name__}: {exc}"
            probe.page_text_chars.append(chars)
            probe.page_image_counts.append(images)

        joined = "\n".join(text_chunks)
        probe.text_head = joined.strip()[:200]
        probe.bill_traits_hit, probe.bill_traits_missing = bill_feature_hits(joined)

        density = probe.chars_per_page
        if probe.text_chars == 0:
            if probe.image_count > 0:
                probe.ocr_hint = _default_ocr_hint()
                probe.failure = _failure(
                    PdfFailureCode.SCANNED_NO_TEXT,
                    f"「{target.name}」疑似扫描件：有 {probe.page_count} 页、"
                    f"含 {probe.image_count} 张位图，但没有任何可提取文本。",
                    f"逐页统计文本字符数为 {probe.page_text_chars}，位图数为 {probe.page_image_counts}"
                    "（实测真实账单约 1,300~1,500 字/页）。",
                    probe.ocr_hint,
                )
            else:
                probe.failure = _failure(
                    PdfFailureCode.BLANK_PAGES,
                    f"「{target.name}」是空白 PDF：有 {probe.page_count} 页，但既无文本也无图像。",
                    f"逐页统计文本字符数为 {probe.page_text_chars}，位图数为 {probe.page_image_counts}。",
                    "请确认导出的账单是否为空白页；如为误导出，请重新获取带内容的账单。",
                )
            return probe

        if density < MIN_TEXT_CHARS_PER_PAGE:
            probe.ocr_hint = _default_ocr_hint()
            probe.failure = _failure(
                PdfFailureCode.TEXT_LAYER_TOO_SPARSE,
                f"「{target.name}」的文本层几乎为空（平均每页仅 {density:.1f} 字），"
                "无法作为电费账单解析。",
                f"共 {probe.page_count} 页、文本 {probe.text_chars} 字，"
                f"低于阈值 {MIN_TEXT_CHARS_PER_PAGE} 字/页。",
                probe.ocr_hint,
            )
            return probe

        if probe.page_count > MAX_EXPECTED_PAGES:
            probe.failure = _failure(
                PdfFailureCode.ABNORMAL_PAGE_COUNT,
                f"「{target.name}」共 {probe.page_count} 页，远超电费账单的正常页数，"
                "请确认是否选错了文件。",
                f"page_count={probe.page_count} > 阈值 {MAX_EXPECTED_PAGES}"
                "（真实国网湖北月度账单为 4~5 页）。",
                "请确认所选文件是否为单月账单；如确为多页合订本，请先拆分出月度账单。",
                blocking=False,
            )
            return probe

        # ---- 6. 账单特征（probe_pdf 只报告，不判失败；probe_bill_pdf 才判失败） ---- #
        logger.debug(
            "PDF 预检通过：%s（%d 页、%d 字、命中账单特征 %d 个）",
            target.name,
            probe.page_count,
            probe.text_chars,
            len(probe.bill_traits_hit),
        )
        return probe
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001 - pragma: no cover
            logger.debug("关闭 PDF 文档时出现异常（已忽略）", exc_info=True)


def _default_ocr_hint() -> str:
    """取 OCR 提示文案：优先问 :mod:`cenep.data.pdf_ocr`，不可用时用内置文案。"""
    try:
        from .pdf_ocr import ocr_availability_notice

        return ocr_availability_notice()
    except Exception:  # noqa: BLE001 - OCR 模块缺失/出错都不能影响失败分类
        return SCANNED_PDF_OCR_HINT


def probe_bill_pdf(path: str | Path) -> PdfProbe:
    """在 :func:`probe_pdf` 基础上追加"**是不是电费账单**"判定。

    若文本层可读，但命中的核心特征词少于 :data:`MIN_BILL_TOKEN_HITS`，
    归类为 :attr:`PdfFailureCode.NOT_A_BILL`（**能打开，但不是账单**），
    并在提示里列出「未识别到账单特征」的具体依据。
    """
    probe = probe_pdf(path)
    if not probe.ok:
        return probe
    if len(probe.bill_traits_hit) >= MIN_BILL_TOKEN_HITS:
        return probe

    hit_text = "、".join(probe.bill_traits_hit) if probe.bill_traits_hit else "（无）"
    head = probe.text_head.replace("\n", " ")[:80]
    probe.failure = _failure(
        PdfFailureCode.NOT_A_BILL,
        f"「{probe.file_name}」能正常读取（{probe.page_count} 页、{probe.text_chars} 字），"
        f"但未识别到电费账单特征（仅命中 {len(probe.bill_traits_hit)} 个特征词）。",
        f"命中特征词：{hit_text}；要求至少命中 {MIN_BILL_TOKEN_HITS} 个。"
        f"文本开头：「{head}」",
        "请确认所选 PDF 是否为电网电费账单；若确认是账单但版式不同，"
        "请把该文件作为新版式样本反馈给开发方以补充特征词。",
    )
    return probe


def validation_error_of(probe: PdfProbe):
    """把预检失败转成中文 :class:`ValidationError`；通过预检时返回 ``None``。

    ``field`` 使用错误码（如 ``pdf.failure.encrypted``），供界面定位具体控件。
    """
    if probe.ok or probe.failure is None:
        return None
    from ..calculation.errors import ValidationError

    return ValidationError(probe.failure.text(), field=probe.failure.code.value)


def raise_for_bill_pdf(path: str | Path) -> PdfProbe:
    """预检并在**阻断性失败**时抛中文 :class:`ValidationError`；否则返回预检结果。

    与 ``bill_pdf_importer`` 既有行为一致：只有"读不了 / 不是账单"才抛异常；
    "页数偏多"这类非阻断提示只写日志、不打断导入。
    """
    probe = probe_bill_pdf(path)
    if probe.ok:
        if probe.failure is None and probe.page_count > MAX_EXPECTED_PAGES:  # pragma: no cover
            logger.warning("页数异常（非阻断）：%s", probe.path)
        return probe
    if probe.failure is not None and not probe.failure.blocking:
        logger.warning("PDF 预检非阻断提示：%s", probe.failure.text())
        return probe
    error = validation_error_of(probe)
    if error is not None:
        raise error
    return probe  # pragma: no cover - 逻辑上不可达


def describe_probe(probe: PdfProbe) -> str:
    """把预检结果渲染成**多行中文报告**（供界面"详细信息"与日志使用）。"""
    lines = [
        f"文件：{probe.file_name}（{probe.file_size} 字节）",
        f"状态：{'✔ 可读' if probe.ok else '✘ ' + probe.code.value}",
        f"页数：{probe.page_count}；文本：{probe.text_chars} 字"
        f"（平均 {probe.chars_per_page:.1f} 字/页）；位图：{probe.image_count} 张",
        f"文件头：{'含 %PDF- 文件头' if probe.has_pdf_header else '缺少 %PDF- 文件头'}"
        f"；加密：{'是' if probe.is_encrypted or probe.needs_password else '否'}"
        f"；结构被修复：{'是' if probe.was_repaired else '否'}",
    ]
    if probe.bill_traits_hit:
        lines.append("命中账单特征：" + "、".join(probe.bill_traits_hit))
    if probe.detected_format:
        lines.append(f"识别到的实际格式：{probe.detected_format}")
    if probe.failure is not None:
        lines.append(f"失败分类：{probe.failure.code.value}")
        lines.append(f"原因：{probe.failure.summary}")
        lines.append(f"依据：{probe.failure.detail}")
        lines.append(f"处理建议：{probe.failure.action}")
        lines.append(f"是否阻断导入：{'是' if probe.failure.blocking else '否（仅提示）'}")
    if probe.internal_error:
        lines.append(f"内部异常（排障用）：{probe.internal_error}")
    return "\n".join(lines)


def failure_to_extras(probe: PdfProbe) -> dict[str, Any]:
    """把预检结果封装成**结构化账单事实**，便于写进结果对象/报告。

    返回的字典可直接交给 ``bill_importer`` 的 ``bill_extras`` 或报表层：

    * ``pdf_probe``：完整的 :meth:`PdfProbe.to_dict`；
    * ``pdf_failure_code`` / ``pdf_failure_message``：供界面直接显示的中文错误码与文案；
    * ``pdf_ocr_required``：是否需要 OCR 才能继续识别。
    """
    extras: dict[str, Any] = {"pdf_probe": probe.to_dict()}
    if probe.failure is not None:
        extras["pdf_failure_code"] = probe.failure.code.value
        extras["pdf_failure_message"] = probe.failure.text()
        extras["pdf_failure_blocking"] = probe.failure.blocking
    extras["pdf_ocr_required"] = probe.code in (
        PdfFailureCode.SCANNED_NO_TEXT,
        PdfFailureCode.TEXT_LAYER_TOO_SPARSE,
    )
    return extras
