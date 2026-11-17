"""Unit and smoke tests for app.services.cv_parser covering DOCX, DOC, PDF (digital + scanned OCR),
TXT, PNG, JPG, JPEG, messy/non-English CVs, error cases, and batch resilience.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import zipfile

import docx
from PIL import Image, ImageDraw
import pytest

from app.services.cv_parser import (
    CVParser,
    ExtractionStatus,
    build_minimal_ole2_doc_bytes,
    ocr_image,
    parse_cv,
    parse_cv_batch,
)

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


def build_sample_pdf_bytes(text: str) -> bytes:
    """Create a valid single-page PDF 1.4 byte stream with extractable text."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream_data = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        f"5 0 obj\n<< /Length {len(stream_data)} >>\nstream\n".encode("latin-1")
        + stream_data
        + b"\nendstream\nendobj\n",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objects:
        offsets.append(len(body))
        body.extend(obj)

    xref_pos = len(body)
    xref_lines = [f"xref\n0 {len(objects) + 1}\n", "0000000000 65535 f \n"]
    for offset in offsets:
        xref_lines.append(f"{offset:010d} 00000 n \n")

    trailer = (
        "".join(xref_lines)
        + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    body.extend(trailer)
    return bytes(body)


def build_blank_pdf_bytes() -> bytes:
    """Create a valid single-page PDF with an empty content stream (no text)."""
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objects:
        offsets.append(len(body))
        body.extend(obj)

    xref_pos = len(body)
    xref_lines = [f"xref\n0 {len(objects) + 1}\n", "0000000000 65535 f \n"]
    for offset in offsets:
        xref_lines.append(f"{offset:010d} 00000 n \n")

    trailer = (
        "".join(xref_lines)
        + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    body.extend(trailer)
    return bytes(body)


def build_readable_cv_image(text: str = "Candidate 004 - Python SQL Git Developer") -> Image.Image:
    """Create a high-contrast RGB PIL image containing readable CV text."""
    img = Image.new("RGB", (900, 220), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((40, 70), text, fill=(0, 0, 0), font_size=30)
    return img


@pytest.fixture()
def sample_files(tmp_path: Path) -> dict[str, Path]:
    """Generate synthetic sample CV files (valid, empty, and corrupted) in tmp_path."""
    # 1. Valid DOCX (paragraphs + table)
    valid_docx = tmp_path / "candidate_valid.docx"
    doc = docx.Document()
    doc.add_heading("Candidate 001", level=1)
    doc.add_paragraph("Junior Level Software Developer with 3 years of Python and SQL experience.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Core Skills"
    table.rows[0].cells[1].text = "Python, FastAPI, PostgreSQL, Git"
    doc.save(str(valid_docx))

    # 2. Valid PDF (digital text layer)
    valid_pdf = tmp_path / "candidate_valid.pdf"
    valid_pdf.write_bytes(
        build_sample_pdf_bytes("Candidate 002 - Software Engineer - Python SQL REST APIs")
    )

    # 3. Valid TXT
    valid_txt = tmp_path / "candidate_valid.txt"
    valid_txt.write_text(
        "Candidate 003\nRole: Junior Software Developer\nSkills: Python, Java, Git\n",
        encoding="utf-8",
    )

    # 4. Valid Legacy DOC (OLE2 Word 97-2003 binary)
    valid_doc = tmp_path / "candidate_valid.doc"
    valid_doc.write_bytes(
        build_minimal_ole2_doc_bytes(
            "Candidate 004\nRole: Junior Software Developer\nSkills: Python, SQL, Git"
        )
    )

    # 5. Valid PNG, JPG, JPEG, and Scanned PDF
    cv_img = build_readable_cv_image("Candidate 005 - Python SQL Git Developer")
    valid_png = tmp_path / "candidate_valid.png"
    valid_jpg = tmp_path / "candidate_valid.jpg"
    valid_jpeg = tmp_path / "candidate_valid.jpeg"
    scanned_pdf = tmp_path / "candidate_scanned.pdf"
    cv_img.save(valid_png, format="PNG")
    cv_img.save(valid_jpg, format="JPEG", quality=95)
    cv_img.save(valid_jpeg, format="JPEG", quality=95)
    cv_img.save(scanned_pdf, format="PDF", resolution=150.0)

    # 6. Empty (0-byte) files
    empty_docx = tmp_path / "empty.docx"
    empty_docx.write_bytes(b"")
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")
    empty_txt = tmp_path / "empty.txt"
    empty_txt.write_bytes(b"")
    empty_doc = tmp_path / "empty.doc"
    empty_doc.write_bytes(b"")
    empty_png = tmp_path / "empty.png"
    empty_png.write_bytes(b"")

    # 7. Whitespace-only / blank documents
    blank_docx = tmp_path / "blank.docx"
    docx.Document().save(str(blank_docx))
    blank_pdf = tmp_path / "blank.pdf"
    blank_pdf.write_bytes(build_blank_pdf_bytes())
    whitespace_txt = tmp_path / "whitespace.txt"
    whitespace_txt.write_text("   \n\t  \n ", encoding="utf-8")
    blank_png = tmp_path / "blank.png"
    Image.new("RGB", (400, 150), color=(255, 255, 255)).save(blank_png, format="PNG")

    # 8. Corrupted files
    corrupted_docx = tmp_path / "corrupted.docx"
    corrupted_docx.write_bytes(b"PK\x03\x04not-a-valid-zip-or-docx-stream")
    corrupted_doc = tmp_path / "corrupted.doc"
    corrupted_doc.write_bytes(b"\xd0\xcf\x11\xe0legacy-word-binary-corrupted")
    corrupted_pdf = tmp_path / "corrupted.pdf"
    corrupted_pdf.write_bytes(b"%PDF-1.4\n%%corrupted-stream-without-xref")
    corrupted_txt = tmp_path / "corrupted_binary.txt"
    corrupted_txt.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    corrupted_png = tmp_path / "corrupted.png"
    corrupted_png.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-valid-png-body")
    corrupted_jpg = tmp_path / "corrupted.jpg"
    corrupted_jpg.write_bytes(b"\xff\xd8\xff\xe0corrupted-jpeg-stream")

    return {
        "valid_docx": valid_docx,
        "valid_doc": valid_doc,
        "valid_pdf": valid_pdf,
        "valid_txt": valid_txt,
        "valid_png": valid_png,
        "valid_jpg": valid_jpg,
        "valid_jpeg": valid_jpeg,
        "scanned_pdf": scanned_pdf,
        "empty_docx": empty_docx,
        "empty_doc": empty_doc,
        "empty_pdf": empty_pdf,
        "empty_txt": empty_txt,
        "empty_png": empty_png,
        "blank_docx": blank_docx,
        "blank_pdf": blank_pdf,
        "whitespace_txt": whitespace_txt,
        "blank_png": blank_png,
        "corrupted_docx": corrupted_docx,
        "corrupted_doc": corrupted_doc,
        "corrupted_pdf": corrupted_pdf,
        "corrupted_txt": corrupted_txt,
        "corrupted_png": corrupted_png,
        "corrupted_jpg": corrupted_jpg,
    }


def test_parse_valid_docx(sample_files: dict[str, Path]) -> None:
    result = parse_cv(sample_files["valid_docx"])
    assert result.extraction_status == ExtractionStatus.SUCCESS
    assert result.extraction_status == "success"
    assert result.file_type == "docx"
    assert result.error_message is None
    assert "Candidate 001" in result.extracted_text
    assert "Python, FastAPI, PostgreSQL, Git" in result.extracted_text
    assert result["file_type"] == "docx"


def test_parse_valid_pdf_does_not_invoke_ocr(sample_files: dict[str, Path]) -> None:
    """When a PDF already contains digital text, OCR must never be invoked."""
    ocr_spy = MagicMock(side_effect=AssertionError("OCR must not run on digital text PDF"))
    parser = CVParser(ocr_engine=ocr_spy)

    result = parser.parse(sample_files["valid_pdf"])
    assert result.extraction_status == ExtractionStatus.SUCCESS
    assert result.file_type == "pdf"
    assert result.error_message is None
    assert "Candidate 002" in result.extracted_text
    assert "Python SQL REST APIs" in result.extracted_text
    ocr_spy.assert_not_called()


def test_parse_valid_txt(sample_files: dict[str, Path]) -> None:
    result = parse_cv(sample_files["valid_txt"])
    assert result.extraction_status == ExtractionStatus.SUCCESS
    assert result.file_type == "txt"
    assert result.error_message is None
    assert "Candidate 003" in result.extracted_text
    assert "Skills: Python, Java, Git" in result.extracted_text


def test_parse_valid_legacy_doc_and_rtf_in_doc(
    sample_files: dict[str, Path], tmp_path: Path
) -> None:
    """Verify legacy .doc extraction for OLE2 Word 97-2003 binary and RTF-in-.doc files."""
    # 1. OLE2 Word 97-2003 binary .doc
    res_ole = parse_cv(sample_files["valid_doc"])
    assert res_ole.extraction_status == ExtractionStatus.SUCCESS
    assert res_ole.file_type == "doc"
    assert res_ole.error_message is None
    assert "Candidate 004" in res_ole.extracted_text
    assert "Python, SQL, Git" in res_ole.extracted_text

    # 2. RTF saved with .doc extension
    rtf_doc = tmp_path / "rtf_resume.doc"
    rtf_doc.write_bytes(
        b"{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Arial;}}\\f0\\fs24 Candidate RTF\\par Junior Developer (Python, SQL, Git)\\par}"
    )
    res_rtf = parse_cv(rtf_doc)
    assert res_rtf.extraction_status == ExtractionStatus.SUCCESS
    assert res_rtf.file_type == "doc"
    assert "Candidate RTF" in res_rtf.extracted_text
    assert "Junior Developer (Python, SQL, Git)" in res_rtf.extracted_text


def test_parse_image_cvs_png_jpg_jpeg_with_mocked_ocr(
    sample_files: dict[str, Path],
) -> None:
    """Verify PNG, JPG, and JPEG image CVs extract text via the configured OCR engine."""
    mock_ocr = MagicMock(return_value="Candidate 005\nJunior Developer\nSkills: Python, SQL, Git")
    parser = CVParser(ocr_engine=mock_ocr)

    for key, ext in (("valid_png", "png"), ("valid_jpg", "jpg"), ("valid_jpeg", "jpeg")):
        res = parser.parse(sample_files[key])
        assert res.extraction_status == ExtractionStatus.SUCCESS
        assert res.file_type == ext
        assert res.error_message is None
        assert "Candidate 005" in res.extracted_text
        assert "Python, SQL, Git" in res.extracted_text

    assert mock_ocr.call_count == 3


def test_parse_scanned_pdf_renders_pages_and_runs_ocr(
    sample_files: dict[str, Path],
) -> None:
    """Verify a scanned PDF with no digital text layer renders pages to images and extracts via OCR."""
    mock_ocr = MagicMock(
        return_value="Scanned Candidate 006\nExperience: 2 years Python & SQL Developer"
    )
    parser = CVParser(ocr_engine=mock_ocr)

    res = parser.parse(sample_files["scanned_pdf"])
    assert res.extraction_status == ExtractionStatus.SUCCESS
    assert res.file_type == "pdf"
    assert res.error_message is None
    assert "Scanned Candidate 006" in res.extracted_text
    assert mock_ocr.call_count == 1


def test_real_ocr_smoke_on_committed_fixtures() -> None:
    """Perform a live OCR smoke test on the committed PNG, JPG, Scanned PDF, and legacy DOC fixtures."""
    for filename, expected_type in (
        ("sample_cv.docx", "docx"),
        ("sample_cv.doc", "doc"),
        ("sample_cv.pdf", "pdf"),
        ("sample_cv.txt", "txt"),
        ("sample_cv.png", "png"),
        ("sample_cv.jpg", "jpg"),
        ("sample_scanned_cv.pdf", "pdf"),
    ):
        sample_path = SAMPLES_DIR / filename
        assert sample_path.is_file(), f"Missing committed fixture: {sample_path}"
        result = parse_cv(sample_path)
        assert result.extraction_status == ExtractionStatus.SUCCESS, (
            f"Failed on {filename}: {result.error_message}"
        )
        assert result.file_type == expected_type
        assert result.error_message is None
        assert "Synthetic Candidate" in result.extracted_text
        assert "Python" in result.extracted_text


def test_committed_negative_and_edge_case_fixtures() -> None:
    """Verify committed empty_cv.docx, corrupt_cv.pdf, messy_cv.txt, and non_english_cv.txt fixtures."""
    # 1. Empty DOCX fixture -> Failed
    empty_res = parse_cv(SAMPLES_DIR / "empty_cv.docx")
    assert empty_res.extraction_status == ExtractionStatus.FAILED
    assert empty_res.file_type == "docx"
    assert empty_res.extracted_text == ""
    assert "empty" in (empty_res.error_message or "").lower()

    # 2. Corrupt PDF fixture -> Failed
    corrupt_res = parse_cv(SAMPLES_DIR / "corrupt_cv.pdf")
    assert corrupt_res.extraction_status == ExtractionStatus.FAILED
    assert corrupt_res.file_type == "pdf"
    assert corrupt_res.extracted_text == ""
    assert "corrupted" in (corrupt_res.error_message or "").lower()

    # 3. Messy CV fixture -> Cleaned and normalized
    messy_res = parse_cv(SAMPLES_DIR / "messy_cv.txt")
    assert messy_res.extraction_status == ExtractionStatus.SUCCESS
    assert messy_res.file_type == "txt"
    assert "Name: Synthetic Candidate" in messy_res.extracted_text
    assert "Role: Junior Level Software Developer (2.5 years)" in messy_res.extracted_text
    assert "\u200b" not in messy_res.extracted_text
    assert "\x0c" not in messy_res.extracted_text

    # 4. Non-English CV fixture -> Preserves Unicode characters across scripts
    non_eng_res = parse_cv(SAMPLES_DIR / "non_english_cv.txt")
    assert non_eng_res.extraction_status == ExtractionStatus.SUCCESS
    assert non_eng_res.file_type == "txt"
    assert "Desarrollador de Software Júnior" in non_eng_res.extracted_text
    assert "Programmation orientée objet" in non_eng_res.extracted_text
    assert "Бакалавр компьютерных наук" in non_eng_res.extracted_text
    assert "सॉफ्टवेयर डेवलपर" in non_eng_res.extracted_text


def test_punctuation_only_noise_rejected_as_empty_text(tmp_path: Path) -> None:
    """Files containing only punctuation/symbols with zero alphanumeric characters must fail cleanly."""
    noisy_txt = tmp_path / "symbols_only.txt"
    noisy_txt.write_text(" --- === *** ... !!! ??? ~~~ \n\t ___ ", encoding="utf-8")
    res = parse_cv(noisy_txt)
    assert res.extraction_status == ExtractionStatus.FAILED
    assert res.extracted_text == ""
    assert "No extractable text" in (res.error_message or "")


@pytest.mark.parametrize(
    "key,expected_type",
    [
        ("empty_docx", "docx"),
        ("empty_doc", "doc"),
        ("empty_pdf", "pdf"),
        ("empty_txt", "txt"),
        ("empty_png", "png"),
        ("blank_docx", "docx"),
        ("blank_pdf", "pdf"),
        ("whitespace_txt", "txt"),
        ("blank_png", "png"),
    ],
)
def test_parse_empty_and_blank_files(
    sample_files: dict[str, Path], key: str, expected_type: str
) -> None:
    parser = CVParser(ocr_engine=lambda _img: "")
    result = parser.parse(sample_files[key])
    assert result.extraction_status == ExtractionStatus.FAILED
    assert result.file_type == expected_type
    assert result.extracted_text == ""
    assert result.error_message is not None
    assert len(result.error_message) > 0


@pytest.mark.parametrize(
    "key,expected_type",
    [
        ("corrupted_docx", "docx"),
        ("corrupted_doc", "doc"),
        ("corrupted_pdf", "pdf"),
        ("corrupted_txt", "txt"),
        ("corrupted_png", "png"),
        ("corrupted_jpg", "jpg"),
    ],
)
def test_parse_corrupted_files(
    sample_files: dict[str, Path], key: str, expected_type: str
) -> None:
    result = parse_cv(sample_files[key])
    assert result.extraction_status == ExtractionStatus.FAILED
    assert result.file_type == expected_type
    assert result.extracted_text == ""
    assert result.error_message is not None


def test_ocr_engine_failure_handled_gracefully(sample_files: dict[str, Path]) -> None:
    """If the OCR engine raises an error, CVParser returns FAILED with a meaningful error message."""
    failing_parser = CVParser(
        ocr_engine=MagicMock(side_effect=RuntimeError("Tesseract OCR binary crashed"))
    )
    res_img = failing_parser.parse(sample_files["valid_png"])
    assert res_img.extraction_status == ExtractionStatus.FAILED
    assert res_img.extracted_text == ""
    assert "Tesseract OCR binary crashed" in (res_img.error_message or "")

    res_pdf = failing_parser.parse(sample_files["scanned_pdf"])
    assert res_pdf.extraction_status == ExtractionStatus.FAILED
    assert res_pdf.extracted_text == ""
    assert "Tesseract OCR binary crashed" in (res_pdf.error_message or "")


def test_parse_missing_and_unreadable_paths(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.docx"
    missing_result = parse_cv(missing)
    assert missing_result.extraction_status == ExtractionStatus.FAILED
    assert missing_result.file_type == "docx"
    assert missing_result.extracted_text == ""
    assert "File not found" in (missing_result.error_message or "")

    directory_result = parse_cv(tmp_path)
    assert directory_result.extraction_status == ExtractionStatus.FAILED
    assert directory_result.extracted_text == ""
    assert "not a regular file" in (directory_result.error_message or "")


def test_parse_unsupported_extension(tmp_path: Path) -> None:
    unsupported_file = tmp_path / "resume.xlsx"
    unsupported_file.write_bytes(b"PK\x03\x04spreadsheet-bytes")
    result = parse_cv(unsupported_file)
    assert result.extraction_status == ExtractionStatus.FAILED
    assert result.file_type == "xlsx"
    assert result.extracted_text == ""
    assert "Unsupported file format 'xlsx'" in (result.error_message or "")


def test_batch_never_crashes_on_failed_cvs(
    sample_files: dict[str, Path], tmp_path: Path
) -> None:
    batch_inputs = [
        sample_files["valid_docx"],
        sample_files["corrupted_docx"],
        sample_files["valid_doc"],
        sample_files["corrupted_doc"],
        sample_files["valid_pdf"],
        sample_files["scanned_pdf"],
        sample_files["corrupted_pdf"],
        sample_files["valid_png"],
        sample_files["corrupted_png"],
        tmp_path / "missing_candidate.txt",
        sample_files["valid_txt"],
        sample_files["corrupted_txt"],
    ]

    results = parse_cv_batch(batch_inputs)
    assert len(results) == len(batch_inputs)

    statuses = [r.extraction_status for r in results]
    assert statuses == [
        ExtractionStatus.SUCCESS,
        ExtractionStatus.FAILED,
        ExtractionStatus.SUCCESS,
        ExtractionStatus.FAILED,
        ExtractionStatus.SUCCESS,
        ExtractionStatus.SUCCESS,
        ExtractionStatus.FAILED,
        ExtractionStatus.SUCCESS,
        ExtractionStatus.FAILED,
        ExtractionStatus.FAILED,
        ExtractionStatus.SUCCESS,
        ExtractionStatus.FAILED,
    ]


def test_modular_extensibility_for_legacy_doc_and_ocr(
    sample_files: dict[str, Path], tmp_path: Path
) -> None:
    """Verify custom extractor override and custom OCR fallback hook continue to work cleanly."""
    parser = CVParser(
        ocr_fallback=lambda path, fmt: f"OCR extracted text from {path.name} ({fmt})"
    )
    parser.register_extractor("doc", lambda path: "Extracted legacy DOC resume content")

    doc_file = tmp_path / "legacy_candidate.doc"
    doc_file.write_bytes(b"legacy-doc-bytes")
    doc_result = parser.parse(doc_file)
    assert doc_result.extraction_status == ExtractionStatus.SUCCESS
    assert doc_result.file_type == "doc"
    assert doc_result.extracted_text == "Extracted legacy DOC resume content"

    ocr_result = parser.parse(sample_files["blank_pdf"])
    assert ocr_result.extraction_status == ExtractionStatus.SUCCESS
    assert ocr_result.file_type == "pdf"
    assert "OCR extracted text from blank.pdf (pdf)" in ocr_result.extracted_text


def test_xml_textbox_docx_fallback(tmp_path: Path) -> None:
    """Verify DOCX files with text inside custom XML nodes still extract cleanly."""
    custom_docx = tmp_path / "textbox_only.docx"
    docx.Document().save(str(custom_docx))
    with zipfile.ZipFile(custom_docx, "r") as zin:
        items = {name: zin.read(name) for name in zin.namelist()}

    items["word/document.xml"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:txbxContent><w:p><w:r><w:t>Textbox Resume Content</w:t></w:r></w:p></w:txbxContent></w:body>"
        b"</w:document>"
    )
    with zipfile.ZipFile(custom_docx, "w") as zout:
        for name, content in items.items():
            zout.writestr(name, content)

    result = parse_cv(custom_docx)
    assert result.extraction_status == ExtractionStatus.SUCCESS
    assert result.extracted_text == "Textbox Resume Content"
