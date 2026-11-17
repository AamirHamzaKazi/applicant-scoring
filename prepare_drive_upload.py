"""Prepare selected CV files for Google Drive upload using applicants_manifest.csv.

Reads `applicants_manifest.csv` (without modifying it or any original CV files),
copies the 40 selected CV files into `drive_upload/CVs/` renamed by `applicant_id`,
writes `drive_upload/cv_upload_mapping.csv` (without any CV contents), and verifies
that exactly 40 CV files were copied cleanly.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import shutil

WORKSPACE_ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST_CSV = WORKSPACE_ROOT / "applicants_manifest.csv"
DEFAULT_UPLOAD_DIR = WORKSPACE_ROOT / "drive_upload"
DEFAULT_CVS_DIR = DEFAULT_UPLOAD_DIR / "CVs"
DEFAULT_MAPPING_CSV = DEFAULT_UPLOAD_DIR / "cv_upload_mapping.csv"

EXPECTED_COUNT = 40
MAPPING_FIELDNAMES = ["applicant_id", "name", "email", "cv_filename", "local_path"]


def prepare_drive_upload(
    manifest_csv: Path = DEFAULT_MANIFEST_CSV,
    cvs_output_dir: Path = DEFAULT_CVS_DIR,
    mapping_csv: Path = DEFAULT_MAPPING_CSV,
    workspace_root: Path = WORKSPACE_ROOT,
    expected_count: int = EXPECTED_COUNT,
) -> list[dict[str, str]]:
    """Copy manifest CVs into cvs_output_dir renamed by applicant_id and write mapping CSV."""
    manifest_path = Path(manifest_csv).resolve()
    root_path = Path(workspace_root).resolve()
    target_cvs_dir = Path(cvs_output_dir).resolve()
    target_mapping_csv = Path(mapping_csv).resolve()

    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required_cols = {"applicant_id", "name", "email", "cv_filename", "cv_path"}
        missing_cols = required_cols - set(reader.fieldnames or [])
        if missing_cols:
            raise ValueError(
                f"Manifest missing required column(s): {', '.join(sorted(missing_cols))}"
            )
        manifest_rows = list(reader)

    if len(manifest_rows) != expected_count:
        raise ValueError(
            f"Expected {expected_count} applicant records in manifest, found {len(manifest_rows)}"
        )

    target_cvs_dir.mkdir(parents=True, exist_ok=True)
    target_mapping_csv.parent.mkdir(parents=True, exist_ok=True)

    mapping_rows: list[dict[str, str]] = []
    seen_targets: set[Path] = set()

    for row in manifest_rows:
        applicant_id = (row.get("applicant_id") or "").strip()
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip()
        cv_filename = (row.get("cv_filename") or "").strip()
        cv_path_raw = (row.get("cv_path") or "").strip()

        if not all([applicant_id, name, email, cv_filename, cv_path_raw]):
            raise ValueError(f"Incomplete manifest record: {row}")

        src_path = Path(cv_path_raw)
        if not src_path.is_absolute():
            src_path = (root_path / src_path).resolve()
        else:
            src_path = src_path.resolve()

        if not src_path.is_file():
            raise FileNotFoundError(f"Source CV file not found for {applicant_id}: {src_path}")
        if src_path.name != cv_filename:
            raise ValueError(
                f"Filename mismatch for {applicant_id}: manifest cv_filename={cv_filename!r}, "
                f"cv_path basename={src_path.name!r}"
            )

        ext = src_path.suffix or Path(cv_filename).suffix
        dest_filename = f"{applicant_id}{ext}"
        dest_path = (target_cvs_dir / dest_filename).resolve()

        if dest_path == src_path:
            raise ValueError(f"Refusing to overwrite original CV file: {src_path}")
        if dest_path in seen_targets:
            raise ValueError(f"Duplicate destination filename generated: {dest_path}")
        seen_targets.add(dest_path)

        shutil.copy2(src_path, dest_path)

        try:
            rel_local_path = dest_path.relative_to(root_path).as_posix()
        except ValueError:
            rel_local_path = dest_path.as_posix()

        mapping_rows.append(
            {
                "applicant_id": applicant_id,
                "name": name,
                "email": email,
                "cv_filename": cv_filename,
                "local_path": rel_local_path,
            }
        )

    copied_files = sorted(p for p in target_cvs_dir.iterdir() if p.is_file())
    if len(copied_files) != expected_count:
        raise RuntimeError(
            f"Verification failed: expected {expected_count} files in {target_cvs_dir}, "
            f"found {len(copied_files)}"
        )

    with target_mapping_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_FIELDNAMES)
        writer.writeheader()
        writer.writerows(mapping_rows)

    return mapping_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy manifest CVs renamed by applicant_id and generate Google Drive upload mapping CSV."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_CSV,
        help="Path to applicants_manifest.csv",
    )
    parser.add_argument(
        "--cvs-dir",
        type=Path,
        default=DEFAULT_CVS_DIR,
        help="Destination directory for renamed CV copies (default: drive_upload/CVs)",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=DEFAULT_MAPPING_CSV,
        help="Destination path for mapping CSV (default: drive_upload/cv_upload_mapping.csv)",
    )
    args = parser.parse_args()

    rows = prepare_drive_upload(
        manifest_csv=args.manifest,
        cvs_output_dir=args.cvs_dir,
        mapping_csv=args.mapping,
    )
    print(f"Copied and verified {len(rows)} CV files in {args.cvs_dir}")
    print(f"Mapping CSV written to {args.mapping}")
    print(f"Columns: {', '.join(MAPPING_FIELDNAMES)}")


if __name__ == "__main__":
    main()
