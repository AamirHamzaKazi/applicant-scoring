"""Unit tests for app.services.cv_parser covering DOCX, PDF, TXT, error cases, and batch resilience."""

from __future__ import annotations

from pathlib import Path
import zipfile

import docx
import pytest

from app.services.cv_parser import (
    CVParser,
    ExtractionStatus,
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

    # 2. Valid PDF
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

    # 4. Empty (0-byte) files
    empty_docx = tmp_path / "empty.docx"
    empty_docx.write_bytes(b"")
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")
    empty_txt = tmp_path / "empty.txt"
    empty_txt.write_bytes(b"")

    # 5. Whitespace-only / blank documents
    blank_docx = tmp_path / "blank.docx"
    docx.Document().save(str(blank_docx))
    blank_pdf = tmp_path / "blank.pdf"
    blank_pdf.write_bytes(build_blank_pdf_bytes())
    whitespace_txt = tmp_path / "whitespace.txt"
    whitespace_txt.write_text("   \n\t  \n ", encoding="utf-8")

    # 6. Corrupted files
    corrupted_docx = tmp_path / "corrupted.docx"
    corrupted_docx.write_bytes(b"PK\x03\x04not-a-valid-zip-or-docx-stream")
    corrupted_pdf = tmp_path / "corrupted.pdf"
    corrupted_pdf.write_bytes(b"%PDF-1.4\n%%corrupted-stream-without-xref")
    corrupted_txt = tmp_path / "corrupted_binary.txt"
    corrupted_txt.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    return {
        "valid_docx": valid_docx,
        "valid_pdf": valid_pdf,
        "valid_txt": valid_txt,
        "empty_docx": empty_docx,
        "empty_pdf": empty_pdf,
        "empty_txt": empty_txt,
        "blank_docx": blank_docx,
        "blank_pdf": blank_pdf,
        "whitespace_txt": whitespace_txt,
        "corrupted_docx": corrupted_docx,
        "corrupted_pdf": corrupted_pdf,
        "corrupted_txt": corrupted_txt,
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


def test_parse_valid_pdf(sample_files: dict[str, Path]) -> None:
    result = parse_cv(sample_files["valid_pdf"])
    assert result.extraction_status == ExtractionStatus.SUCCESS
    assert result.file_type == "pdf"
    assert result.error_message is None
    assert "Candidate 002" in result.extracted_text
    assert "Python SQL REST APIs" in result.extracted_text


def test_parse_valid_txt(sample_files: dict[str, Path]) -> None:
    result = parse_cv(sample_files["valid_txt"])
    assert result.extraction_status == ExtractionStatus.SUCCESS
    assert result.file_type == "txt"
    assert result.error_message is None
    assert "Candidate 003" in result.extracted_text
    assert "Skills: Python, Java, Git" in result.extracted_text


def test_parse_committed_sample_files() -> None:
    """Test against the committed synthetic sample files in tests/samples/."""
    for ext in ("docx", "pdf", "txt"):
        sample_path = SAMPLES_DIR / f"sample_cv.{ext}"
        result = parse_cv(sample_path)
        assert result.extraction_status == "success", f"Failed on {sample_path}: {result.error_message}"
        assert result.file_type == ext
        assert result.error_message is None
        assert "Synthetic Candidate" in result.extracted_text


@pytest.mark.parametrize("key,expected_type", [
    ("empty_docx", "docx"),
    ("empty_pdf", "pdf"),
    ("empty_txt", "txt"),
    ("blank_docx", "docx"),
    ("blank_pdf", "pdf"),
    ("whitespace_txt", "txt"),
])
def test_parse_empty_and_blank_files(
    sample_files: dict[str, Path], key: str, expected_type: str
) -> None:
    result = parse_cv(sample_files[key])
    assert result.extraction_status == ExtractionStatus.FAILED
    assert result.file_type == expected_type
    assert result.extracted_text == ""
    assert result.error_message is not None
    assert len(result.error_message) > 0


@pytest.mark.parametrize("key,expected_type", [
    ("corrupted_docx", "docx"),
    ("corrupted_pdf", "pdf"),
    ("corrupted_txt", "txt"),
])
def test_parse_corrupted_files(
    sample_files: dict[str, Path], key: str, expected_type: str
) -> None:
    result = parse_cv(sample_files[key])
    assert result.extraction_status == ExtractionStatus.FAILED
    assert result.file_type == expected_type
    assert result.extracted_text == ""
    assert result.error_message is not None


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
    legacy_doc = tmp_path / "resume.doc"
    legacy_doc.write_bytes(b"\xd0\xcf\x11\xe0legacy-word-binary")
    result = parse_cv(legacy_doc)
    assert result.extraction_status == ExtractionStatus.FAILED
    assert result.file_type == "doc"
    assert result.extracted_text == ""
    assert "Unsupported file format 'doc'" in (result.error_message or "")


def test_batch_never_crashes_on_failed_cvs(sample_files: dict[str, Path], tmp_path: Path) -> None:
    batch_inputs = [
        sample_files["valid_docx"],
        sample_files["corrupted_docx"],
        sample_files["valid_pdf"],
        sample_files["empty_pdf"],
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
        ExtractionStatus.FAILED,
        ExtractionStatus.SUCCESS,
        ExtractionStatus.FAILED,
    ]


def test_modular_extensibility_for_legacy_doc_and_ocr(
    sample_files: dict[str, Path], tmp_path: Path
) -> None:
    """Verify legacy .doc extractor registration and OCR fallback hook work cleanly."""
    parser = CVParser(
        ocr_fallback=lambda path, fmt: f"OCR extracted text from {path.name} ({fmt})"
    )
    parser.register_extractor("doc", lambda path: "Extracted legacy DOC resume content")

    # 1. Legacy .doc now succeeds when registered
    doc_file = tmp_path / "legacy_candidate.doc"
    doc_file.write_bytes(b"legacy-doc-bytes")
    doc_result = parser.parse(doc_file)
    assert doc_result.extraction_status == ExtractionStatus.SUCCESS
    assert doc_result.file_type == "doc"
    assert doc_result.extracted_text == "Extracted legacy DOC resume content"

    # 2. Blank/scanned PDF automatically triggers OCR fallback when configured
    ocr_result = parser.parse(sample_files["blank_pdf"])
    assert ocr_result.extraction_status == ExtractionStatus.SUCCESS
    assert ocr_result.file_type == "pdf"
    assert "OCR extracted text from blank.pdf (pdf)" in ocr_result.extracted_text


def test_xml_textbox_docx_fallback(tmp_path: Path) -> None:
    """Verify DOCX files with text inside custom XML nodes still extract cleanly."""
    custom_docx = tmp_path / "textbox_only.docx"
    # Start from a blank valid docx and inject a text box run in word/document.xml
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
