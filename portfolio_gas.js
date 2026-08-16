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
const SCRATCH_SHEET  = "_price_scratch";               // hidden tab, auto-created
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

    // ── action=summary → return the two dashboard tables ──────────────
    // Return display values AND per-cell formatting so the rendered image
    // matches the sheet exactly (don't change the format).
    if (e.parameter.action === "summary") {
      const out = ContentService.createTextOutput(
        JSON.stringify({
          status:    200,
          trend:     rangeData(sheet, "L4:O10"),   // Week-to-Week Trend
          breakdown: rangeData(sheet, "H13:J21"),  // Asset Breakdown
        })
      );
      out.setMimeType(ContentService.MimeType.JSON);
      return out;
    }

    // ── action=prices → resolve GOOGLEFINANCE to STATIC numbers ───────
    // Why not just write =GOOGLEFINANCE into column G? Because that formula
    // recalculates forever, so every past week's snapshot would silently
    // rewrite itself to today's price. We resolve it here and hand back plain
    // numbers, which get stored as fixed values.
    if (e.parameter.action === "prices") {
      return resolvePrices(e.parameter.tickers);
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

    // ── 4a. Duplicate-date guard ───────────────────────────────
    // Chat is freeform — "hmm, try again?" is an easy way to append a second
    // 23-row block for the same week and silently inflate the summary. Refuse
    // unless the caller explicitly passes force:true.
    const blockDate = String(rows[0][0]).trim();
    if (payload.force !== true && dateAlreadyPresent(sheet, blockDate)) {
      return respond(409,
        `A block dated ${blockDate} already exists in this sheet — refusing to ` +
        `write a duplicate. Pass force:true to override.`);
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

/**
 * Resolve tickers to STATIC prices using the sheet's own GOOGLEFINANCE.
 *
 * Writes formulas to a hidden scratch tab, waits for them to settle, reads the
 * computed numbers, then clears it. Returns { status, prices: {TICKER: number} }.
 * Tickers that fail to resolve are simply absent from `prices` — the caller
 * decides what to do about them.
 */
function resolvePrices(tickersParam) {
  // Sanitise hard: these strings are interpolated into a formula, and they
  // originate from a language model. Allow only ticker-shaped input.
  // Accepts bare tickers ("VOO") and currency pairs ("CURRENCY:CNYIDR").
  const tickers = String(tickersParam || "")
    .split(",")
    .map(function (t) { return t.trim().toUpperCase(); })
    .filter(function (t) { return /^[A-Z0-9.:-]{1,24}$/.test(t); });

  if (!tickers.length) {
    return respond(400, "No valid tickers supplied");
  }

  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  let scratch = ss.getSheetByName(SCRATCH_SHEET);
  if (!scratch) {
    scratch = ss.insertSheet(SCRATCH_SHEET);
    scratch.hideSheet();
  }
  scratch.clearContents();

  // No attribute argument: it defaults to "price" for securities, and currency
  // pairs ("CURRENCY:CNYIDR") only accept the bare form. Passing "price" to a
  // currency pair yields an error.
  //
  // No IFERROR either. It looks defensive but it converts the transient
  // "Loading..." state into "", which the poll below then reads as "settled,
  // no value" — so a cold symbol is reported unresolvable on the first pass.
  const formulas = tickers.map(function (t) {
    return ['=GOOGLEFINANCE("' + t + '")'];
  });
  const range = scratch.getRange(1, 1, formulas.length, 1);
  range.setFormulas(formulas);
  SpreadsheetApp.flush();

  // Poll until every cell holds an actual number. A cell still loading, or in
  // an error state, is not a number — so this waits out the fetch and gives up
  // only on something genuinely unavailable.
  let values = range.getValues();
  for (let attempt = 0; attempt < 15; attempt++) {
    const pending = values.some(function (r) { return typeof r[0] !== "number"; });
    if (!pending) break;
    Utilities.sleep(1000);
    SpreadsheetApp.flush();
    values = range.getValues();
  }

  // Anything that did not settle to a positive number is reported back with
  // whatever the cell actually held ("#N/A", "Loading...", ""), so a failure is
  // diagnosable from the tool output instead of silently absent.
  const prices = {};
  const unresolved = {};
  tickers.forEach(function (t, i) {
    const v = values[i] ? values[i][0] : "";
    if (typeof v === "number" && v > 0) {
      prices[t] = v;
    } else {
      unresolved[t] = String(v) || "(blank)";
    }
  });

  scratch.clearContents();

  const out = ContentService.createTextOutput(
    JSON.stringify({ status: 200, prices: prices, unresolved: unresolved })
  );
  out.setMimeType(ContentService.MimeType.JSON);
  return out;
}


/**
 * Helper: does column A already contain this date?
 *
 * Each weekly block writes the same date into all 23 rows, so the date
 * appearing anywhere means that week is already recorded. Column A may hold
 * real Date objects or strings depending on how the cell was formatted, so
 * normalise both to yyyy-MM-dd before comparing.
 */
function dateAlreadyPresent(sheet, dateStr) {
  const tz     = Session.getScriptTimeZone();
  const values = sheet.getRange("A:A").getValues();

  for (let i = values.length - 1; i >= 0; i--) {
    const v = values[i][0];
    if (v === "" || v === null) continue;

    const asText = Object.prototype.toString.call(v) === "[object Date]"
      ? Utilities.formatDate(v, tz, "yyyy-MM-dd")
      : String(v).trim();

    if (asText === dateStr) return true;
  }
  return false;
}


/**
 * Helper: read a range and return its display values + per-cell formatting,
 * so the downstream renderer can reproduce the sheet's exact look.
 */
function rangeData(sheet, a1) {
  const r = sheet.getRange(a1);
  return {
    values:      r.getDisplayValues(),  // text exactly as shown (currency, %, mn)
    backgrounds: r.getBackgrounds(),    // "#rrggbb" fill per cell
    fontColors:  r.getFontColors(),     // "#rrggbb" text colour per cell
    fontWeights: r.getFontWeights(),    // "bold" | "normal"
    fontStyles:  r.getFontStyles(),     // "italic" | "normal"
  };
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
 * Manual test for action=prices — run in the Apps Script editor to confirm
 * GOOGLEFINANCE resolves before wiring the MCP server up to it.
 */
function testResolvePrices() {
  const fakeEvent = {
    parameter: {
      token: SECRET_TOKEN,
      action: "prices",
      tickers: "VOO,VT,GLD,CURRENCY:USDIDR,CURRENCY:JPYIDR"
    }
  };
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
