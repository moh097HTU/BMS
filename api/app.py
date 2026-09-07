"""
HTTP API + static host for the BMS drawing verifier.

Run it:
    .venv/Scripts/python -m uvicorn api.app:app --reload
    then open http://127.0.0.1:8000

Endpoints
    POST /api/login          username + password -> a session cookie
    POST /api/logout
    GET  /api/me
    POST /api/excel/sheets   list a workbook's sheets (before choosing one)
    POST /api/jobs           the uploads -> a job id
    GET  /api/jobs           this user's recent jobs
    GET  /api/jobs/{id}      job state (poll this while it runs)
    GET  /api/jobs/{id}/findings   the card queue
    GET  /api/jobs/{id}/report     the full verification report
    GET  /api/jobs/{id}/pdf        the uploaded drawing PDF, if any

Sessions are signed, server-side-random tokens held in memory: restarting the
server logs everyone out, which is the right trade for a tool of this size.
"""

import os
import secrets
import shutil
import sys
import time

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api import jobs as jobs_mod
from api.auth import authenticate
from core.schedule import ScheduleError, extract, list_sheets, write_csv

# The job runner prints each finished run to this console (api/jobs.py::_log),
# and a point name comes from the customer's schedule - on this project, one
# that is not all Latin-1. A Windows console is cp1252 and would raise
# UnicodeEncodeError mid-run, so widen stdout once, here, at the entry point.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
SESSION_COOKIE = "bms_session"
SESSION_TTL = 12 * 3600
# Uploads are whole EPLAN members; Function.eod runs to tens of MB.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

app = FastAPI(title="BMS Drawing Verifier", docs_url=None, redoc_url=None)

_sessions = {}


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def _new_session(user):
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"user": user, "expires": time.time() + SESSION_TTL}
    return token


def current_user(request: Request):
    """Dependency: the logged-in user, or 401."""
    token = request.cookies.get(SESSION_COOKIE)
    session = _sessions.get(token or "")
    if not session or session["expires"] < time.time():
        _sessions.pop(token or "", None)
        raise HTTPException(status_code=401, detail="Not signed in")
    return session["user"]


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

@app.post("/api/login")
async def login(username: str = Form(...), password: str = Form(...)):
    user = authenticate(username, password)
    if user is None:
        # One message for both failure modes - never reveal which was wrong.
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    token = _new_session(user["username"])
    response = JSONResponse({"username": user["username"],
                             "display_name": user["display_name"] or user["username"],
                             "role": user["role"]})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_TTL)
    return response


@app.post("/api/logout")
async def logout(request: Request):
    _sessions.pop(request.cookies.get(SESSION_COOKIE) or "", None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/me")
async def me(user: str = Depends(current_user)):
    return {"username": user}


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------

async def _save(upload: UploadFile, path: str):
    """Stream an upload to disk, refusing anything over the size cap."""
    total = 0
    with open(path, "wb") as fh:
        while chunk := await upload.read(1 << 20):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                fh.close()
                os.remove(path)
                raise HTTPException(
                    status_code=413,
                    detail=f"{upload.filename} is larger than the "
                           f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
            fh.write(chunk)
    return total


@app.post("/api/excel/sheets")
async def excel_sheets(excel: UploadFile = File(...),
                       password: str = Form(""),
                       user: str = Depends(current_user)):
    """List a workbook's sheets so the user can pick the right one up front."""
    tmp = os.path.join(jobs_mod.JOBS_ROOT, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    path = os.path.join(tmp, f"{secrets.token_hex(8)}.xlsx")
    try:
        await _save(excel, path)
        return {"sheets": list_sheets(path, password or None)}
    except ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if os.path.exists(path):
            os.remove(path)


@app.post("/api/jobs")
async def create_job(
    excel: UploadFile = File(None),
    schedule_csv: UploadFile = File(None),
    function_eod: UploadFile = File(...),
    page_eod: UploadFile = File(...),
    ref_function_eod: UploadFile = File(None),
    ref_page_eod: UploadFile = File(None),
    pdf: UploadFile = File(None),
    excel_password: str = Form(""),
    sheet: str = Form("Sheet1"),
    label: str = Form(""),
    user: str = Depends(current_user),
):
    """
    Start a verification.

    The drawing arrives as its two EPLAN members rather than a whole .edb
    folder or a .zw1: those two files are all the pipeline ever reads (see
    core.eplan_verify.resolve_source), so the browser sends 2 files instead of
    several hundred, and the server needs no archiver installed.

    The schedule arrives either as the Excel workbook (parsed here) or as an
    already-built points_tags.csv.
    """
    if excel is None and schedule_csv is None:
        raise HTTPException(status_code=400,
                            detail="Provide either an Excel schedule or a points_tags.csv")

    job_id = jobs_mod.create_job(user)
    root = jobs_mod.job_dir(job_id)
    source_dir = os.path.join(root, "source")
    ref_dir = os.path.join(root, "reference")

    try:
        await _save(function_eod, os.path.join(source_dir, "Function.eod"))
        await _save(page_eod, os.path.join(source_dir, "Page.eod"))

        reference_source = None
        if ref_function_eod is not None and ref_page_eod is not None:
            await _save(ref_function_eod, os.path.join(ref_dir, "Function.eod"))
            await _save(ref_page_eod, os.path.join(ref_dir, "Page.eod"))
            reference_source = ref_dir

        if pdf is not None:
            await _save(pdf, os.path.join(root, "upload", "drawing.pdf"))

        points_csv = os.path.join(root, "points_tags.csv")
        if schedule_csv is not None:
            await _save(schedule_csv, points_csv)
        else:
            xlsx = os.path.join(root, "upload", excel.filename or "schedule.xlsx")
            await _save(excel, xlsx)
            try:
                rows = extract(xlsx, excel_password or None, sheet)
            except ScheduleError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            if not rows:
                raise HTTPException(
                    status_code=400,
                    detail=f'No physical points found on sheet "{sheet}". '
                           "Check the sheet name and that the DI/DO/AI/AO "
                           "columns are filled in.")
            write_csv(rows, points_csv)
    except HTTPException:
        shutil.rmtree(root, ignore_errors=True)
        raise

    jobs_mod.start(job_id, source_dir, points_csv,
                   reference_source=reference_source,
                   label=label or (excel.filename if excel else None))
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# job state and results
# ---------------------------------------------------------------------------

def _owned_job(job_id, user):
    job = jobs_mod.get_job(job_id)
    if job is None or job["owner"] != user:
        raise HTTPException(status_code=404, detail="No such job")
    return job


@app.get("/api/jobs")
async def my_jobs(user: str = Depends(current_user)):
    return {"jobs": jobs_mod.list_jobs(owner=user)}


@app.get("/api/jobs/{job_id}")
async def job_state(job_id: str, user: str = Depends(current_user)):
    return _owned_job(job_id, user)


@app.get("/api/jobs/{job_id}/findings")
async def job_findings(job_id: str, user: str = Depends(current_user)):
    job = _owned_job(job_id, user)
    data = jobs_mod.read_artifact(job_id, "findings.json")
    if data is None:
        raise HTTPException(status_code=409,
                            detail=f"Job is {job['state']}, not finished")
    report = jobs_mod.read_artifact(job_id, "verification_report.json") or {}
    data["source"] = report.get("source")
    data["scope"] = report.get("page_scoping")
    data["has_pdf"] = os.path.exists(
        os.path.join(jobs_mod.job_dir(job_id), "upload", "drawing.pdf"))
    return data


@app.get("/api/jobs/{job_id}/report")
async def job_report(job_id: str, user: str = Depends(current_user)):
    _owned_job(job_id, user)
    data = jobs_mod.read_artifact(job_id, "verification_report.json")
    if data is None:
        raise HTTPException(status_code=409, detail="No report yet")
    return data


@app.get("/api/jobs/{job_id}/pdf")
async def job_pdf(job_id: str, user: str = Depends(current_user)):
    _owned_job(job_id, user)
    path = os.path.join(jobs_mod.job_dir(job_id), "upload", "drawing.pdf")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No PDF was uploaded")
    return FileResponse(path, media_type="application/pdf",
                        filename="drawing.pdf")


# The single-page front end. Mounted last so /api/* always wins.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
