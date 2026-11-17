"""Modular CV parsing service supporting DOCX, PDF, and TXT files.

Designed so that additional extractors (e.g. legacy .doc) and OCR fallbacks
can be registered without changing the public interface or batch pipeline.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import Enum
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

import docx
from docx.opc.exceptions import PackageNotFoundError
from pydantic import BaseModel
from pypdf import PdfReader
from pypdf.errors import PdfReadError, PdfStreamError

WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

ExtractorFunc = Callable[[Path], str]
OCRFallbackFunc = Callable[[Path, str], str]


class ExtractionStatus(str, Enum):
    """Status values for a CV parsing attempt."""

    SUCCESS = "success"
    FAILED = "failed"


class CVParseResult(BaseModel):
    """Structured result returned when parsing a single CV file."""

    extracted_text: str = ""
    file_type: str = "unknown"
    extraction_status: ExtractionStatus = ExtractionStatus.FAILED
    error_message: str | None = None

    def __getitem__(self, key: str) -> Any:
        """Allow dictionary-style access in addition to attribute access."""
        return getattr(self, key)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation with string status."""
        return {
            "extracted_text": self.extracted_text,
            "file_type": self.file_type,
            "extraction_status": self.extraction_status.value,
            "error_message": self.error_message,
        }


def detect_file_type(file_path: str | Path) -> str:
    """Return normalized lowercase extension without leading dot, or 'unknown'."""
    suffix = Path(file_path).suffix.lower().lstrip(".")
    return suffix if suffix else "unknown"


def _normalize_text(text: str) -> str:
    """Normalize line breaks and collapse excessive blank lines/whitespace."""
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    non_empty_lines = [line for line in lines if line]
    return "\n".join(non_empty_lines).strip()


def extract_docx_text(path: Path) -> str:
    """Extract plain text from a .docx file, including paragraphs, tables, and XML text runs."""
    try:
        document = docx.Document(str(path))
    except (PackageNotFoundError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as exc:
        raise ValueError(f"Corrupted or invalid DOCX file: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Unable to read DOCX file: {exc}") from exc

    blocks: list[str] = []

    for paragraph in document.paragraphs:
        if paragraph.text and paragraph.text.strip():
            blocks.append(paragraph.text.strip())

    for table in document.tables:
        for row in table.rows:
            row_cells: list[str] = []
            for cell in row.cells:
                cell_text = " ".join(cell.text.split())
                if cell_text and (not row_cells or cell_text != row_cells[-1]):
                    row_cells.append(cell_text)
            if row_cells:
                blocks.append(" | ".join(row_cells))

    combined = _normalize_text("\n".join(blocks))
    if combined:
        return combined

    # Fallback to raw word/document.xml <w:t> inspection for resumes that place text
    # inside Word text boxes or drawing shapes not exposed via Document.paragraphs.
    try:
        with zipfile.ZipFile(path, "r") as archive:
            xml_bytes = archive.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        tokens = [node.text for node in root.iterfind(".//w:t", WORD_NS) if node.text]
        return _normalize_text(" ".join(tokens))
    except Exception as exc:
        raise ValueError(f"Corrupted DOCX XML structure: {exc}") from exc


def extract_pdf_text(path: Path) -> str:
    """Extract plain text from a .pdf file across all pages."""
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            decrypt_result = reader.decrypt("")
            if decrypt_result == 0:
                raise ValueError("PDF file is password-protected / encrypted.")

        page_texts: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                page_texts.append(text)
    except (PdfReadError, PdfStreamError, ValueError) as exc:
        raise ValueError(f"Corrupted or unreadable PDF file: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Unable to read PDF file: {exc}") from exc

    return _normalize_text("\n".join(page_texts))


def extract_txt_text(path: Path) -> str:
    """Extract plain text from a .txt file with encoding fallback and binary detection."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Unable to read TXT file: {exc}") from exc

    if not raw_bytes:
        return ""

    # Check UTF-16 BOMs before checking for null bytes in UTF-8/single-byte encodings
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return _normalize_text(raw_bytes.decode("utf-16"))
        except UnicodeDecodeError as exc:
            raise ValueError(f"Corrupted UTF-16 text file: {exc}") from exc

    if b"\x00" in raw_bytes:
        raise ValueError("File appears to contain binary or corrupted data (null bytes detected).")

    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            decoded = raw_bytes.decode(encoding)
            return _normalize_text(decoded)
        except UnicodeDecodeError:
            continue

    raise ValueError("Unable to decode text file with supported character encodings.")


class CVParser:
    """Modular CV parser supporting pluggable format extractors and optional OCR fallback."""

    def __init__(
        self,
        extractors: dict[str, ExtractorFunc] | None = None,
        ocr_fallback: OCRFallbackFunc | None = None,
    ) -> None:
        self._extractors: dict[str, ExtractorFunc] = {
            "docx": extract_docx_text,
            "pdf": extract_pdf_text,
            "txt": extract_txt_text,
        }
        if extractors:
            for fmt, func in extractors.items():
                self.register_extractor(fmt, func)
        self._ocr_fallback = ocr_fallback

    @property
    def supported_types(self) -> tuple[str, ...]:
        """Return tuple of currently registered file types."""
        return tuple(sorted(self._extractors.keys()))

    def register_extractor(self, file_type: str, extractor: ExtractorFunc) -> None:
        """Register or override an extractor for a file extension (e.g. 'doc')."""
        normalized = file_type.lower().lstrip(".")
        if not normalized:
            raise ValueError("file_type must be a non-empty extension string.")
        self._extractors[normalized] = extractor

    def parse(self, file_path: str | Path) -> CVParseResult:
        """Parse a single CV file and never raise uncaught exceptions."""
        try:
            path = Path(file_path)
            file_type = detect_file_type(path)
        except Exception as exc:
            return CVParseResult(
                extracted_text="",
                file_type="unknown",
                extraction_status=ExtractionStatus.FAILED,
                error_message=f"Invalid file path: {exc}",
            )

        if not path.exists():
            return CVParseResult(
                extracted_text="",
                file_type=file_type,
                extraction_status=ExtractionStatus.FAILED,
                error_message=f"File not found: {path}",
            )

        if not path.is_file():
            return CVParseResult(
                extracted_text="",
                file_type=file_type,
                extraction_status=ExtractionStatus.FAILED,
                error_message=f"Path is not a regular file: {path}",
            )

        extractor = self._extractors.get(file_type)
        if extractor is None:
            return CVParseResult(
                extracted_text="",
                file_type=file_type,
                extraction_status=ExtractionStatus.FAILED,
                error_message=(
                    f"Unsupported file format '{file_type}'. "
                    f"Supported formats: {', '.join(self.supported_types)}"
                ),
            )

        try:
            file_size = path.stat().st_size
        except OSError as exc:
            return CVParseResult(
                extracted_text="",
                file_type=file_type,
                extraction_status=ExtractionStatus.FAILED,
                error_message=f"Unable to access file metadata: {exc}",
            )

        if file_size == 0:
            return CVParseResult(
                extracted_text="",
                file_type=file_type,
                extraction_status=ExtractionStatus.FAILED,
                error_message="File is empty (0 bytes).",
            )

        try:
            text = extractor(path)
            if not text and self._ocr_fallback is not None:
                text = _normalize_text(self._ocr_fallback(path, file_type))
        except Exception as exc:
            return CVParseResult(
                extracted_text="",
                file_type=file_type,
                extraction_status=ExtractionStatus.FAILED,
                error_message=str(exc) or f"Failed to extract text from {file_type.upper()} file.",
            )

        if not text:
            return CVParseResult(
                extracted_text="",
                file_type=file_type,
                extraction_status=ExtractionStatus.FAILED,
                error_message="No extractable text found in document.",
            )

        return CVParseResult(
            extracted_text=text,
            file_type=file_type,
            extraction_status=ExtractionStatus.SUCCESS,
            error_message=None,
        )

    def parse_batch(self, file_paths: Iterable[str | Path]) -> list[CVParseResult]:
        """Parse multiple CV files safely; one failed CV never crashes the batch."""
        results: list[CVParseResult] = []
        for item in file_paths:
            try:
                results.append(self.parse(item))
            except Exception as exc:  # Defensive safeguard
                results.append(
                    CVParseResult(
                        extracted_text="",
                        file_type=detect_file_type(str(item)) if item is not None else "unknown",
                        extraction_status=ExtractionStatus.FAILED,
                        error_message=f"Unexpected batch parsing error: {exc}",
                    )
                )
        return results


_default_parser = CVParser()


def parse_cv(file_path: str | Path) -> CVParseResult:
    """Parse a single CV file using the default CVParser instance."""
    return _default_parser.parse(file_path)


def parse_cv_batch(file_paths: Iterable[str | Path]) -> list[CVParseResult]:
    """Parse a batch of CV files using the default CVParser instance."""
    return _default_parser.parse_batch(file_paths)
