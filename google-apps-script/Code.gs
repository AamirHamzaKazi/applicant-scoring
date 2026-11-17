/**
 * Google Apps Script integration for the Daily Applicant Scoring Pipeline.
 *
 * Responsibilities:
 *  - Read applicant rows (Name, Email, Role, CV_Link) from a Google Spreadsheet.
 *  - Identify applicants by normalized email and skip any already submitted.
 *  - Send unsubmitted applicants via HTTP POST to the Python FastAPI backend.
 *  - Track submission status in both Sheet tracking columns and Script Properties.
 *  - Configure and run a daily time-driven trigger.
 *
 * Security notes:
 *  - Never hardcode BACKEND_URL, API keys, or webhook tokens in this script.
 *  - Configure runtime settings exclusively via Project Settings -> Script Properties.
 */

var REQUIRED_COLUMNS = ["Name", "Email", "Role", "CV_Link"];

var TRACKING_COLUMNS = {
  STATUS: "Submission_Status",
  SUBMITTED_AT: "Submitted_At",
  LAST_ERROR: "Last_Error"
};

var STATUS_VALUES = {
  SUBMITTED: "Submitted",
  FAILED: "Failed",
  SKIPPED_INVALID: "Invalid"
};

var SCRIPT_PROP_KEYS = {
  BACKEND_URL: "BACKEND_URL",
  WEBHOOK_AUTH_TOKEN: "WEBHOOK_AUTH_TOKEN",
  SPREADSHEET_ID: "SPREADSHEET_ID",
  SHEET_NAME: "SHEET_NAME",
  DRIVE_FOLDER_ID: "DRIVE_FOLDER_ID",
  TRIGGER_HOUR: "TRIGGER_HOUR",
  DRY_RUN: "DRY_RUN",
  SUBMITTED_EMAILS: "SUBMITTED_EMAILS"
};

var EXPECTED_CV_MAPPING_COUNT = 40;
var DEFAULT_TRIGGER_HOUR = 8; // 08:00 in the spreadsheet/script timezone


// ============================================================================
// Public Entrypoints (visible in the Apps Script Run menu)
// ============================================================================

/**
 * Main pipeline entrypoint: reads new applicants and posts them to the backend.
 * Runs automatically on the daily time-driven trigger or manually from the editor.
 */
function processNewApplicants() {
  return runPipeline_({ dryRunOverride: null });
}

/**
 * Validates sheet structure and previews applicants eligible for submission
 * without making HTTP requests. Safe to run before the backend is publicly exposed.
 */
function testDryRun() {
  return runPipeline_({ dryRunOverride: true });
}

/**
 * Installs an idempotent daily time-driven trigger for `processNewApplicants`.
 * Re-running removes older triggers for the same handler before creating a new one.
 */
function setupDailyTrigger() {
  var config = getConfig_();
  var removedCount = removeTriggersForHandler_("processNewApplicants");
  if (removedCount > 0) {
    logInfo_("Removed existing daily trigger(s)", { count: removedCount });
  }

  var trigger = ScriptApp.newTrigger("processNewApplicants")
    .timeBased()
    .everyDays(1)
    .atHour(config.triggerHour)
    .create();

  logInfo_("Created daily time-driven trigger", {
    handler: "processNewApplicants",
    triggerId: trigger.getUniqueId(),
    hour: config.triggerHour
  });

  return trigger.getUniqueId();
}

/**
 * Removes all time-driven triggers associated with `processNewApplicants`.
 */
function removeDailyTriggers() {
  var removedCount = removeTriggersForHandler_("processNewApplicants");
  logInfo_("Removed triggers for processNewApplicants", { removedCount: removedCount });
  return removedCount;
}

/**
 * Manually maps APP-001.docx .. APP-040.docx from Google Drive (DRIVE_FOLDER_ID)
 * into the existing CV_Link column for Candidate 001 .. Candidate 040.
 *
 * Guarantees:
 *  - Reads DRIVE_FOLDER_ID from Script Properties.
 *  - Updates only the CV_Link column; Name, Email, and Role are untouched.
 *  - Never modifies file sharing permissions.
 *  - Never triggers backend HTTP processing.
 *  - Validates that exactly 40 CV files are mapped.
 */
function populateCvLinksFromDriveFolder() {
  var summary = {
    startedAt: new Date().toISOString(),
    docxFilesFound: 0,
    mappedCount: 0,
    missingFiles: [],
    unmatchedApplicants: [],
    unexpectedFiles: []
  };

  try {
    var config = getConfig_();
    var props = PropertiesService.getScriptProperties();
    var driveFolderId = trimOrEmpty_(props.getProperty(SCRIPT_PROP_KEYS.DRIVE_FOLDER_ID));
    if (!driveFolderId) {
      throw new Error(
        "Missing Script Property 'DRIVE_FOLDER_ID'. Set DRIVE_FOLDER_ID in Project Settings -> Script Properties."
      );
    }

    var sheet = getTargetSheet_(config);
    var colMap = getRequiredColumnMap_(sheet);
    var driveFilesByName = listDocxFilesInFolder_(driveFolderId);
    summary.docxFilesFound = Object.keys(driveFilesByName).length;

    var expectedFiles = buildExpectedDriveFileMap_(EXPECTED_CV_MAPPING_COUNT);
    for (var expectedFilename in expectedFiles) {
      if (!Object.prototype.hasOwnProperty.call(driveFilesByName, expectedFilename)) {
        summary.missingFiles.push(expectedFilename);
      }
    }

    for (var actualFilename in driveFilesByName) {
      if (
        Object.prototype.hasOwnProperty.call(driveFilesByName, actualFilename) &&
        !Object.prototype.hasOwnProperty.call(expectedFiles, actualFilename)
      ) {
        summary.unexpectedFiles.push(actualFilename);
      }
    }

    if (summary.missingFiles.length > 0) {
      logWarn_("Missing expected DOCX files in Drive folder", {
        count: summary.missingFiles.length,
        missingFiles: summary.missingFiles
      });
    }
    if (summary.unexpectedFiles.length > 0) {
      logWarn_("Found extra DOCX files outside APP-001.docx..APP-040.docx", {
        count: summary.unexpectedFiles.length,
        unexpectedFiles: summary.unexpectedFiles
      });
    }

    var rows = readApplicantRows_(sheet, colMap);
    var seenCandidateIndices = {};

    for (var i = 0; i < rows.length; i++) {
      var applicant = rows[i];
      if (applicant.isCompletelyEmpty) {
        continue;
      }

      var candidateNum = parseCandidateNumber_(applicant.name, EXPECTED_CV_MAPPING_COUNT);
      if (candidateNum === null) {
        summary.unmatchedApplicants.push({
          rowNumber: applicant.rowNumber,
          name: applicant.name,
          reason: "Name does not match Candidate 001 through Candidate 040"
        });
        logWarn_("Unmatched applicant row in sheet", {
          rowNumber: applicant.rowNumber,
          name: applicant.name,
          reason: "Expected Candidate 001..Candidate 040"
        });
        continue;
      }

      var paddedNum = padThreeDigits_(candidateNum);
      var targetFilename = "APP-" + paddedNum + ".docx";
      var driveFile = driveFilesByName[targetFilename];

      if (!driveFile) {
        summary.unmatchedApplicants.push({
          rowNumber: applicant.rowNumber,
          name: applicant.name,
          expectedFile: targetFilename,
          reason: "Corresponding DOCX file missing in Drive folder"
        });
        logWarn_("No matching Drive DOCX file for applicant", {
          rowNumber: applicant.rowNumber,
          name: applicant.name,
          expectedFile: targetFilename
        });
        continue;
      }

      if (seenCandidateIndices[paddedNum]) {
        summary.unmatchedApplicants.push({
          rowNumber: applicant.rowNumber,
          name: applicant.name,
          expectedFile: targetFilename,
          reason: "Duplicate Candidate row in sheet (already mapped at row " + seenCandidateIndices[paddedNum] + ")"
        });
        logWarn_("Duplicate Candidate row skipped during CV_Link mapping", {
          rowNumber: applicant.rowNumber,
          name: applicant.name,
          firstRowNumber: seenCandidateIndices[paddedNum]
        });
        continue;
      }

      // Populate ONLY CV_Link; never modify Name, Email, Role, or file sharing permissions
      sheet.getRange(applicant.rowNumber, colMap.CV_Link).setValue(driveFile.url);
      seenCandidateIndices[paddedNum] = applicant.rowNumber;
      summary.mappedCount += 1;
    }

    // Verify none of Candidate 001..040 were missing from the sheet altogether
    for (var expectedNum = 1; expectedNum <= EXPECTED_CV_MAPPING_COUNT; expectedNum++) {
      var key = padThreeDigits_(expectedNum);
      if (!seenCandidateIndices[key]) {
        var missingCandidateName = "Candidate " + key;
        var alreadyRecorded = false;
        for (var u = 0; u < summary.unmatchedApplicants.length; u++) {
          if (summary.unmatchedApplicants[u].name === missingCandidateName) {
            alreadyRecorded = true;
            break;
          }
        }
        if (!alreadyRecorded) {
          summary.unmatchedApplicants.push({
            rowNumber: null,
            name: missingCandidateName,
            expectedFile: "APP-" + key + ".docx",
            reason: "Expected candidate not found in Google Sheet"
          });
          logWarn_("Expected candidate missing from Google Sheet", {
            name: missingCandidateName,
            expectedFile: "APP-" + key + ".docx"
          });
        }
      }
    }

    summary.completedAt = new Date().toISOString();

    if (
      summary.mappedCount !== EXPECTED_CV_MAPPING_COUNT ||
      summary.missingFiles.length > 0 ||
      summary.unmatchedApplicants.length > 0
    ) {
      logError_("Drive CV mapping validation failed", summary);
      throw new Error(
        "CV link mapping validation failed: mapped " +
          summary.mappedCount +
          " of " +
          EXPECTED_CV_MAPPING_COUNT +
          " expected files (missingFiles=" +
          summary.missingFiles.length +
          ", unmatchedApplicants=" +
          summary.unmatchedApplicants.length +
          "). Check Execution log for details."
      );
    }

    logInfo_("Successfully mapped all 40 Drive CV links", summary);
    return summary;
  } catch (err) {
    var msg = err && err.message ? err.message : String(err);
    logError_("populateCvLinksFromDriveFolder failed", { error: msg });
    throw new Error(msg);
  }
}


// ============================================================================
// Core Pipeline Orchestration
// ============================================================================

/**
 * Executes the applicant ingestion pipeline.
 *
 * @param {{ dryRunOverride: (boolean|null) }} options
 * @return {Object} Execution summary metrics.
 */
function runPipeline_(options) {
  var summary = {
    startedAt: new Date().toISOString(),
    dryRun: false,
    totalRows: 0,
    submitted: 0,
    skippedAlreadySubmitted: 0,
    skippedInvalid: 0,
    failed: 0
  };

  var config;
  try {
    config = getConfig_();
    if (options && options.dryRunOverride !== null && options.dryRunOverride !== undefined) {
      config.dryRun = Boolean(options.dryRunOverride);
    }
    summary.dryRun = config.dryRun;

    if (!config.dryRun && !config.backendUrl) {
      throw new Error(
        "Missing Script Property 'BACKEND_URL'. Set BACKEND_URL in Project Settings -> Script Properties, or run testDryRun()."
      );
    }

    var sheet = getTargetSheet_(config);
    var colMap = getRequiredColumnMap_(sheet);
    if (!config.dryRun) {
      ensureTrackingColumns_(sheet, colMap);
    }

    var props = PropertiesService.getScriptProperties();
    var submittedEmails = loadSubmittedEmails_(props);
    var rows = readApplicantRows_(sheet, colMap);
    summary.totalRows = rows.length;

    // Seed in-memory email set from sheet rows already marked Submitted
    for (var i = 0; i < rows.length; i++) {
      var existingRow = rows[i];
      if (existingRow.isMarkedSubmitted && existingRow.normalizedEmail) {
        submittedEmails[existingRow.normalizedEmail] = true;
      }
    }

    logInfo_("Starting Daily Applicant Scoring Pipeline run", {
      sheetName: sheet.getName(),
      totalRows: rows.length,
      dryRun: config.dryRun
    });

    for (var idx = 0; idx < rows.length; idx++) {
      var applicant = rows[idx];

      if (applicant.isCompletelyEmpty) {
        continue;
      }

      var validationError = validateApplicantRow_(applicant);
      if (validationError) {
        summary.skippedInvalid += 1;
        logWarn_("Skipping invalid applicant row", {
          rowNumber: applicant.rowNumber,
          reason: validationError
        });
        if (!config.dryRun) {
          updateRowTracking_(
            sheet,
            colMap,
            applicant.rowNumber,
            STATUS_VALUES.SKIPPED_INVALID,
            "",
            validationError
          );
        }
        continue;
      }

      if (applicant.isMarkedSubmitted || submittedEmails[applicant.normalizedEmail]) {
        summary.skippedAlreadySubmitted += 1;
        logInfo_("Skipping already-submitted applicant", {
          rowNumber: applicant.rowNumber,
          email: applicant.normalizedEmail
        });
        // Backfill row status if duplicate email appeared on another row
        if (!applicant.isMarkedSubmitted && !config.dryRun) {
          updateRowTracking_(
            sheet,
            colMap,
            applicant.rowNumber,
            STATUS_VALUES.SUBMITTED,
            new Date().toISOString(),
            "Skipped: email already submitted"
          );
        }
        continue;
      }

      var payload = buildApplicantPayload_(applicant, sheet.getName());

      if (config.dryRun) {
        logInfo_("[DRY RUN] Applicant ready for POST", {
          rowNumber: applicant.rowNumber,
          email: payload.email,
          role: payload.role
        });
        summary.submitted += 1;
        submittedEmails[applicant.normalizedEmail] = true;
        continue;
      }

      try {
        var responseMeta = postApplicantToBackend_(config, payload);
        var nowIso = new Date().toISOString();

        submittedEmails[applicant.normalizedEmail] = true;
        saveSubmittedEmails_(props, submittedEmails);
        updateRowTracking_(
          sheet,
          colMap,
          applicant.rowNumber,
          STATUS_VALUES.SUBMITTED,
          nowIso,
          ""
        );

        summary.submitted += 1;
        logInfo_("Submitted applicant successfully", {
          rowNumber: applicant.rowNumber,
          email: applicant.normalizedEmail,
          statusCode: responseMeta.statusCode
        });
      } catch (postErr) {
        var safeErr = redactSecrets_(
          postErr && postErr.message ? postErr.message : String(postErr),
          config.secretsToRedact
        );
        summary.failed += 1;
        logError_("Failed to submit applicant", {
          rowNumber: applicant.rowNumber,
          email: applicant.normalizedEmail,
          error: safeErr
        });
        updateRowTracking_(
          sheet,
          colMap,
          applicant.rowNumber,
          STATUS_VALUES.FAILED,
          "",
          safeErr
        );
      }
    }

    summary.completedAt = new Date().toISOString();
    logInfo_("Completed pipeline run", summary);
    return summary;
  } catch (fatalErr) {
    var secrets = config && config.secretsToRedact ? config.secretsToRedact : [];
    var safeFatal = redactSecrets_(
      fatalErr && fatalErr.message ? fatalErr.message : String(fatalErr),
      secrets
    );
    logError_("Pipeline run aborted due to fatal error", { error: safeFatal });
    throw new Error(safeFatal);
  }
}


// ============================================================================
// Configuration Module
// ============================================================================

/**
 * Loads configuration from Script Properties without hardcoding secrets.
 *
 * @return {Object} Normalized configuration object.
 */
function getConfig_() {
  var props = PropertiesService.getScriptProperties();

  var backendUrl = trimOrEmpty_(props.getProperty(SCRIPT_PROP_KEYS.BACKEND_URL));
  var webhookToken = trimOrEmpty_(props.getProperty(SCRIPT_PROP_KEYS.WEBHOOK_AUTH_TOKEN));
  var spreadsheetId = trimOrEmpty_(props.getProperty(SCRIPT_PROP_KEYS.SPREADSHEET_ID));
  var sheetName = trimOrEmpty_(props.getProperty(SCRIPT_PROP_KEYS.SHEET_NAME));
  var triggerHour = parseHour_(
    props.getProperty(SCRIPT_PROP_KEYS.TRIGGER_HOUR),
    DEFAULT_TRIGGER_HOUR
  );
  var dryRun = parseBoolean_(props.getProperty(SCRIPT_PROP_KEYS.DRY_RUN), false);

  if (backendUrl && !/^https?:\/\//i.test(backendUrl)) {
    throw new Error("Script Property 'BACKEND_URL' must start with http:// or https://");
  }

  var secrets = [];
  if (webhookToken) {
    secrets.push(webhookToken);
  }

  return {
    backendUrl: backendUrl,
    webhookAuthToken: webhookToken,
    spreadsheetId: spreadsheetId,
    sheetName: sheetName,
    triggerHour: triggerHour,
    dryRun: dryRun,
    secretsToRedact: secrets
  };
}

function parseBoolean_(rawValue, defaultValue) {
  var cleaned = trimOrEmpty_(rawValue).toLowerCase();
  if (!cleaned) {
    return Boolean(defaultValue);
  }
  return cleaned === "true" || cleaned === "1" || cleaned === "yes";
}

function parseHour_(rawValue, defaultHour) {
  var cleaned = trimOrEmpty_(rawValue);
  if (!cleaned) {
    return defaultHour;
  }
  var parsed = parseInt(cleaned, 10);
  if (isNaN(parsed) || parsed < 0 || parsed > 23) {
    logWarn_("Invalid TRIGGER_HOUR Script Property; falling back to default", {
      provided: cleaned,
      fallback: defaultHour
    });
    return defaultHour;
  }
  return parsed;
}


// ============================================================================
// Spreadsheet Module
// ============================================================================

/**
 * Opens the target sheet from active spreadsheet or configured SPREADSHEET_ID.
 *
 * @param {Object} config
 * @return {GoogleAppsScript.Spreadsheet.Sheet}
 */
function getTargetSheet_(config) {
  var spreadsheet = config.spreadsheetId
    ? SpreadsheetApp.openById(config.spreadsheetId)
    : SpreadsheetApp.getActiveSpreadsheet();

  if (!spreadsheet) {
    throw new Error(
      "No active Spreadsheet found. Bind this script to a Google Sheet or set 'SPREADSHEET_ID' in Script Properties."
    );
  }

  if (config.sheetName) {
    var namedSheet = spreadsheet.getSheetByName(config.sheetName);
    if (!namedSheet) {
      throw new Error("Sheet '" + config.sheetName + "' not found in spreadsheet.");
    }
    return namedSheet;
  }

  var firstSheet = spreadsheet.getSheets()[0];
  if (!firstSheet) {
    throw new Error("Spreadsheet contains no sheets.");
  }
  return firstSheet;
}

/**
 * Builds a 1-based column index map from header row 1 and verifies required columns exist.
 *
 * @param {GoogleAppsScript.Spreadsheet.Sheet} sheet
 * @return {Object<string, number>}
 */
function getRequiredColumnMap_(sheet) {
  var lastColumn = sheet.getLastColumn();
  if (lastColumn < 1) {
    throw new Error("Header row is empty. Expected columns: " + REQUIRED_COLUMNS.join(", "));
  }

  var headers = sheet.getRange(1, 1, 1, lastColumn).getValues()[0];
  var normalizedHeaderMap = {};
  for (var colIdx = 0; colIdx < headers.length; colIdx++) {
    var headerText = trimOrEmpty_(headers[colIdx]);
    if (headerText) {
      normalizedHeaderMap[headerText.toLowerCase()] = colIdx + 1;
    }
  }

  var colMap = {};
  var missing = [];
  for (var i = 0; i < REQUIRED_COLUMNS.length; i++) {
    var reqName = REQUIRED_COLUMNS[i];
    var foundCol = normalizedHeaderMap[reqName.toLowerCase()];
    if (!foundCol) {
      missing.push(reqName);
    } else {
      colMap[reqName] = foundCol;
    }
  }

  if (missing.length > 0) {
    throw new Error("Missing required spreadsheet column(s): " + missing.join(", "));
  }

  var trackingKeys = [
    TRACKING_COLUMNS.STATUS,
    TRACKING_COLUMNS.SUBMITTED_AT,
    TRACKING_COLUMNS.LAST_ERROR
  ];
  for (var j = 0; j < trackingKeys.length; j++) {
    var trackName = trackingKeys[j];
    var trackCol = normalizedHeaderMap[trackName.toLowerCase()];
    if (trackCol) {
      colMap[trackName] = trackCol;
    }
  }

  colMap._maxColumn = lastColumn;
  return colMap;
}

/**
 * Ensures Submission_Status, Submitted_At, and Last_Error headers exist in the sheet.
 *
 * @param {GoogleAppsScript.Spreadsheet.Sheet} sheet
 * @param {Object<string, number>} colMap
 */
function ensureTrackingColumns_(sheet, colMap) {
  var trackingHeaders = [
    TRACKING_COLUMNS.STATUS,
    TRACKING_COLUMNS.SUBMITTED_AT,
    TRACKING_COLUMNS.LAST_ERROR
  ];
  var nextCol = colMap._maxColumn + 1;

  for (var i = 0; i < trackingHeaders.length; i++) {
    var header = trackingHeaders[i];
    if (!colMap[header]) {
      sheet.getRange(1, nextCol).setValue(header);
      colMap[header] = nextCol;
      nextCol += 1;
    }
  }
  colMap._maxColumn = nextCol - 1;
}

/**
 * Reads all data rows (starting at row 2) into structured applicant objects.
 *
 * @param {GoogleAppsScript.Spreadsheet.Sheet} sheet
 * @param {Object<string, number>} colMap
 * @return {Array<Object>}
 */
function readApplicantRows_(sheet, colMap) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return [];
  }

  var values = sheet.getRange(2, 1, lastRow - 1, colMap._maxColumn).getValues();
  var rows = [];

  for (var i = 0; i < values.length; i++) {
    var rowArr = values[i];
    var rowNumber = i + 2;

    var name = getCellText_(rowArr, colMap.Name);
    var email = getCellText_(rowArr, colMap.Email);
    var role = getCellText_(rowArr, colMap.Role);
    var cvLink = getCellText_(rowArr, colMap.CV_Link);
    var submissionStatus = getCellText_(rowArr, colMap[TRACKING_COLUMNS.STATUS]);

    var isCompletelyEmpty = !name && !email && !role && !cvLink;
    var normalizedStatus = submissionStatus.toLowerCase();
    var isMarkedSubmitted = normalizedStatus === STATUS_VALUES.SUBMITTED.toLowerCase();

    rows.push({
      rowNumber: rowNumber,
      name: name,
      email: email,
      normalizedEmail: normalizeEmail_(email),
      role: role,
      cvLink: cvLink,
      submissionStatus: submissionStatus,
      isMarkedSubmitted: isMarkedSubmitted,
      isCompletelyEmpty: isCompletelyEmpty
    });
  }

  return rows;
}

function updateRowTracking_(sheet, colMap, rowNumber, status, submittedAt, errorMessage) {
  sheet.getRange(rowNumber, colMap[TRACKING_COLUMNS.STATUS]).setValue(status);
  if (submittedAt) {
    sheet.getRange(rowNumber, colMap[TRACKING_COLUMNS.SUBMITTED_AT]).setValue(submittedAt);
  }
  sheet.getRange(rowNumber, colMap[TRACKING_COLUMNS.LAST_ERROR]).setValue(errorMessage || "");
}


// ============================================================================
// Validation & Email Deduplication Module
// ============================================================================

function normalizeEmail_(email) {
  return trimOrEmpty_(email).toLowerCase();
}

function validateApplicantRow_(applicant) {
  if (!applicant.name) {
    return "Missing required field: Name";
  }
  if (!applicant.normalizedEmail || applicant.normalizedEmail.indexOf("@") === -1) {
    return "Missing or invalid Email address";
  }
  if (!applicant.role) {
    return "Missing required field: Role";
  }
  if (!applicant.cvLink) {
    return "Missing required field: CV_Link";
  }
  return null;
}

function loadSubmittedEmails_(props) {
  var raw = props.getProperty(SCRIPT_PROP_KEYS.SUBMITTED_EMAILS);
  if (!raw) {
    return {};
  }
  try {
    var parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      var map = {};
      for (var i = 0; i < parsed.length; i++) {
        var normalized = normalizeEmail_(parsed[i]);
        if (normalized) {
          map[normalized] = true;
        }
      }
      return map;
    }
  } catch (err) {
    logWarn_("Unable to parse SUBMITTED_EMAILS Script Property; resetting in-memory index");
  }
  return {};
}

function saveSubmittedEmails_(props, emailMap) {
  var list = Object.keys(emailMap).sort();
  props.setProperty(SCRIPT_PROP_KEYS.SUBMITTED_EMAILS, JSON.stringify(list));
}


// ============================================================================
// HTTP Client Module
// ============================================================================

/**
 * Builds the JSON payload expected by the Python FastAPI backend.
 */
function buildApplicantPayload_(applicant, sheetName) {
  return {
    name: applicant.name,
    email: applicant.normalizedEmail,
    role: applicant.role,
    cv_link: applicant.cvLink,
    source: "google_sheets",
    sheet_name: sheetName,
    sheet_row: applicant.rowNumber,
    submitted_at: new Date().toISOString()
  };
}

/**
 * Sends a single applicant record to the Python backend via HTTP POST.
 *
 * @param {Object} config
 * @param {Object} payload
 * @return {Object} Response metadata ({ statusCode, body }).
 */
function postApplicantToBackend_(config, payload) {
  var headers = {
    Accept: "application/json"
  };

  if (config.webhookAuthToken) {
    headers.Authorization = "Bearer " + config.webhookAuthToken;
    headers["X-Webhook-Token"] = config.webhookAuthToken;
  }

  var fetchOptions = {
    method: "post",
    contentType: "application/json",
    headers: headers,
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(config.backendUrl, fetchOptions);
  var statusCode = response.getResponseCode();
  var responseText = redactSecrets_(response.getContentText(), config.secretsToRedact);

  if (statusCode < 200 || statusCode >= 300) {
    var preview = responseText ? responseText.slice(0, 300) : "No response body";
    throw new Error("Backend returned HTTP " + statusCode + ": " + preview);
  }

  return {
    statusCode: statusCode,
    body: responseText
  };
}


// ============================================================================
// Google Drive Mapping Helpers (no permission changes, no backend calls)
// ============================================================================

/**
 * Lists non-trashed DOCX files inside the given Google Drive folder.
 * Reads only file ID, name, and URL without modifying sharing permissions.
 *
 * @param {string} folderId
 * @return {Object<string, {id: string, name: string, url: string}>}
 */
function listDocxFilesInFolder_(folderId) {
  var folder = DriveApp.getFolderById(folderId);
  var files = folder.getFiles();
  var docxMap = {};

  while (files.hasNext()) {
    var file = files.next();
    if (file.isTrashed && file.isTrashed()) {
      continue;
    }
    var fileName = trimOrEmpty_(file.getName());
    if (!/\.docx$/i.test(fileName)) {
      continue;
    }

    // Normalize expected APP-NNN.docx key casing while preserving exact URL
    var normalizedKey = /^APP-\d{3}\.docx$/i.test(fileName)
      ? fileName.slice(0, 7).toUpperCase() + ".docx"
      : fileName;

    if (Object.prototype.hasOwnProperty.call(docxMap, normalizedKey)) {
      logWarn_("Duplicate DOCX filename found in Drive folder; keeping first match", {
        filename: normalizedKey
      });
      continue;
    }

    docxMap[normalizedKey] = {
      id: file.getId(),
      name: fileName,
      url: file.getUrl()
    };
  }

  return docxMap;
}

function buildExpectedDriveFileMap_(count) {
  var map = {};
  for (var i = 1; i <= count; i++) {
    map["APP-" + padThreeDigits_(i) + ".docx"] = true;
  }
  return map;
}

function parseCandidateNumber_(candidateName, maxCount) {
  var cleaned = trimOrEmpty_(candidateName);
  var match = /^Candidate\s+(\d{3})$/i.exec(cleaned);
  if (!match) {
    return null;
  }
  var num = parseInt(match[1], 10);
  if (isNaN(num) || num < 1 || num > maxCount) {
    return null;
  }
  return num;
}

function padThreeDigits_(num) {
  var str = String(num);
  while (str.length < 3) {
    str = "0" + str;
  }
  return str;
}


// ============================================================================
// Trigger, Logging, and Security Helpers
// ============================================================================

function removeTriggersForHandler_(handlerFunctionName) {
  var triggers = ScriptApp.getProjectTriggers();
  var removed = 0;
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === handlerFunctionName) {
      ScriptApp.deleteTrigger(triggers[i]);
      removed += 1;
    }
  }
  return removed;
}

function redactSecrets_(text, secrets) {
  if (!text) {
    return "";
  }
  var cleaned = String(text);
  if (secrets && secrets.length) {
    for (var i = 0; i < secrets.length; i++) {
      var secret = secrets[i];
      if (secret) {
        cleaned = cleaned.split(secret).join("[REDACTED]");
      }
    }
  }
  cleaned = cleaned.replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]");
  return cleaned;
}

function getCellText_(rowArray, oneBasedColIndex) {
  if (!oneBasedColIndex || oneBasedColIndex < 1 || oneBasedColIndex > rowArray.length) {
    return "";
  }
  return trimOrEmpty_(rowArray[oneBasedColIndex - 1]);
}

function trimOrEmpty_(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).trim();
}

function logInfo_(message, context) {
  var line = context ? message + " | " + JSON.stringify(context) : message;
  Logger.log("[INFO] " + line);
  console.log("[INFO] " + line);
}

function logWarn_(message, context) {
  var line = context ? message + " | " + JSON.stringify(context) : message;
  Logger.log("[WARN] " + line);
  console.warn("[WARN] " + line);
}

function logError_(message, context) {
  var line = context ? message + " | " + JSON.stringify(context) : message;
  Logger.log("[ERROR] " + line);
  console.error("[ERROR] " + line);
}
