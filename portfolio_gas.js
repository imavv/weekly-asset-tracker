/**
 * Portfolio Tracker — GAS Web App Endpoint
 *
 * Deploy as: Extensions → Apps Script → Deploy → New Deployment
 *   Type: Web App
 *   Execute as: Me
 *   Who has access: Anyone  (secured by SECRET_TOKEN below)
 *
 * After deploying, copy the /exec URL — that's your GAS_ENDPOINT.
 *
 * CONFIG ─────────────────────────────────────────────────────────
 */
const SECRET_TOKEN   = "REPLACE_WITH_YOUR_SECRET";    // any random string
const SHEET_NAME     = "Sheet1";                       // your tracker tab name
const SPREADSHEET_ID = "REPLACE_WITH_YOUR_SHEET_ID";  // from the Sheet URL
// ────────────────────────────────────────────────────────────────

/**
 * GET handler — returns the next empty row in column A.
 *
 * Python calls this automatically at the start of each run to find
 * where to write (no more manual --start-row).
 *
 * Request:  GET {endpoint}?token=your-secret
 * Response: { "status": 200, "start_row": 1608 }
 */
function doGet(e) {
  try {
    if (e.parameter.token !== SECRET_TOKEN) {
      return respond(403, "Unauthorized");
    }

    const ss    = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(SHEET_NAME);

    if (!sheet) {
      return respond(404, `Sheet "${SHEET_NAME}" not found`);
    }

    // Find last row with data in col A, then go one past it
    const lastRow = sheet.getRange("A:A")
                         .getValues()
                         .reduce((last, [val], i) => val !== "" ? i + 1 : last, 0);
    const startRow = lastRow + 1;

    const output = ContentService.createTextOutput(
      JSON.stringify({ status: 200, start_row: startRow })
    );
    output.setMimeType(ContentService.MimeType.JSON);
    return output;

  } catch (err) {
    return respond(500, `Server error: ${err.message}`);
  }
}

/**
 * POST handler — writes rows to the sheet.
 *
 * Expected JSON body:
 * {
 *   "token": "your-secret",
 *   "start_row": 1585,          // first row to write (integer)
 *   "rows": [                   // array of 23 rows, each an 11-element array
 *     ["2026-06-16", "Cash", "Mandiri", 12500000, "", "", "", "", "", "", "=GOOGLEFINANCE(\"CURRENCY:USDIDR\")"],
 *     ["2026-06-16", "Cash", "BCA",     8200000,  "", "", "", "", "", "", ""],
 *     ...
 *   ]
 * }
 *
 * Formula strings (e.g. "=F1573*G1573") are written as-is — Sheets evaluates them.
 */
function doPost(e) {
  try {
    // ── 1. Parse body ──────────────────────────────────────────
    const payload = JSON.parse(e.postData.contents);

    // ── 2. Auth check ──────────────────────────────────────────
    if (payload.token !== SECRET_TOKEN) {
      return respond(403, "Unauthorized");
    }

    // ── 3. Validate shape ──────────────────────────────────────
    const startRow = parseInt(payload.start_row);
    const rows     = payload.rows;

    if (!startRow || !Array.isArray(rows) || rows.length === 0) {
      return respond(400, "Missing or invalid start_row / rows");
    }

    const numCols = 11; // A–K
    for (let i = 0; i < rows.length; i++) {
      if (!Array.isArray(rows[i]) || rows[i].length !== numCols) {
        return respond(400, `Row ${i} does not have exactly ${numCols} columns (got ${rows[i]?.length})`);
      }
    }

    // ── 4. Write to sheet ──────────────────────────────────────
    const ss    = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(SHEET_NAME);

    if (!sheet) {
      return respond(404, `Sheet "${SHEET_NAME}" not found`);
    }

    const range = sheet.getRange(startRow, 1, rows.length, numCols);

    // USER_ENTERED so formula strings are evaluated by Sheets, not stored as text
    range.setValues(rows);
    SpreadsheetApp.flush();

    return respond(200, `OK — wrote ${rows.length} rows starting at row ${startRow}`);

  } catch (err) {
    return respond(500, `Server error: ${err.message}`);
  }
}

/** Helper: return a JSON HTTP response */
function respond(code, message) {
  const output = ContentService.createTextOutput(
    JSON.stringify({ status: code, message })
  );
  output.setMimeType(ContentService.MimeType.JSON);
  return output;
}

/**
 * Manual test for doGet — run in Apps Script editor to verify
 * start_row detection before deploying.
 */
function testDoGet() {
  const fakeEvent = { parameter: { token: SECRET_TOKEN } };
  Logger.log(doGet(fakeEvent).getContent());
}

/**
 * Manual test for doPost — run in Apps Script editor to verify
 * your sheet ID and token before deploying.
 */
function testDoPost() {
  const fakeEvent = {
    postData: {
      contents: JSON.stringify({
        token: SECRET_TOKEN,
        start_row: 1585,
        rows: [
          ["2026-06-16","Cash","Mandiri",99999999,"","","","","","","=GOOGLEFINANCE(\"CURRENCY:USDIDR\")"],
          ["2026-06-16","Cash","BCA",    11111111,"","","","","","",""]
        ]
      })
    }
  };
  Logger.log(doPost(fakeEvent).getContent());
}
