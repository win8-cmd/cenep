"""V2.5 缺口①验收测试：**PDF 失败模式与可解释的中文失败状态**。

覆盖规范点名的每一类失败：

* 损坏 / 非 PDF（文件头不对、无法打开）
* 受保护 / 加密（需要密码）
* 扫描版 / 无文本层（有页但 ``get_text`` 几乎为空）
* 空白页 / 页数异常
* 能打开但不是电费账单（缺关键字段）

样本**全部在本测试内用 PyMuPDF 真实构造**（不提交二进制夹具、不伪造真实账单），
并对每类断言：① 失败分类码；② **中文**文案存在且可操作；③ 不抛裸异常。

真实账单（9 月 / 10 月）只做"应当通过预检"的正向断言，
文件缺失时 skip 并给出中文原因（不伪造样本）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="构造 PDF 失败样本需要 PyMuPDF")

from cenep.calculation.errors import ValidationError  # noqa: E402
from cenep.data.pdf_failure_modes import (  # noqa: E402
    BILL_FEATURE_TOKENS,
    MAX_EXPECTED_PAGES,
    MIN_BILL_TOKEN_HITS,
    PdfFailureCode,
    PdfProbe,
    bill_feature_hits,
    describe_probe,
    failure_to_extras,
    looks_like_pdf_header,
    probe_bill_pdf,
    probe_pdf,
    raise_for_bill_pdf,
    read_file_header,
    validation_error_of,
)

# 真实账单目录（只读；缺失时相关用例 skip）
REAL_BILL_DIR = Path(
    r"C:\Users\Administrator\Desktop\参考资料\东风本田\开标资料\式样附件-三工厂"
    r"\式样书给出的相关文件\电费单\25年电费"
)
REAL_BILLS = {
    "2025-09": REAL_BILL_DIR / "本三9月账单.pdf",
    "2025-10": REAL_BILL_DIR / "东本三厂10月账单.pdf",
}

CJK = r"[\u4e00-\u9fff]"


def _has_chinese(text: str) -> bool:
    """文本是否含中文字符。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


# --------------------------------------------------------------------------- #
# 样本构造（真实写入 PDF 字节，不是 mock）
# --------------------------------------------------------------------------- #
def _blank_pdf(path: Path, pages: int = 3) -> Path:
    """空白 PDF：有页、无文本、无位图。"""
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return path


def _image_only_pdf(path: Path, pages: int = 2) -> Path:
    """扫描件仿真：每页一张位图，无任何文本对象。"""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 500))
        pixmap.set_rect(pixmap.irect, (250, 250, 250))
        page.insert_image(page.rect, pixmap=pixmap)
    doc.save(str(path))
    doc.close()
    return path


def _encrypted_pdf(path: Path) -> Path:
    """AES-256 加密 PDF（需用户口令）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "电费账单 户号 4206851784045 本期电量 8780498", fontname="china-s")
    doc.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
        permissions=0,
    )
    doc.close()
    return path


def _text_pdf(path: Path, lines: list[str], *, chinese: bool = False, pages: int = 1) -> Path:
    """写入指定文本行的 PDF（用于"有文本但不是账单"/"文本层过稀"）。"""
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        for row, line in enumerate(lines):
            kwargs = {"fontname": "china-s"} if chinese else {}
            page.insert_text((72, 72 + row * 14), line, fontsize=10, **kwargs)
        if index == 0:
            continue
    doc.save(str(path))
    doc.close()
    return path


def _bill_like_pdf(path: Path) -> Path:
    """含电费账单特征词的 PDF（用于证明"像账单"时不会误判为失败）。"""
    return _text_pdf(
        path,
        [
            "国网湖北省电力公司 电费账单",
            "户号：4206851784045 户名：某某公司",
            "本期电量 8780498 千瓦时 本期电费 6699241.94 元",
            "电能表编号：4230000100027673777 电价：用电户-110千伏-大工业用电",
        ],
        chinese=True,
    )


def _bill_like_many_pages(path: Path, pages: int = MAX_EXPECTED_PAGES + 1) -> Path:
    """页数超过阈值、但含账单特征的 PDF（用于页数异常用例）。"""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), "电费账单 户号 4206851784045 本期电量 1234", fontname="china-s")
    doc.save(str(path))
    doc.close()
    return path


# --------------------------------------------------------------------------- #
# 1. 损坏 / 非 PDF
# --------------------------------------------------------------------------- #
def test_garbage_bytes_is_not_a_pdf(tmp_path: Path) -> None:
    """无 %PDF 文件头的垃圾字节 → NOT_A_PDF，且给出中文可操作建议。"""
    path = tmp_path / "garbage.pdf"
    path.write_bytes(b"this is definitely not a pdf file at all" * 8)

    probe = probe_bill_pdf(path)

    assert probe.code is PdfFailureCode.NOT_A_PDF
    assert probe.failure is not None
    assert "不是 PDF" in probe.failure.summary
    assert _has_chinese(probe.failure.action)
    assert probe.failure.blocking is True
    assert probe.detected_format == "纯文本文件"
    # 不得裸抛异常
    assert isinstance(probe, PdfProbe)


def test_office_file_misnamed_as_pdf(tmp_path: Path) -> None:
    """把 ZIP/Office 文件改名为 .pdf → NOT_A_PDF，并提示"这其实是 Office 文档"。"""
    path = tmp_path / "电费单.pdf"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 128)

    probe = probe_bill_pdf(path)

    assert probe.code is PdfFailureCode.NOT_A_PDF
    assert probe.detected_format is not None and "ZIP" in probe.detected_format
    assert "另存为 PDF" in probe.failure.action


def test_truncated_pdf_is_corrupted_not_non_pdf(tmp_path: Path) -> None:
    """正常 PDF 截断到 40% → 文件头仍是 %PDF，但打不开 → CORRUPTED（与 NOT_A_PDF 区分）。"""
    source = _bill_like_pdf(tmp_path / "source.pdf")
    raw = source.read_bytes()
    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(raw[: max(1, int(len(raw) * 0.4))])

    probe = probe_bill_pdf(truncated)

    assert probe.has_pdf_header is True
    assert probe.code is PdfFailureCode.CORRUPTED
    assert "损坏" in probe.failure.summary or "不完整" in probe.failure.summary
    assert "重新下载" in probe.failure.action or "重新复制" in probe.failure.action


def test_fake_pdf_header_with_zero_body_is_corrupted(tmp_path: Path) -> None:
    """只有 %PDF-1.7 文件头、正文全 0 → CORRUPTED（不得误判成"不是 PDF"）。"""
    path = tmp_path / "fake_magic.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"\x00" * 256)

    probe = probe_bill_pdf(path)

    assert probe.has_pdf_header is True
    assert probe.code is PdfFailureCode.CORRUPTED
    assert probe.internal_error  # 保留底层异常文本以便排障
    assert _has_chinese(probe.failure.detail)


def test_empty_file_and_missing_file(tmp_path: Path) -> None:
    """0 字节文件 → EMPTY_FILE；路径不存在 → FILE_MISSING。两者文案不同。"""
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    empty_probe = probe_bill_pdf(empty)
    assert empty_probe.code is PdfFailureCode.EMPTY_FILE
    assert "0 字节" in empty_probe.failure.summary

    missing_probe = probe_bill_pdf(tmp_path / "根本没有这个文件.pdf")
    assert missing_probe.code is PdfFailureCode.FILE_MISSING
    assert "不存在" in missing_probe.failure.summary

    # 目录路径也不能崩
    directory_probe = probe_bill_pdf(tmp_path)
    assert directory_probe.code is PdfFailureCode.FILE_MISSING


# --------------------------------------------------------------------------- #
# 2. 受保护 / 加密
# --------------------------------------------------------------------------- #
def test_encrypted_pdf_reports_password_required(tmp_path: Path) -> None:
    """AES-256 加密 PDF → ENCRYPTED，文案要提到"口令/密码"，且不崩在页面迭代上。"""
    path = _encrypted_pdf(tmp_path / "encrypted.pdf")

    probe = probe_bill_pdf(path)

    assert probe.opened is True
    assert probe.needs_password is True or probe.is_encrypted is True
    assert probe.code is PdfFailureCode.ENCRYPTED
    assert "口令" in probe.failure.summary or "密码" in probe.failure.summary
    assert "不保存任何账单口令" in probe.failure.action
    # 逐页统计在加密文档上会抛异常，必须被吞掉而不是冒出来
    assert probe.page_text_chars == [0] * probe.page_count
    assert probe.text_chars == 0


# --------------------------------------------------------------------------- #
# 3. 扫描版 / 无文本层 与 空白页
# --------------------------------------------------------------------------- #
def test_image_only_pdf_is_scanned_and_mentions_ocr(tmp_path: Path) -> None:
    """每页只有位图、无文本 → SCANNED_NO_TEXT，文案必须点明"疑似扫描件"与"需要 OCR"。"""
    path = _image_only_pdf(tmp_path / "scanned.pdf")

    probe = probe_bill_pdf(path)

    assert probe.code is PdfFailureCode.SCANNED_NO_TEXT
    assert probe.page_count == 2
    assert probe.text_chars == 0
    assert probe.image_count == 2
    assert "疑似扫描件" in probe.failure.summary
    assert "OCR" in probe.failure.action
    assert probe.ocr_hint is not None and _has_chinese(probe.ocr_hint)


def test_blank_pdf_is_blank_pages_not_scanned(tmp_path: Path) -> None:
    """有页、无文本、**也无位图** → BLANK_PAGES（与扫描件区分开的依据就是"有没有图"）。"""
    path = _blank_pdf(tmp_path / "blank.pdf", pages=3)

    probe = probe_bill_pdf(path)

    assert probe.code is PdfFailureCode.BLANK_PAGES
    assert probe.page_count == 3
    assert probe.image_count == 0
    assert "空白" in probe.failure.summary
    assert probe.ocr_hint is None  # 空白页不需要 OCR


def test_sparse_text_layer_is_reported(tmp_path: Path) -> None:
    """文本层只有 1 个字 → TEXT_LAYER_TOO_SPARSE（不是"账单缺字段"，是"根本没有文本层"）。"""
    path = _text_pdf(tmp_path / "sparse.pdf", ["会"], chinese=True)

    probe = probe_bill_pdf(path)

    assert probe.code is PdfFailureCode.TEXT_LAYER_TOO_SPARSE
    assert "文本层几乎为空" in probe.failure.summary
    assert probe.ocr_hint is not None


def test_abnormal_page_count_is_non_blocking(tmp_path: Path) -> None:
    """页数远超账单正常范围 → ABNORMAL_PAGE_COUNT，且**不阻断**（只提示确认选错文件）。"""
    path = _bill_like_many_pages(tmp_path / "many.pdf")

    probe = probe_bill_pdf(path)

    assert probe.page_count > MAX_EXPECTED_PAGES
    assert probe.code is PdfFailureCode.ABNORMAL_PAGE_COUNT
    assert probe.failure.blocking is False
    assert "选错了文件" in probe.failure.summary


# --------------------------------------------------------------------------- #
# 4. 能打开但不是电费账单
# --------------------------------------------------------------------------- #
def test_english_text_pdf_is_not_a_bill(tmp_path: Path) -> None:
    """纯英文文本 PDF → NOT_A_BILL，并写明"未识别到账单特征"及命中数。"""
    path = _text_pdf(
        tmp_path / "english.pdf",
        [f"This is line {index} about the weather and nothing else." for index in range(20)],
        pages=2,
    )

    probe = probe_bill_pdf(path)

    assert probe.code is PdfFailureCode.NOT_A_BILL
    assert probe.text_chars > 0
    assert "未识别到电费账单特征" in probe.failure.summary
    assert str(MIN_BILL_TOKEN_HITS) in probe.failure.detail
    assert probe.bill_traits_hit == []


def test_chinese_non_bill_pdf_is_not_a_bill(tmp_path: Path) -> None:
    """中文但非账单（光伏方案说明）→ NOT_A_BILL，不能因为"有中文"就放行。"""
    path = _text_pdf(
        tmp_path / "chinese_other.pdf",
        [
            "光伏组件选型与支架方案说明",
            "本报告说明屋面荷载核算与组件排布原则。",
            "逆变器效率按百分之九十八取值。",
        ],
        chinese=True,
    )

    probe = probe_bill_pdf(path)

    assert probe.code is PdfFailureCode.NOT_A_BILL
    assert len(probe.bill_traits_hit) < MIN_BILL_TOKEN_HITS


def test_bill_like_pdf_passes_probe(tmp_path: Path) -> None:
    """含账单特征词的 PDF → 预检通过（证明分类器不会把正常账单判成失败）。"""
    path = _bill_like_pdf(tmp_path / "bill_like.pdf")

    probe = probe_bill_pdf(path)

    assert probe.ok is True
    assert probe.failure is None
    assert probe.code is PdfFailureCode.OK
    assert len(probe.bill_traits_hit) >= MIN_BILL_TOKEN_HITS
    assert "电费" in probe.bill_traits_hit


# --------------------------------------------------------------------------- #
# 5. 错误码 → ValidationError 的映射与"绝不裸抛"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (lambda root: root / "missing.pdf", PdfFailureCode.FILE_MISSING),
        (lambda root: _write(root / "empty.pdf", b""), PdfFailureCode.EMPTY_FILE),
        (lambda root: _write(root / "junk.pdf", b"junk" * 32), PdfFailureCode.NOT_A_PDF),
        (lambda root: _blank_pdf(root / "blank.pdf"), PdfFailureCode.BLANK_PAGES),
        (lambda root: _image_only_pdf(root / "img.pdf"), PdfFailureCode.SCANNED_NO_TEXT),
        (lambda root: _encrypted_pdf(root / "enc.pdf"), PdfFailureCode.ENCRYPTED),
        (
            lambda root: _text_pdf(root / "other.pdf", ["hello world"] * 20, pages=2),
            PdfFailureCode.NOT_A_BILL,
        ),
    ],
)
def test_validation_error_carries_code_and_chinese(tmp_path: Path, builder, expected) -> None:
    """每类失败都能转成中文 ValidationError，``field`` 即错误码（供界面定位）。"""
    probe = probe_bill_pdf(builder(tmp_path))

    assert probe.code is expected, f"{expected} 未按预期分类，实得 {probe.code}"
    error = validation_error_of(probe)
    assert isinstance(error, ValidationError)
    assert error.field == expected.value
    assert _has_chinese(error.message)

    # raise_for_bill_pdf 对阻断性失败必须抛，且抛的是中文 ValidationError
    if probe.failure.blocking:
        with pytest.raises(ValidationError) as excinfo:
            raise_for_bill_pdf(Path(probe.path))
        assert excinfo.value.field == expected.value


def _write(path: Path, data: bytes) -> Path:
    """写字节的小工具（供上面的参数化表使用）。"""
    path.write_bytes(data)
    return path


def test_all_failure_codes_have_actionable_chinese_messages(tmp_path: Path) -> None:
    """**全量**检查：每个失败分类的 summary/detail/action 都是中文且可操作、不裸抛异常。"""
    samples = {
        PdfFailureCode.FILE_MISSING: tmp_path / "none.pdf",
        PdfFailureCode.EMPTY_FILE: _write(tmp_path / "e.pdf", b""),
        PdfFailureCode.NOT_A_PDF: _write(tmp_path / "n.pdf", b"not a pdf" * 20),
        PdfFailureCode.CORRUPTED: _write(tmp_path / "c.pdf", b"%PDF-1.7\n" + b"\x00" * 64),
        PdfFailureCode.ENCRYPTED: _encrypted_pdf(tmp_path / "enc2.pdf"),
        PdfFailureCode.BLANK_PAGES: _blank_pdf(tmp_path / "b.pdf"),
        PdfFailureCode.SCANNED_NO_TEXT: _image_only_pdf(tmp_path / "i.pdf"),
        PdfFailureCode.TEXT_LAYER_TOO_SPARSE: _text_pdf(tmp_path / "s.pdf", ["会"], chinese=True),
        PdfFailureCode.NOT_A_BILL: _text_pdf(
            tmp_path / "nb.pdf", ["plain english text"] * 12, pages=2
        ),
        PdfFailureCode.ABNORMAL_PAGE_COUNT: _bill_like_many_pages(tmp_path / "many2.pdf"),
    }
    covered = set()
    for expected_code, path in samples.items():
        probe = probe_bill_pdf(path)
        assert probe.code is expected_code, f"{expected_code} 未命中，实得 {probe.code}"
        failure = probe.failure
        assert failure is not None
        assert _has_chinese(failure.summary), f"{expected_code} 的 summary 不是中文"
        assert _has_chinese(failure.detail), f"{expected_code} 的 detail 不是中文"
        assert _has_chinese(failure.action), f"{expected_code} 的 action 不是中文"
        assert len(failure.action) >= 8, f"{expected_code} 的处理建议过于简略"
        assert failure.code.value.startswith("pdf.")
        covered.add(probe.code)

    # NO_PAGES 无法用 PyMuPDF 构造（它不允许保存 0 页文档），此处显式记录为未覆盖
    assert PdfFailureCode.NO_PAGES not in covered, (
        "若某天能构造出 0 页 PDF，请把它加入本用例的样本表"
    )
    missing = set(PdfFailureCode) - covered - {PdfFailureCode.OK, PdfFailureCode.NO_PAGES}
    assert not missing, f"以下失败分类没有被中文文案用例覆盖：{sorted(c.value for c in missing)}"


# --------------------------------------------------------------------------- #
# 6. 结果对象 / 报告 / 结构化输出
# --------------------------------------------------------------------------- #
def test_probe_to_dict_is_json_serializable(tmp_path: Path) -> None:
    """预检结果可 JSON 序列化（供报告与界面展示）。"""
    import json

    probe = probe_bill_pdf(_image_only_pdf(tmp_path / "img2.pdf"))
    payload = probe.to_dict()

    text = json.dumps(payload, ensure_ascii=False)
    assert "pdf.failure.scanned_no_text" in text
    assert payload["ok"] is False
    assert payload["failure"]["blocking"] is True
    assert payload["page_text_chars"] == [0, 0]


def test_failure_to_extras_exposes_code_and_ocr_flag(tmp_path: Path) -> None:
    """结构化账单事实里要有错误码、中文文案与"是否需要 OCR"标记。"""
    probe = probe_bill_pdf(_image_only_pdf(tmp_path / "img3.pdf"))

    extras = failure_to_extras(probe)

    assert extras["pdf_failure_code"] == PdfFailureCode.SCANNED_NO_TEXT.value
    assert _has_chinese(extras["pdf_failure_message"])
    assert extras["pdf_ocr_required"] is True
    assert extras["pdf_probe"]["code"] == PdfFailureCode.SCANNED_NO_TEXT.value

    ok_extras = failure_to_extras(probe_bill_pdf(_bill_like_pdf(tmp_path / "ok.pdf")))
    assert ok_extras["pdf_ocr_required"] is False
    assert "pdf_failure_code" not in ok_extras


def test_describe_probe_renders_chinese_report(tmp_path: Path) -> None:
    """多行中文报告要包含文件、页数、失败分类、原因与建议。"""
    probe = probe_bill_pdf(_encrypted_pdf(tmp_path / "enc3.pdf"))

    text = describe_probe(probe)

    assert "文件：" in text
    assert "状态：" in text
    assert "失败分类：pdf.failure.encrypted" in text
    assert "处理建议：" in text
    assert "是否阻断导入：是" in text


def test_probe_never_raises_on_arbitrary_input(tmp_path: Path) -> None:
    """任意输入（目录、空路径、随机字节）都必须返回结果对象，绝不抛异常。"""
    cases: list[Path] = [
        tmp_path,
        tmp_path / "no-such-file",
        _write(tmp_path / "random.bin", bytes(range(256)) * 4),
        _write(tmp_path / "one_byte.pdf", b"%"),
    ]
    for case in cases:
        probe = probe_pdf(case)
        assert isinstance(probe, PdfProbe)
        assert isinstance(probe.to_dict(), dict)


def test_read_file_header_and_header_detection(tmp_path: Path) -> None:
    """文件头读取与 %PDF- 判定（含前置垃圾字节的容错）。"""
    path = _write(tmp_path / "h.pdf", b"junk" + b"%PDF-1.7\n" + b"x" * 32)
    assert looks_like_pdf_header(read_file_header(path)) is True
    assert looks_like_pdf_header(b"PK\x03\x04zip") is False
    assert read_file_header(tmp_path / "missing.bin") == b""


def test_bill_feature_hits_is_pure_function() -> None:
    """特征词命中是纯函数，便于复核。"""
    hit, missing = bill_feature_hits("电费账单：户号 4206851784045")
    assert "电费" in hit and "账单" in hit and "户号" in hit
    assert set(hit) | set(missing) == set(BILL_FEATURE_TOKENS)
    assert not (set(hit) & set(missing))


# --------------------------------------------------------------------------- #
# 7. 真实账单（正向）—— 文件缺失时 skip，不伪造
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("month", sorted(REAL_BILLS))
def test_real_bill_passes_probe(month: str) -> None:
    """真实 9 月 / 10 月账单应通过预检并命中大量账单特征词。"""
    path = REAL_BILLS[month]
    if not path.exists():
        pytest.skip(f"真实账单样本缺失（业务资料不入仓库）：{path}")

    probe = probe_bill_pdf(path)

    assert probe.ok is True, describe_probe(probe)
    assert probe.page_count in (4, 5)
    assert probe.chars_per_page > 200  # 真实账单约 1,300~1,500 字/页
    assert len(probe.bill_traits_hit) >= 6
    assert probe.is_encrypted is False
    assert probe.needs_password is False
    assert probe.was_repaired is False


@pytest.mark.parametrize("month", sorted(REAL_BILLS))
def test_truncated_real_bill_is_explained_in_chinese(month: str, tmp_path: Path) -> None:
    """**真实账单截断**（丢 60% 字节）：必须给出中文、可操作的失败状态。

    实测真实账单被截断时有两种表现：``open`` 抛 ``FileDataError``（→ CORRUPTED），
    或能打开但 ``page_count == 0``（→ NO_PAGES）。两者都必须被解释成中文，
    且文案要点明"截断/损坏/页面"这类可操作信息。
    """
    source = REAL_BILLS[month]
    if not source.exists():
        pytest.skip(f"真实账单样本缺失（业务资料不入仓库）：{source}")

    raw = source.read_bytes()
    truncated = tmp_path / f"截断_{source.name}"
    truncated.write_bytes(raw[: max(1, int(len(raw) * 0.4))])

    probe = probe_bill_pdf(truncated)

    assert probe.code in (PdfFailureCode.CORRUPTED, PdfFailureCode.NO_PAGES), describe_probe(probe)
    assert probe.failure.blocking is True
    assert _has_chinese(probe.failure.summary)
    assert any(
        keyword in probe.failure.summary + probe.failure.action
        for keyword in ("截断", "损坏", "不完整", "页面")
    ), describe_probe(probe)
    assert probe.text_chars == 0
