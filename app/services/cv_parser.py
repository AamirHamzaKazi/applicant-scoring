"""Modular CV parsing service supporting DOCX, DOC, PDF (digital + scanned OCR), TXT, PNG, JPG, and JPEG files.

Designed so that format extractors and OCR engines work out of the box while remaining
pluggable for unit testing and custom extensions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import Enum
import logging
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

import docx
from docx.opc.exceptions import PackageNotFoundError
import olefile
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel
from pypdf import PdfReader
from pypdf.errors import PdfReadError, PdfStreamError
import pypdfium2 as pdfium
import pytesseract

logger = logging.getLogger(__name__)

WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

ExtractorFunc = Callable[[Path], str]
OCRFallbackFunc = Callable[[Path, str], str]
OCREngineFunc = Callable[[Image.Image], str]

_ZERO_WIDTH_CHARS_REGEX = re.compile(r"[\ufeff\u200b\u200c\u200d\u2060]")
_CONTROL_CHARS_REGEX = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


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
    """Normalize line breaks, strip control/zero-width chars, and collapse excessive whitespace.

    Preserves Unicode characters across English and non-English scripts. Returns empty string
    if the text contains no alphanumeric characters (e.g., whitespace or punctuation-only noise).
    """
    if not text:
        return ""
    cleaned = _ZERO_WIDTH_CHARS_REGEX.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
    cleaned = _CONTROL_CHARS_REGEX.sub(" ", cleaned)
    lines = [" ".join(line.split()) for line in cleaned.split("\n")]
    non_empty_lines = [line for line in lines if line]
    result = "\n".join(non_empty_lines).strip()
    if not any(ch.isalnum() for ch in result):
        return ""
    return result


def _configure_tesseract_cmd() -> None:
    """Configure pytesseract binary path from TESSERACT_CMD or common Windows locations if present."""
    env_cmd = (os.getenv("TESSERACT_CMD") or "").strip()
    if env_cmd and Path(env_cmd).is_file():
        pytesseract.pytesseract.tesseract_cmd = env_cmd
        return
    if shutil.which("tesseract"):
        return
    if os.name == "nt":
        candidates = [
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                pytesseract.pytesseract.tesseract_cmd = str(candidate)
                return


def _windows_media_ocr(image: Image.Image) -> str:
    """Fallback OCR on Windows using the built-in OS-signed Windows.Media.Ocr.OcrEngine."""
    with tempfile.TemporaryDirectory(prefix="win_ocr_") as tmp_dir:
        tmp_png = (Path(tmp_dir) / "ocr_input.png").resolve()
        image.save(tmp_png, format="PNG")
        escaped_path = str(tmp_png).replace("'", "''")

        ps_script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
}})[0]
Function Await($WinRtTask, $ResultType) {{
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}}
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('{escaped_path}')) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {{
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) {{
        throw 'Windows.Media.Ocr.OcrEngine could not be initialized for user profile languages.'
    }}
    $ocrResult = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    if ($null -ne $ocrResult.Lines -and $ocrResult.Lines.Count -gt 0) {{
        ($ocrResult.Lines | ForEach-Object {{ $_.Text }}) -join "`n" | Write-Output
    }} else {{
        Write-Output $ocrResult.Text
    }}
}} finally {{
    $stream.Dispose()
}}
"""
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "Windows.Media.Ocr failed").strip()
            raise RuntimeError(f"OCR engine unavailable (Tesseract not found and Windows OCR failed: {err})")
        return proc.stdout or ""


def _prepare_image_for_ocr(image: Image.Image) -> Image.Image:
    """Normalize a PIL image to RGB on a white background so OCR engines read it reliably."""
    img = ImageOps.exif_transpose(image)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        composited = Image.alpha_composite(background, rgba)
        return composited.convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def ocr_image(image: Image.Image) -> str:
    """Extract text from a PIL Image using Tesseract (pytesseract) or Windows.Media.Ocr fallback."""
    rgb_img = _prepare_image_for_ocr(image)
    _configure_tesseract_cmd()
    try:
        raw_text = pytesseract.image_to_string(rgb_img)
        return _normalize_text(raw_text or "")
    except pytesseract.TesseractNotFoundError as exc:
        if os.name == "nt":
            return _normalize_text(_windows_media_ocr(rgb_img))
        raise RuntimeError(
            "Tesseract OCR is not installed or not in PATH. Install 'tesseract-ocr' or set TESSERACT_CMD."
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"OCR text extraction failed: {exc}") from exc


def extract_image_text(
    path: Path,
    ocr_engine: OCREngineFunc = ocr_image,
) -> str:
    """Validate and extract text from a PNG, JPG, or JPEG image CV via OCR."""
    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
            if width < 2 or height < 2:
                raise ValueError(f"Image dimensions too small for OCR ({width}x{height}).")
            prepared = _prepare_image_for_ocr(img)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValueError(f"Corrupted or unreadable image file: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Unable to open image file: {exc}") from exc

    return _normalize_text(ocr_engine(prepared))


def extract_scanned_pdf_ocr_text(
    path: Path,
    ocr_engine: OCREngineFunc = ocr_image,
    scale: float = 2.0,
) -> str:
    """Render each page of a scanned PDF to an image via pypdfium2 and extract text via OCR."""
    page_texts: list[str] = []
    pdf_doc = None
    try:
        pdf_doc = pdfium.PdfDocument(str(path))
        page_count = len(pdf_doc)
        if page_count == 0:
            return ""
        for page_idx in range(page_count):
            page = pdf_doc[page_idx]
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
            finally:
                page.close()
            ocr_text = _normalize_text(ocr_engine(pil_image))
            if ocr_text:
                page_texts.append(ocr_text)
    except RuntimeError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to render scanned PDF for OCR: {exc}") from exc
    finally:
        if pdf_doc is not None:
            try:
                pdf_doc.close()
            except Exception:
                pass

    return _normalize_text("\n".join(page_texts))


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


def _extract_rtf_text(raw_bytes: bytes) -> str:
    """Extract plain text from an RTF byte stream saved with a .doc extension."""
    text = raw_bytes.decode("latin-1", errors="ignore")
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\line\b", "\n", text)
    text = re.sub(r"\\tab\b", "\t", text)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", errors="ignore"), text)
    text = re.sub(r"\{\\(?:fonttbl|colortbl|stylesheet|info|pict|\*\\)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return _normalize_text(text)


def _decode_word_binary_text_slice(raw_slice: bytes) -> str:
    """Decode a Word 97-2003 text stream slice (8-bit CP1252 or 16-bit UTF-16LE)."""
    if not raw_slice:
        return ""
    if len(raw_slice) >= 4 and len(raw_slice) % 2 == 0:
        odd_bytes = raw_slice[1::2]
        null_ratio = odd_bytes.count(0) / len(odd_bytes)
        if null_ratio > 0.35:
            decoded = raw_slice.decode("utf-16le", errors="ignore")
        else:
            decoded = raw_slice.decode("cp1252", errors="ignore")
    else:
        decoded = raw_slice.decode("cp1252", errors="ignore")

    decoded = decoded.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")
    decoded = decoded.replace("\x07", " | ")
    return _normalize_text(decoded)


def extract_doc_text(path: Path) -> str:
    """Extract plain text from a legacy .doc file (Word 97-2003 OLE2 binary, RTF-in-DOC, or DOCX-in-DOC).

    Never silently treats a corrupted or unsupported .doc file as successfully parsed.
    """
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Unable to read DOC file: {exc}") from exc

    if not raw_bytes:
        return ""

    # 1. Misnamed modern DOCX (ZIP container)
    if raw_bytes.startswith(b"PK\x03\x04"):
        return extract_docx_text(path)

    # 2. RTF document saved with .doc extension
    if raw_bytes.lstrip().startswith(b"{\\rtf"):
        return _extract_rtf_text(raw_bytes)

    # 3. External CLI converter (antiword or LibreOffice soffice) if installed on system
    antiword_bin = shutil.which("antiword")
    if antiword_bin:
        proc = subprocess.run(
            [antiword_bin, str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout and proc.stdout.strip():
            return _normalize_text(proc.stdout)

    # 4. Native OLE2 Compound Document (Word 97-2003 binary .doc)
    if not olefile.isOleFile(raw_bytes):
        raise ValueError(
            "Corrupted or unsupported legacy DOC file: not a valid OLE2 Word document."
        )

    try:
        with olefile.OleFileIO(raw_bytes) as ole:
            if not ole.exists("WordDocument"):
                raise ValueError(
                    "Invalid legacy DOC file: missing 'WordDocument' OLE stream."
                )
            word_stream = ole.openstream("WordDocument").read()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Corrupted OLE2 structure in legacy DOC file: {exc}") from exc

    if len(word_stream) < 68:
        raise ValueError("Corrupted WordDocument stream in legacy DOC file (stream too short).")

    w_ident, _n_fib = struct.unpack_from("<HH", word_stream, 0)
    if w_ident not in (0xA5EC, 0xA5DC):
        raise ValueError(
            f"Unsupported or corrupted Word binary header (wIdent=0x{w_ident:04X})."
        )

    fc_min, fc_mac = struct.unpack_from("<II", word_stream, 0x0018)
    ccp_text = struct.unpack_from("<i", word_stream, 0x004C)[0]

    if 0 < fc_min < len(word_stream) and fc_mac > fc_min:
        clamped_end = min(fc_mac, len(word_stream))
        extracted = _decode_word_binary_text_slice(word_stream[fc_min:clamped_end])
        if extracted:
            return extracted

    if ccp_text > 0 and 0 < fc_min < len(word_stream):
        byte_end = min(len(word_stream), fc_min + ccp_text * 2)
        extracted = _decode_word_binary_text_slice(word_stream[fc_min:byte_end])
        if extracted:
            return extracted

    raise ValueError("Unable to extract readable text from legacy DOC WordDocument stream.")


def build_minimal_ole2_doc_bytes(text: str) -> bytes:
    """Create a valid MS-CFB (OLE2) Word 97-2003 .doc byte stream for testing and fixtures."""
    text_bytes = text.replace("\r\n", "\r").replace("\n", "\r").encode("cp1252", errors="replace")
    fib = bytearray(1024)
    struct.pack_into("<HH", fib, 0, 0xA5EC, 0x00C1)
    fc_min = 1024
    fc_mac = fc_min + len(text_bytes)
    struct.pack_into("<II", fib, 0x0018, fc_min, fc_mac)
    struct.pack_into("<i", fib, 0x004C, len(text_bytes))

    word_stream = bytes(fib) + text_bytes
    min_stream_len = max(4096, ((len(word_stream) + 511) // 512) * 512)
    word_stream_padded = word_stream.ljust(min_stream_len, b"\x00")
    num_stream_sectors = len(word_stream_padded) // 512

    ENDOFCHAIN = 0xFFFFFFFE
    FREESECT = 0xFFFFFFFF
    FATSECT = 0xFFFFFFFD

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 0x0003)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, 1)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<I", header, 60, ENDOFCHAIN)
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<I", header, 68, ENDOFCHAIN)
    struct.pack_into("<I", header, 72, 0)
    struct.pack_into("<I", header, 76, 0)
    for idx in range(1, 109):
        struct.pack_into("<I", header, 76 + idx * 4, FREESECT)

    fat = [FREESECT] * 128
    fat[0] = FATSECT
    fat[1] = ENDOFCHAIN
    for sec in range(num_stream_sectors):
        sec_idx = 2 + sec
        fat[sec_idx] = (sec_idx + 1) if (sec < num_stream_sectors - 1) else ENDOFCHAIN
    fat_bytes = struct.pack("<128I", *fat)

    def _dir_entry(name: str, obj_type: int, child_id: int, start_sect: int, size: int) -> bytes:
        entry = bytearray(128)
        if name:
            name_utf16 = (name + "\x00").encode("utf-16le")
            entry[0 : len(name_utf16)] = name_utf16
            struct.pack_into("<H", entry, 64, len(name_utf16))
        else:
            struct.pack_into("<H", entry, 64, 0)
        entry[66] = obj_type
        entry[67] = 1
        struct.pack_into("<I", entry, 68, 0xFFFFFFFF)
        struct.pack_into("<I", entry, 72, 0xFFFFFFFF)
        struct.pack_into("<I", entry, 76, child_id)
        struct.pack_into("<I", entry, 116, start_sect)
        struct.pack_into("<I", entry, 120, size)
        return bytes(entry)

    dir_sector = (
        _dir_entry("Root Entry", 5, 1, ENDOFCHAIN, 0)
        + _dir_entry("WordDocument", 2, 0xFFFFFFFF, 2, len(word_stream_padded))
        + _dir_entry("", 0, 0xFFFFFFFF, ENDOFCHAIN, 0)
        + _dir_entry("", 0, 0xFFFFFFFF, ENDOFCHAIN, 0)
    )

    return bytes(header) + fat_bytes + dir_sector + word_stream_padded


def extract_pdf_text(path: Path) -> str:
    """Extract embedded digital plain text from a .pdf file across all pages."""
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
    """Modular CV parser supporting DOCX, DOC, PDF (digital + scanned OCR), TXT, PNG, JPG, and JPEG."""

    def __init__(
        self,
        extractors: dict[str, ExtractorFunc] | None = None,
        ocr_fallback: OCRFallbackFunc | None = None,
        ocr_engine: OCREngineFunc | None = None,
    ) -> None:
        self._ocr_engine: OCREngineFunc = ocr_engine or ocr_image

        def _image_extractor(path: Path) -> str:
            return extract_image_text(path, ocr_engine=self._ocr_engine)

        self._extractors: dict[str, ExtractorFunc] = {
            "docx": extract_docx_text,
            "doc": extract_doc_text,
            "pdf": extract_pdf_text,
            "txt": extract_txt_text,
            "png": _image_extractor,
            "jpg": _image_extractor,
            "jpeg": _image_extractor,
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
            if not text:
                if self._ocr_fallback is not None:
                    text = _normalize_text(self._ocr_fallback(path, file_type))
                elif file_type == "pdf":
                    # Normal PDF text extraction yielded empty text -> run scanned PDF OCR fallback
                    text = extract_scanned_pdf_ocr_text(path, ocr_engine=self._ocr_engine)
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
