"""
Report + findings  ->  the console log lines.

Both front ends show the same run: cli.py prints it to the terminal, and the
job runner (api/jobs.py) prints it to the server console so a web run is not a
silent one. The formatting therefore cannot live in either of them - it lives
here, and both call it.

Every function RETURNS LINES and prints nothing, for the same reason
core/findings.py returns findings and writes nothing: a pure function is
testable without capturing stdout, and it lets the job runner emit a whole run
as ONE atomic write (see api/jobs.py - two jobs run concurrently and would
otherwise interleave line by line).
"""

import os

from core.findings import SEVERITY_ORDER

TERMINAL_CATEGORIES = ("WRONG_TR", "TR_PRESENCE")


def result_lines(report, excluded_count):
    """The run header: what was read, the phase counters, the verdict.

    Individual problems are NOT here - they are findings, and findings_lines()
    renders them once, in severity order."""
    src = report["source"]
    excel = report["excel_classification"]
    hints = report["uncertified_diagnostics"]["channel_type_hint_mismatches"]
    terminal = report["terminal_diagnostics"]
    applied = report["page_scoping"]["active_filter_applied"]

    out = []
    if not applied:
        out.append("      WARNING: 0 active pages detected for the scoped DDC. "
                   "The active-page flag (Page.eod byte 1129) may not apply to "
                   "this project/DDC; keeping ALL records rather than dropping "
                   "everything.")

    out.append("      Result")
    out.append(f"      SOURCE ({src['source_kind']})         : "
               f"{os.path.basename(src['source'].rstrip(chr(92) + chr(47)))}")
    out.append(f"      Function.eod sha256   : {src['function_eod_sha256']}")
    out.append(f"      EPLAN points (active) : {report['eplan_points']} "
               f"(dropped {excluded_count} on deleted pages"
               f"{'' if applied else '; ACTIVE FILTER NOT APPLIED'})")
    out.append(f"      schedule expects      : {report['excel_expected_total']} "
               f"(+{report['schedule']['summary_rows_ignored']} calculated "
               f"summary rows ignored)")
    out.append(f"      matched (multiplicity): {report['matched']}")
    out.append(f"      missing from drawing  : {report['missing_from_eplan']}")
    out.append(f"      extra instances       : {report['extra_instances_in_eplan']}")
    out.append(f"      page unresolved       : {report['page_unresolved']}")
    out.append(f"      page ref inconsistent : {report['page_ref_inconsistent']}")
    out.append(f"      invalid Excel type    : {excel['invalid_type_count']}")
    out.append(f"      totals mismatch       : {excel['totals_mismatch_count']}")
    out.append(f"      type-hint mismatch (advisory): {len(hints)}")
    if terminal.get("skipped"):
        out.append("      terminal ('TR') check : SKIPPED (no reference drawing)")
    else:
        out.append(f"      terminal ('TR') object changes: "
                   f"{terminal['per_object_change_count']}")
    out.append(f"      STATUS                : {report['status']}")
    return out


def findings_lines(findings):
    """The findings queue - the same list core/findings.py hands the web UI.

    ADVISORY findings are rendered like any other but never counted as
    failures (see the certified/advisory rule in core/findings.py)."""
    queue = findings["findings"]
    summary = findings["summary"]
    if not queue:
        return ["      Findings              : none"]

    out = [f"      Findings ({summary['total']} total: "
           f"{summary['failure_count']} failure, "
           f"{summary['advisory_count']} advisory)"]
    for severity in SEVERITY_ORDER:
        group = sorted((f for f in queue if f["severity"] == severity),
                       key=_order)
        if not group:
            continue
        note = "  (reported, never fails the run)" if severity == "ADVISORY" else ""
        out.append(f"      -- {severity} ({len(group)}){note} --")
        for f in group:
            if f["category"] in TERMINAL_CATEGORIES:
                out.append(f"      {_terminal_line(f)}")
            else:
                out.extend(_finding_lines(f))
    return out


def _slot(f):
    return ((f["evidence"] or [{}])[0]).get("terminal_slot")


def _order(f):
    """Findings arrive sorted by (severity, category, title), which for a
    terminal puts 185 before 186 by TEXT and so scrambles the positions. Sort
    those by page and position instead, so a shifted rail reads straight down
    the column; everything else keeps the queue's own order."""
    if f["category"] in TERMINAL_CATEGORIES:
        slot = _slot(f)
        return (f["category"], f["page_name"] or "",
                -1 if slot is None else slot, f["title"] or "")
    return (f["category"], "", -1, f["title"] or "")


def _terminal_line(f):
    """One line for a terminal finding. A shifted rail is dozens of these, and
    at six lines apiece the pattern - every position on a page moved by one -
    stops being visible; on one line it reads straight down the column."""
    ev = (f["evidence"] or [{}])[0]
    slot = _slot(f)
    where = f["page_name"] or "?"
    if slot is not None:
        where += f" pos {slot + 1}"
    oids = f"oid={ev.get('function_object_id')}"
    ref_oid = (f.get("detail") or {}).get("reference_function_object_id")
    if ref_oid is not None:
        oids += f" ref={ref_oid}"
    if f["category"] == "WRONG_TR":
        what = f"{f['expected']} -> {f['found']}"
    elif f.get("found") is not None:
        what = f"terminal {f['found']} in target only"
    else:
        what = f"terminal {f['expected']} in reference only"
    return f"[{f['id']}] {f['category']}  {where}: {what}  ({oids})"


def _finding_lines(f):
    out = [f"      [{f['id']}] {f['category']}  {f['title']}",
           f"             {f['summary']}"]
    bits = []
    if f.get("expected") is not None:
        bits.append(f"expected={f['expected']}")
    if f.get("found") is not None:
        bits.append(f"found={f['found']}")
    if f.get("page_name"):
        bits.append(f"page={f['page_name']}")
    if bits:
        out.append("             " + "  ".join(bits))
    for e in f.get("evidence") or []:
        line = "  ".join(f"{k}={v}" for k, v in e.items() if v is not None)
        if line:
            out.append(f"             {line}")
    for key, value in (f.get("detail") or {}).items():
        # why_advisory / note are prose for the web card, not for a log line
        if value is None or key.startswith("why_") or key == "note":
            continue
        out.append(f"             {key}={value}")
    pair = f.get("likely_pair")
    if pair:
        # UNCERTIFIED on purpose: a text-similarity hint, never a merge
        out.append(f"             likely pair -> [{pair['id']}] {pair['title']} "
                   f"({pair['similarity']}, UNCERTIFIED)")
    return out
