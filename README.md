# Daily Applicant Scoring Pipeline

## 1. Project Overview and Objective

The **Daily Applicant Scoring Pipeline** is an automated recruiting workflow designed to evaluate incoming job applicants against a target job vacancy on a daily schedule.

**Objective:** Automate end-to-end resume ingestion, text extraction, AI-powered candidate evaluation against structured job criteria, and centralized score logging so hiring teams can consistently review and prioritize applicants.

## 2. Architecture Overview

```text
Google Sheets (Applicant Source)  +  Google Drive (Private CV Files)
       │                                      │
       │  (Google Service Account: Viewer)    │
       ▼                                      ▼
Python Pipeline Runner (`run_pipeline.py` / `app/services/pipeline.py`)
       ├──► Groq API – OpenAI GPT-OSS 120B (AI-Based CV Evaluation)
       └──► Coda (Scoring Results Database)
```

- **Google Sheets – Applicant Source:** Stores applicant metadata (`Name`, `Email`, `Role`, `CV_Link`). Read directly by Python via the Google Sheets API v4 using a read-only Google Cloud Service Account (`applicant-scoring-reader`).
- **Google Drive – Resume Storage:** Hosts the 40 applicant `.docx` CVs. Downloaded securely into an isolated temporary directory using authenticated Google Drive API v3 calls (`drive.readonly`) and cleaned up after each applicant.
- **Python – Backend Orchestration and CV Processing:** Handles direct Google Sheets ingestion (`run_pipeline.py`) or HTTP webhook ingestion (`app/main.py`), `.docx` / `.pdf` / `.txt` text extraction, status-aware email and SHA-256 CV deduplication, prompt construction, response validation, and downstream Coda writes.
- **Groq API (`openai/gpt-oss-120b`) – AI-Based CV Evaluation:** Evaluates extracted resume content against vacancy requirements using Structured Outputs (JSON Schema, strict mode) to generate criterion scores, CV evidence, `evidence_status` (`supported`, `insufficient`, `needs_review`), and evaluation rationales.
- **Coda – Scoring Results Database:** Persists applicant evaluation scores, recommendations, evidence, and status (`Scored` / `Failed`) in a 12-column table, supporting in-place row updates when retrying `Failed` applicants.
- **Google Apps Script (`google-apps-script/Code.gs`):** Optional helper script for mapping Drive CV links into the sheet and webhook-based triggering.

## 3. Current Project Status

**Stage:** Production-ready pipeline with direct Google Sheets & Google Drive service account integration, Groq AI scoring (`openai/gpt-oss-120b`), status-aware Coda retry mechanism, and CLI runner (`run_pipeline.py`).

- **Completed:**
  - Dataset inspection and structure validation.
  - Applicant manifest generation script (`prepare_applicants.py`) that selects CVs 1–40 (30 annotated, 10 unannotated), assigns synthetic candidate identities for Vacancy ID 90, preserves original resume files untouched, and flags near-duplicate resumes (`applicants_manifest.csv`).
  - Direct Google Sheets client (`app/integrations/google_sheets_client.py`) authenticating via Google Service Account JSON (`secrets/google-service-account.json`), supporting configurable column mappings, skipping empty rows, and isolating invalid rows.
  - Authenticated Google Drive CV downloader (`GoogleDriveDownloader` in `app/services/pipeline.py`) using Google Drive API v3 (`drive.readonly`) with service account credentials.
  - Production CLI runner (`run_pipeline.py`) supporting `--dry-run`, `--limit N`, `--email EMAIL`, `--retry-failed`, and `--applicant-ids`, with file logging under `logs/pipeline.log`, secret redaction, and overlapping execution lock protection (`logs/pipeline.lock`).
  - FastAPI backend (`app/main.py`, `app/config.py`) with `GET /health`, `POST /api/v1/applicants`, optional webhook authentication (`WEBHOOK_AUTH_TOKEN`), and YAML + `.env` configuration.
  - Modular CV parser (`app/services/cv_parser.py`) supporting DOCX, PDF, and TXT with graceful handling of empty, missing, and corrupted files.
  - AI scoring engine (`app/services/scoring_engine.py`) using Groq (`openai/gpt-oss-120b`) with Structured Outputs (JSON Schema, strict mode) for criterion-level evaluation + CV evidence extraction, `needs_review` flagging for ambiguous qualifications, deterministic Python weighting, bounded exponential backoff (`429`/`500`/`502`/`503`/timeouts), and threshold classification (`Shortlist` / `Maybe` / `Reject`).
  - Status-aware duplicate detector (`app/services/duplicate_detector.py`) checking normalized emails, SHA-256 CV file hashes, and existing Coda statuses (`Scored`, `Failed`, `Processing`).
  - Coda API integration (`app/integrations/coda_client.py`) mapping all 12 `Applicants` columns (`insert_applicant_record` via `POST` and `update_applicant_record` via `PUT` for retries, pre-flight payload type validation, and token redaction).
  - Google Apps Script integration (`google-apps-script/Code.gs`) for Drive CV link mapping and optional webhook posting.
  - Comprehensive unit test suite under `tests/` (using synthetic samples and mocked external APIs) and 3-applicant local E2E runner (`run_e2e_integration_test.py`).

### Project Structure

```text
applicant-scoring/
├── .github/
│   └── workflows/
│       └── daily-pipeline.yml          # PRIMARY daily automation (GitHub Actions, 09:00 AM IST / 03:30 UTC)
├── app/
│   ├── __init__.py
│   ├── main.py                         # FastAPI application (GET /health, POST /api/v1/applicants)
│   ├── config.py                       # Loads config.yaml and environment variables
│   ├── services/
│   │   ├── cv_parser.py                # DOCX / PDF / TXT text extraction
│   │   ├── duplicate_detector.py       # Status-aware email & SHA-256 CV deduplication
│   │   ├── pipeline.py                 # End-to-end orchestrator + authenticated Drive downloader
│   │   └── scoring_engine.py           # Groq (openai/gpt-oss-120b) evaluation + deterministic scoring
│   └── integrations/
│       ├── coda_client.py              # Coda API client (POST insert + PUT retry update)
│       └── google_sheets_client.py     # Read-only Google Sheets API v4 client (Service Account)
├── google-apps-script/
│   └── Code.gs                         # Drive link mapper & optional Apps Script trigger
├── scripts/
│   ├── setup_daily_task.ps1            # BACKUP automation: registers the 9:00 AM daily Windows Task
│   ├── remove_daily_task.ps1           # Unregisters the Windows Scheduled Task
│   ├── check_daily_task.ps1            # Displays task status, last result, lock state & log summary
│   └── run_pipeline_now.ps1            # Manually executes the pipeline with optional CLI flags
├── logs/                               # Rotating & date-based logs + execution lock (gitignored)
├── secrets/                            # Local service account JSON credentials (gitignored)
├── tests/                              # Unit tests (mocked APIs & synthetic CV samples)
├── run_pipeline.py                     # Main production CLI runner (Google Sheets -> Drive -> Groq -> Coda)
├── run_e2e_integration_test.py         # Local E2E integration test (up to 3 applicants, DRY_RUN default)
├── config.yaml                         # Groq, role requirements, weights, thresholds, Coda & Sheets settings
├── .env.example                        # Placeholder environment variable names
├── requirements.txt                    # Python dependencies
├── prepare_applicants.py               # Synthetic applicant manifest generator
├── applicants_manifest.csv             # Generated synthetic manifest (CVs 1–40)
└── README.md
```

## 4. Technology Stack

- **Data Intake & Scheduling:** Google Sheets, Google Apps Script
- **Backend & Document Processing:** Python 3, FastAPI, Uvicorn, Pydantic, PyYAML, python-dotenv, python-docx, pypdf, httpx
- **AI Evaluation:** Groq Python SDK (`groq`) — `openai/gpt-oss-120b` (`https://api.groq.com/openai/v1`)
- **Results Storage & Reporting:** Coda API
- **Testing & Version Control:** pytest, Git & GitHub

## 5. Setup and Run Instructions

### Prerequisites
- Python 3.11+ installed locally.
- (Optional) Source dataset placed locally under `Dataset/` if re-running `prepare_applicants.py` (excluded from Git).

### 1. Create and Activate a Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
python -m pip install -r requirements.txt
```

> **Windows Application Control note:** Unsigned launcher shims inside `.venv\Scripts\` (such as `pip.exe`, `pytest.exe`, and `uvicorn.exe`) may be blocked by Windows Application Control / Smart App Control. Always invoke tools through the signed Python interpreter instead:
> - `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
> - `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload`
> - `.\.venv\Scripts\python.exe -m pytest -v`

### 3. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your local values (never commit `.env`):

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

**macOS / Linux:**
```bash
cp .env.example .env
```

Review `config.yaml` to adjust placeholder role requirements, scoring weights, thresholds, or Coda table settings.

### 4. Run the FastAPI Backend

```bash
uvicorn app.main:app --reload
```

### 5. Verify the Health Endpoint

Send a `GET` request to `http://127.0.0.1:8000/health`:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:
```json
{
  "status": "ok",
  "service": "Daily Applicant Scoring Pipeline",
  "version": "0.1.0",
  "environment": "development"
}
```

Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

### 6. Run Unit Tests

```bash
pytest -v
```

All tests use synthetic sample CVs and mocked API clients (no real Groq or Coda API calls are made).

### 7. Direct Google Sheets & Google Drive Setup (`run_pipeline.py`)

#### 7.1 Google Cloud Project & Required APIs
1. In the Google Cloud Console (e.g., your project such as `Default Gemini Project`), navigate to **APIs & Services → Library**.
2. Enable both of the following APIs:
   - **Google Sheets API** (`sheets.googleapis.com`) — used by `GoogleSheetsClient` (`spreadsheets.readonly` scope) to read applicant rows.
   - **Google Drive API** (`drive.googleapis.com`) — used by `GoogleDriveDownloader` (`drive.readonly` scope) to download private CV files without requiring public sharing links.

#### 7.2 Service Account Setup
1. Go to **IAM & Admin → Service Accounts** and create (or select) the service account:
   - Name: `applicant-scoring-reader`
2. Under the **Keys** tab, create a new JSON key and save it locally at:
   ```text
   secrets/google-service-account.json
   ```
   *(The `secrets/` directory and `*.json` files are excluded via `.gitignore` and must never be committed.)*

#### 7.3 Spreadsheet & Google Drive Folder Sharing
1. Copy the `client_email` address from `secrets/google-service-account.json` (e.g. `applicant-scoring-reader@<project-id>.iam.gserviceaccount.com`).
2. **Share the Google Sheet:** Open your applicant Google Sheet, click **Share**, and grant the service account email **Viewer** (read-only) access.
   - Ensure Row 1 contains the headers configured in `config.yaml`: `Name`, `Email`, `Role`, `CV_Link`.
3. **Share the Google Drive CV Folder:** Open the Google Drive folder containing the 40 applicant CVs (`APP-001.docx`–`APP-040.docx`), click **Share**, and grant the same service account email **Viewer** access.

#### 7.4 Environment & `config.yaml` Configuration
In `.env`:
```ini
GOOGLE_APPLICATION_CREDENTIALS=secrets/google-service-account.json
GOOGLE_SHEETS_ID=your_google_spreadsheet_id
GOOGLE_SHEET_TAB=Sheet1
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=openai/gpt-oss-120b
CODA_API_TOKEN=your_coda_api_token
CODA_DOC_ID=XjS-p8Qhai
CODA_TABLE_ID=grid-Rm8rg-hWVB
```

In `config.yaml`:
```yaml
google_sheets:
  spreadsheet_id: ""
  worksheet_name: "Sheet1"
  columns:
    name: "Name"
    email: "Email"
    role: "Role"
    cv_link: "CV_Link"
```

#### 7.5 Running `run_pipeline.py`

All executions write structured, secret-redacted logs to `logs/pipeline.log` and acquire an exclusive process lock (`logs/pipeline.lock`) to prevent overlapping runs.

- **Dry-run from Google Sheets (no live Groq calls and no Coda writes):**
  ```powershell
  .\.venv\Scripts\python.exe run_pipeline.py --dry-run --limit 3
  ```
- **Default production run (reads Google Sheets, processes only new applicants, skips `Scored` and `Failed` applicants):**
  ```powershell
  .\.venv\Scripts\python.exe run_pipeline.py
  ```
- **Process a bounded number of new applicants:**
  ```powershell
  .\.venv\Scripts\python.exe run_pipeline.py --limit 5
  ```
- **Process a specific applicant by email or Applicant ID:**
  ```powershell
  .\.venv\Scripts\python.exe run_pipeline.py --email candidate001@example.com
  .\.venv\Scripts\python.exe run_pipeline.py --applicant-ids APP-001
  ```
- **Retry only `Failed` applicants (updates existing Coda rows in place via `PUT` without creating duplicate rows):**
  ```powershell
  .\.venv\Scripts\python.exe run_pipeline.py --retry-failed
  ```

#### 7.6 Daily Automation Architecture (Primary vs. Backup)

| Platform | Role | Default State | Daily Schedule | Concurrency Protection |
| :--- | :--- | :--- | :--- | :--- |
| **GitHub Actions** ([.github/workflows/daily-pipeline.yml](file:///c:/projects/applicant-scoring/.github/workflows/daily-pipeline.yml)) | **PRIMARY** daily automation platform | Enabled on GitHub | **9:00 AM IST** (`03:30 UTC`, cron `30 3 * * *`) | GitHub Actions `concurrency` group (`daily-applicant-scoring-pipeline`, `cancel-in-progress: false`) + runner-local `logs/pipeline.lock` |
| **Windows Task Scheduler** (`scripts/*.ps1`) | **BACKUP** automation platform only | **Unregistered** by default | **9:00 AM local time** (only when manually activated during failover) | Windows Task Scheduler `MultipleInstances = IgnoreNew` + local `logs/pipeline.lock` |

> **CRITICAL — Cross-Machine Concurrency Limitation:**
> - **Never activate both schedules simultaneously.**
> - The Python execution lock (`logs/pipeline.lock`) is a **local filesystem lock** on whichever machine is executing `run_pipeline.py`. A lock file on your local Windows machine **does not** coordinate with a cloud runner in GitHub Actions (and vice versa).
> - Running both schedules at the same time can cause race conditions between Coda duplicate checks and Coda row insertions. Always keep Windows Task Scheduler **unregistered** during normal operation when GitHub Actions is active.

---

#### 7.7 Primary Automation: GitHub Actions (`.github/workflows/daily-pipeline.yml`)

1. **Schedule:**
   - Runs automatically every day at **9:00 AM IST** (**03:30 UTC**) via `cron: "30 3 * * *"`.
   - Scheduled runs always execute live processing (`python run_pipeline.py`), processing only new applicants and skipping `Scored` and `Failed` applicants.

2. **Required GitHub Repository Secrets:**
   In your GitHub repository (`AamirHamzaKazi/applicant-scoring`), navigate to **Settings → Secrets and variables → Actions → New repository secret** and add the following 7 secrets:

   | Secret Name | Value / Description |
   | :--- | :--- |
   | `GROQ_API_KEY` | Your Groq API key (`gsk_...`). |
   | `GROQ_MODEL` | `openai/gpt-oss-120b` |
   | `CODA_API_TOKEN` | Your Coda API bearer token. |
   | `CODA_DOC_ID` | `XjS-p8Qhai` (target Coda document ID). |
   | `CODA_TABLE_ID` | `grid-Rm8rg-hWVB` (target Coda table ID). |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire JSON content** of your local Google Service Account key file (`secrets/*.json`), pasted verbatim. |
   | `GOOGLE_SHEETS_ID` | Your Google Spreadsheet ID (from `.env` `GOOGLE_SHEET_ID` / `GOOGLE_SHEETS_ID`). |

   At runtime, the workflow writes `GOOGLE_SERVICE_ACCOUNT_JSON` to an isolated temporary file (`${RUNNER_TEMP}/google-service-account.json` with `0600` permissions), points `GOOGLE_APPLICATION_CREDENTIALS` to that file, executes `run_pipeline.py`, and deletes the temporary credentials file in an `if: always()` cleanup step. No CVs, logs, or credentials are uploaded as artifacts.

3. **How to Run Manually or Run a Dry-Run in GitHub Actions:**
   - Go to **Actions → Daily Applicant Scoring Pipeline → Run workflow**.
   - Configure the optional `workflow_dispatch` inputs:
     - **`dry_run`**:
       - Set to `true` for a **safe dry-run** (reads Google Sheets, downloads CVs from Drive, checks Coda duplicates, uses the local mock Groq evaluator, and **never calls Groq or writes to Coda**).
       - Leave `false` for a **live manual run**.
     - **`limit`**:
       - Optional positive integer (e.g., `3`) to cap the number of eligible applicants processed in that run, or leave blank to process all eligible new applicants.

4. **How to Disable and Re-Enable GitHub Scheduled Execution:**
   - **To Disable:** Go to **Actions → Daily Applicant Scoring Pipeline**, click the **`...`** menu in the top-right corner, and select **Disable workflow** (or run `gh workflow disable daily-pipeline.yml`).
   - **To Re-Enable:** First ensure the Windows backup task is unregistered (`.\scripts\remove_daily_task.ps1`), then go to **Actions → Daily Applicant Scoring Pipeline** and click **Enable workflow** (or run `gh workflow enable daily-pipeline.yml`).

---

#### 7.8 Operational Failover Procedure (GitHub Actions ↔ Windows Backup)

- **Normal Operation:**
  - **GitHub Actions** is enabled and runs daily at **9:00 AM IST** (`03:30 UTC`).
  - **Windows Task Scheduler** remains **unregistered** (`Registered : False` when checking `.\scripts\check_daily_task.ps1`).

- **If GitHub Actions Becomes Unavailable (Failover to Windows Backup):**
  1. **Disable or pause the GitHub Actions scheduled workflow** in GitHub (**Actions → Daily Applicant Scoring Pipeline → `...` → Disable workflow**).
  2. **Verify no GitHub workflow is currently running** under the **Actions** tab.
  3. **Activate Windows Task Scheduler as the backup:**
     ```powershell
     powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_daily_task.ps1
     ```
  4. **Once GitHub Actions is restored**, first **disable (unregister) the Windows scheduled task** before re-enabling GitHub scheduling:
     ```powershell
     powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\remove_daily_task.ps1
     ```
     Verify `Registered : False` via `.\scripts\check_daily_task.ps1`, and only then re-enable the workflow in GitHub Actions.

---

#### 7.9 Backup Automation: Windows Task Scheduler (`scripts/`)

PowerShell management scripts are provided under `scripts/` to manage the local Windows backup schedule and run manual commands:

| Script | Purpose |
| :--- | :--- |
| [scripts/setup_daily_task.ps1](file:///c:/projects/applicant-scoring/scripts/setup_daily_task.ps1) | Registers the backup `Daily Applicant Scoring Pipeline` task to run daily at **9:00 AM local time** under the current Windows user (`Interactive` logon, no password stored). |
| [scripts/check_daily_task.ps1](file:///c:/projects/applicant-scoring/scripts/check_daily_task.ps1) | Displays task registration state, next run time, last run time, last execution result code, active lock state, and the most recent summary log entry. |
| [scripts/run_pipeline_now.ps1](file:///c:/projects/applicant-scoring/scripts/run_pipeline_now.ps1) | Manually runs the pipeline immediately (`-DryRun`, `-Limit`, `-Email`, `-RetryFailed`, `-ApplicantIds`, or `-UseScheduledTask`). |
| [scripts/remove_daily_task.ps1](file:///c:/projects/applicant-scoring/scripts/remove_daily_task.ps1) | Unregisters the `Daily Applicant Scoring Pipeline` scheduled task cleanly. |

1. **How to Activate the Windows Backup Schedule (Only When GitHub Actions Is Disabled):**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_daily_task.ps1
   ```
   *(To validate paths and settings without registering the task, add `-ValidateOnly`.)*

2. **How to Deactivate the Windows Backup Schedule:**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\remove_daily_task.ps1
   ```

3. **How to Check Windows Task & Lock Status:**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check_daily_task.ps1
   ```

4. **How to Execute Manually on Windows:**
   - **Standard manual run (processes only new applicants):**
     ```powershell
     powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_pipeline_now.ps1
     ```
   - **Safe dry-run (no live Groq API calls and no Coda writes):**
     ```powershell
     powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_pipeline_now.ps1 -DryRun -Limit 3
     ```

5. **Where Local Execution Logs Are Stored:**
   - Persistent logs are stored in `C:\projects\applicant-scoring\logs\` (excluded from Git):
     - **Rotating log:** `logs/pipeline.log` (rotates automatically at 5 MB, keeping up to 14 backups).
     - **Date-based daily log:** `logs/pipeline_YYYY-MM-DD.log` (one file per calendar day).
   - Every run logs its start/end timestamps, total applicants fetched, new applicants found, skipped count, scored count, failed count, total duration, and error details. All API keys, tokens, and credentials are automatically redacted.

6. **What Happens if the Computer Is Switched Off at 9:00 AM (During Windows Backup Mode):**
   - The Windows scheduled task is configured with **`StartWhenAvailable = True`**. If the computer is powered off, asleep, or hibernating at 9:00 AM, Windows Task Scheduler will automatically trigger the missed run as soon as the machine is turned back on and the user logs in.

7. **What Happens if an Execution Fails or Overlaps:**
   - **Individual applicant failure isolation:** If a single applicant's CV fails to download, fails to parse, or encounters a Groq API error (such as HTTP 429 rate-limit exhaustion after 4 retries), that applicant is recorded in Coda with `Status = Failed` and a descriptive `Error Note`, and the batch continues processing the remaining applicants.
   - **Same-platform overlapping execution protection:** GitHub Actions (`concurrency` group `daily-applicant-scoring-pipeline`), Windows Task Scheduler (`MultipleInstances = IgnoreNew`), and `run_pipeline.py` (`logs/pipeline.lock`) prevent simultaneous runs on the same platform.
   - **Crash / stale lock recovery:** If a local process terminates abnormally (e.g., power loss), the next execution checks whether the PID recorded in `logs/pipeline.lock` is still alive; if the process is no longer running, the stale lock is automatically recovered.

8. **How to Retry `Failed` Applicants Manually:**
   - Daily scheduled runs skip `Failed` applicants by default so persistent errors do not consume quota every day. To retry only applicants whose Coda status is `Failed` (updating their existing Coda row in-place via `PUT` without creating duplicates):
     ```powershell
     powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_pipeline_now.ps1 -RetryFailed
     ```
   - Or directly via Python:
     ```powershell
     .\.venv\Scripts\python.exe run_pipeline.py --retry-failed
     ```

### 8. Local End-to-End Integration Test (`run_e2e_integration_test.py`)

This script exercises CV parsing (`drive_upload/CVs/`), Coda duplicate detection, Groq (`openai/gpt-oss-120b`) scoring, 12-column Coda validation, and live Coda insertion/readback on **up to 3 selected applicants** from `applicants_manifest.csv` (it refuses to process more than 3 applicants at once).

**Prerequisites:**
- Ensure `drive_upload/CVs/` has been prepared (`python prepare_drive_upload.py`).
- Configure `GROQ_API_KEY`, `GROQ_MODEL=openai/gpt-oss-120b`, `CODA_API_TOKEN`, `CODA_DOC_ID`, and `CODA_TABLE_ID` in `.env` (never commit `.env`).

**Run in Safe Dry-Run Mode (default — no real Groq API calls and no Coda writes):**
```bash
python run_e2e_integration_test.py --dry-run
```
*(Uses a deterministic local mock Groq client, runs read-only Coda `GET` duplicate checks, validates all 12 Coda columns, and skips `POST` writes. Add `--live-groq` to test real Groq scoring while still skipping Coda writes.)*

**Run in Live Integration Mode (writes selected records to Coda & verifies readback):**
```bash
python run_e2e_integration_test.py --applicant-ids APP-001 --live
```
*(Alternatively, set `DRY_RUN=false` in the environment. After insertion, the script polls Coda to verify the persisted `Status`, `Total Score`, `Recommendation`, `Reason`, and `Sub-scores` evidence.)*

**Select up to 3 specific applicants:**
```bash
python run_e2e_integration_test.py --applicant-ids APP-001 APP-002 APP-004 --dry-run
```

### 8. (Optional) Regenerate Applicant Manifest

```bash
python prepare_applicants.py
```

### 9. Google Apps Script Integration Setup (`google-apps-script/Code.gs`)

1. **Prepare the Google Sheet:**
   - Ensure Row 1 contains the required headers (case-insensitive):
     - `Name`
     - `Email`
     - `Role`
     - `CV_Link`
   - On the first live run, the script automatically adds `Submission_Status`, `Submitted_At`, and `Last_Error` columns to track processed rows without altering your input columns.

2. **Add the Script to Apps Script:**
   - Open your Google Sheet and go to **Extensions → Apps Script** (or create a standalone project at `script.google.com`).
   - Replace the default editor contents with [google-apps-script/Code.gs](google-apps-script/Code.gs) and save.

3. **Configure Script Properties (never hardcode secrets or IDs in `Code.gs`):**
   - In the Apps Script editor, go to **Project Settings → Script Properties → Add script property**.
   - Configure the following property names:
     - `DRIVE_FOLDER_ID` *(required for `populateCvLinksFromDriveFolder`)* — Google Drive folder ID containing `APP-001.docx` through `APP-040.docx`.
     - `BACKEND_URL` *(required for live POST)* — Full HTTPS URL of your FastAPI applicant scoring endpoint (once deployed or exposed via a secure tunnel; local `localhost` URLs are not reachable from Google Apps Script servers).
     - `WEBHOOK_AUTH_TOKEN` *(optional, recommended)* — Shared bearer/webhook secret matching your backend's `WEBHOOK_AUTH_TOKEN`.
     - `SPREADSHEET_ID` *(optional)* — Required only if using a standalone Apps Script project not bound to the Google Sheet.
     - `SHEET_NAME` *(optional)* — Specific tab name to read (defaults to the first sheet).
     - `TRIGGER_HOUR` *(optional)* — Hour of day (`0`–`23`) in the script's timezone for the daily trigger (defaults to `8`).
     - `DRY_RUN` *(optional)* — Set to `true` to log eligible rows without sending HTTP requests.

4. **Populate `CV_Link` from Google Drive (Manual Step, No Backend Call):**
   - Ensure `DRIVE_FOLDER_ID` is set in **Script Properties**.
   - Select **`populateCvLinksFromDriveFolder`** and click **Run** to map `APP-001.docx`–`APP-040.docx` to `Candidate 001`–`Candidate 040` in `CV_Link` without modifying `Name`, `Email`, `Role`, sharing permissions, or triggering backend scoring.

5. **Test Before Exposing the Backend:**
   - In the Apps Script editor toolbar, select **`testDryRun`** and click **Run**.
   - Grant spreadsheet permissions when prompted, then check **Execution log** to confirm columns, email deduplication, and payloads look right—even while the FastAPI backend is still running locally.

6. **Enable the Daily Time-Driven Trigger:**
   - Select **`setupDailyTrigger`** in the function dropdown and click **Run**.
   - This removes any older trigger for `processNewApplicants` and creates a daily time-driven trigger (verified under **Triggers** in the left sidebar).
   - Once your backend endpoint is publicly reachable and `BACKEND_URL` is configured, you can also run **`processNewApplicants`** manually at any time to submit pending rows immediately.

## 6. Required Environment Variables

Set the following environment variables in your local `.env` file or deployment environment (see `.env.example`; never commit actual values):

- `APP_ENV` — Runtime environment name (e.g., `development`, `production`).
- `APP_HOST` — Host address for the backend server.
- `APP_PORT` — Port number for the backend server.
- `CONFIG_PATH` — Optional override path to `config.yaml`.
- `GROQ_API_KEY` — API key for authenticating requests to the Groq API.
- `GROQ_MODEL` — Groq model identifier (`openai/gpt-oss-120b`).
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
