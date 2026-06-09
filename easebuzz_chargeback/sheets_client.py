"""Read a Google Sheet using the same user OAuth token as gmail_client.

The refresh token in token.json carries the spreadsheets.readonly scope
alongside the Gmail scopes, so both services share one credential.

Usage:
    creds = gmail_client.build_oauth_creds("credentials.json", "token.json")
    lookup = sheets_client.merchant_lookup(cfg["sheet"], creds=creds)
    # -> { normalized_name: {"extra_to": [...], "extra_cc": [...]}, ... }
"""
from __future__ import annotations

import re
from pathlib import Path

from googleapiclient.discovery import build


def _normalize(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[\s._\-'\"/&]+", "", s).lower()


def _split_emails(cell: str) -> list[str]:
    if not cell:
        return []
    parts = re.split(r"[,;\n]+", cell)
    out: list[str] = []
    for p in parts:
        m = re.search(r"<([^>]+@[^>]+)>", p)
        if m:
            out.append(m.group(1).strip())
            continue
        m = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", p)
        if m:
            out.append(m.group(0))
    return out


def _build_lookup(
    rows: list[list[str]],
    *,
    name_col: str,
    to_col: str,
    cc_col: str | None,
    status_col: str | None = None,
) -> dict:
    if not rows:
        return {}
    header_row = rows[0]

    def idx(colname: str) -> int:
        target = _normalize(colname)
        for i, h in enumerate(header_row):
            if _normalize(h) == target:
                return i
        raise KeyError(f"column {colname!r} not found in header: {header_row}")

    i_name = idx(name_col)
    i_to = idx(to_col)
    i_cc = idx(cc_col) if cc_col else None
    i_status = idx(status_col) if status_col else None

    out: dict[str, dict] = {}
    for row in rows[1:]:
        name = row[i_name] if i_name < len(row) else ""
        key = _normalize(name)
        if not key:
            continue
        status = row[i_status] if (i_status is not None and i_status < len(row)) else ""
        out[key] = {
            "raw_name": name,
            "extra_to": _split_emails(row[i_to] if i_to < len(row) else ""),
            "extra_cc": _split_emails(row[i_cc] if (i_cc is not None and i_cc < len(row)) else ""),
            "status":  (status or "").strip(),
        }
    return out


def fetch_rows(creds, spreadsheet_id: str, range_a1: str) -> list[list[str]]:
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_a1
    ).execute().get("values", [])


# ---------------------------------------------------------------------------
# Tracker sheet: append a row per successful send
# ---------------------------------------------------------------------------

def _month_variants() -> list[str]:
    """Return today's month name in 4 spellings (full/short × normal/upper).
    First entry is the canonical name used when creating a new tab."""
    import datetime
    now = datetime.datetime.now()
    return [
        now.strftime("%B"),         # April
        now.strftime("%B").upper(), # APRIL
        now.strftime("%b"),         # Apr
        now.strftime("%b").upper(), # APR
    ]


def _compact_skeleton(svc, spreadsheet_id: str, sheet_id: int, tab_title: str) -> None:
    """After duplicating a template tab, compact its layout:
        - Keep all structural rows (rows in a merge, OR bold first cell — i.e.,
          section title bands and cyan column-header rows).
        - Keep 1 empty buffer row between consecutive structural groups so
          values.append's table-boundary detection terminates inside the Pre
          Arbitration L2 (or whichever the active appending section is).
        - Delete every other row (data rows + trailing empty grid space).

    Works generically for both L1 (single section) and L2 (multi-section) tabs.
    """
    SCAN_ROWS = 500  # cover any reasonable template; April 26 L2 tops out at row 184

    # 1) Read structure: merges + per-row bold flag for column A.
    info = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        ranges=[f"'{tab_title}'!A1:A{SCAN_ROWS}"],
        fields=("sheets(properties(sheetId,gridProperties),merges,"
                "data(rowData(values(userEnteredFormat(textFormat(bold))))))"),
    ).execute()
    sheet = next((s for s in info["sheets"]
                  if s["properties"]["sheetId"] == sheet_id), None)
    if not sheet:
        return

    total_rows = sheet["properties"]["gridProperties"].get("rowCount", 0)

    # Set of row indices (0-indexed) that are part of any merge.
    merged_rows: set[int] = set()
    for m in sheet.get("merges", []):
        for r in range(m["startRowIndex"], m["endRowIndex"]):
            merged_rows.add(r)

    rowData = sheet.get("data", [{}])[0].get("rowData", [])

    # Classify each row in scan window as 'S' (structural) or 'D' (data/empty).
    # Structural ⇔ in a merge OR first cell has bold text.
    cls: list[str] = []
    for i in range(min(SCAN_ROWS, total_rows)):
        in_merge = i in merged_rows
        bold = False
        if i < len(rowData):
            vals = rowData[i].get("values", [])
            if vals:
                bold = (vals[0].get("userEnteredFormat", {})
                                 .get("textFormat", {})
                                 .get("bold", False))
        cls.append("S" if (in_merge or bold) else "D")

    # 2) Walk the classification and decide which rows to KEEP.
    # Group consecutive same-class rows into runs.
    if not cls:
        return
    groups: list[tuple[str, int, int]] = []  # (class, start, end_exclusive)
    cur_cls, cur_start = cls[0], 0
    for i in range(1, len(cls)):
        if cls[i] != cur_cls:
            groups.append((cur_cls, cur_start, i))
            cur_cls, cur_start = cls[i], i
    groups.append((cur_cls, cur_start, len(cls)))

    keep: set[int] = set()
    for gi, (gcls, s, e) in enumerate(groups):
        if gcls == "S":
            keep.update(range(s, e))
        else:
            # D group — keep 1 empty buffer row only if another S group follows.
            # Pick the LAST row of the group: in real templates, the row right
            # before the next merge band is the empty buffer (data from the
            # previous month sits at the TOP of the group).
            has_next_s = any(g[0] == "S" for g in groups[gi + 1:])
            if has_next_s:
                keep.add(e - 1)
            # else: trailing data — delete entirely.

    # If NO rows qualified as structural (e.g. plain PayU-style tab with no
    # bold/cyan header), `deleteDimension` over the entire range would error
    # ("can't delete all rows on the sheet"). Wipe values instead — keeps
    # the grid intact and gives us an empty tab ready for fresh appends.
    if not keep:
        svc.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab_title}'",
            body={},
        ).execute()
        return

    # 3) Compute rows to DELETE: everything in [0, total_rows) not in keep.
    delete = sorted(r for r in range(total_rows) if r not in keep)
    if not delete:
        return

    # 4) Compress consecutive deletes into ranges; submit deleteDimension
    # requests in REVERSE order so earlier indices stay valid.
    ranges: list[tuple[int, int]] = []
    cs, ce = delete[0], delete[0] + 1
    for r in delete[1:]:
        if r == ce:
            ce = r + 1
        else:
            ranges.append((cs, ce))
            cs, ce = r, r + 1
    ranges.append((cs, ce))

    requests = [
        {"deleteDimension": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": s, "endIndex": e}
        }}
        for s, e in reversed(ranges)
    ]
    svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


def _ensure_log_tab(svc, spreadsheet_id: str, pattern: str) -> str:
    """Resolve the current month's tab, creating it if missing.

    When creating, copies headers + bold+colored header formatting from any
    existing tab in the spreadsheet so the new month's sheet looks identical
    to the previous one.
    """
    months = _month_variants()
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(title,sheetId)",
    ).execute()
    all_sheets = meta.get("sheets", [])
    title_to_sid = {s["properties"]["title"]: s["properties"]["sheetId"] for s in all_sheets}
    lower_to_actual = {t.lower(): t for t in title_to_sid}

    # 1) Tab already exists?
    for m in months:
        cand = pattern.replace("{month}", m).lower()
        if cand in lower_to_actual:
            return lower_to_actual[cand]

    # 2) Need to create it. Use full month name for canonical naming.
    new_tab = pattern.replace("{month}", months[0])

    # Find a template tab matching the SAME pattern (e.g., another
    # "{month} 26 L1" tab from a prior month). We DUPLICATE it so the new
    # tab inherits headers, header formatting (cyan/TNR/bold), column count,
    # frozen rows — everything that makes the tabs look identical.
    pattern_regex = re.compile(
        "^" + re.escape(pattern).replace(re.escape("{month}"), r"[A-Za-z]+") + "$",
        re.IGNORECASE,
    )
    matching = [(t, sid) for t, sid in title_to_sid.items()
                if pattern_regex.match(t) and t != new_tab]

    if matching:
        # Pick the newest pattern-matching tab as template (last in API order)
        template_title, template_sid = matching[-1]
        # Find template tab's index so we can insert the new one right AFTER it
        # (chronological order — June 26 L2 sits to the right of May 26 L2, etc.)
        template_index = next(
            (i for i, s in enumerate(all_sheets)
             if s["properties"]["sheetId"] == template_sid),
            None,
        )
        insert_index = (template_index + 1) if template_index is not None else None

        # 1) Duplicate the template tab — copies headers + formatting + column widths
        # + section bands (Fraudulent / Arbitration / RBI) + merges.
        dup_req = {"sourceSheetId": template_sid, "newSheetName": new_tab}
        if insert_index is not None:
            dup_req["insertSheetIndex"] = insert_index
        dup_resp = svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"duplicateSheet": dup_req}]},
        ).execute()
        new_sheet_id = dup_resp["replies"][0]["duplicateSheet"]["properties"]["sheetId"]

        # 2) Compact the skeleton: keep structural rows (merge bands + cyan
        # column-header rows), drop data rows, leave 1 empty buffer row between
        # consecutive structural groups so values.append's table-detection
        # terminates cleanly inside the active (Pre Arbitration L2) section.
        _compact_skeleton(svc, spreadsheet_id, new_sheet_id, new_tab)
    else:
        # No template — fall back to a bare new tab with default styling.
        add_resp = svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{
                "addSheet": {
                    "properties": {
                        "title": new_tab,
                        "gridProperties": {"frozenRowCount": 1, "columnCount": 12},
                    }
                }
            }]},
        ).execute()
        # No headers / formatting since we have no template to copy.

    return new_tab


def _find_section_append_range(svc, spreadsheet_id: str, tab: str,
                               marker: str) -> str:
    """Find the cyan column-header row of the section whose title contains
    `marker` (e.g., "Arbitration/ L3 CB") inside `tab`, and return an A1
    range pointing at that header row's column A. Used to direct
    values.append into a sub-section instead of the top of the tab.

    Section bands in L2-style tabs are 2-row merged titles, so the cyan
    column-header row sits at (band_row_1idx + 2).
    """
    rows = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{tab}'!A1:A300",
    ).execute().get("values", [])
    needle = marker.strip().lower()
    for i, r in enumerate(rows):
        cell = (r[0] if r else "").strip().lower()
        if needle in cell:
            header_row_1idx = i + 1 + 2  # band row + 2-row band height
            return f"'{tab}'!A{header_row_1idx}"
    raise RuntimeError(
        f"Section marker {marker!r} not found in tab {tab!r}"
    )


def append_log_row(creds, cfg_log: dict, row: list[str]) -> dict:
    """Append one row to the tracker sheet in the current month's tab.
    Auto-creates the tab on the first call of a new month.

    If cfg_log["target_section"] is set, append goes into that named sub-
    section (e.g., "Arbitration/ L3 CB") rather than the top of the tab.
    """
    spreadsheet_id = cfg_log["spreadsheet_id"]
    pattern = cfg_log.get("tab_pattern", "{month} 26 L1")
    target_section = cfg_log.get("target_section")
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    tab = _ensure_log_tab(svc, spreadsheet_id, pattern)
    if target_section:
        rng = _find_section_append_range(svc, spreadsheet_id, tab, target_section)
    else:
        rng = f"'{tab}'!A1"
    resp = svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=rng,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return {"tab": tab, "updatedRange": resp.get("updates", {}).get("updatedRange")}


def merchant_lookup(cfg_sheet: dict, *, creds, base_dir: Path | None = None) -> dict:
    """Load the merchant-contacts lookup from a Google Sheet.

    Required keys in cfg_sheet:
        spreadsheet_id, name_col, to_col, [cc_col], [range]
    """
    if not cfg_sheet or not cfg_sheet.get("spreadsheet_id"):
        return {}
    rows = fetch_rows(
        creds,
        cfg_sheet["spreadsheet_id"],
        cfg_sheet.get("range", "A1:Z"),
    )
    return _build_lookup(
        rows,
        name_col=cfg_sheet["name_col"],
        to_col=cfg_sheet["to_col"],
        cc_col=cfg_sheet.get("cc_col"),
        status_col=cfg_sheet.get("status_col"),
    )
