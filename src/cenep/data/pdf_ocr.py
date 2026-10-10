"""可插拔的 OCR 接口（V2.5 缺口②："必需的 OCR 接口"）。

定位
----
扫描版账单**没有文本层**，:mod:`cenep.data.bill_pdf_importer` 无法解析。本模块提供
**清晰、可插拔、默认关闭**的 OCR 抽象，使未来的识别能力可以接入而不改动账单解析链路，
同时保证"没有装 OCR 后端"时得到的是**可解释的中文提示**而不是 ``ImportError``。

接口契约
--------
* **输入**：一张图片的字节流（PNG/JPEG 等，由 :meth:`OcrBackend.recognize_image` 接收），
  或一个 PyMuPDF 页面对象（:meth:`OcrBackend.recognize_page` 默认实现会按 ``dpi``
  渲染成 PNG 字节流再调用 ``recognize_image``）。
* **输出**：:class:`OcrPageResult`，含
  ① 纯文本 ``text``；② 字词框 ``words``（:class:`OcrWord`：文本 + 像素坐标 + 置信度）。
  两者都给，是因为账单解析既需要"文本"（识别字段）也需要"坐标"（定位表格列）。
* **失败语义**：**任何失败都通过结果对象返回**（``ok=False`` + :class:`OcrFailureCode`
  + 中文 ``message``），**绝不抛裸异常**。异常只可能来自调用方自己对结果的断言。
* **默认不启用**：注册表中即使存在后端，只要它 ``is_available()`` 为假，
  :func:`get_ocr_backend` 就返回 :class:`NullOcrBackend`，其识别结果是一条中文提示。

依赖策略
--------
* **不新增任何 Python 重量级依赖**。已内置的 PyMuPDF 是可选通道（仍需外部 Tesseract 程序）。
* 所有可选的第三方/外部程序都**惰性探测**：只有真正调用时才去找可执行文件；
  找不到就优雅降级为中文提示。
* 唯一"纯 Python、零外部依赖"的实现是 :class:`StubOcrBackend`。它**不做真实识别**，
  只用于证明接口可插拔、验证降级与错误路径，**不得用于生产识别**（类文档已写明）。

当前环境的实际情况（**如实记录，不冒充已实现**）
------------------------------------------------
本机（Python 3.12.14 / PyMuPDF 1.28.2）**未安装 Tesseract**，因此：

* :class:`TesseractCliOcrBackend` 与 :class:`PyMuPdfOcrBackend` 的 ``is_available()``
  均返回 ``False``，:func:`ocr_pdf` 返回 :attr:`OcrFailureCode.BACKEND_MISSING` 与
  中文提示，**不会报错**；
* **本版本不具备可用的真实 OCR 能力**——这是明确的遗留/阻塞项，不是"已完成识别"。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DPI",
    "NullOcrBackend",
    "OcrBackend",
    "OcrDocumentResult",
    "OcrFailureCode",
    "OcrPageResult",
    "OcrWord",
    "PyMuPdfOcrBackend",
    "StubOcrBackend",
    "TesseractCliOcrBackend",
    "available_ocr_backends",
    "get_ocr_backend",
    "ocr_availability_notice",
    "ocr_pdf",
    "register_ocr_backend",
    "registered_ocr_backends",
    "unregister_ocr_backend",
]

#: 渲染页面为位图时的默认分辨率（账单小字需要 300dpi）
DEFAULT_DPI: int = 300

#: 单次识别的超时（秒）；防止外部 OCR 程序挂死
DEFAULT_TIMEOUT_S: float = 60.0


class OcrFailureCode(str, Enum):
    """OCR 失败分类（值即稳定错误码，供界面与报告使用）。"""

    OK = "ocr.ok"
    """识别成功。"""

    BACKEND_MISSING = "ocr.failure.backend_missing"
    """未安装/未注册任何可用的 OCR 后端（**默认状态**）。"""

    BACKEND_UNAVAILABLE = "ocr.failure.backend_unavailable"
    """后端已注册但当前不可用（可执行文件缺失、语言包缺失、依赖未装）。"""

    RENDER_FAILED = "ocr.failure.render_failed"
    """把 PDF 页面渲染成位图失败。"""

    RECOGNIZE_FAILED = "ocr.failure.recognize_failed"
    """后端调用失败（外部程序非零退出、超时、输出无法解析）。"""

    EMPTY_RESULT = "ocr.failure.empty_result"
    """后端返回了结果但没有识别出任何文字。"""

    NOT_APPLICABLE = "ocr.failure.not_applicable"
    """该文件不需要/不适合 OCR（例如有正常文本层、加密、损坏）。"""


@dataclass(frozen=True)
class OcrWord:
    """一个被识别的字词及其像素坐标框。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    #: 置信度 0~1；后端不提供时为 ``None``（**不臆造**）
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "text": self.text,
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
            "confidence": self.confidence,
        }


@dataclass
class OcrPageResult:
    """**单页**识别结果（成功或失败都在这里，不抛异常）。"""

    page_number: int
    ok: bool
    code: OcrFailureCode
    #: 面向用户的中文说明（成功时为简短说明，失败时为可操作提示）
    message: str
    backend: str = ""
    words: list[OcrWord] = field(default_factory=list)
    text: str = ""
    width: int | None = None
    height: int | None = None

    @property
    def word_count(self) -> int:
        """识别到的字词个数。"""
        return len(self.words)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（``words`` 按需保留，避免报告过大取前 200 个）。"""
        return {
            "page_number": self.page_number,
            "ok": self.ok,
            "code": self.code.value,
            "message": self.message,
            "backend": self.backend,
            "word_count": self.word_count,
            "char_count": len(self.text),
            "text": self.text,
            "words": [word.to_dict() for word in self.words[:200]],
            "width": self.width,
            "height": self.height,
        }


@dataclass
class OcrDocumentResult:
    """**整份文档**的识别结果。"""

    path: str
    backend: str
    ok: bool
    code: OcrFailureCode
    message: str
    pages: list[OcrPageResult] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        """尝试识别的页数。"""
        return len(self.pages)

    @property
    def recognized_pages(self) -> int:
        """识别成功的页数。"""
        return sum(1 for page in self.pages if page.ok)

    @property
    def text(self) -> str:
        """把各页文本按页拼接。"""
        return "\n".join(page.text for page in self.pages if page.ok)

    @property
    def words(self) -> list[OcrWord]:
        """全部页的字词框（页码见 :attr:`OcrWord` 所属的 :class:`OcrPageResult`）。"""
        out: list[OcrWord] = []
        for page in self.pages:
            out.extend(page.words)
        return out

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "path": self.path,
            "backend": self.backend,
            "ok": self.ok,
            "code": self.code.value,
            "message": self.message,
            "page_count": self.page_count,
            "recognized_pages": self.recognized_pages,
            "pages": [page.to_dict() for page in self.pages],
        }


class OcrBackend(ABC):
    """OCR 后端的抽象基类。

    实现者**必须**提供：

    * :attr:`name`：稳定标识（注册表键）；
    * :meth:`is_available`：当前环境是否真的能用；
    * :meth:`unavailable_reason`：不可用的中文原因（可用时返回空串）；
    * :meth:`recognize_image`：识别一张图片的字节流。

    可选覆盖：

    * :attr:`supported_languages`：支持的语言标签（如 ``("chi_sim", "eng")``）；
    * :meth:`recognize_page`：默认实现按 ``dpi`` 把页面渲染成 PNG 再调用
      :meth:`recognize_image`，一般无需覆盖。

    注册表中也接受**鸭子类型**对象（不继承本类但提供同名方法），
    但为获得 :meth:`recognize_page` 的默认实现，推荐继承本类。
    """

    #: 后端稳定标识
    name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool:
        """返回当前环境是否可用（**不得抛异常**，探测失败一律返回 ``False``）。"""

    @abstractmethod
    def unavailable_reason(self) -> str:
        """返回不可用的中文原因；可用时返回空字符串。"""

    @abstractmethod
    def recognize_image(
        self,
        image: bytes,
        *,
        page_number: int = 1,
        width: int | None = None,
        height: int | None = None,
    ) -> OcrPageResult:
        """识别一张图片（PNG/JPEG 等字节流），返回 :class:`OcrPageResult`。

        参数
        ----
        image:
            图片字节流。
        page_number:
            该图片对应的页码（仅用于回填结果，便于回溯来源页）。
        width / height:
            像素尺寸（后端不提供坐标换算依据时可为 ``None``）。

        **失败时返回 ``ok=False`` 的结果，不抛异常。**
        """

    def supported_languages(self) -> tuple[str, ...]:
        """支持的语言标签；默认未知（空元组）。"""
        return ()

    def recognize_page(self, page: Any, *, dpi: int = DEFAULT_DPI) -> OcrPageResult:
        """把 PyMuPDF 页面渲染成 PNG 后调用 :meth:`recognize_image`（默认实现）。"""
        number = int(getattr(page, "number", 0)) + 1
        try:
            pixmap = page.get_pixmap(dpi=dpi)
            image = pixmap.tobytes("png")
            width, height = int(pixmap.width), int(pixmap.height)
        except Exception as exc:  # noqa: BLE001 - 渲染失败必须降级为结果对象
            logger.warning("页面渲染为位图失败（第 %d 页）：%s", number, exc)
            return OcrPageResult(
                page_number=number,
                ok=False,
                code=OcrFailureCode.RENDER_FAILED,
                message=(
                    f"第 {number} 页无法渲染为图片，OCR 无法进行"
                    f"（技术原因：{type(exc).__name__}: {exc}）。"
                    "请确认该 PDF 未加密、未损坏。"
                ),
                backend=self.name,
            )
        return self.recognize_image(
            image, page_number=number, width=width, height=height
        )


class NullOcrBackend(OcrBackend):
    """**默认后端**：不做任何识别，只返回可解释的中文提示。

    这是"本版本未启用 OCR"的正式表达方式——它保证调用方拿到的是
    :attr:`OcrFailureCode.BACKEND_MISSING` 与中文说明，而不是 ``ImportError``。
    """

    name = "null"

    def is_available(self) -> bool:
        """恒为 ``False``：本后端不提供识别能力。"""
        return False

    def unavailable_reason(self) -> str:
        """返回固定中文说明。"""
        return (
            "本版本未启用 OCR：当前没有可用的 OCR 后端。若需识别扫描版账单，"
            "请安装可选后端（Tesseract 及其中文语言包 chi_sim）后重试，"
            "或先用其它工具把扫描件转换为带文本层的 PDF。"
        )

    def recognize_image(
        self,
        image: bytes,
        *,
        page_number: int = 1,
        width: int | None = None,
        height: int | None = None,
    ) -> OcrPageResult:  # noqa: ARG002 - 参数保留以符合接口
        """返回 :attr:`OcrFailureCode.BACKEND_MISSING`（**不抛异常**）。"""
        return OcrPageResult(
            page_number=page_number,
            ok=False,
            code=OcrFailureCode.BACKEND_MISSING,
            message=self.unavailable_reason(),
            backend=self.name,
            width=width,
            height=height,
        )


class StubOcrBackend(OcrBackend):
    """**桩实现**：按预设文本返回字词框，**不做任何真实识别**。

    用途**仅限**：

    * 证明 OCR 接口可插拔（注册 → 调用 → 拿到 ``text`` 与 ``words``）；
    * 让"有后端可用"的分支（含"提示文案应变化"）可被自动化测试覆盖。

    它把输入的第一个字节当作"页序号"之类的信息毫无意义，因此本桩**忽略图片内容**，
    只按 :attr:`pages_text` 里预置的文本逐页返回，坐标按等宽字面估算。

    .. warning::
       本类**不得用于生产识别**。任何使用它的结果都不得作为账单事实或验收依据。
    """

    name = "stub"

    def __init__(
        self,
        pages_text: Iterable[str] = ("桩实现示例文本",),
        *,
        available: bool = True,
        reason: str = "",
        char_width: float = 6.0,
        line_height: float = 12.0,
    ) -> None:
        self.pages_text = list(pages_text)
        self._available = available
        self._reason = reason
        self.char_width = char_width
        self.line_height = line_height
        #: 记录被调用的次数（供测试断言"接口确实被走到"）
        self.calls: int = 0

    def is_available(self) -> bool:
        """按构造参数返回（默认可用）。"""
        return self._available

    def unavailable_reason(self) -> str:
        """按构造参数返回中文原因。"""
        return self._reason

    def supported_languages(self) -> tuple[str, ...]:
        """桩实现固定声明支持简体中文。"""
        return ("chi_sim",)

    def recognize_image(
        self,
        image: bytes,
        *,
        page_number: int = 1,
        width: int | None = None,
        height: int | None = None,
    ) -> OcrPageResult:
        """按页序号返回预置文本的字词框（不读图片内容）。"""
        self.calls += 1
        if not self._available:
            return OcrPageResult(
                page_number=page_number,
                ok=False,
                code=OcrFailureCode.BACKEND_UNAVAILABLE,
                message=self._reason or "桩 OCR 后端被配置为不可用。",
                backend=self.name,
            )
        index = min(max(page_number, 1), len(self.pages_text)) - 1
        text = self.pages_text[index] if self.pages_text else ""
        words: list[OcrWord] = []
        y = 0.0
        for line in text.splitlines() or [""]:
            x = 0.0
            for token in line.split():
                words.append(
                    OcrWord(
                        text=token,
                        x0=x,
                        y0=y,
                        x1=x + self.char_width * len(token),
                        y1=y + self.line_height,
                        confidence=1.0,
                    )
                )
                x += self.char_width * len(token) + self.char_width
            y += self.line_height * 1.5
        if not words:
            return OcrPageResult(
                page_number=page_number,
                ok=False,
                code=OcrFailureCode.EMPTY_RESULT,
                message="桩 OCR 后端未返回任何文字（该页预置文本为空）。",
                backend=self.name,
                width=width,
                height=height,
            )
        return OcrPageResult(
            page_number=page_number,
            ok=True,
            code=OcrFailureCode.OK,
            message=f"桩实现返回第 {page_number} 页的 {len(words)} 个词块（非真实识别结果）。",
            backend=self.name,
            words=words,
            text=text,
            width=width,
            height=height,
        )


class TesseractCliOcrBackend(OcrBackend):
    """**可选的真实后端**：调用外部 ``tesseract`` 命令行程序。

    依赖策略：**不引入任何 Python 依赖**，只要求系统上存在 ``tesseract`` 可执行文件
    （以及需要的语言包，账单需要 ``chi_sim``）。找不到就优雅降级。

    参数
    ----
    command:
        可执行文件名或路径（默认 ``tesseract``）。
    languages:
        语言标签，默认 ``("chi_sim", "eng")``。
    which:
        可执行文件查找函数，默认 :func:`shutil.which`；**测试可注入**以模拟可用/不可用。
    timeout_s:
        单次识别超时（秒）。
    """

    name = "tesseract-cli"

    def __init__(
        self,
        command: str = "tesseract",
        *,
        languages: tuple[str, ...] = ("chi_sim", "eng"),
        which: Callable[[str], str | None] = shutil.which,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.command = command
        self.languages = languages
        self._which = which
        self.timeout_s = timeout_s

    def supported_languages(self) -> tuple[str, ...]:
        """返回配置的语言标签。"""
        return self.languages

    def _executable(self) -> str | None:
        """返回可执行文件路径；找不到返回 ``None``（不缓存，便于运行中安装后立即可用）。"""
        try:
            return self._which(self.command)
        except Exception as exc:  # noqa: BLE001
            logger.debug("查找 %s 失败：%s", self.command, exc)
            return None

    def is_available(self) -> bool:
        """系统上能否找到 ``tesseract`` 可执行文件。"""
        return self._executable() is not None

    def unavailable_reason(self) -> str:
        """不可用时的中文原因。"""
        if self._executable() is not None:
            return ""
        return (
            f"未检测到 OCR 可执行程序「{self.command}」：无法进行扫描件识别。"
            "安装方式：Windows 可安装 UB-Mannheim 版 Tesseract 并勾选中文语言包 chi_sim，"
            "安装后把 tesseract 加入 PATH，或在界面中指定其完整路径。"
        )

    def recognize_image(
        self,
        image: bytes,
        *,
        page_number: int = 1,
        width: int | None = None,
        height: int | None = None,
    ) -> OcrPageResult:
        """调用 ``tesseract`` 识别图片（TSV 输出解析为字词框）。**失败返回结果对象。**"""
        executable = self._executable()
        if executable is None:
            return OcrPageResult(
                page_number=page_number,
                ok=False,
                code=OcrFailureCode.BACKEND_UNAVAILABLE,
                message=self.unavailable_reason(),
                backend=self.name,
                width=width,
                height=height,
            )

        language = "+".join(self.languages)
        with tempfile.TemporaryDirectory(prefix="cenep_ocr_") as tmp:
            image_path = Path(tmp) / f"page_{page_number}.png"
            try:
                image_path.write_bytes(image)
            except OSError as exc:
                return OcrPageResult(
                    page_number=page_number,
                    ok=False,
                    code=OcrFailureCode.RECOGNIZE_FAILED,
                    message=f"OCR 临时图片写入失败：{exc}。请检查磁盘剩余空间与临时目录权限。",
                    backend=self.name,
                )
            command = [
                executable,
                str(image_path),
                "stdout",
                "-l",
                language,
                "--psm",
                "6",
                "tsv",
            ]
            try:
                completed = subprocess.run(  # noqa: S603 - 命令与参数均由本模块构造
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout_s,
                )
            except subprocess.TimeoutExpired:
                return OcrPageResult(
                    page_number=page_number,
                    ok=False,
                    code=OcrFailureCode.RECOGNIZE_FAILED,
                    message=f"OCR 识别超时（超过 {self.timeout_s:g} 秒），已放弃本页。",
                    backend=self.name,
                )
            except (OSError, ValueError) as exc:
                return OcrPageResult(
                    page_number=page_number,
                    ok=False,
                    code=OcrFailureCode.BACKEND_UNAVAILABLE,
                    message=(
                        f"无法启动 OCR 程序「{self.command}」：{type(exc).__name__}: {exc}。"
                        "请确认程序路径正确且可执行。"
                    ),
                    backend=self.name,
                )

        if completed.returncode != 0:
            tail = (completed.stderr or "").strip().splitlines()
            reason = tail[-1] if tail else "无错误输出"
            return OcrPageResult(
                page_number=page_number,
                ok=False,
                code=OcrFailureCode.RECOGNIZE_FAILED,
                message=(
                    f"OCR 程序返回非零退出码 {completed.returncode}：{reason}。"
                    "若提示语言包缺失，请安装 chi_sim 中文语言包。"
                ),
                backend=self.name,
            )

        words, full_text = _parse_tesseract_tsv(completed.stdout or "")
        if not words:
            return OcrPageResult(
                page_number=page_number,
                ok=False,
                code=OcrFailureCode.EMPTY_RESULT,
                message="OCR 已执行但未识别出任何文字，请确认扫描件清晰、方向正确、语言包匹配。",
                backend=self.name,
                width=width,
                height=height,
            )
        return OcrPageResult(
            page_number=page_number,
            ok=True,
            code=OcrFailureCode.OK,
            message=f"OCR 识别出 {len(words)} 个词块。",
            backend=self.name,
            words=words,
            text=full_text,
            width=width,
            height=height,
        )


class PyMuPdfOcrBackend(TesseractCliOcrBackend):
    """**可选的真实后端**：走 PyMuPDF 内置的 Tesseract 通道。

    与 :class:`TesseractCliOcrBackend` 一样**依赖外部 Tesseract 程序**
    （PyMuPDF 的 ``get_textpage_ocr`` 需要它）；这里复用同一套可用性探测与中文提示，
    只把标识改为 ``pymupdf-tesseract``，便于报告中区分走了哪条通道。

    当前仅用于**能力登记**：若环境同时具备 PyMuPDF 与 Tesseract，可据此实现词框提取。
    """

    name = "pymupdf-tesseract"

    def is_available(self) -> bool:
        """需同时具备 PyMuPDF 的 OCR 接口与 Tesseract 可执行文件。"""
        if self._executable() is None:
            return False
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 取决于运行环境
            return False
        return hasattr(pymupdf.Page, "get_textpage_ocr")

    def unavailable_reason(self) -> str:
        """不可用时的中文原因（区分"缺 PyMuPDF OCR 接口"与"缺 Tesseract"）。"""
        if self._executable() is None:
            return (
                "PyMuPDF 的 OCR 通道需要外部 Tesseract 程序，"
                "但未检测到可执行文件「tesseract」。请先安装 Tesseract 及 chi_sim 语言包。"
            )
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 取决于运行环境
            return "解析扫描件需要 PyMuPDF（pip install pymupdf）。"
        if not hasattr(pymupdf.Page, "get_textpage_ocr"):  # pragma: no cover - 版本相关
            return f"当前 PyMuPDF 版本（{getattr(pymupdf, '__version__', '未知')}）不含 OCR 接口。"
        return ""


def _render_page_to_png(page: Any, dpi: int) -> tuple[bytes | None, int | None, int | None, str]:
    """把 PDF 页面渲染成 PNG 字节流。

    返回 ``(图片字节, 宽, 高, 错误说明)``；成功时错误说明为空串、字节不为 ``None``。
    """
    try:
        pixmap = page.get_pixmap(dpi=dpi)
        return pixmap.tobytes("png"), int(pixmap.width), int(pixmap.height), ""
    except Exception as exc:  # noqa: BLE001 - 渲染失败必须降级为结果对象
        return None, None, None, f"{type(exc).__name__}: {exc}"


def _recognize_page(backend: Any, page: Any, *, dpi: int) -> OcrPageResult:
    """统一入口：优先用后端的 ``recognize_page``；鸭子类型后端则走"渲染 + recognize_image"。

    这样"只实现 :meth:`OcrBackend.recognize_image` 的后端"也能被主流程使用，
    **不要求**它继承 :class:`OcrBackend`。
    """
    number = int(getattr(page, "number", 0)) + 1
    backend_name = str(getattr(backend, "name", type(backend).__name__))
    custom = getattr(backend, "recognize_page", None)
    if callable(custom):
        return custom(page, dpi=dpi)

    image, width, height, error = _render_page_to_png(page, dpi)
    if image is None:
        logger.warning("页面渲染为位图失败（第 %d 页）：%s", number, error)
        return OcrPageResult(
            page_number=number,
            ok=False,
            code=OcrFailureCode.RENDER_FAILED,
            message=(
                f"第 {number} 页无法渲染为图片，OCR 无法进行（技术原因：{error}）。"
                "请确认该 PDF 未加密、未损坏。"
            ),
            backend=backend_name,
        )
    return backend.recognize_image(image, page_number=number, width=width, height=height)


def _parse_tesseract_tsv(stdout: str) -> tuple[list[OcrWord], str]:
    """解析 Tesseract 的 ``tsv`` 输出为 ``(字词框, 全文)``。

    TSV 表头为 ``level page_num block_num par_num line_num word_num left top width
    height conf text``；``level==5`` 的行才是词。
    """
    words: list[OcrWord] = []
    lines_by_key: dict[tuple[int, int, int], list[str]] = {}
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        if len(parts) < 12 or parts[0].strip() not in {"5"}:
            continue
        try:
            block = int(parts[2])
            par = int(parts[3])
            line_no = int(parts[4])
            left = float(parts[6])
            top = float(parts[7])
            width = float(parts[8])
            height = float(parts[9])
            conf_raw = float(parts[10])
        except (TypeError, ValueError):
            continue
        token = parts[11].strip()
        if not token:
            continue
        confidence = None if conf_raw < 0 else round(conf_raw / 100.0, 4)
        words.append(
            OcrWord(
                text=token,
                x0=left,
                y0=top,
                x1=left + width,
                y1=top + height,
                confidence=confidence,
            )
        )
        lines_by_key.setdefault((block, par, line_no), []).append(token)
    text = "\n".join(" ".join(tokens) for _key, tokens in sorted(lines_by_key.items()))
    return words, text


# --------------------------------------------------------------------------- #
# 后端注册表
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, OcrBackend] = {}


def register_ocr_backend(backend: Any, *, replace: bool = False) -> str:
    """注册一个 OCR 后端，返回其名称。

    参数
    ----
    backend:
        继承 :class:`OcrBackend` 的实例，或提供同名方法的鸭子类型对象。
    replace:
        同名后端已存在时是否覆盖；``False`` 时**原样保留已有后端**并只记警告
        （避免插件重复注册把可用后端顶掉）。

    异常
    ----
    :class:`ValueError`：缺少 ``name`` / ``is_available`` / ``recognize_image`` 等必需成员。
    """
    name = getattr(backend, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("注册 OCR 后端失败：后端必须提供非空的字符串属性 name。")
    for attribute in ("is_available", "recognize_image"):
        if not callable(getattr(backend, attribute, None)):
            raise ValueError(
                f"注册 OCR 后端「{name}」失败：缺少可调用方法 {attribute}()。"
            )
    if not callable(getattr(backend, "unavailable_reason", None)):
        # 鸭子类型兜底：缺该方法时给一个中文占位，保证界面文案不缺
        backend.unavailable_reason = lambda: f"OCR 后端「{name}」未说明不可用原因。"  # type: ignore[method-assign]
    if name in _REGISTRY and not replace:
        logger.warning("OCR 后端「%s」已注册，本次忽略重复注册（replace=False）。", name)
        return name
    _REGISTRY[name] = backend
    logger.info("已注册 OCR 后端：%s（可用：%s）", name, bool(_safe_available(backend)))
    return name


def _safe_available(backend: Any) -> bool:
    """调用后端 ``is_available()`` 并把任何异常吞成 ``False``。"""
    try:
        return bool(backend.is_available())
    except Exception as exc:  # noqa: BLE001 - 探测失败不能影响注册
        logger.warning("OCR 后端 %s 的可用性探测失败：%s", getattr(backend, "name", "?"), exc)
        return False


def unregister_ocr_backend(name: str) -> bool:
    """注销指定后端；返回是否确实删除了一个后端。"""
    return _REGISTRY.pop(name, None) is not None


def registered_ocr_backends() -> tuple[str, ...]:
    """返回已注册后端名称（按注册顺序）。"""
    return tuple(_REGISTRY)


def available_ocr_backends() -> list[Any]:
    """返回当前**确实可用**的后端列表（按注册顺序）。"""
    return [backend for backend in _REGISTRY.values() if _safe_available(backend)]


def get_ocr_backend(name: str | None = None) -> Any:
    """取一个 OCR 后端。

    * ``name`` 为空 → 返回**第一个可用的**已注册后端；
    * 都不存在/都不可用 → 返回 :class:`NullOcrBackend`（**绝不返回 ``None``、绝不抛异常**）；
    * ``name`` 指定但未注册 → 记警告并回退到"第一个可用或 Null"，
      这样界面传了过期后端名也不会崩。
    """
    if name:
        backend = _REGISTRY.get(name)
        if backend is None:
            logger.warning("请求的 OCR 后端「%s」未注册，回退到默认选择。", name)
        elif _safe_available(backend):
            return backend
        else:
            logger.info("OCR 后端「%s」当前不可用，回退到其它可用后端。", name)
    for backend in _REGISTRY.values():
        if _safe_available(backend):
            return backend
    return NullOcrBackend()


def ocr_availability_notice() -> str:
    """返回可直接写进中文提示的 OCR 可用性说明。

    供 :mod:`cenep.data.pdf_failure_modes` 在"疑似扫描件"文案里使用。
    """
    usable = available_ocr_backends()
    if not usable:
        return NullOcrBackend().unavailable_reason()
    names = "、".join(f"{backend.name}" for backend in usable)
    languages: set[str] = set()
    for backend in usable:
        try:
            languages.update(backend.supported_languages())
        except Exception:  # noqa: BLE001 - 语言探测失败不影响提示
            continue
    language_text = f"（语言：{'、'.join(sorted(languages))}）" if languages else ""
    return (
        f"检测到可用 OCR 后端：{names}{language_text}。"
        "如需识别本扫描件，请在导入时显式启用 OCR（本版本默认不自动启用）。"
    )


# --------------------------------------------------------------------------- #
# 对外主流程
# --------------------------------------------------------------------------- #
def ocr_pdf(
    path: str | Path,
    *,
    backend: Any = None,
    dpi: int = DEFAULT_DPI,
    max_pages: int | None = None,
) -> OcrDocumentResult:
    """对 PDF 逐页做 OCR，返回 :class:`OcrDocumentResult`（**永不抛异常**）。

    参数
    ----
    path:
        PDF 路径。
    backend:
        后端实例或后端名称；``None`` 时用 :func:`get_ocr_backend` 的默认选择
        （**默认环境下即"未启用"**，直接返回中文提示）。
    dpi:
        页面渲染分辨率。
    max_pages:
        最多识别多少页（``None`` 表示全部，受预检页数约束）。

    行为
    ----
    1. 先用 :func:`cenep.data.pdf_failure_modes.probe_pdf` 预检：
       加密/损坏/非 PDF/不是扫描件 → 直接返回对应中文说明，**不会去调 OCR**；
    2. 后端不可用 → 返回 :attr:`OcrFailureCode.BACKEND_MISSING` /
       :attr:`OcrFailureCode.BACKEND_UNAVAILABLE` + 中文提示；
    3. 逐页渲染 + 识别，把每页结果收进 :attr:`OcrDocumentResult.pages`。
    """
    from .pdf_failure_modes import PdfFailureCode, probe_pdf

    target = Path(path)
    selected = backend if backend is not None else get_ocr_backend()
    if isinstance(selected, str):
        selected = get_ocr_backend(selected)
    backend_name = str(getattr(selected, "name", type(selected).__name__))

    probe = probe_pdf(target)
    if not probe.ok and probe.failure is not None:
        if probe.code in (
            PdfFailureCode.SCANNED_NO_TEXT,
            PdfFailureCode.TEXT_LAYER_TOO_SPARSE,
        ):
            pass  # 正是需要 OCR 的两类，继续
        elif probe.code in (PdfFailureCode.NOT_A_BILL, PdfFailureCode.ABNORMAL_PAGE_COUNT):
            pass  # 文本层可用时不走 OCR；仍继续是为了让调用方拿到一致提示
        else:
            return OcrDocumentResult(
                path=str(target),
                backend=backend_name,
                ok=False,
                code=OcrFailureCode.NOT_APPLICABLE,
                message=(
                    f"该文件不适合做 OCR：{probe.failure.summary}"
                    f"处理建议：{probe.failure.action}"
                ),
            )

    if probe.text_chars > 0 and probe.chars_per_page >= 20:
        return OcrDocumentResult(
            path=str(target),
            backend=backend_name,
            ok=False,
            code=OcrFailureCode.NOT_APPLICABLE,
            message=(
                f"「{probe.file_name}」已含可用文本层（{probe.page_count} 页、"
                f"{probe.text_chars} 字），无需 OCR；请直接走 PDF 文本解析。"
            ),
        )

    if not _safe_available(selected):
        reason = ""
        try:
            reason = selected.unavailable_reason()
        except Exception:  # noqa: BLE001
            reason = ""
        code = (
            OcrFailureCode.BACKEND_MISSING
            if backend_name == "null"
            else OcrFailureCode.BACKEND_UNAVAILABLE
        )
        return OcrDocumentResult(
            path=str(target),
            backend=backend_name,
            ok=False,
            code=code,
            message=reason or "当前没有可用的 OCR 后端，无法识别扫描件。",
        )

    pymupdf = _import_pymupdf_for_render()
    if pymupdf is None:  # pragma: no cover - 取决于运行环境
        return OcrDocumentResult(
            path=str(target),
            backend=backend_name,
            ok=False,
            code=OcrFailureCode.RENDER_FAILED,
            message="渲染 PDF 页面需要 PyMuPDF（pip install pymupdf）。",
        )

    try:
        document = pymupdf.open(str(target))
    except Exception as exc:  # noqa: BLE001
        return OcrDocumentResult(
            path=str(target),
            backend=backend_name,
            ok=False,
            code=OcrFailureCode.RENDER_FAILED,
            message=f"无法打开 PDF 以进行 OCR：{type(exc).__name__}: {exc}。",
        )

    pages: list[OcrPageResult] = []
    try:
        total = int(document.page_count)
        limit = total if max_pages is None else min(total, max(0, int(max_pages)))
        for index in range(limit):
            try:
                page = document.load_page(index)
            except Exception as exc:  # noqa: BLE001
                pages.append(
                    OcrPageResult(
                        page_number=index + 1,
                        ok=False,
                        code=OcrFailureCode.RENDER_FAILED,
                        message=f"第 {index + 1} 页无法加载：{type(exc).__name__}: {exc}。",
                        backend=backend_name,
                    )
                )
                continue
            pages.append(_recognize_page(selected, page, dpi=dpi))
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001 - pragma: no cover
            logger.debug("关闭 PDF 文档时出现异常（已忽略）", exc_info=True)

    recognized = sum(1 for page in pages if page.ok)
    ok = recognized > 0
    if ok:
        message = f"OCR 完成：{recognized}/{len(pages)} 页识别成功（后端 {backend_name}）。"
        code = OcrFailureCode.OK
    else:
        first = next((page for page in pages if not page.ok), None)
        code = first.code if first is not None else OcrFailureCode.EMPTY_RESULT
        message = (
            first.message if first is not None else "OCR 未识别出任何文字。"
        )
    result = OcrDocumentResult(
        path=str(target),
        backend=backend_name,
        ok=ok,
        code=code,
        message=message,
        pages=pages,
    )
    logger.info(
        "PDF OCR：%s，后端 %s，%d/%d 页成功",
        target.name,
        backend_name,
        recognized,
        len(pages),
    )
    return result


def _import_pymupdf_for_render():  # noqa: ANN202 - 返回模块对象
    """延迟导入 PyMuPDF（仅渲染时使用）。"""
    try:
        import pymupdf  # type: ignore[import-not-found]

        return pymupdf
    except ImportError:  # pragma: no cover - 取决于运行环境
        try:
            import fitz  # type: ignore[import-not-found]

            return fitz
        except ImportError:
            return None


#: 注册内置后端：真实后端排在前（可用时优先），``null`` 永远最后（兜底）
def _register_builtin_backends() -> None:
    """注册内置后端（幂等；导入本模块时执行一次）。"""
    for backend in (
        PyMuPdfOcrBackend(),
        TesseractCliOcrBackend(),
        NullOcrBackend(),
    ):
        register_ocr_backend(backend, replace=True)


_register_builtin_backends()
