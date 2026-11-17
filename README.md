# Daily Applicant Scoring Pipeline

## 1. Project Overview and Objective

The **Daily Applicant Scoring Pipeline** is an automated recruiting workflow designed to evaluate incoming job applicants against a target job vacancy on a daily schedule.

**Objective:** Automate end-to-end resume ingestion, text extraction, AI-powered candidate evaluation against structured job criteria, and centralized score logging so hiring teams can consistently review and prioritize applicants.

## 2. Architecture Overview

```text
Google Sheets (Applicant Source)
       │
       ▼
Google Apps Script (Daily Trigger & HTTP Client)
       │
       ▼
Python Backend (Orchestration & CV Processing)
       ├──► Gemini API (AI-Based CV Evaluation)
       └──► Coda (Scoring Results Database)
```

- **Google Sheets – Applicant Source:** Stores applicant metadata, target vacancy references, and resume links as the intake source.
- **Google Apps Script – Daily Trigger and HTTP Communication:** Runs on a scheduled daily trigger to collect pending applicant rows from Google Sheets and send them via HTTP to the Python backend.
- **Python – Backend Orchestration and CV Processing:** Handles request orchestration, `.docx` resume text extraction, near-duplicate detection, prompt construction, response parsing, and downstream database writes.
- **Gemini API – AI-Based CV Evaluation:** Evaluates extracted resume content against vacancy requirements to generate structured scores and evaluation rationales.
- **Coda – Scoring Results Database:** Persists applicant evaluation scores, rankings, and review status in a structured workspace for recruiters and hiring managers.

## 3. Current Project Status

**Stage:** Initial dataset preparation and repository setup.

- **Completed:**
  - Dataset inspection and structure validation.
  - Applicant manifest generation script (`prepare_applicants.py`) that selects CVs 1–40 (30 annotated, 10 unannotated), assigns synthetic candidate identities for Vacancy ID 90, preserves original resume files untouched, and flags near-duplicate resumes.
  - Generated `applicants_manifest.csv` containing synthetic applicant metadata only (no real PII).
  - Repository setup with `.gitignore` rules excluding raw resumes, credentials, and environment files.
- **Planned / In Progress:**
  - Google Sheets applicant source setup and Google Apps Script daily trigger implementation.
  - Python backend service for CV ingestion and Gemini API scoring integration.
  - Coda API integration for storing scoring outputs.
  - Evaluation benchmarking against the dataset's human ground-truth rankings.

## 4. Technology Stack

- **Data Intake & Scheduling:** Google Sheets, Google Apps Script
- **Backend & Document Processing:** Python 3 (standard library `zipfile`, `xml.etree.ElementTree`, `csv`, `difflib`, plus planned HTTP/API clients)
- **AI Evaluation:** Google Gemini API
- **Results Storage & Reporting:** Coda API
- **Version Control:** Git & GitHub

## 5. Setup Instructions

> *To be completed as backend services and external integrations are implemented.*

Planned setup steps will cover:
1. Cloning the repository and placing the source dataset locally under `Dataset/` (excluded from version control).
2. Creating a Python virtual environment and installing dependencies.
3. Configuring local environment variables (see below).
4. Running `python prepare_applicants.py` to generate `applicants_manifest.csv`.
5. Deploying the Python backend service and configuring the Google Apps Script daily trigger and Coda destination table.

## 6. Required Environment Variables

Set the following environment variables in your local `.env` file or deployment environment (never commit actual values):

- `GEMINI_API_KEY` — API key for authenticating requests to the Gemini API.
- `CODA_API_TOKEN` — API token for writing scoring records to Coda.
- `CODA_DOC_ID` — Identifier of the target Coda document.
- `CODA_TABLE_ID` — Identifier of the Coda table storing applicant evaluation results.
- `GOOGLE_SHEETS_ID` — Identifier of the Google Sheet acting as the applicant source.
- `GOOGLE_DRIVE_FOLDER_ID` — Identifier of the Google Drive folder hosting applicant resumes.
- `GOOGLE_APPLICATION_CREDENTIALS` — File path to Google Cloud service account credentials (if using Google APIs from Python).
- `WEBHOOK_AUTH_TOKEN` — Shared secret token used to authorize HTTP requests from Google Apps Script to the Python backend.

## 7. Dataset Information and Attribution

This project uses the **vacancy-resume-matching-dataset**, which contains **65 anonymized DOCX resumes** and **five job vacancies** sourced from data.world, along with human ground-truth rankings for the first 30 resumes produced by two annotators following senior HR specialist instructions.

To protect privacy and adhere to repository hygiene, raw resume files under `Dataset/` are excluded via `.gitignore` and are never committed to version control. All records in `applicants_manifest.csv` use synthetic candidate names and placeholder email addresses.

### Citation

If you use any part of this dataset, please cite the original authors:

```bibtex
@Article{info14080468,
  AUTHOR = {Vanetik, Natalia and Kogan, Genady},
  TITLE = {Job Vacancy Ranking with Sentence Embeddings, Keywords, and Named Entities},
  JOURNAL = {Information},
  VOLUME = {14},
  YEAR = {2023},
  NUMBER = {8},
  ARTICLE-NUMBER = {468},
  URL = {https://www.mdpi.com/2078-2489/14/8/468},
  ISSN = {2078-2489},
  DOI = {10.3390/info14080468}
}
```
