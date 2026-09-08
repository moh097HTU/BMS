"""
Background verification jobs.

A run takes tens of seconds (Function.eod is tens of MB and every record is a
separate zlib stream), which is far too long to hold an HTTP request open, so
uploads create a JOB and the browser follows its progress.

Concurrency is deliberately small. core.eplan_verify.iter_records() reads the
whole Function.eod into memory, and a run holds both the target and the
reference; a handful of parallel jobs would exhaust RAM long before they
exhausted CPU. MAX_WORKERS is the knob.

Each job owns a directory under jobs/, which is also where the uploads land:

    jobs/<job_id>/
        upload/        exactly what the browser sent
        source/        Function.eod + Page.eod for the drawing under test
        reference/     the same two members for the known-good drawing (if any)
        points_tags.csv
        verification_report.json, eplan_points.json, ... (run_verification)
        findings.json  (core.findings)

Jobs are in-memory state plus that directory: restarting the server forgets the
job list but keeps every artifact on disk.
"""

import json
import os
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from core.eplan_verify import TOTAL_STAGES, run_verification
from core.findings import build_findings
from core.render import findings_lines, result_lines

MAX_WORKERS = 2
MAX_JOBS_REMEMBERED = 200

JOBS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jobs")

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                               thread_name_prefix="verify")
_jobs = OrderedDict()
_lock = threading.Lock()


def job_dir(job_id):
    return os.path.join(JOBS_ROOT, job_id)


def create_job(owner):
    """Reserve a job id and its directory. The caller then writes the uploads
    into upload/ and calls start()."""
    job_id = uuid.uuid4().hex[:16]
    root = job_dir(job_id)
    for sub in ("upload", "source", "reference"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "owner": owner,
            "state": "pending",
            "stage": 0,
            "total_stages": TOTAL_STAGES,
            "message": "Queued",
            "created": time.time(),
            "finished": None,
            "error": None,
            "summary": None,
            "label": None,
        }
        while len(_jobs) > MAX_JOBS_REMEMBERED:
            _jobs.popitem(last=False)
    return job_id


def get_job(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def list_jobs(owner=None):
    with _lock:
        jobs = [dict(j) for j in _jobs.values()]
    if owner is not None:
        jobs = [j for j in jobs if j["owner"] == owner]
    return sorted(jobs, key=lambda j: -j["created"])


def _update(job_id, **fields):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def start(job_id, source, points_csv, reference_source=None, label=None):
    """Queue the verification. Returns immediately."""
    _update(job_id, state="queued", message="Waiting for a worker", label=label)
    _executor.submit(_run, job_id, source, points_csv, reference_source)


def _run(job_id, source, points_csv, reference_source):
    root = job_dir(job_id)
    _update(job_id, state="running", message="Starting", stage=0)

    def progress(stage, total, message):
        _update(job_id, stage=stage, total_stages=total, message=message)

    try:
        result = run_verification(source, points_csv,
                                  reference_source=reference_source,
                                  out_dir=root, progress=progress)
        report = result["report"]
        findings = build_findings(report)
        _attach_ai_review(job_id, findings)
        with open(os.path.join(root, "findings.json"), "w", encoding="utf-8") as fh:
            json.dump(findings, fh, indent=2, ensure_ascii=False)
        _log(job_id, result_lines(report, len(result["excluded"]))
             + findings_lines(findings))
        _update(job_id, state="done", stage=TOTAL_STAGES,
                message="Complete", finished=time.time(),
                summary=findings["summary"])
    except Exception as exc:
        # The message reaches the browser, so keep it about what went wrong with
        # the INPUT; the traceback stays in the log for whoever runs the server.
        traceback.print_exc()
        _update(job_id, state="error", message=str(exc) or exc.__class__.__name__,
                error=exc.__class__.__name__, finished=time.time())


def _attach_ai_review(job_id, findings):
    """Best-effort AI review of the findings queue (core/recommend.py).

    Runs on every job, but NEVER blocks completion: no key, a missing package,
    or a Gemini failure just sets summary.ai_status="unavailable" and leaves the
    findings exactly as the deterministic verifier produced them. The recommend
    module is imported here, lazily, so a machine without google-genai still
    starts and runs verifications.
    """
    if not findings.get("findings"):
        findings["summary"]["ai_status"] = "skipped"
        return
    from core.recommend import analyze_findings, AIReviewUnavailable
    try:
        _update(job_id, message="AI review")
        findings["recommendations"] = analyze_findings(findings["findings"])
        findings["summary"]["ai_status"] = "ok"
    except AIReviewUnavailable as exc:
        # expected config case (no key / package) - one clean line, no traceback
        sys.stdout.write(f"--- job {job_id}: AI review skipped ({exc}) ---\n")
        sys.stdout.flush()
        findings["summary"]["ai_status"] = "unavailable"
    except Exception:
        # a real failure (network, bad response) - keep the traceback for the log
        traceback.print_exc()
        findings["summary"]["ai_status"] = "unavailable"


def _log(job_id, lines):
    """Print a finished run to the server console.

    ONE write, not one per line: MAX_WORKERS jobs run at once and line-by-line
    prints from two of them would interlace into nonsense. The job id leads
    every block so a reader can tell the runs apart.

    Findings reach the browser as JSON, which leaves whoever is running the
    server with nothing but uvicorn's access log - the same run the CLI prints
    in full. This closes that gap.
    """
    header = f"--- job {job_id} ---"
    sys.stdout.write("\n".join([header] + list(lines)) + "\n")
    sys.stdout.flush()


def read_artifact(job_id, name):
    """Load one of the job's JSON artifacts, or None if it is not there."""
    path = os.path.join(job_dir(job_id), name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
