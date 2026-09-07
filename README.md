# BMS Drawing Verifier

Checks an EPLAN schematic against the BMS points schedule and reports every
discrepancy as a card you can walk through.

The analysis reads EPLAN's binary project database directly (`Function.eod`,
`Page.eod`) rather than exporting from EPLAN. What it can and cannot prove — and
why it refuses to guess — is documented at the top of
[`core/eplan_verify.py`](core/eplan_verify.py); read that before changing
anything in it.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

# create the first login (prompts for a password; never stored in the clear)
.venv/Scripts/python -m api.auth add admin --name "Your Name" --role admin

.venv/Scripts/python -m uvicorn api.app:app --reload
# then open http://127.0.0.1:8000
```

In the browser: sign in, drop in the Excel schedule, point step 2 at the
project's `.edb` **folder** — the page keeps only `Function.eod` and `Page.eod`
and uploads just those two — optionally add a known-good reference drawing and
the PDF, then **Run verification**.

## Layout

| Path | What it is |
|---|---|
| `core/eplan_verify.py` | The analysis. Binary parsing, active-page scoping, multiplicity verification, Phase D hints, Phase E terminals. `run_verification()` is the single entry point. |
| `core/schedule.py` | Excel workbook → `points_tags.csv` (the expected side). Reads font colour as well as text. |
| `core/findings.py` | Report → one flat, uniform list of findings, for the web UI and the console alike. |
| `core/render.py` | Findings → the console log lines. Shared by `cli.py` and the job runner so both print the same run. |
| `api/` | FastAPI app, CSV-backed login, background job runner. Each finished job also prints its full run to the server console. |
| `web/` | The front end. Plain HTML/CSS/ES modules — no build step, no Node. |
| `cli.py` | Same pipeline from the command line. |
| `tests/test_fixtures.py` | Regression suite over the seeded fixtures in `data/`. |

## Command line

```bash
.venv/Scripts/python cli.py \
  --source    "data/DDC 3 - FAULTS/DDC 3.edb" \
  --schedule  "data/DDC 3 - FAULTS/points_tags.csv" \
  --reference "data/DDC 3 - CORRECT/DDC 3.edb"
```

`--excel FILE [--password P] [--sheet NAME]` builds the schedule CSV first.
Exit status is 0 when CERTIFIED, 1 when UNRESOLVED, 2 on a usage error.

It prints the run counters, then the findings queue itself — the same list the
web UI shows, worst first, including the `likely_pair` hints that tie a MISSING
point to the EXTRA one that is really the same point misspelled. The queue is
also written to `findings.json` next to the other artifacts, so a CLI run and a
web run leave the same files behind.

## Certified vs advisory

The distinction the whole tool is built around, and the one thing not to blur:

- **Certified** findings are structural facts about the EPLAN database — a point
  the schedule asks for that is not in the drawing, a terminal numbered
  differently from the reference. These decide PASS/FAIL.
- **Advisory** findings are string inferences — chiefly the Phase D check that
  reads an I/O family off a page *name* (`16UIO-1-UP` → AI/AO). They are
  reported, badged, dashed, sorted last, and **never** affect the verdict.

`core/findings.py` enforces it (every uncertified finding is severity
`ADVISORY`, and `failure_count` counts certified findings only), the CSS
enforces it visually, and `tests/test_fixtures.py` asserts it.

The `likely_pair` hint on a card — "this MISSING and this EXTRA look like one
mistyped point" — is a `difflib` similarity, so it is labelled the same way and
only annotates; it never merges or reclassifies either finding.

## Testing

```bash
.venv/Scripts/python tests/test_fixtures.py      # or: python -m pytest tests -q
```

Each `data/DDC 3 - *` folder is the same project with one class of fault seeded
in, so the expected numbers are known. Any change that moves one of them has
changed behaviour.

## Notes and limits

- **`.zw1` backups need 7-Zip installed.** Without it there is a `py7zr`
  fallback, but on `DDC 3 - WRONG TR` it raises a CRC error on `Page.eod`, and
  whether that is a py7zr bug or a damaged archive is not decidable without a
  reference archiver. The web UI sidesteps this by uploading the two `.eod`
  members directly. `DDC 3 - WRONG TR` therefore skips in the test suite — its
  `.edb` ships without those members.
- **The terminal ("TR") check needs a reference drawing.** Without one it is
  reported as *skipped*, never as a pass. Comparing a drawing against itself is
  refused outright.
- **Login is for a small internal team.** Passwords are PBKDF2-SHA256 and
  compared in constant time, but there is no rate limiting, lockout or MFA. Put
  it behind a VPN or an authenticating proxy before exposing it.
- **Jobs run two at a time** (`api/jobs.py: MAX_WORKERS`). `Function.eod` is
  read wholly into memory, so parallelism costs RAM, not CPU.
- Job state is in memory; restarting the server forgets the job list but keeps
  every artifact under `jobs/<id>/`.
- The channel-allocation-order check (does EPLAN allocate DI → AI → AO → DO?)
  is still only *generated*, not verified — it needs certified module/channel
  typing on the EPLAN side. See `EXPECTED_PRIORITY_ORDER_GENERATION` in
  `core/eplan_verify.py`.
