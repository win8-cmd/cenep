"""V2.5 缺口②验收测试：**可插拔 OCR 接口与优雅降级**。

验收要点（规范原文："必需的 OCR 接口"）：

* 接口清晰：抽象基类 + 明确的中文文档；输入（图片字节流 / 页面）与输出
  （文本 **或** 字词框）与失败语义都有定义；
* **默认不启用**：没有安装 OCR 后端时必须返回**可解释的中文提示**，而不是 ``ImportError``；
* **不新增重量级依赖**：可选后端惰性探测外部程序，缺失即降级；
* 至少一个**桩实现**证明接口可插拔（桩实现**不做真实识别**，不得用于生产）。

本机（Python 3.12 / PyMuPDF 1.28）未安装 Tesseract，因此**真实 OCR 能力在本环境不可用**，
相关用例只验证"降级行为正确"，并在报告里如实记录为遗留/阻塞项。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="构造扫描件样本需要 PyMuPDF")

from cenep.data.pdf_ocr import (  # noqa: E402
    NullOcrBackend,
    OcrBackend,
    OcrFailureCode,
    OcrPageResult,
    OcrWord,
    PyMuPdfOcrBackend,
    StubOcrBackend,
    TesseractCliOcrBackend,
    _parse_tesseract_tsv,
    available_ocr_backends,
    get_ocr_backend,
    ocr_availability_notice,
    ocr_pdf,
    register_ocr_backend,
    registered_ocr_backends,
    unregister_ocr_backend,
)

REAL_BILL_DIR = Path(
    r"C:\Users\Administrator\Desktop\参考资料\东风本田\开标资料\式样附件-三工厂"
    r"\式样书给出的相关文件\电费单\25年电费"
)
REAL_BILL = REAL_BILL_DIR / "本三9月账单.pdf"


def _has_chinese(text: str) -> bool:
    """文本是否含中文字符。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _scanned_pdf(path: Path, pages: int = 2) -> Path:
    """扫描件仿真：每页一张位图、无文本对象。"""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 400))
        pixmap.set_rect(pixmap.irect, (240, 240, 240))
        page.insert_image(page.rect, pixmap=pixmap)
    doc.save(str(path))
    doc.close()
    return path


def _text_pdf(path: Path) -> Path:
    """含文本层的 PDF（用于"无需 OCR"分支）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72), "电费账单 户号 4206851784045 本期电量 8780498", fontname="china-s"
    )
    doc.save(str(path))
    doc.close()
    return path


def _encrypted_pdf(path: Path) -> Path:
    """加密 PDF（用于"不适合 OCR"分支）。"""
    doc = pymupdf.open()
    doc.new_page()
    doc.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="o",
        user_pw="u",
        permissions=0,
    )
    doc.close()
    return path


@pytest.fixture
def isolated_registry():
    """在用例内注册/注销后端，结束后恢复注册表，避免污染其它用例。"""
    before = registered_ocr_backends()
    yield
    for name in registered_ocr_backends():
        if name not in before:
            unregister_ocr_backend(name)


# --------------------------------------------------------------------------- #
# 1. 接口形状：抽象基类 + 明确的方法集
# --------------------------------------------------------------------------- #
def test_ocr_backend_is_abstract_and_documents_contract() -> None:
    """抽象基类不可直接实例化，且方法集与中文文档齐备。"""
    with pytest.raises(TypeError):
        OcrBackend()  # type: ignore[abstract]

    for attribute in ("is_available", "unavailable_reason", "recognize_image", "recognize_page"):
        assert callable(getattr(OcrBackend, attribute))
    assert OcrBackend.__doc__ and _has_chinese(OcrBackend.__doc__)
    assert OcrBackend.recognize_image.__doc__ and _has_chinese(OcrBackend.recognize_image.__doc__)
    assert OcrBackend.recognize_page.__doc__ and _has_chinese(OcrBackend.recognize_page.__doc__)


def test_ocr_word_and_page_result_shape() -> None:
    """输出契约：字词框含文本与像素坐标（置信度可为 None，不臆造）；页面结果含文本。"""
    word = OcrWord(text="电费", x0=1.0, y0=2.0, x1=11.0, y1=12.0, confidence=0.93)
    assert word.to_dict()["text"] == "电费"
    assert word.to_dict()["confidence"] == 0.93

    page = OcrPageResult(
        page_number=1,
        ok=True,
        code=OcrFailureCode.OK,
        message="识别成功",
        backend="stub",
        words=[word],
        text="电费",
    )
    assert page.word_count == 1
    assert page.to_dict()["word_count"] == 1


# --------------------------------------------------------------------------- #
# 2. 默认不启用：中文降级，不抛 ImportError
# --------------------------------------------------------------------------- #
def test_null_backend_is_unavailable_and_speaks_chinese() -> None:
    """``NullOcrBackend`` 恒不可用，且失败语义是可解释的中文，而不是异常。"""
    backend = NullOcrBackend()

    assert backend.is_available() is False
    assert _has_chinese(backend.unavailable_reason())
    assert "本版本未启用 OCR" in backend.unavailable_reason()

    result = backend.recognize_image(b"\x89PNG\r\n\x1a\n")
    assert isinstance(result, OcrPageResult)
    assert result.ok is False
    assert result.code is OcrFailureCode.BACKEND_MISSING
    assert _has_chinese(result.message)


def test_get_ocr_backend_never_returns_none(isolated_registry) -> None:
    """取后端要么给可用的真实后端，要么给 Null 兜底；指定不存在的名字也不崩。"""
    backend = get_ocr_backend()
    assert backend is not None
    if not available_ocr_backends():
        assert isinstance(backend, NullOcrBackend)
    assert get_ocr_backend("根本不存在的后端") is not None


def test_ocr_pdf_degrades_gracefully_without_backend(tmp_path: Path) -> None:
    """**核心用例**：扫描件 + 无可用后端 → 中文提示，不抛 ImportError。"""
    path = _scanned_pdf(tmp_path / "scanned.pdf")

    result = ocr_pdf(path, backend=NullOcrBackend())

    assert result.ok is False
    assert result.code is OcrFailureCode.BACKEND_MISSING
    assert _has_chinese(result.message)
    assert "OCR" in result.message
    # 没有可用后端时不应白白渲染页面
    assert result.pages == []


def test_ocr_pdf_default_backend_on_scanned_pdf(tmp_path: Path) -> None:
    """默认调用路径：有后端就跑，没后端就给中文提示（不依赖本机是否装了 Tesseract）。"""
    path = _scanned_pdf(tmp_path / "scanned2.pdf")

    result = ocr_pdf(path)

    if available_ocr_backends():
        assert result.backend != "null"
    else:
        assert result.ok is False
        assert result.code in (
            OcrFailureCode.BACKEND_MISSING,
            OcrFailureCode.BACKEND_UNAVAILABLE,
        )
        assert _has_chinese(result.message)


def test_ocr_availability_notice_is_chinese() -> None:
    """可用性提示必须是中文，供扫描件失败文案直接引用。"""
    notice = ocr_availability_notice()

    assert _has_chinese(notice)
    if not available_ocr_backends():
        assert "未启用 OCR" in notice or "未检测到" in notice
    else:
        assert "检测到可用 OCR 后端" in notice


# --------------------------------------------------------------------------- #
# 3. 可插拔：桩实现与鸭子类型后端
# --------------------------------------------------------------------------- #
def test_stub_backend_is_pluggable_through_registry(tmp_path: Path, isolated_registry) -> None:
    """桩实现注册后可被 OCR 主流程调用，产出文本与字词框（证明接口可插拔）。"""
    path = _scanned_pdf(tmp_path / "scanned3.pdf", pages=2)
    stub = StubOcrBackend(["电费账单 户号 4206851784045", "本期电量 8780498 千瓦时"])
    register_ocr_backend(stub, replace=True)

    assert "stub" in registered_ocr_backends()
    assert get_ocr_backend("stub") is stub

    result = ocr_pdf(path, backend="stub")

    assert result.ok is True
    assert result.backend == "stub"
    assert result.page_count == 2
    assert result.recognized_pages == 2
    assert "8780498" in result.text
    assert result.words and all(word.text for word in result.words)
    assert stub.calls == 2


def test_stub_backend_reports_unavailable_when_configured_so(tmp_path: Path) -> None:
    """桩实现可被配置为"不可用"，用来覆盖后端已注册但不可用的分支。"""
    path = _scanned_pdf(tmp_path / "scanned4.pdf")
    stub = StubOcrBackend(["x"], available=False, reason="桩后端被配置为不可用（测试用）。")

    result = ocr_pdf(path, backend=stub)

    assert result.ok is False
    assert result.code is OcrFailureCode.BACKEND_UNAVAILABLE
    assert "桩后端被配置为不可用" in result.message


def test_duck_typed_backend_can_be_registered(tmp_path: Path, isolated_registry) -> None:
    """不继承 ABC 的鸭子类型对象也能注册（接口靠方法契约，不靠继承）。"""

    class DuckBackend:
        """只提供必需方法的最小后端。"""

        name = "duck"

        def is_available(self) -> bool:
            """恒可用。"""
            return True

        def unavailable_reason(self) -> str:
            """可用时返回空串。"""
            return ""

        def recognize_image(self, image: bytes, *, page_number: int = 1, **kwargs) -> OcrPageResult:
            """返回一个固定词块。"""
            return OcrPageResult(
                page_number=page_number,
                ok=True,
                code=OcrFailureCode.OK,
                message="鸭子类型后端返回成功。",
                backend=self.name,
                words=[OcrWord("测试", 0, 0, 1, 1)],
                text="测试",
            )

    register_ocr_backend(DuckBackend(), replace=True)

    assert get_ocr_backend("duck").name == "duck"
    result = ocr_pdf(_scanned_pdf(tmp_path / "duck.pdf", pages=1), backend="duck")
    assert result.ok is True
    assert result.text == "测试"
    assert result.pages[0].words[0].text == "测试"


def test_register_rejects_backend_without_required_members(isolated_registry) -> None:
    """注册缺成员的对象的报错必须是中文 ``ValueError``。"""

    class Bad:
        """缺少 is_available / recognize_image。"""

        name = "bad"

    with pytest.raises(ValueError) as excinfo:
        register_ocr_backend(Bad())
    assert _has_chinese(str(excinfo.value))
    assert "bad" in str(excinfo.value)

    class NoName:
        """缺少 name。"""

    with pytest.raises(ValueError) as excinfo2:
        register_ocr_backend(NoName())
    assert _has_chinese(str(excinfo2.value))


def test_register_duplicate_keeps_existing(isolated_registry) -> None:
    """同名重复注册默认不覆盖（避免插件把可用后端顶掉）。"""
    first = StubOcrBackend(["第一个"])
    second = StubOcrBackend(["第二个"])
    register_ocr_backend(first, replace=True)
    register_ocr_backend(second, replace=False)

    assert get_ocr_backend("stub") is first
    register_ocr_backend(second, replace=True)
    assert get_ocr_backend("stub") is second
    assert unregister_ocr_backend("stub") is True
    assert unregister_ocr_backend("stub") is False


# --------------------------------------------------------------------------- #
# 4. 可选真实后端：惰性探测 + 缺失即降级（不引入 Python 依赖）
# --------------------------------------------------------------------------- #
def test_tesseract_backend_unavailable_reason_is_chinese() -> None:
    """找不到可执行文件时，原因必须是中文且给出安装指引。"""
    backend = TesseractCliOcrBackend(which=lambda _name: None)

    assert backend.is_available() is False
    reason = backend.unavailable_reason()
    assert _has_chinese(reason)
    assert "未检测到" in reason
    assert "chi_sim" in reason


def test_tesseract_backend_recognize_degrades_without_binary() -> None:
    """可执行文件缺失时识别直接返回中文失败结果，**不会去启动进程**。"""
    backend = TesseractCliOcrBackend(which=lambda _name: None)

    result = backend.recognize_image(b"\x89PNG\r\n\x1a\n", page_number=3)

    assert result.ok is False
    assert result.code is OcrFailureCode.BACKEND_UNAVAILABLE
    assert result.page_number == 3
    assert _has_chinese(result.message)


def test_tesseract_backend_detects_binary_by_injection() -> None:
    """注入 ``which`` 可模拟"已安装"，证明探测逻辑本身可用（不依赖本机是否真装了）。"""
    backend = TesseractCliOcrBackend(which=lambda _name: r"C:\fake\tesseract.exe")

    assert backend.is_available() is True
    assert backend.unavailable_reason() == ""
    assert backend.supported_languages() == ("chi_sim", "eng")


def test_pymupdf_backend_unavailable_without_tesseract() -> None:
    """PyMuPDF OCR 通道同样依赖外部 Tesseract；缺失时必须给出中文原因。"""
    backend = PyMuPdfOcrBackend(which=lambda _name: None)

    assert backend.is_available() is False
    assert _has_chinese(backend.unavailable_reason())
    assert backend.name == "pymupdf-tesseract"


def test_tesseract_tsv_parsing_builds_words_and_lines() -> None:
    """TSV 输出解析：level==5 的行才是词，按 (block, par, line) 分行拼文本。"""
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth"
            "\theight\tconf\ttext",
            "1\t1\t0\t0\t0\t0\t0\t0\t300\t400\t-1\t",
            "5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t96.5\t电费",
            "5\t1\t1\t1\t1\t2\t45\t20\t30\t10\t88.0\t账单",
            "5\t1\t1\t1\t2\t1\t10\t40\t30\t10\t91.2\t户号",
            "5\t1\t1\t1\t2\t2\t45\t40\t30\t10\t-1\t4206851784045",
        ]
    )

    words, text = _parse_tesseract_tsv(tsv)

    assert [word.text for word in words] == ["电费", "账单", "户号", "4206851784045"]
    assert text.splitlines() == ["电费 账单", "户号 4206851784045"]
    assert words[0].confidence == 0.965
    assert words[3].confidence is None  # conf = -1 → 不臆造置信度
    assert (words[0].x0, words[0].y0, words[0].x1, words[0].y1) == (10.0, 20.0, 40.0, 30.0)
    assert _parse_tesseract_tsv("") == ([], "")


# --------------------------------------------------------------------------- #
# 5. 不该走 OCR 的情形：必须有中文说明
# --------------------------------------------------------------------------- #
def test_ocr_pdf_skips_pdf_with_text_layer(tmp_path: Path) -> None:
    """已含文本层 → NOT_APPLICABLE，并提示直接走文本解析。"""
    path = _text_pdf(tmp_path / "text.pdf")

    result = ocr_pdf(path, backend=StubOcrBackend(["不应被调用"]))

    assert result.ok is False
    assert result.code is OcrFailureCode.NOT_APPLICABLE
    assert "已含可用文本层" in result.message
    assert "无需 OCR" in result.message


def test_ocr_pdf_reports_encrypted_pdf_as_not_applicable(tmp_path: Path) -> None:
    """加密 PDF 不能直接 OCR（需先解密），提示必须是中文。"""
    result = ocr_pdf(_encrypted_pdf(tmp_path / "enc.pdf"), backend=StubOcrBackend(["x"]))

    assert result.code is OcrFailureCode.NOT_APPLICABLE
    assert _has_chinese(result.message)


def test_ocr_pdf_reports_missing_file_as_not_applicable(tmp_path: Path) -> None:
    """文件不存在时给中文提示，而不是抛 FileNotFoundError。"""
    result = ocr_pdf(tmp_path / "不存在.pdf", backend=StubOcrBackend(["x"]))

    assert result.code is OcrFailureCode.NOT_APPLICABLE
    assert "不存在" in result.message


def test_ocr_pdf_respects_max_pages(tmp_path: Path) -> None:
    """``max_pages`` 限流生效（避免误对合订本做全量 OCR）。"""
    path = _scanned_pdf(tmp_path / "many.pdf", pages=3)

    result = ocr_pdf(path, backend=StubOcrBackend(["a", "b", "c"]), max_pages=1)

    assert result.page_count == 1
    assert result.recognized_pages == 1


def test_ocr_document_result_json_serializable(tmp_path: Path) -> None:
    """整份文档结果可 JSON 序列化（供报告与界面）。"""
    import json

    result = ocr_pdf(_scanned_pdf(tmp_path / "s.pdf"), backend=StubOcrBackend(["某文本"]))
    payload = json.dumps(result.to_dict(), ensure_ascii=False)

    assert "ocr.ok" in payload
    assert "某文本" in payload


def test_real_bill_has_text_layer_so_ocr_is_not_used() -> None:
    """真实账单有文本层：OCR 必须拒绝介入（避免把好数据送去做识别）。"""
    if not REAL_BILL.exists():
        pytest.skip(f"真实账单样本缺失（业务资料不入仓库）：{REAL_BILL}")

    result = ocr_pdf(REAL_BILL, backend=StubOcrBackend(["不应被调用"]))

    assert result.code is OcrFailureCode.NOT_APPLICABLE
    assert result.ok is False
