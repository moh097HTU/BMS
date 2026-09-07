"""
verification_report.json  ->  a flat list of uniform "findings".

The report is deliberately shaped by ANALYSIS phase (discrepancies, Excel
classification, Phase D hints, Phase E terminals), which is right for the file
but wrong for a reviewer, who wants one queue of problems to walk. This module
is the only place that flattens it. It is pure: report in, findings out, no I/O.

THE ONE RULE THIS MODULE MUST NOT BREAK
    core/eplan_verify.py draws a hard line between CERTIFIED structural facts
    and UNCERTIFIED string inferences (see its module docstring - Phase D page
    -family hints and derived_hints.module_name are inferences and never gate
    certification). Every finding therefore carries `certified: bool`, and an
    uncertified finding is always severity ADVISORY. `failure_count` counts only
    certified findings, so nothing advisory can ever change a PASS into a FAIL.

The `likely_pair` field is the other inference in here and is labelled the same
way: when a MISSING point and an EXTRA_INSTANCE have nearly the same text, they
are almost always one renamed/mistyped point rather than two faults. That is a
similarity heuristic (difflib), so it is attached as a HINT on the finding and
never merges, hides or reclassifies either one.
"""

import difflib
import re

# Severity ladder, worst first. The UI orders and colours by this.
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "ADVISORY"]
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}

# How alike two point names must be before we suggest they are the same point
# renamed. Tuned so "2 Acting" vs "2 Actong" pairs but two genuinely different
# points do not; a miss here costs nothing (the two findings just stand alone).
_PAIR_THRESHOLD = 0.86


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _finding(fid, severity, category, title, summary, *, certified=True,
             expected=None, found=None, evidence=None, detail=None, page=None):
    return {
        "id": fid,
        "severity": severity,
        "certified": certified,
        "category": category,
        "title": title,
        "summary": summary,
        "expected": expected,
        "found": found,
        "page_name": page,
        "evidence": evidence or [],
        "detail": detail or {},
    }


def _pair_hints(findings):
    """
    Attach a `likely_pair` HINT to MISSING/EXTRA_INSTANCE findings whose titles
    are nearly identical - the signature of a renamed or mistyped point rather
    than one point vanishing and an unrelated one appearing.

    UNCERTIFIED (a difflib similarity), so it only annotates: both findings keep
    their own severity, their own place in the queue and their own certified
    status. Each side is paired at most once, best match first.
    """
    missing = [f for f in findings if f["category"] == "MISSING_POINT"]
    extra = [f for f in findings if f["category"] == "EXTRA_INSTANCE"]
    if not missing or not extra:
        return

    scored = []
    for m in missing:
        for e in extra:
            ratio = difflib.SequenceMatcher(
                None, _norm(m["title"]), _norm(e["title"])).ratio()
            if ratio >= _PAIR_THRESHOLD:
                scored.append((ratio, m, e))
    scored.sort(key=lambda t: -t[0])

    taken = set()
    for ratio, m, e in scored:
        if m["id"] in taken or e["id"] in taken:
            continue
        taken.add(m["id"])
        taken.add(e["id"])
        for a, b in ((m, e), (e, m)):
            a["likely_pair"] = {
                "id": b["id"],
                "title": b["title"],
                "similarity": round(ratio, 4),
                "certified": False,
                "note": "Text-similarity hint only: these two look like ONE "
                        "point renamed or mistyped, not two separate faults. "
                        "Both findings still stand on their own.",
            }


def build_findings(report):
    """
    Flatten a verification report into findings, worst first.

    Returns {"summary": {...}, "findings": [...]}. `summary.failure_count`
    counts CERTIFIED findings only - advisory ones are reported but never
    contribute to a failure.
    """
    out = []
    n = 0

    def nid(prefix):
        nonlocal n
        n += 1
        return f"{prefix}-{n:04d}"

    # --- certified: point multiplicity (the core verification) --------------
    for d in report.get("discrepancies", []):
        is_missing = d["kind"] == "MISSING"
        evidence = [{
            "function_object_id": i.get("function_object_id"),
            "page_name": i.get("page_name"),
            "channel_raw": i.get("channel_raw"),
        } for i in d.get("instances", [])]
        pages = [i.get("page_name") for i in evidence if i.get("page_name")]
        out.append(_finding(
            nid("missing" if is_missing else "extra"),
            "CRITICAL" if is_missing else "HIGH",
            "MISSING_POINT" if is_missing else "EXTRA_INSTANCE",
            d.get("full_combined") or "(unnamed point)",
            (f"The schedule asks for {d['expected']}, the drawing has "
             f"{d['found']}." if is_missing else
             f"The drawing has {d['found']} instance(s); the schedule asks for "
             f"{d['expected']}."),
            expected=d["expected"], found=d["found"], evidence=evidence,
            page=pages[0] if pages else None))

    # --- certified: terminal ('TR') designations, Phase E --------------------
    term = report.get("terminal_diagnostics") or {}
    for c in term.get("per_object_changes", []):
        slot = c.get("terminal_slot")
        where = f"position {slot + 1}" if slot is not None else "this object"
        out.append(_finding(
            nid("tr"), "HIGH", "WRONG_TR",
            f"Terminal {c['reference_terminal']} → {c['target_terminal']} "
            f"on {c['page_name']}",
            f"On page {c['page_name']}, {where} is numbered "
            f"{c['target_terminal']} but the reference drawing has "
            f"{c['reference_terminal']}.",
            expected=c["reference_terminal"], found=c["target_terminal"],
            page=c.get("page_name"),
            evidence=[{"function_object_id": c.get("function_object_id"),
                       "page_name": c.get("page_name"),
                       "terminal_slot": slot}],
            detail={"join_key": c.get("join_key"),
                    "reference_function_object_id":
                        c.get("reference_function_object_id")}))

    for o in term.get("objects_only_in_target", []):
        out.append(_finding(
            nid("tr"), "HIGH", "TR_PRESENCE",
            f"Extra terminal {o['terminal']} on {o['page_name']}",
            f"Terminal {o['terminal']} exists in this drawing but not in the "
            f"reference.", found=o["terminal"], page=o.get("page_name"),
            evidence=[{"function_object_id": o.get("function_object_id"),
                       "page_name": o.get("page_name"),
                       "terminal_slot": o.get("terminal_slot")}]))
    for o in term.get("objects_only_in_reference", []):
        out.append(_finding(
            nid("tr"), "HIGH", "TR_PRESENCE",
            f"Missing terminal {o['terminal']} on {o['page_name']}",
            f"Terminal {o['terminal']} exists in the reference drawing but not "
            f"in this one.", expected=o["terminal"], page=o.get("page_name"),
            evidence=[{"function_object_id": o.get("function_object_id"),
                       "page_name": o.get("page_name"),
                       "terminal_slot": o.get("terminal_slot")}]))

    # --- certified: the schedule contradicting itself ------------------------
    excel = report.get("excel_classification") or {}
    for name in excel.get("invalid_type_rows", []):
        out.append(_finding(
            nid("xltype"), "MEDIUM", "INVALID_EXCEL_TYPE", name,
            "This schedule row sets more than one of DI/DO/AI/AO, so its I/O "
            "type is ambiguous. The tool never guesses one - fix it in the "
            "workbook."))
    for r in excel.get("totals_mismatch_rows", []):
        out.append(_finding(
            nid("xltotal"), "MEDIUM", "TOTALS_MISMATCH",
            r.get("full_combined") or "(unnamed point)",
            f"QTY × flag does not equal the Total_* columns "
            f"(quantity {r.get('quantity')}).",
            detail={"expected": r.get("expected"), "actual": r.get("actual")}))

    # --- certified: page-reference integrity --------------------------------
    for p in report.get("page_unresolved_list", []):
        out.append(_finding(
            nid("page"), "MEDIUM", "PAGE_INTEGRITY",
            p.get("full_combined") or "(unnamed point)",
            "This point's owning schematic page could not be resolved, so it "
            "could not be scoped to an active page.",
            evidence=[{"function_object_id": p.get("function_object_id"),
                       "page_name": p.get("page_name"),
                       "channel_raw": p.get("channel_raw")}]))
    for p in report.get("page_ref_inconsistent_list", []):
        out.append(_finding(
            nid("page"), "MEDIUM", "PAGE_INTEGRITY",
            p.get("full_combined") or "(unnamed point)",
            "The two page references in this record (f1eb / f2eb) disagree - "
            "they agree on every known-good record.",
            evidence=[{"function_object_id": p.get("function_object_id"),
                       "page_name": p.get("page_name"),
                       "channel_raw": p.get("channel_raw")}]))

    for ddc, names in (report.get("legacy_ddc_page_variants") or {}).items():
        out.append(_finding(
            nid("ddc"), "MEDIUM", "AMBIGUOUS_DDC", f"{ddc}: {len(names)} name variants",
            "The same DDC number appears under several different page names, so "
            "scoping is ambiguous.", detail={"variants": names}))

    # --- UNCERTIFIED: Phase D page-family hints ------------------------------
    # String inference off the page name. Advisory by construction - see the
    # module docstring in core/eplan_verify.py.
    unc = report.get("uncertified_diagnostics") or {}
    for f in unc.get("channel_type_hint_mismatches", []):
        out.append(_finding(
            nid("hint"), "ADVISORY", "TYPE_HINT_MISMATCH",
            f.get("full_combined") or "(unnamed point)",
            f"The schedule types this point {f['expected_io_type']}, but it is "
            f"drawn on page {f['page_name']}, whose name suggests "
            f"{'/'.join(f['page_family_hint'])}.",
            certified=False, expected=f.get("expected_io_type"),
            page=f.get("page_name"),
            evidence=[{"function_object_id": f.get("function_object_id"),
                       "page_name": f.get("page_name"),
                       "channel_raw": f.get("channel_raw")}],
            detail={"page_family_hint": f.get("page_family_hint"),
                    "why_advisory": "The I/O family is read from the page NAME, "
                                    "not from a certified module object."}))

    _pair_hints(out)
    out.sort(key=lambda f: (SEVERITY_RANK[f["severity"]], f["category"],
                            f["title"] or ""))

    by_severity = {s: 0 for s in SEVERITY_ORDER}
    by_category = {}
    for f in out:
        by_severity[f["severity"]] += 1
        by_category[f["category"]] = by_category.get(f["category"], 0) + 1

    return {
        "summary": {
            "status": report.get("status"),
            "total": len(out),
            # advisory findings are reported but never make a run fail
            "failure_count": sum(1 for f in out if f["certified"]),
            "advisory_count": sum(1 for f in out if not f["certified"]),
            "by_severity": by_severity,
            "by_category": by_category,
            "terminal_check_skipped": bool(term.get("skipped")),
            "eplan_points": report.get("eplan_points"),
            "schedule_expects": report.get("excel_expected_total"),
            "matched": report.get("matched"),
        },
        "findings": out,
    }
