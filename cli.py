"""
Command-line front end for the EPLAN -> BMS point verifier.

All the analysis lives in core/eplan_verify.py; this file only parses arguments
and renders the result to the console. It is one of three callers of
run_verification() - the others are the web API (api/) and the tests.

Examples
    python cli.py --source "data/DDC 3 - CORRECT/DDC 3.edb" \
                  --schedule "data/DDC 3 - CORRECT/points_tags.csv"

    # with the Phase E terminal ('TR') check against a known-good drawing
    python cli.py --source "data/DDC 3 - FAULTS/DDC 3.edb" \
                  --schedule "data/DDC 3 - FAULTS/points_tags.csv" \
                  --reference "data/DDC 3 - CORRECT/DDC 3.edb"

    # build the schedule CSV straight from the protected workbook first
    python cli.py --excel "parse/AI TEST - DDC 3 IRQAH.xlsx" --password Estimation \
                  --source "data/DDC 3 - CORRECT/DDC 3.edb"

Exit status: 0 if the run is CERTIFIED, 1 if UNRESOLVED, 2 on a usage/IO error.
"""

import argparse
import json
import os
import sys

from core.eplan_verify import run_verification
from core.findings import build_findings
from core.render import findings_lines, result_lines


def _progress(stage, total, message):
    print(f"[{stage}/{total}] {message} ...")


def build_parser():
    p = argparse.ArgumentParser(
        description="Verify an EPLAN drawing against a BMS points schedule.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True,
                   help="drawing to verify: an .edb folder or a .zw1 backup")
    sched = p.add_mutually_exclusive_group(required=True)
    sched.add_argument("--schedule",
                       help="points_tags.csv in the QTY/DI/DO/AI/AO/Total_* schema")
    sched.add_argument("--excel",
                       help="Excel schedule to parse into a CSV first")
    p.add_argument("--password", help="password for an encrypted --excel workbook")
    p.add_argument("--sheet", default="Sheet1", help="worksheet name (default: Sheet1)")
    p.add_argument("--reference",
                   help="known-good drawing of the SAME project; enables the "
                        "terminal ('TR') check. Omit to skip it - it is never "
                        "silently reported as a pass.")
    p.add_argument("--out-dir",
                   help="where to write the JSON artifacts (default: alongside "
                        "the source)")
    return p


def main(argv=None):
    # findings.py titles a terminal change with '->' (U+2192); a Windows console
    # is cp1252 and raises UnicodeEncodeError on it. Point names come out of the
    # schedule and can carry anything, so widen stdout rather than sanitise text.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(
        args.source.rstrip("\\/")))

    schedule_csv = args.schedule
    if args.excel:
        # importing here keeps the EPLAN-only path free of the Excel dependencies
        from core.schedule import extract, write_csv
        schedule_csv = os.path.join(out_dir, "points_tags.csv")
        os.makedirs(out_dir, exist_ok=True)
        print(f"[0/7] Parsing the Excel schedule ({args.sheet}) ...")
        rows = extract(args.excel, args.password, args.sheet)
        write_csv(rows, schedule_csv)
        print(f"      {len(rows)} point rows -> {schedule_csv}")

    try:
        result = run_verification(args.source, schedule_csv,
                                  reference_source=args.reference,
                                  out_dir=out_dir, progress=_progress)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    report = result["report"]
    findings = build_findings(report)
    findings_path = os.path.join(out_dir, "findings.json")
    with open(findings_path, "w", encoding="utf-8") as fh:
        json.dump(findings, fh, indent=2, ensure_ascii=False)

    print("\n".join(result_lines(report, len(result["excluded"]))))
    print("\n".join(findings_lines(findings)))
    print(f"      report   -> {report['outputs']['verification_report.json']}")
    print(f"      findings -> {findings_path}")
    return 0 if report["status"] == "CERTIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
