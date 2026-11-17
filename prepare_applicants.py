"""Prepare a clean, reproducible applicant dataset manifest from CVs 1-40.

Reads CVs 1.docx through 40.docx without modifying or renaming any original files,
assigns synthetic candidate identities for Vacancy ID 90, detects near-duplicate
resumes, writes `applicants_manifest.csv`, and prints a dataset summary.
"""

from __future__ import annotations

import csv
from difflib import SequenceMatcher
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

WORKSPACE_ROOT = Path(__file__).resolve().parent
DATASET_DIR = WORKSPACE_ROOT / "Dataset" / "vacancy-resume-matching-dataset-main"
CV_DIR = DATASET_DIR / "CV"
VACANCIES_CSV = DATASET_DIR / "5_vacancies.csv"
OUTPUT_CSV = WORKSPACE_ROOT / "applicants_manifest.csv"

TARGET_VACANCY_ID = 90
CV_START = 1
CV_END = 40
ANNOTATED_END = 30
NEAR_DUPLICATE_THRESHOLD = 0.80

WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def load_vacancy_title(vacancies_csv: Path, vacancy_id: int) -> str:
    """Return the job_title for the specified vacancy_id from 5_vacancies.csv."""
    with vacancies_csv.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if int(row["id"]) == vacancy_id:
                return row["job_title"].strip()
    raise ValueError(f"Vacancy ID {vacancy_id} not found in {vacancies_csv}")


def extract_docx_text(docx_path: Path) -> str:
    """Extract normalized plain text from a .docx file using only stdlib."""
    with zipfile.ZipFile(docx_path, "r") as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    tokens = [node.text for node in root.iterfind(".//w:t", WORD_NS) if node.text]
    return " ".join(" ".join(tokens).split())


def detect_near_duplicates(
    cv_texts: dict[int, str], threshold: float = NEAR_DUPLICATE_THRESHOLD
) -> tuple[dict[int, list[tuple[int, float]]], list[tuple[int, int, float]]]:
    """Detect near-duplicate CV pairs using normalized SequenceMatcher similarity."""
    cv_ids = sorted(cv_texts.keys())
    matches_by_cv: dict[int, list[tuple[int, float]]] = {cv_id: [] for cv_id in cv_ids}
    duplicate_pairs: list[tuple[int, int, float]] = []

    for idx_a, cv_a in enumerate(cv_ids):
        text_a = cv_texts[cv_a]
        len_a = len(text_a)
        for cv_b in cv_ids[idx_a + 1 :]:
            text_b = cv_texts[cv_b]
            len_b = len(text_b)
            if max(len_a, len_b) == 0:
                continue
            if min(len_a, len_b) / max(len_a, len_b) < threshold * 0.75:
                continue

            matcher = SequenceMatcher(None, text_a, text_b)
            if matcher.quick_ratio() < threshold:
                continue

            similarity = matcher.ratio()
            if similarity >= threshold:
                matches_by_cv[cv_a].append((cv_b, similarity))
                matches_by_cv[cv_b].append((cv_a, similarity))
                duplicate_pairs.append((cv_a, cv_b, similarity))

    return matches_by_cv, duplicate_pairs


def build_manifest(
    role: str,
    cv_texts: dict[int, str],
    matches_by_cv: dict[int, list[tuple[int, float]]],
) -> list[dict[str, object]]:
    """Build manifest rows for CVs 1-40 with synthetic candidate identities."""
    rows: list[dict[str, object]] = []
    for cv_num in range(CV_START, CV_END + 1):
        cv_filename = f"{cv_num}.docx"
        cv_abs_path = CV_DIR / cv_filename
        if not cv_abs_path.is_file():
            raise FileNotFoundError(f"Expected CV file not found: {cv_abs_path}")

        rel_cv_path = cv_abs_path.relative_to(WORKSPACE_ROOT).as_posix()
        matches = matches_by_cv.get(cv_num, [])
        is_near_duplicate = bool(matches)
        near_duplicate_cv = ", ".join(f"{other}.docx" for other, _ in matches)
        near_duplicate_similarity = (
            ", ".join(f"{score:.3f}" for _, score in matches) if matches else ""
        )

        rows.append(
            {
                "applicant_id": f"APP-{cv_num:03d}",
                "name": f"Candidate {cv_num:03d}",
                "email": f"candidate{cv_num:03d}@example.com",
                "role": role,
                "cv_filename": cv_filename,
                "cv_path": rel_cv_path,
                "vacancy_id": TARGET_VACANCY_ID,
                "annotation_available": cv_num <= ANNOTATED_END,
                "is_near_duplicate": is_near_duplicate,
                "near_duplicate_cv": near_duplicate_cv,
                "near_duplicate_similarity": near_duplicate_similarity,
            }
        )
    return rows


def write_manifest(output_csv: Path, rows: list[dict[str, object]]) -> None:
    """Write the applicant manifest CSV."""
    fieldnames = [
        "applicant_id",
        "name",
        "email",
        "role",
        "cv_filename",
        "cv_path",
        "vacancy_id",
        "annotation_available",
        "is_near_duplicate",
        "near_duplicate_cv",
        "near_duplicate_similarity",
    ]
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(
    rows: list[dict[str, object]],
    role: str,
    duplicate_pairs: list[tuple[int, int, float]],
    output_csv: Path,
) -> None:
    """Print a concise summary of the generated applicant dataset."""
    total = len(rows)
    annotated_count = sum(1 for r in rows if r["annotation_available"])
    unannotated_count = total - annotated_count
    flagged_count = sum(1 for r in rows if r["is_near_duplicate"])

    print("=== Applicant Dataset Preparation Summary ===")
    print(f"Manifest file         : {output_csv.relative_to(WORKSPACE_ROOT).as_posix()}")
    print(f"Target vacancy        : {TARGET_VACANCY_ID} ({role})")
    print(f"Total applicants      : {total} (CVs {CV_START}-{CV_END})")
    print(f"  - Annotated         : {annotated_count} (CVs {CV_START}-{ANNOTATED_END})")
    print(f"  - Unannotated       : {unannotated_count} (CVs {ANNOTATED_END + 1}-{CV_END})")
    print(f"Near-duplicate threshold : {NEAR_DUPLICATE_THRESHOLD:.2f}")
    print(f"Flagged near-duplicates  : {flagged_count} records ({len(duplicate_pairs)} pair(s))")
    for cv_a, cv_b, score in duplicate_pairs:
        print(
            f"  - APP-{cv_a:03d} ({cv_a}.docx) <-> APP-{cv_b:03d} ({cv_b}.docx) "
            f"[similarity={score:.3f}]"
        )
    print("Original CV files     : Preserved untouched (no renames or edits).")


def main() -> None:
    role = load_vacancy_title(VACANCIES_CSV, TARGET_VACANCY_ID)

    cv_texts: dict[int, str] = {}
    for cv_num in range(CV_START, CV_END + 1):
        cv_path = CV_DIR / f"{cv_num}.docx"
        if not cv_path.is_file():
            raise FileNotFoundError(f"Missing CV file: {cv_path}")
        cv_texts[cv_num] = extract_docx_text(cv_path)

    matches_by_cv, duplicate_pairs = detect_near_duplicates(
        cv_texts, threshold=NEAR_DUPLICATE_THRESHOLD
    )
    rows = build_manifest(role, cv_texts, matches_by_cv)
    write_manifest(OUTPUT_CSV, rows)
    print_summary(rows, role, duplicate_pairs, OUTPUT_CSV)


if __name__ == "__main__":
    main()
