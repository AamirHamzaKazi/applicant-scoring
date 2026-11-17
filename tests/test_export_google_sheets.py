"""Unit tests for export_for_google_sheets.py."""

from __future__ import annotations

import csv
from pathlib import Path

from export_for_google_sheets import (
    DEFAULT_INPUT_CSV,
    DEFAULT_OUTPUT_CSV,
    OUTPUT_FIELDNAMES,
    export_applicants_for_google_sheets,
)


def test_export_preserves_40_synthetic_applicants_and_exact_columns(tmp_path: Path) -> None:
    """Verify exact header mapping, blank CV_Link, and preservation of all 40 synthetic records."""
    original_manifest_bytes = DEFAULT_INPUT_CSV.read_bytes()
    output_file = tmp_path / "applicants_for_google_sheets.csv"

    exported = export_applicants_for_google_sheets(
        input_csv=DEFAULT_INPUT_CSV,
        output_csv=output_file,
    )

    assert len(exported) == 40
    assert output_file.is_file()

    with output_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["Name", "Email", "Role", "CV_Link"]
        assert reader.fieldnames == OUTPUT_FIELDNAMES
        rows = list(reader)

    assert len(rows) == 40
    for idx, row in enumerate(rows, start=1):
        assert row["Name"] == f"Candidate {idx:03d}"
        assert row["Email"] == f"candidate{idx:03d}@example.com"
        assert row["Role"] == "Junior Level Software Developer (1-4 years experience)"
        assert row["CV_Link"] == ""

    # Original manifest must remain untouched
    assert DEFAULT_INPUT_CSV.read_bytes() == original_manifest_bytes


def test_generated_root_export_csv_matches_requirements() -> None:
    """Verify applicants_for_google_sheets.csv in the project root has all 40 valid rows."""
    assert DEFAULT_OUTPUT_CSV.is_file()
    with DEFAULT_OUTPUT_CSV.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["Name", "Email", "Role", "CV_Link"]
        rows = list(reader)

    assert len(rows) == 40
    assert all(row["Email"].endswith("@example.com") for row in rows)
    assert all(row["CV_Link"] == "" for row in rows)
