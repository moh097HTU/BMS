"""
Regression tests over the seeded fixtures in data/.

Each data/DDC 3 - * folder is the SAME project with one class of fault seeded
into it, which makes them a free, real regression suite: the expected outcome
of every one is known, and any refactor that changes a single number here has
changed behaviour.

Run:  .venv/Scripts/python -m pytest tests -q
      .venv/Scripts/python tests/test_fixtures.py     (no pytest needed)

Fixtures whose .edb lacks Function.eod / Page.eod are skipped, not failed -
'DDC 3 - WRONG TR' ships only its .zw1, which needs 7-Zip installed.
"""

import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.eplan_verify import run_verification          # noqa: E402
from core.findings import build_findings                # noqa: E402
from core.render import findings_lines                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
MEMBERS = ("Function.eod", "Page.eod")
REFERENCE = os.path.join(DATA, "DDC 3 - CORRECT", "DDC 3.edb")

# (fixture, status, matched, missing, extra, terminal changes)
# 'DDC 3 - CORRECT' is the reference, so it is verified WITHOUT one: comparing a
# drawing against itself is refused by run_verification, and would prove nothing.
EXPECTED = [
    ("DDC 3 - CORRECT",                                  "CERTIFIED",  32, 0, 0, None),
    ("DDC 3 - FAULTS",                                   "UNRESOLVED", 29, 3, 4, 17),
    ("DDC 3 - spelling mistakes",                        "UNRESOLVED", 27, 5, 5, 0),
    ("DDC 3 - duplication - Copy",                       "UNRESOLVED", 29, 3, 3, 0),
    ("DDC 3 - wrong priorities - Copy",                  "UNRESOLVED", 32, 0, 1, 0),
    ("DDC 3 - Additional signal and missing signal - Copy",
                                                         "UNRESOLVED", 31, 1, 2, 0),
]


def _render(findings):
    """Render the findings the way cli.py and api/jobs.py both do, onto the
    kind of stream they write to.

    The buffer is wrapped in a cp1252 writer on purpose: that is what a Windows
    console gives you, and findings.py titles a terminal change with '->'
    (U+2192), which used to raise UnicodeEncodeError there. errors="strict"
    keeps this test honest about it.
    """
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict",
                              newline="")
    stream.write("\n".join(findings_lines(findings)))
    stream.flush()
    return raw.getvalue().decode("cp1252")


def _synthetic_report():
    """A report carrying one of EVERY finding category.

    The fixtures cannot reach them all - none of them has a self-contradicting
    schedule, an unresolved page, an ambiguous DDC or an advisory type hint - so
    those rendering paths would otherwise never be exercised.
    """
    return {
        "status": "UNRESOLVED",
        "eplan_points": 3, "excel_expected_total": 3, "matched": 1,
        "discrepancies": [
            {"kind": "MISSING", "full_combined": "AHU-1 Supply Fan Status",
             "expected": 1, "found": 0, "instances": []},
            {"kind": "EXTRA", "full_combined": "AHU-1 Supply Fan Stauts",
             "expected": 0, "found": 1,
             "instances": [{"function_object_id": 1, "page_name": "16DI-1",
                            "channel_raw": "DI-1"}]},
        ],
        "terminal_diagnostics": {
            "skipped": False, "has_differences": True, "join_key": "page_slot",
            "per_object_change_count": 1,
            "per_object_changes": [
                {"function_object_id": 2, "reference_function_object_id": 3,
                 "page_name": "8DO-1", "terminal_slot": 0, "join_key": "page_slot",
                 "reference_terminal": "178", "target_terminal": "177"}],
            "objects_only_in_target": [
                {"function_object_id": 4, "page_name": "8DO-1",
                 "terminal_slot": 5, "terminal": "199"}],
            "objects_only_in_reference": [
                {"function_object_id": 5, "page_name": "8DO-1",
                 "terminal_slot": 6, "terminal": "200"}],
            "page_roster_summary": [],
        },
        "excel_classification": {
            "invalid_type_rows": ["Chiller-1 Run Status"],
            "totals_mismatch_rows": [
                {"full_combined": "Chiller-2 Alarm", "quantity": 2,
                 "expected": 2, "actual": 3}],
        },
        "page_unresolved_list": [
            {"full_combined": "Pump-1 Trip", "function_object_id": 6,
             "page_name": None, "channel_raw": "DI-2"}],
        "page_ref_inconsistent_list": [
            {"full_combined": "Pump-2 Trip", "function_object_id": 7,
             "page_name": "16DI-1", "channel_raw": "DI-3"}],
        "legacy_ddc_page_variants": {"DDC 3": ["DDC 3", "DDC-3"]},
        "uncertified_diagnostics": {
            "channel_type_hint_mismatches": [
                {"full_combined": "FCU-1 Valve Command", "expected_io_type": "AO",
                 "page_name": "16DI-1-UP", "page_family_hint": ["DI"],
                 "function_object_id": 8, "channel_raw": "DI-9"}]},
    }


def test_render_covers_every_category():
    """Every category renders, on a cp1252 console, without losing a finding."""
    findings = build_findings(_synthetic_report())
    categories = set(findings["summary"]["by_category"])
    expected = {"MISSING_POINT", "EXTRA_INSTANCE", "WRONG_TR", "TR_PRESENCE",
                "INVALID_EXCEL_TYPE", "TOTALS_MISMATCH", "PAGE_INTEGRITY",
                "AMBIGUOUS_DDC", "TYPE_HINT_MISMATCH"}
    if categories != expected:
        raise AssertionError(f"synthetic report no longer covers every "
                             f"category: missing {expected - categories}, "
                             f"unexpected {categories - expected}")

    text = _render(findings)
    missing = [f["id"] for f in findings["findings"] if f["id"] not in text]
    if missing:
        raise AssertionError(f"findings never rendered: {missing}")
    # the advisory one is printed, but under its own heading and never as a
    # failure - the certified/advisory split has to survive rendering
    if "ADVISORY" not in text:
        raise AssertionError("the advisory finding rendered without its heading")
    return f"PASS render covers {len(categories)} categories"


def _run(fixture, use_reference=True):
    """Verify one fixture from a scratch copy of just the two members."""
    edb = os.path.join(DATA, fixture, "DDC 3.edb")
    csv_path = os.path.join(DATA, fixture, "points_tags.csv")
    if not all(os.path.exists(os.path.join(edb, m)) for m in MEMBERS):
        return None
    tmp = tempfile.mkdtemp()
    try:
        source = os.path.join(tmp, "DDC 3.edb")
        os.makedirs(source)
        for m in MEMBERS:
            shutil.copy2(os.path.join(edb, m), os.path.join(source, m))
        return run_verification(
            source, csv_path,
            reference_source=REFERENCE if use_reference else None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_fixture(fixture, status, matched, missing, extra, terminal):
    result = _run(fixture, use_reference=terminal is not None)
    if result is None:
        return f"SKIP {fixture} (no {' / '.join(MEMBERS)} in its .edb)"
    report = result["report"]
    problems = []

    def eq(label, got, want):
        if got != want:
            problems.append(f"{label}: got {got}, expected {want}")

    eq("status", report["status"], status)
    eq("matched", report["matched"], matched)
    eq("missing", report["missing_from_eplan"], missing)
    eq("extra", report["extra_instances_in_eplan"], extra)
    if terminal is None:
        eq("terminal check skipped",
           report["terminal_diagnostics"].get("skipped"), True)
    else:
        eq("terminal changes",
           report["terminal_diagnostics"]["per_object_change_count"], terminal)

    # the findings adapter must preserve the certified/advisory split exactly
    findings = build_findings(report)
    summary = findings["summary"]
    eq("findings total",
       summary["failure_count"] + summary["advisory_count"], summary["total"])
    for f in findings["findings"]:
        if not f["certified"] and f["severity"] != "ADVISORY":
            problems.append(f"{f['id']} is uncertified but severity "
                            f"{f['severity']} (must be ADVISORY)")
        if not f["title"] or not f["summary"]:
            problems.append(f"{f['id']} cannot be rendered as a card")
    if status == "CERTIFIED":
        eq("clean drawing has no findings", summary["total"], 0)

    # the console renderer must survive real findings - it is the only consumer
    # that has to encode them for a terminal (see _render for what that catches)
    text = _render(findings)
    for f in findings["findings"]:
        if f["id"] not in text:
            problems.append(f"{f['id']} never reached the rendered output")

    if problems:
        raise AssertionError(f"{fixture}:\n  " + "\n  ".join(problems))
    return (f"PASS {fixture}: {status}, matched {matched}, "
            f"{summary['total']} finding(s)")


def test_self_reference_is_refused():
    """A drawing compared against itself can only ever report 'no differences',
    so run_verification must refuse rather than emit a meaningless pass."""
    edb = os.path.join(DATA, "DDC 3 - CORRECT", "DDC 3.edb")
    try:
        run_verification(edb, os.path.join(DATA, "DDC 3 - CORRECT", "points_tags.csv"),
                         reference_source=edb)
    except ValueError:
        return "PASS self-reference is refused"
    raise AssertionError("a self-comparison was accepted")


def test_fixtures():
    for row in EXPECTED:
        print(check_fixture(*row))


if __name__ == "__main__":
    print(test_self_reference_is_refused())
    print(test_render_covers_every_category())
    test_fixtures()
    print("\nAll fixture checks passed.")
