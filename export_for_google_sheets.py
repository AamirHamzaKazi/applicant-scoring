"""Export applicants_manifest.csv into a Google Sheets-compatible CSV file.

Reads the synthetic applicant manifest without modifying it and outputs
`applicants_for_google_sheets.csv` with exact headers:
    Name, Email, Role, CV_Link
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_CSV = WORKSPACE_ROOT / "applicants_manifest.csv"
DEFAULT_OUTPUT_CSV = WORKSPACE_ROOT / "applicants_for_google_sheets.csv"
OUTPUT_FIELDNAMES = ["Name", "Email", "Role", "CV_Link"]


def export_applicants_for_google_sheets(
    input_csv: Path = DEFAULT_INPUT_CSV,
    output_csv: Path = DEFAULT_OUTPUT_CSV,
) -> list[dict[str, str]]:
    """Read synthetic applicants from input_csv and write a Google Sheets CSV to output_csv."""
    input_resolved = Path(input_csv).resolve()
    output_resolved = Path(output_csv).resolve()

    if not input_resolved.is_file():
        raise FileNotFoundError(f"Manifest file not found: {input_resolved}")
    if input_resolved == output_resolved:
        raise ValueError("Output CSV path must differ from input manifest path.")

    exported_rows: list[dict[str, str]] = []
    with input_resolved.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required_source_cols = {"name", "email", "role"}
        fieldnames = set(reader.fieldnames or [])
        missing = required_source_cols - fieldnames
        if missing:
            raise ValueError(
                f"Input manifest missing required column(s): {', '.join(sorted(missing))}"
            )

        for row in reader:
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip()
            role = (row.get("role") or "").strip()

            exported_rows.append(
                {
                    "Name": name,
                    "Email": email,
                    "Role": role,
                    "CV_Link": "",
                }
            )

    output_resolved.parent.mkdir(parents=True, exist_ok=True)
    with output_resolved.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(exported_rows)

    return exported_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export applicants_manifest.csv to a Google Sheets-compatible CSV."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT_CSV,
        help="Path to input applicants_manifest.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_CSV,
        help="Path to output applicants_for_google_sheets.csv",
    )
    args = parser.parse_args()

    rows = export_applicants_for_google_sheets(
        input_csv=args.input,
        output_csv=args.output,
    )
    print(f"Exported {len(rows)} applicant records to {args.output}")
    print(f"Columns: {', '.join(OUTPUT_FIELDNAMES)}")


if __name__ == "__main__":
    main()
