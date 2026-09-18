/**
 * Syncs new rows from the "Daily Reports" sheet to the Neon Postgres
 * backend via a Vercel endpoint, as rows are appended by the reporting
 * bot's API writes (not manual edits, not a Form).
 *
 * Opens the sheet by SHEET_ID explicitly (SpreadsheetApp.openById), rather
 * than assuming this script is container-bound to it — works whether this
 * project is bound to the sheet or standalone, as long as the account
 * running it has access to that spreadsheet.
 *
 * Setup (run once from the Apps Script editor):
 *   1. Project Settings > Script Properties: add SYNC_SECRET with the same
 *      value configured as SYNC_SECRET in Vercel.
 *   2. Run createOnChangeTrigger() once to install the trigger.
 *   3. Run backfillAll() once to start the initial load of existing rows.
 *      It paces itself to Voyage AI's free-tier rate limit and resumes
 *      itself automatically every 10 minutes until done -- no need to
 *      re-run it manually, just leave it (check progress via
 *      `SELECT count(*) FROM reports;` in Neon).
 */

var SHEET_ID = '1JvG-freIMAxkU5m3sI5kKZJJGBQg3rH8CdxBKjLtQ94';
var SHEET_NAME = 'Daily Reports';
var SYNC_ENDPOINT = 'https://backend-g1-3c69.vercel.app/api/sync';
var SYNC_SECRET_PROPERTY = 'SYNC_SECRET';
var LAST_SYNCED_ROW_PROPERTY = 'lastSyncedRow';
var BATCH_SIZE = 50; // rows per HTTP request -- kept small so one batch's
                      // worth of embeddings stays comfortably under Voyage
                      // AI's free-tier 10K-tokens/minute cap
var LOCK_TIMEOUT_MS = 30 * 1000;
var BATCH_DELAY_MS = 21 * 1000; // >20s between batches, to stay under
                                 // Voyage AI's free-tier 3 requests/minute cap
var BACKFILL_TRIGGER_HANDLER = 'continueBackfill';

var HEADER_ROW = 1;

// ===== Trigger setup (run manually, once) =====

function createOnChangeTrigger() {
  // onChange (not onEdit) fires for rows appended via the Sheets API by the
  // bot; onEdit only fires for interactive UI edits and would miss them.
  // There's also no Form involved, so onFormSubmit doesn't apply.
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'onChangeSync') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('onChangeSync')
    .forSpreadsheet(SpreadsheetApp.openById(SHEET_ID))
    .onChange()
    .create();
}

// ===== Trigger handler =====

function onChangeSync(e) {
  var lock = LockService.getScriptLock();
  try {
    // waitLock (not tryLock) so a burst of rapid bot writes queues up
    // instead of silently dropping. Each queued run re-reads the sheet's
    // current last row once it acquires the lock, so it always picks up
    // everything appended while it was waiting — no missed rows, and no
    // duplicates because the watermark (lastSyncedRow) only advances after
    // a successful send.
    lock.waitLock(LOCK_TIMEOUT_MS);
  } catch (err) {
    console.error('Could not acquire sync lock (possible stuck run): ' + err);
    return;
  }
  try {
    syncNewRows_();
  } catch (err) {
    console.error('Sync failed: ' + err);
  } finally {
    lock.releaseLock();
  }
}

// ===== Core sync logic =====

function syncNewRows_() {
  var sheet = SpreadsheetApp.openById(SHEET_ID).getSheetByName(SHEET_NAME);
  var props = PropertiesService.getScriptProperties();
  var lastRow = sheet.getLastRow();
  var lastSyncedRow = Number(props.getProperty(LAST_SYNCED_ROW_PROPERTY) || HEADER_ROW);

  if (lastRow <= lastSyncedRow) {
    return; // nothing new since the last run
  }

  var colIndex = getColumnIndexMap_(sheet);
  var startRow = lastSyncedRow + 1;
  var startedAt = Date.now();
  var MAX_RUNTIME_MS = 5 * 60 * 1000; // stop well before Apps Script's ~6-minute execution cap

  while (startRow <= lastRow) {
    var endRow = Math.min(startRow + BATCH_SIZE - 1, lastRow);
    var numRows = endRow - startRow + 1;
    var values = sheet.getRange(startRow, 1, numRows, sheet.getLastColumn()).getValues();

    var payloadRows = values
      .map(function (row) { return rowToPayload_(row, colIndex); })
      .filter(function (r) { return r !== null; });

    if (payloadRows.length > 0) {
      sendBatch_(payloadRows); // throws on failure -> stops before advancing watermark, so this chunk retries next run
    }

    props.setProperty(LAST_SYNCED_ROW_PROPERTY, String(endRow));
    startRow = endRow + 1;

    if (startRow > lastRow) break; // done, no need to sleep or check runtime

    if (Date.now() - startedAt > MAX_RUNTIME_MS) {
      console.log('Approaching execution time limit; stopping early. Resumes on next run.');
      break;
    }

    Utilities.sleep(BATCH_DELAY_MS); // stay under Voyage AI's free-tier 3 requests/minute cap
  }
}

function getColumnIndexMap_(sheet) {
  var headers = sheet.getRange(HEADER_ROW, 1, 1, sheet.getLastColumn()).getValues()[0];
  var map = {};
  headers.forEach(function (h, i) { map[String(h).trim()] = i; });
  return map;
}

function rowToPayload_(row, colIndex) {
  var timestamp = row[colIndex['Timestamp']];
  var phone = row[colIndex['Phone']];
  if (!timestamp && !phone) return null; // blank row

  return {
    sheet_row_id: computeSheetRowId_(timestamp, phone),
    report_timestamp: (timestamp instanceof Date) ? timestamp.toISOString() : String(timestamp),
    name: row[colIndex['Name']],
    phone: String(phone || ''),
    state: row[colIndex['State']],
    location: row[colIndex['Location']],
    project: row[colIndex['Project']],
    area_of_intervention: row[colIndex['Area of Intervention']],
    description: row[colIndex['Description']],
    beneficiaries: toNumberOrNull_(row[colIndex['Beneficiaries']]),
    project_beneficiaries: toNumberOrNull_(row[colIndex['Project Beneficiaries']]),
    training_participants: toNumberOrNull_(row[colIndex['Training Participants']]),
    community_outreach: toNumberOrNull_(row[colIndex['Community Outreach']]),
    attachment_url: row[colIndex['Attachment']]
  };
}

/** sha256("<ISO timestamp>|<digits-only phone>"), hex-encoded. Stable even if
 * the row's position in the sheet later changes. */
function computeSheetRowId_(timestamp, phone) {
  var isoTimestamp = (timestamp instanceof Date) ? timestamp.toISOString() : String(timestamp || '').trim();
  var normalizedPhone = String(phone || '').replace(/\D/g, '');
  var raw = isoTimestamp + '|' + normalizedPhone;
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, raw, Utilities.Charset.UTF_8);
  return digest.map(function (b) {
    var v = (b < 0) ? b + 256 : b;
    return ('0' + v.toString(16)).slice(-2);
  }).join('');
}

function toNumberOrNull_(v) {
  if (v === '' || v === null || v === undefined) return null;
  var n = Number(v);
  return isNaN(n) ? null : n;
}

function sendBatch_(rows) {
  var secret = PropertiesService.getScriptProperties().getProperty(SYNC_SECRET_PROPERTY);
  var response = UrlFetchApp.fetch(SYNC_ENDPOINT, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + secret },
    payload: JSON.stringify({ rows: rows }),
    muteHttpExceptions: true
  });
  var code = response.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error('Sync endpoint returned ' + code + ': ' + response.getContentText());
  }
}

// ===== One-off backfill (run manually from the editor) =====

/**
 * Starts (or restarts from scratch) a full backfill of every existing row.
 * Resumable by design: a single Apps Script execution can't finish ~7,974
 * rows within its ~6-minute cap while also respecting Voyage AI's free-tier
 * rate limit (3 requests/minute), so this installs a time-driven trigger
 * that re-invokes continueBackfill() every 10 minutes until caught up, then
 * removes itself.
 */
function backfillAll() {
  PropertiesService.getScriptProperties().setProperty(LAST_SYNCED_ROW_PROPERTY, String(HEADER_ROW));
  installBackfillTrigger_();
  continueBackfill();
}

/** One resumable chunk of the backfill. Safe to run manually too. */
function continueBackfill() {
  var lock = LockService.getScriptLock();
  lock.waitLock(LOCK_TIMEOUT_MS);
  try {
    syncNewRows_();
  } finally {
    lock.releaseLock();
  }

  var sheet = SpreadsheetApp.openById(SHEET_ID).getSheetByName(SHEET_NAME);
  var lastSyncedRow = Number(PropertiesService.getScriptProperties().getProperty(LAST_SYNCED_ROW_PROPERTY) || HEADER_ROW);
  if (lastSyncedRow >= sheet.getLastRow()) {
    console.log('Backfill complete.');
    removeBackfillTrigger_();
  }
}

function installBackfillTrigger_() {
  removeBackfillTrigger_();
  ScriptApp.newTrigger(BACKFILL_TRIGGER_HANDLER)
    .timeBased()
    .everyMinutes(10)
    .create();
}

function removeBackfillTrigger_() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === BACKFILL_TRIGGER_HANDLER) ScriptApp.deleteTrigger(t);
  });
}
