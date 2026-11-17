"""Unit tests for prepare_drive_upload.py."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from prepare_drive_upload import (
    DEFAULT_CVS_DIR,
    DEFAULT_MANIFEST_CSV,
    DEFAULT_MAPPING_CSV,
    MAPPING_FIELDNAMES,
    WORKSPACE_ROOT,
    prepare_drive_upload,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepare_drive_upload_synthetic_manifest(tmp_path: Path) -> None:
    """Verify renaming, copying, exact columns, and original-file preservation on synthetic inputs."""
    src_dir = tmp_path / "Dataset" / "CV"
    src_dir.mkdir(parents=True)
    manifest_csv = tmp_path / "applicants_manifest.csv"

    rows = []
    source_hashes: dict[str, str] = {}
    for idx in range(1, 3):
        src_file = src_dir / f"{idx}.docx"
        payload = f"Synthetic resume bytes for applicant {idx}".encode("utf-8")
        src_file.write_bytes(payload)
        app_id = f"APP-{idx:03d}"
        source_hashes[app_id] = _sha256(src_file)
        rows.append(
            {
                "applicant_id": app_id,
                "name": f"Candidate {idx:03d}",
                "email": f"candidate{idx:03d}@example.com",
                "cv_filename": f"{idx}.docx",
                "cv_path": f"Dataset/CV/{idx}.docx",
            }
        )

    with manifest_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["applicant_id", "name", "email", "cv_filename", "cv_path"],
        )
        writer.writeheader()
        writer.writerows(rows)

    cvs_out = tmp_path / "drive_upload" / "CVs"
    mapping_out = tmp_path / "drive_upload" / "cv_upload_mapping.csv"

    result = prepare_drive_upload(
        manifest_csv=manifest_csv,
        cvs_output_dir=cvs_out,
        mapping_csv=mapping_out,
        workspace_root=tmp_path,
        expected_count=2,
    )

    assert len(result) == 2
    copied = sorted(p.name for p in cvs_out.iterdir() if p.is_file())
    assert copied == ["APP-001.docx", "APP-002.docx"]

    # Originals untouched and hashes identical
    for app_id in ("APP-001", "APP-002"):
        assert _sha256(cvs_out / f"{app_id}.docx") == source_hashes[app_id]

    with mapping_out.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["applicant_id", "name", "email", "cv_filename", "local_path"]
        assert reader.fieldnames == MAPPING_FIELDNAMES
        mapped_rows = list(reader)

    assert mapped_rows[0] == {
        "applicant_id": "APP-001",
        "name": "Candidate 001",
        "email": "candidate001@example.com",
        "cv_filename": "1.docx",
        "local_path": "drive_upload/CVs/APP-001.docx",
    }


def test_generated_drive_upload_output_contains_exact_40_files() -> None:
    """Verify the generated drive_upload/CVs/ folder and cv_upload_mapping.csv when present."""
    if not DEFAULT_MAPPING_CSV.is_file() or not DEFAULT_MANIFEST_CSV.is_file():
        return

    copied_files = sorted(p for p in DEFAULT_CVS_DIR.iterdir() if p.is_file())
    assert len(copied_files) == 40

    with DEFAULT_MANIFEST_CSV.open("r", encoding="utf-8", newline="") as handle:
        manifest_rows = {r["applicant_id"]: r for r in csv.DictReader(handle)}

    with DEFAULT_MAPPING_CSV.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["applicant_id", "name", "email", "cv_filename", "local_path"]
        mapping_rows = list(reader)

    assert len(mapping_rows) == 40
    for row in mapping_rows:
        app_id = row["applicant_id"]
        src_row = manifest_rows[app_id]
        assert row["name"] == src_row["name"]
        assert row["email"] == src_row["email"]
        assert row["cv_filename"] == src_row["cv_filename"]
        assert row["local_path"] == f"drive_upload/CVs/{app_id}.docx"

        copied_path = WORKSPACE_ROOT / row["local_path"]
        src_path = WORKSPACE_ROOT / src_row["cv_path"]
        assert copied_path.is_file()
        assert src_path.is_file()
        assert _sha256(copied_path) == _sha256(src_path)
