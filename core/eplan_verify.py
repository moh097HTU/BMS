"""
EPLAN -> BMS point extractor for the IRQAH UNIVERSITY project.

Pipeline: read Function.eod + Page.eod from the SOURCE (an .edb working folder
OR a .zw1 backup - see resolve_source), fingerprint exactly what was read, then
structurally extract points and VERIFY against points_tags.csv by MULTIPLICITY.
Anything it cannot prove structurally is reported, not silently dropped.

SOURCE matters: the loose .edb is the project EPLAN edits (where seeded QA
changes live); a same-named .zw1 is a clean backup and can differ. Point SOURCE
at whatever you actually want to verify - the run records its sha256 so the
snapshot is never ambiguous.

Two-part identity (see extract_points):
  structural_identity - WHICH EPLAN Function object: ddc, function object id,
    owning schematic page (id + name) via the CERTIFIED Function->Page reference
    (token f1eb 04 00 <uint32 page id>; f2eb is a verified cross-check), and the
    raw channel. This is what tells instances apart, so two objects that share
    schedule text but sit on different pages/channels are NOT duplicates.
  schedule_identity   - WHAT scheduled point it represents: equipment type/tag,
    point tag, Full Combined.
  derived_hints.module_name is inferred from the page name (e.g. 16DI-1-UP ->
    16DI-1) and is explicitly UNCERTIFIED - never used for a module PASS/FAIL.
    Certifying the real module device object is a later phase (Connection.eod /
    Instance.eod).

Active-page scoping (see build_page_map / filter_by_active_page): a deleted
  page (e.g. a removed 16DI-3/16DI-4 module) lingers in the database and its
  orphaned Function records still carry the DDC tag, so a DDC-scoped extraction
  would wrongly count points EPLAN no longer shows. Each Page.eod slot is flagged
  active (byte 1129 == 0x0a) or deleted (0x00) - validated 18/18 against DDC 3's
  eView page tree - and only points on active pages are kept. This certified,
  structural filter REPLACED an earlier "-N instance-number" naming heuristic
  (those records all turned out to live on deleted pages). If a scoped DDC has
  records but zero active pages, main's safety net keeps everything and flags it
  rather than silently dropping the lot.

Verification is by count, not text equality: for each Full Combined, expected
(schedule) vs found (distinct EPLAN Function objects). found==expected is a
MATCH even at count 2; found>expected is EXTRA_INSTANCE, found<expected MISSING,
each reported WITH object id / page / channel.

I/O type classification (Excel-authoritative, see classify_io_type):
  The schedule's DI/DO/AI/AO flag columns are the ONLY source of a point's I/O
  type - NEVER inferred from the point's name/description ("Status" -> DI,
  "Command" -> DO, etc. is explicitly forbidden; the schedule can and does
  assign a type a name would mislead you on). Exactly one non-zero flag -> that
  type; all zero -> not a physical point; more than one non-zero ->
  "INVALID_EXCEL_TYPE" (the schedule is inconsistent - reported, never guessed).
  The QTY column and Total_DI/DO/AI/AO columns are a separate validation total
  (QTY * flag == Total), never the type source.

EXPECTED_PRIORITY_ORDER_GENERATION (see build_expected_points): after every
  point has its Excel-defined type, the expected list is ORDERED by the
  module-allocation priority DI -> AI -> AO -> DO and written to
  expected_points.json. This is GENERATION of the expected order ONLY - it is
  NOT verification. The tool does NOT yet check whether the EPLAN drawing
  actually ALLOCATES its channels in that order; doing so requires certified
  module/channel typing on the EPLAN side (a future phase, alongside the
  Connection.eod/Instance.eod module certification). Priority never influences
  the type - classification and ordering are strictly separate operations.

Phase D (UNCERTIFIED diagnostic, see channel_type_hint_diagnostics): compares
each matched point's certified expected_io_type against the physical I/O family
suggested by the EPLAN page name it is actually wired to (e.g. '16DI-1-UP' hints
DI, '16UIO-1-UP' hints AI or AO). This is a STRING INFERENCE - exactly like
derived_hints.module_name - so it is advisory only and NEVER gates CERTIFIED;
findings live in report["uncertified_diagnostics"].

How a point is recognised (properties, not positional text):
  Every EPLAN Function record is a zlib stream inside Function.eod. Inside a
  record, a physical/integration point carries up to three EPLAN
  UserSupplementaryField properties, each stored in EPLAN's multi-language
  string form  ``??_??@<text>;`` , in ascending token order:
      field 1 -> token 09 9d
      field 2 -> token 0a 9d
      field 3 -> token 0b 9d
  and the owning page/DDC name (token f6 ea)      e.g. "DDC 5 - RF-2".
  The record's object id (token 61 ea) gives each function a unique identity.

  CRITICAL: the token number is NOT a fixed column. The drafters filled these
  fields inconsistently, so 09 9d is sometimes the Equipment Tag and sometimes
  the Equipment Type, and tag-less integration points (e.g. "Lighting Control
  System") have only two fields where the second is the Point Tag. The mapping
  that reproduces the schedule's "Full Combined" column for every DDC-3 point,
  verified against points_tags.csv, is:
      * Point Tag      = the LAST supplementary field present.
      * Equipment Tag  = the earlier field wrapped in parentheses "(...)",
                         or empty when no earlier field is parenthesised.
      * Equipment Type = the remaining earlier field.
      * Full Combined  = "Type Tag PointTag" (space-joined, tag omitted if empty)
  This is the canonical identity used for verification against the CSV.

This module is pure analysis plus ONE orchestration entry point,
run_verification(source, points_csv, reference_source, out_dir, progress) - it
takes explicit paths, returns the report, and never prints. Drive it from the
command line with  python cli.py --help  , or from the web API in api/.
"""

import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import zlib
from collections import Counter, defaultdict

# ---------------------------------------------------------------------------
# Source handling: read the SOURCE the caller chose (.edb folder or .zw1 backup)
# and always fingerprint it. An .edb folder and a same-named .zw1 can hold
# DIFFERENT bytes - the .edb is the working/edited project (seeded QA changes
# live there), the .zw1 is a clean backup - so the sha256 in the report makes
# the exact snapshot unambiguous.
# ---------------------------------------------------------------------------

def _sha256_size(path):
    """Return (sha256_hex, size_bytes) of a file, streamed."""
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


# A .zw1 is a 7-Zip archive. We deliberately shell out to a real archiver
# (7-Zip, else WinRAR) rather than decode the 7-Zip/LZMA container ourselves:
# archive extraction is not part of the EPLAN reverse-engineering task.
_ARCHIVERS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    r"C:\Program Files\WinRAR\WinRAR.exe",
    r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
]


def _find_archiver():
    for p in _ARCHIVERS:
        if os.path.exists(p):
            return p
    return None


def resolve_source(source, dest_dir):
    """
    Return {member: path} for the EPLAN members we parse, from a source that is
    either an .edb folder (the working/edited project the .elk points to) or a
    .zw1 backup archive.

    IMPORTANT: an .edb folder and a same-named .zw1 backup can differ - the .edb
    is what EPLAN edits (and where seeded QA changes live), the .zw1 is a clean
    backup. Point SOURCE at whatever you actually want to verify; the run always
    fingerprints what it read so the snapshot is unambiguous.
    """
    members = ["Function.eod", "Page.eod"]
    if os.path.isdir(source):
        out = {}
        for m in members:
            p = os.path.join(source, m)
            if not os.path.exists(p):
                raise FileNotFoundError(f"{m!r} not found in .edb folder {source!r}")
            out[m] = p
        return out
    if source.lower().endswith(".zw1"):
        return extract_from_zw1(source, members, dest_dir)
    raise ValueError(f"source must be an .edb folder or a .zw1 file: {source!r}")


def extract_from_zw1(zw1_path, members, dest_dir):
    """
    Extract one or more members (e.g. 'Function.eod', 'Page.eod') from an EPLAN
    .zw1 backup into dest_dir using an external archiver. Returns {member: path}.
    Reads the archive fresh every run so parsed data always matches the packed
    snapshot. Raises if no archiver is available or a member does not appear -
    the caller should treat that as UNRESOLVED, never fall back to a loose .edb.
    """
    if isinstance(members, str):
        members = [members]
    exe = _find_archiver()
    if exe is None:
        # No installed archiver. py7zr is a pure-Python 7-Zip reader; using it
        # is still "let a real archiver do it", just an in-process one, so the
        # decision above (never hand-decode the container) stands. It matters
        # most on a server, where installing 7-Zip is not always an option.
        return _extract_from_zw1_py7zr(zw1_path, members, dest_dir)
    if os.path.basename(exe).lower().startswith("7z"):
        cmd = [exe, "x", "-y", f"-o{dest_dir}", zw1_path, *members]
    else:  # WinRAR:  x <archive> <files...> <dest\>
        cmd = [exe, "x", "-y", "-ibck", zw1_path, *members, dest_dir + os.sep]
    subprocess.run(cmd, check=True)
    out = {}
    for m in members:
        path = os.path.join(dest_dir, m)
        # WinRAR may return just before the file handle is flushed
        for _ in range(60):
            if os.path.exists(path):
                break
            time.sleep(0.5)
        if not os.path.exists(path):
            raise FileNotFoundError(f"{m!r} was not extracted from {zw1_path!r}")
        out[m] = path
    return out


def _extract_from_zw1_py7zr(zw1_path, members, dest_dir):
    """
    Fallback for machines with no 7-Zip/WinRAR installed: extract the members
    with the py7zr library. Same contract as extract_from_zw1 - raises if the
    library is absent or a member does not appear, never falls back to a loose
    .edb. Matching is case-insensitive on the base name, since the members sit
    inside a project folder within the archive.

    CAVEAT: this path is less proven than a real archiver. On the project's
    'DDC 3 - WRONG TR' backup, py7zr 1.1.3 raises CrcError on Page.eod where an
    installed 7-Zip is the reference implementation; whether that is a py7zr
    decode bug or a genuinely damaged archive is not decidable from here. The
    error is allowed to propagate - a CRC failure must never be papered over -
    so INSTALL 7-ZIP if you rely on .zw1 sources. Uploading the two .eod members
    directly (what the web UI does) avoids the question entirely.
    """
    try:
        import py7zr
    except ImportError as exc:
        raise RuntimeError(
            "No 7-Zip or WinRAR found to read the .zw1, and py7zr is not "
            "installed. Install 7-Zip (7z.exe), or  pip install py7zr .") from exc

    wanted = {m.lower(): m for m in members}
    # Hand py7zr an OPEN FILE rather than a path: that makes it take its serial
    # extraction path. Its parallel path crashes on targeted extraction of this
    # archive layout (folders with no file list -> TypeError in py7zr 1.1.3).
    with open(zw1_path, "rb") as fh:
        with py7zr.SevenZipFile(fh, mode="r") as archive:
            targets = [name for name in archive.getnames()
                       if os.path.basename(name).lower() in wanted]
            if not targets:
                raise FileNotFoundError(
                    f"none of {members!r} are present in {zw1_path!r}")
            archive.extract(path=dest_dir, targets=targets)

    out = {}
    for name in targets:
        member = wanted[os.path.basename(name).lower()]
        src = os.path.join(dest_dir, os.path.normpath(name))
        dst = os.path.join(dest_dir, member)
        if os.path.exists(src):
            if os.path.abspath(src) != os.path.abspath(dst):
                os.replace(src, dst)
            out[member] = dst
    missing = [m for m in members if m not in out]
    if missing:
        raise FileNotFoundError(
            f"{missing!r} were not extracted from {zw1_path!r}")
    return out


# ---------------------------------------------------------------------------
# EPLAN Function.eod low-level parsing
# ---------------------------------------------------------------------------

# zlib stream marker used for every serialized Function record (best compression)
_ZLIB_MARKER = b"\x78\xda"

# EPLAN multi-language string: <token:2><00 00>??_??@<text>; . The token is the
# property id; re.DOTALL is required because some tokens contain 0x0a/0x0d.
_MULTILANG = re.compile(rb"(..)\x00\x00\x3f\x3f\x5f\x3f\x3f\x40([^;]*)\x3b", re.DOTALL)

# Property tokens (little-endian, as they appear in the file).
# The three UserSupplementaryField slots, in ascending order. Their SEMANTIC
# meaning (tag / type / point tag) is resolved per-record by _map_fields below,
# NOT by slot number - see module docstring.
TOK_SUPP_FIELDS = [b"\x09\x9d", b"\x0a\x9d", b"\x0b\x9d"]
TOK_OBJECT_ID = b"\x61\xea"   # function object handle
TOK_DDC_NAME = b"\xf6\xea"    # owning page / DDC structure name
# Function -> owning schematic page. Stored as <tok> 04 00 <uint32 LE page id>.
# f1eb and f2eb are two copies that (verified across all DDC-3 records) always
# agree; f1eb is authoritative and f2eb is a cross-check.
TOK_PAGE_REF = b"\xf1\xeb"
TOK_PAGE_REF2 = b"\xf2\xeb"

# A point's connection designation ("DI-2", "IO-3", or a bare terminal number),
# stored as the 2nd printable run in the record (after the leading "<...>" code).
_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{2,}")


def _read_ref(raw, tok):
    """Read an object-id reference stored as <tok> 04 00 <uint32 LE>."""
    k = raw.find(tok + b"\x04\x00")
    return int.from_bytes(raw[k + 4:k + 8], "little") if k >= 0 else None


def _read_channel(raw):
    """Best-effort connection designation (the 2nd printable run). Raw, not
    certified: format varies ('DI-2', 'IO-3', bare terminal numbers)."""
    runs = _PRINTABLE_RUN.findall(raw)
    return runs[1].decode("latin1", "replace") if len(runs) >= 2 else None


# ---------------------------------------------------------------------------
# Page.eod: fixed-slot table (file size == N * 1134). Each slot's own page id is
# stored as 61 ea 04 00 <uint32 LE>; the page name is its first ??_??@..; string.
#
# ACTIVE vs DELETED: slot byte at offset 1129 is 0x0a for pages that are live in
# the project tree and 0x00 for deleted pages (which linger in the database with
# their old page number and still get referenced by orphaned Function records).
# Validated 18/18 against DDC 3's eView page tree: pages 1-11,17,18,19 -> 0x0a,
# the deleted 16DI-3/4/8DI-1 modules (pages 12-16) -> 0x00. NOTE: in this
# DDC-3-focused .edb only DDC 3's pages carry 0x0a, so the flag effectively marks
# "live page of the active structure"; callers must guard against a scoped DDC
# whose pages are all 0x00 (see main's safety net) rather than trust it blindly.
# ---------------------------------------------------------------------------
_PAGE_SLOT_SIZE = 1134
_PAGE_NAME = re.compile(rb"\x3f\x3f\x5f\x3f\x3f\x40([^;]*)\x3b")
_PAGE_ACTIVE_OFFSET = 1129
_PAGE_DDC_RE = re.compile(rb"DDC \d[0-9A-Za-z \-\.]{2,40}\x00")

# The active/deleted page marker was validated ONCE, offline, against DDC 3's
# eView page tree. CERTIFIED therefore means "certified under this documented
# extraction model", NOT "the byte's semantics are proven for every EPLAN
# database ever made". This provenance is emitted as report["scope_validation"].
PAGE_CURRENT_MARKER = "0x0A_AT_SLOT_1129"
PAGE_MARKER_VALIDATION_SCOPE = "IRQAH_DDC3"
PAGE_MARKER_VALIDATED_PAGES = 18
PAGE_MARKER_VALIDATION_MISMATCHES = 0


def build_page_map(page_eod_path):
    """
    Return (page_map, stats) where
        page_map = {page_object_id: {"name", "active", "number"}}.
    active: True if slot byte 1129 == 0x0a (live page), False if deleted.
    number: best-effort page number (bare integer after the last DDC-name field).
    """
    data = open(page_eod_path, "rb").read()
    if len(data) % _PAGE_SLOT_SIZE != 0:
        return {}, {"slot_size": _PAGE_SLOT_SIZE, "slots": None,
                    "note": f"size {len(data)} not a multiple of {_PAGE_SLOT_SIZE}"}
    slots = len(data) // _PAGE_SLOT_SIZE
    page_map = {}
    active = 0
    for i in range(slots):
        slot = data[i * _PAGE_SLOT_SIZE:(i + 1) * _PAGE_SLOT_SIZE]
        k = slot.find(b"\x61\xea\x04\x00")
        if k < 0:
            continue  # empty / dead / non-page slot
        pid = int.from_bytes(slot[k + 4:k + 8], "little")
        m = _PAGE_NAME.search(slot)
        name = m.group(1).decode("latin1", "replace") if m else None
        is_active = slot[_PAGE_ACTIVE_OFFSET] == 0x0a
        number = None
        dm = list(_PAGE_DDC_RE.finditer(slot))
        if dm:
            bn = re.search(rb"(?<![0-9])([0-9]{1,4})(?![0-9])", slot[dm[-1].end():])
            if bn:
                number = int(bn.group(1))
        page_map[pid] = {"name": name, "active": is_active, "number": number}
        active += is_active
    return page_map, {"slot_size": _PAGE_SLOT_SIZE, "slots": slots,
                      "pages_with_id": len(page_map),
                      "active_pages": active, "deleted_pages": len(page_map) - active,
                      "empty_or_dead_slots": slots - len(page_map)}


# A schematic page name like '16DI-1-UP' embeds the module '16DI-1'. This is a
# STRING INFERENCE only - never used for any module-level PASS/FAIL decision.
_PAGE_SUFFIX = re.compile(r"[-_ ]?(UP|DOWN|DN)$", re.I)


def _module_hint_from_page(page_name):
    if not page_name:
        return None
    return _PAGE_SUFFIX.sub("", page_name).strip(" -_") or page_name


def iter_records(eod_path):
    """Yield (record_offset, decompressed_bytes) for every Function record."""
    with open(eod_path, "rb") as fh:
        data = fh.read()
    pos = 0
    while True:
        off = data.find(_ZLIB_MARKER, pos)
        if off < 0:
            break
        pos = off + 2
        try:
            # a bounded window is plenty for one record and avoids scanning 71MB
            raw = zlib.decompressobj().decompress(data[off:off + 80000])
        except zlib.error:
            continue
        if len(raw) >= 10:
            yield off, raw


def _read_multilang_fields(raw):
    """Return {token: text} for every multi-language property in the record."""
    out = {}
    for m in _MULTILANG.finditer(raw):
        out.setdefault(m.group(1), m.group(2).decode("latin1"))
    return out


def _map_fields(fields):
    """
    Resolve the supplementary fields of one record into
    (equipment_type, equipment_tag, point_tag, full_combined) using the
    validated rule (see docstring). Returns None if the record does not carry
    at least two supplementary fields (i.e. it is not a point).
    """
    present = [fields[t] for t in TOK_SUPP_FIELDS if t in fields]
    if len(present) < 2:
        return None  # not a point - fail closed
    point_tag = present[-1]
    earlier = present[:-1]
    if len(earlier) == 1:
        # one earlier field: a parenthesised value is a tag, otherwise a type
        if "(" in earlier[0]:
            equip_type, equip_tag = "", earlier[0]
        else:
            equip_type, equip_tag = earlier[0], ""
    else:
        # two earlier fields. Normal order is [Type, Tag] (e.g. "SMDB/ESMDB
        # Panels" + "(MB-SS-SMDB)", or "Fuel Transfer Pumps" + "2 Acting").
        # Some equipment stores them reversed as [Tag, Type] where the tag is
        # the parenthesised field (e.g. VRF AHU "( AHU-HC-08)" + "VRF ...").
        # Swap ONLY in that reversed case so neither field is ever dropped.
        f1, f2 = earlier[0], earlier[1]
        if "(" in f1 and "(" not in f2:
            equip_type, equip_tag = f2, f1
        else:
            equip_type, equip_tag = f1, f2
    full_combined = " ".join(x for x in (equip_type, equip_tag, point_tag) if x)
    return equip_type, equip_tag, point_tag, full_combined


def _read_object_id(raw):
    j = raw.find(TOK_OBJECT_ID + b"\x11\x00")
    if j < 0:
        return None
    return int.from_bytes(raw[j + 4:j + 8], "little")


def _read_ddc_name(raw):
    k = raw.find(TOK_DDC_NAME)
    if k < 0:
        return None
    m = re.search(rb"(DDC[ ].*?)\x00", raw[k:k + 160], re.DOTALL)
    return m.group(1).decode("latin1") if m else None


def extract_points(eod_path, page_map=None, only_ddc_nums=None):
    """
    Structural extraction. A record is emitted as a point only if it carries at
    least two supplementary fields (see _map_fields); records lacking that proof
    are ignored - never guessed into points.

    Each point separates two concepts:
      structural_identity - WHICH EPLAN Function object is this (ddc, function
        object id, owning page id+name via the certified f1eb reference, raw
        channel). This is the identity used to tell instances apart.
      schedule_identity   - WHAT scheduled point does it represent (equipment
        type/tag, point tag, Full Combined).
    derived_hints.module_name is inferred from the page name ONLY and is marked
    uncertified - it must never drive a module-level PASS/FAIL.

    page_map: {page_object_id: {"name","active","number"}} from build_page_map.
    only_ddc_nums: optional set of DDC numbers (as strings, e.g. {"3"}) to keep.
    """
    page_map = page_map or {}
    points = []
    for off, raw in iter_records(eod_path):
        mapped = _map_fields(_read_multilang_fields(raw))
        if mapped is None:
            continue  # not a point - fail closed
        equip_type, equip_tag, point_tag, full_combined = mapped
        ddc_raw = _read_ddc_name(raw)
        if only_ddc_nums is not None and _ddc_num(ddc_raw) not in only_ddc_nums:
            continue
        page_id = _read_ref(raw, TOK_PAGE_REF)
        page_id2 = _read_ref(raw, TOK_PAGE_REF2)
        pm = page_map.get(page_id) or {}
        page_name = pm.get("name")
        points.append({
            "structural_identity": {
                "ddc": ddc_raw,
                "function_object_id": _read_object_id(raw),
                "page_object_id": page_id,
                "page_name": page_name,
                "page_number": pm.get("number"),
                "page_active": pm.get("active"),
                "channel_raw": _read_channel(raw),
            },
            "derived_hints": {
                "module_name": _module_hint_from_page(page_name),
                "module_name_source": "PAGE_NAME_INFERENCE",
                "module_name_certified": False,
            },
            "schedule_identity": {
                "equipment_type": equip_type,
                "equipment_tag": equip_tag,
                "point_tag": point_tag,
                "full_combined": full_combined,
            },
            "source": {
                "file": "Function.eod",
                "record_offset": off,
                "page_ref_f1eb": page_id,
                "page_ref_f2eb": page_id2,
                "page_ref_consistent": page_id == page_id2,
                "page_resolved": page_name is not None,
            },
        })
    return points


# convenience accessors so downstream code doesn't reach into the nested dict
def _sid(p):
    return p["structural_identity"]


def _full(p):
    return p["schedule_identity"]["full_combined"]


# ---------------------------------------------------------------------------
# Normalisation (only for VERIFICATION - extraction keeps raw values)
# ---------------------------------------------------------------------------

# CSV Point Tag values that are calculated summaries, not real physical points.
SUMMARY_POINT_TAGS = {
    "spare physical points",
    "ddc total physical points",
    "ddc total integration devices / points",
}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _ddc_num(s):
    """Return the bare DDC number as a string, e.g. 'DDC 21  -Level 7' -> '21', else None."""
    m = re.search(r"ddc\s*0*(\d+)", (s or "").casefold())
    return m.group(1) if m else None


def _norm_ddc(s):
    """Collapse legacy name variants to the DDC number, e.g. 'DDC 21  -Level 7' -> 'ddc 21'."""
    n = _ddc_num(s)
    return f"ddc {n}" if n else _norm(s)


def _combined_key(ddc, full_combined):
    """Canonical identity: the DDC number plus the normalised 'Full Combined' text.
    This is the ground truth the schedule (points_tags.csv) is keyed on."""
    return (_norm_ddc(ddc), _norm(full_combined))


# ---------------------------------------------------------------------------
# Excel-authoritative I/O type classification (see project contract). The
# schedule's DI/DO/AI/AO columns are the ONLY source of a point's I/O type.
# NEVER infer type from the point description ("Status" -> DI, "Command" -> DO,
# etc.) - the schedule can and does assign types that a name would mislead you
# on (e.g. "Fire Damper Open/Close Control Signal" is DI, not DO).
# ---------------------------------------------------------------------------

IO_TYPE_ORDER = ("DI", "DO", "AI", "AO")
# Module-allocation priority: classification (above) and priority are separate
# steps - priority is applied AFTER a point already has its Excel-defined type,
# and never influences that type.
PRIORITY_ORDER = ("DI", "AI", "AO", "DO")
PRIORITY_RANK = {t: i for i, t in enumerate(PRIORITY_ORDER)}


def _csv_num(s):
    """Parse a CSV cell as a float; blank / 'None' / unparsable -> None."""
    if s is None:
        return None
    s = s.strip()
    if s == "" or s.lower() == "none":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def classify_io_type(di, do, ai, ao):
    """
    Read the DI/DO/AI/AO flag columns directly - never infer from text.
      exactly one non-zero   -> that type
      all zero / blank       -> None (not a physical I/O point)
      more than one non-zero -> "INVALID_EXCEL_TYPE" (the schedule itself is
        inconsistent; never auto-pick one - this must be reported and fixed
        at the source)
    """
    vals = (di, do, ai, ao)
    active = [t for t, v in zip(IO_TYPE_ORDER, vals) if (_csv_num(v) or 0.0) != 0.0]
    if len(active) == 1:
        return active[0]
    if len(active) == 0:
        return None
    return "INVALID_EXCEL_TYPE"


def priority_rank(io_type):
    return PRIORITY_RANK.get(io_type, len(PRIORITY_ORDER))  # unknown/invalid sorts last


def _classify_row(row):
    """
    Mutate a schedule CSV row in place with its Excel-authoritative
    classification: excel_type_flags, expected_io_type, priority_rank,
    quantity, and a QTY*flag == Total_* consistency check (totals_expected /
    totals_actual / totals_consistent). F..I (DI/DO/AI/AO) is the type source;
    J..M (Total_*) is a validation total, never the type source.
    """
    io_type = classify_io_type(row["DI"], row["DO"], row["AI"], row["AO"])
    qty = _csv_num(row["QTY"])
    row["excel_type_flags"] = {t: row[t] for t in IO_TYPE_ORDER}
    row["expected_io_type"] = io_type
    row["priority_rank"] = priority_rank(io_type)
    row["quantity"] = qty

    totals_expected, totals_actual, have_totals, consistent = {}, {}, False, True
    for t, total_col in zip(IO_TYPE_ORDER,
                            ("Total_DI", "Total_DO", "Total_AI", "Total_AO")):
        actual = _csv_num(row[total_col])
        totals_actual[t] = actual
        if qty is None or actual is None:
            totals_expected[t] = None
            continue
        have_totals = True
        expected = qty * (_csv_num(row[t]) or 0.0)
        totals_expected[t] = expected
        if abs(expected - actual) > 1e-9:
            consistent = False
    row["totals_expected"] = totals_expected
    row["totals_actual"] = totals_actual
    row["totals_consistent"] = consistent if have_totals else None


def build_expected_points(csv_real):
    """
    EXPECTED_PRIORITY_ORDER_GENERATION (NOT verification): produce the
    Excel-authoritative expected point list, ordered by DDC then by the
    module-allocation priority DI -> AI -> AO -> DO. This only GENERATES the
    expectation - it does not check whether EPLAN allocates channels in that
    order (that needs certified EPLAN-side module/channel typing; see module
    docstring). Classification and ordering are separate: every point already
    has its expected_io_type (from classify_io_type) before ordering here.
    """
    out = [{
        "ddc": r["DDC"],
        "device": r["Equipment Type"],
        "equipment_tag": r["Equipment Tag"],
        "point_tag": r["Point Tag"],
        "full_combined": r["Full Combined"],
        "quantity": r["quantity"],
        "excel_type_flags": r["excel_type_flags"],
        "expected_io_type": r["expected_io_type"],
        "priority_rank": r["priority_rank"],
    } for r in csv_real]
    out.sort(key=lambda p: (_norm_ddc(p["ddc"]), p["priority_rank"], p["point_tag"] or ""))
    return out


# ---------------------------------------------------------------------------
# Verification against points_tags.csv (loaded AFTER extraction)
# ---------------------------------------------------------------------------

REQUIRED_CSV_COLUMNS = {
    "DDC", "Equipment Type", "Equipment Tag", "Point Tag", "Full Combined",
    "QTY", "DI", "DO", "AI", "AO", "Total_DI", "Total_DO", "Total_AI", "Total_AO",
}


def load_csv_points(csv_path):
    """
    Load the schedule and classify every real (non-summary) row - see
    _classify_row. Fails loudly if the CSV predates the QTY/DI/DO/AI/AO/Total_*
    schema rather than silently guessing at columns (regenerate with
    parse/extract_points.py).
    """
    real, summary = [], []
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_CSV_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{csv_path!r} is missing required columns {sorted(missing)}. "
                "Regenerate it with parse/extract_points.py (needs the QTY + "
                "DI/DO/AI/AO + Total_DI..Total_AO schema).")
        for row in reader:
            if _norm(row["Point Tag"]) in SUMMARY_POINT_TAGS:
                summary.append(row)
                continue
            _classify_row(row)
            real.append(row)
    return real, summary


def _instance_brief(p):
    """Compact structural view of one drawing instance for discrepancy reports."""
    si = _sid(p)
    return {
        "function_object_id": si["function_object_id"],
        "page_name": si["page_name"],
        "channel_raw": si["channel_raw"],
    }


def verify(points, csv_real):
    """
    Compare EPLAN points against the schedule by MULTIPLICITY, not by text
    equality. For each scheduled 'Full Combined' identity:
        expected = how many the schedule asks for
        found    = how many DISTINCT EPLAN Function objects carry it
    found == expected -> MATCH (even if that count is 2: two distinct objects on
    two different channels are NOT a duplicate). found > expected -> EXTRA_INSTANCE,
    found < expected -> MISSING. Every mismatch is reported WITH the structural
    detail (object id / page / channel) so it can be judged, never silently
    collapsed. Also surfaces points whose owning page could not be resolved.
    """
    by_key = defaultdict(list)
    for p in points:
        by_key[_combined_key(_sid(p)["ddc"], _full(p))].append(p)

    excel_counts = Counter(_combined_key(r["DDC"], r["Full Combined"]) for r in csv_real)
    ex_disp = {_combined_key(r["DDC"], r["Full Combined"]): r["Full Combined"]
               for r in csv_real}
    draw_disp = {k: v[0]["schedule_identity"]["full_combined"] for k, v in by_key.items()}

    matched = missing = extra = 0
    discrepancies = []
    for key in set(by_key) | set(excel_counts):
        expected = excel_counts.get(key, 0)
        found_pts = by_key.get(key, [])
        found = len(found_pts)
        matched += min(expected, found)
        if found > expected:
            extra += found - expected
        elif expected > found:
            missing += expected - found
        if expected != found:
            discrepancies.append({
                "full_combined": ex_disp.get(key) or draw_disp.get(key),
                "kind": "EXTRA_INSTANCE" if found > expected else "MISSING",
                "expected": expected,
                "found": found,
                "instances": [_instance_brief(p) for p in found_pts],
            })

    # points whose owning page did not resolve - reported, never dropped
    unresolved = [_instance_brief(p) | {"full_combined": _full(p)}
                  for p in points if not p["source"]["page_resolved"]]
    inconsistent = [_instance_brief(p) | {"full_combined": _full(p)}
                    for p in points if not p["source"]["page_ref_consistent"]]

    return {
        "eplan_points": len(points),
        "excel_expected_total": sum(excel_counts.values()),
        "matched": matched,
        "missing_from_eplan": missing,
        "extra_instances_in_eplan": extra,
        "page_unresolved": len(unresolved),
        "page_ref_inconsistent": len(inconsistent),
        "discrepancies": sorted(
            discrepancies, key=lambda d: (d["kind"], d["full_combined"] or "")),
        "page_unresolved_list": unresolved,
        "page_ref_inconsistent_list": inconsistent,
    }


# One structural rule for the WHOLE verifier: a record counts only if it sits on
# an ACTIVE page. A deleted page (e.g. a removed 16DI-3/16DI-4 module) lingers in
# the database and its orphaned records still carry the DDC tag, so a DDC-scoped
# extraction wrongly pulls them in - they are NOT part of the drawing EPLAN
# shows. build_page_map flags each page active/deleted from the Page.eod slot
# (byte 1129). This certified, structural rule is applied identically to BOTH
# point records and terminal records, on BOTH target and reference. It fully
# replaced an earlier "-N instance-number" naming heuristic (deleted for good -
# every one of those records turned out to live on a deleted page anyway).

def _page_active(rec):
    """page_active flag for a point (nested under structural_identity) OR a
    terminal (flat) record - so one filter serves both."""
    si = rec.get("structural_identity")
    return (si or rec).get("page_active")


def filter_by_active_page(records):
    """
    Split records (points OR terminals) into (on_active, on_deleted) by their
    page_active flag. A record whose page did not resolve (page_active is None)
    is KEPT on the active side - we never silently drop what we cannot classify
    (points also surface it separately as page_unresolved). Returns
    (active, deleted).
    """
    active, deleted = [], []
    for r in records:
        (deleted if _page_active(r) is False else active).append(r)
    return active, deleted


def scope_diagnostics(points):
    """Surface the page-scope ambiguity that blocks full certification."""
    by_num = defaultdict(set)
    for p in points:
        d = _sid(p)["ddc"]
        if d:
            by_num[_norm_ddc(d)].add(d)
    variants = {k: sorted(v) for k, v in by_num.items() if len(v) > 1}
    return variants


# ---------------------------------------------------------------------------
# Phase D (UNCERTIFIED): does a point's Excel-defined I/O type match the
# physical I/O family of the module it is actually wired to in EPLAN?
#
# We have no certified module-device object (that needs Connection.eod /
# Instance.eod - a later phase). What we DO have is the schematic page name
# (certified via the f1eb Function->Page reference), and EPLAN's own drawing
# convention names each I/O-card page after its physical family, e.g.
# '16DI-1-UP', '8DO-1', '16UIO-1-UP' (Universal I/O - usable as AI or AO).
# Reading that family off the page name is a STRING INFERENCE, exactly like
# derived_hints.module_name, so this whole diagnostic is advisory: it NEVER
# gates CERTIFIED and is reported in its own uncertified_diagnostics section.
# ---------------------------------------------------------------------------

_PAGE_FAMILY = re.compile(r"^\d*([A-Za-z]+)")
_PAGE_FAMILY_TYPES = {
    "DI": {"DI"}, "DO": {"DO"}, "AI": {"AI"}, "AO": {"AO"},
    "DIO": {"DI", "DO"},
    "UIO": {"AI", "AO"}, "UI": {"AI"}, "UO": {"AO"},
}


def _channel_type_hint(page_name):
    """UNCERTIFIED: the set of I/O types a page's family name suggests, or
    None if the page name doesn't match a known family."""
    if not page_name:
        return None
    m = _PAGE_FAMILY.match(page_name)
    return _PAGE_FAMILY_TYPES.get(m.group(1).upper()) if m else None


def channel_type_hint_diagnostics(points, csv_real):
    """
    UNCERTIFIED diagnostic. For each EPLAN point matched to a schedule point,
    compare the schedule's CERTIFIED expected_io_type to the page-family HINT
    of the channel it is actually wired to. A mismatch suggests the point may
    be wired into the wrong module type - but since the hint is a string
    inference, this is advisory only.
    """
    expected_by_key = {
        _combined_key(r["DDC"], r["Full Combined"]): r["expected_io_type"]
        for r in csv_real if r["expected_io_type"] not in (None, "INVALID_EXCEL_TYPE")
    }
    findings = []
    for p in points:
        expected = expected_by_key.get(_combined_key(_sid(p)["ddc"], _full(p)))
        if expected is None:
            continue
        hint = _channel_type_hint(_sid(p)["page_name"])
        if hint is None:
            continue  # unknown page family - no hint available
        if expected not in hint:
            findings.append({
                "full_combined": _full(p),
                "expected_io_type": expected,
                "page_name": _sid(p)["page_name"],
                "page_family_hint": sorted(hint),
                "channel_raw": _sid(p)["channel_raw"],
                "function_object_id": _sid(p)["function_object_id"],
            })
    return sorted(findings, key=lambda f: f["full_combined"] or "")


# ---------------------------------------------------------------------------
# Phase E (CERTIFIED extraction, REFERENCE-based diagnostic): terminal-strip
# designations ("TR" numbers).
#
# A terminal block is a DIFFERENT EPLAN object class from a point - it carries
# NO UserSupplementaryFields (TOK_SUPP_FIELDS), so extract_points correctly
# never sees it as a point. Its own designation number
# (e.g. terminal "184" on a DIN rail) is stored, once per terminal record, as:
#     6b ea 3e 4e 00 00 <ascii digits> 00
# This anchor was proven by direct correlation: on the page where "WRONG TR"
# actually differs from a known-good drawing, this exact field is the one and
# only thing that changes (a run of ~15 consecutive terminal records each
# shifted down by 1). The record still carries the normal ddc name (f6ea),
# object id (61ea) and page reference (f1eb) used everywhere else, so terminal
# designations can be extracted with the same certainty as point identity.
#
# What this can and cannot prove:
#   - Extraction (object id, page, terminal number) is CERTIFIED - same anchor
#     class as everything else in this file.
#   - Whether a given terminal roster is "correct" is NOT decidable from one
#     drawing alone: many legitimate rails intentionally repeat or skip numbers
#     (73 of 547 rails in a known-clean drawing already have duplicates/gaps by
#     design - e.g. a rail where every terminal is wired twice). So there is NO
#     safe universal rule ("must be contiguous", "must be unique") to certify
#     against a single file.
#   - What IS decidable: whether the SAME terminal position carries a different
#     number than it does in a known-good REFERENCE drawing. Pairing the two
#     sides is done by _pick_terminal_join: object ids when both sides really
#     share them (two reads of the same .edb), otherwise (page_name, rail
#     position within the page). Object ids are NOT stable across project
#     COPIES - EPLAN re-issues every page and function id when a copy is saved,
#     so two copies of one project share zero ids and an id-only join compares
#     nothing at all. Either pairing catches a pure SWAP (position A 3->4,
#     position B 4->3), which a page-level multiset misses. The page multiset
#     (keyed by page name) is kept as a rollup / fallback. This is
#     reported as a distinct "terminal_diagnostics" section, separate from the
#     CERTIFIED point-verification (no reference needed) and the UNCERTIFIED
#     string-inference hints (Phase D); it needs a REFERENCE_SOURCE and is only
#     run when one is supplied.
# ---------------------------------------------------------------------------

_TERMINAL_ANCHOR = re.compile(rb"\x6b\xea\x3e\x4e\x00\x00([0-9]{1,6})\x00")


def extract_terminal_designations(eod_path, page_map=None, only_ddc_nums=None):
    """
    Structurally extract every terminal-strip designation number. Returns a
    list of {ddc, function_object_id, page_object_id, page_name, terminal_number}.
    A record with more than one anchor match emits one entry per match, all
    sharing that record's identity.
    """
    page_map = page_map or {}
    out = []
    for off, raw in iter_records(eod_path):
        matches = _TERMINAL_ANCHOR.findall(raw)
        if not matches:
            continue
        ddc_raw = _read_ddc_name(raw)
        if only_ddc_nums is not None and _ddc_num(ddc_raw) not in only_ddc_nums:
            continue
        page_id = _read_ref(raw, TOK_PAGE_REF)
        pm = page_map.get(page_id) or {}
        oid = _read_object_id(raw)
        for digits in matches:
            out.append({
                "ddc": ddc_raw,
                "function_object_id": oid,
                "page_object_id": page_id,
                "page_name": pm.get("name"),
                "page_active": pm.get("active"),
                "terminal_number": int(digits),
            })
    return out


def build_terminal_roster(designations):
    """
    {page_name: Counter(terminal_number -> count)} for one drawing. Keyed by
    page NAME, not page object id: object ids are re-issued per project copy
    and so are not comparable between two drawings (see _pick_terminal_join).
    """
    roster = defaultdict(Counter)
    for d in designations:
        roster[d["page_name"]][d["terminal_number"]] += 1
    return roster


def _terminal_by_object(designations):
    """{(page_object_id, function_object_id): sorted[terminal_number]}."""
    idx = defaultdict(list)
    for d in designations:
        idx[(d["page_object_id"], d["function_object_id"])].append(d["terminal_number"])
    return {k: sorted(v) for k, v in idx.items()}


def _terminal_by_page_slot(designations):
    """
    ({(page_name, slot): [terminal_number]}, {(page_name, slot): object_id}).

    `slot` is the RANK of a record's function_object_id within its own page
    (0-based). EPLAN issues those ids in rail order, so slot 0 is the first
    terminal on the strip, slot 1 the second, and so on - the same physical
    position in every copy of the project, whatever ids EPLAN re-issued. Used
    when the object-id join finds no overlap (see _pick_terminal_join).
    """
    by_page = defaultdict(list)
    for d in designations:
        by_page[d["page_name"]].append(d)
    idx, oids = {}, {}
    for page_name, recs in by_page.items():
        recs.sort(key=lambda r: (r["function_object_id"] is None,
                                 r["function_object_id"]))
        for slot, r in enumerate(recs):
            idx[(page_name, slot)] = [r["terminal_number"]]
            oids[(page_name, slot)] = r["function_object_id"]
    return idx, oids


def _pick_terminal_join(target_designations, reference_designations):
    """
    Choose how to pair a target terminal record with its reference counterpart.

    "object_id" - (page_object_id, function_object_id). Exact, and the right
    key when both sides are snapshots of the SAME .edb (ids untouched).

    "page_slot" - (page_name, rank of object id within the page). EPLAN
    RE-ISSUES every page and function object id when a project is copied and
    re-saved, so two copies of one project routinely share ZERO object ids
    (observed on this project: pages 12893.. vs 13454.., functions 693034.. vs
    724939..). Joining on ids there yields an EMPTY intersection - no terminal
    is ever compared and every real "WRONG TR" change is silently missed - so
    fall back to the page name, which is stable, and the rail position in it.

    Returns (join_key, target_index, reference_index, target_oids, reference_oids).
    """
    t_obj = _terminal_by_object(target_designations)
    r_obj = _terminal_by_object(reference_designations)
    if set(t_obj) & set(r_obj):
        return ("object_id", t_obj, r_obj,
                {k: k[1] for k in t_obj}, {k: k[1] for k in r_obj})
    t_slot, t_oids = _terminal_by_page_slot(target_designations)
    r_slot, r_oids = _terminal_by_page_slot(reference_designations)
    return "page_slot", t_slot, r_slot, t_oids, r_oids


def terminal_diagnostics(target_designations, reference_designations):
    """
    REFERENCE-based diagnostic (see module note above): diff the target
    drawing's terminals against a known-good reference of the SAME project.

    PRIMARY comparison pairs each terminal record with its reference
    counterpart and diffs the terminal_number. The pairing key is chosen by
    _pick_terminal_join: object ids when the two sides actually share them,
    otherwise (page_name, rail position) - because EPLAN re-issues object ids
    on every project copy and an id join across copies matches nothing. Either
    way the check pins the change to one terminal and catches a pure SWAP
    (position A: 3->4 while position B: 4->3), which a page-level multiset
    misses entirely (the set {3,4} is unchanged).

    SUMMARY / fallback is the page-level multiset (build_terminal_roster),
    keyed by PAGE NAME: which numbers appear more/fewer times per page.

    Returns a dict; has_differences is True if any paired terminal changed, any
    terminal exists on only one side, OR any page-multiset difference.
    """
    join_key, t_idx, r_idx, t_oids, r_oids = _pick_terminal_join(
        target_designations, reference_designations)
    page_names = {d["page_object_id"]: d["page_name"]
                  for d in reference_designations + target_designations}
    page_ids = {}
    for d in target_designations + reference_designations:
        page_ids.setdefault(d["page_name"], d["page_object_id"])

    def _page_of(key):
        return page_names.get(key[0]) if join_key == "object_id" else key[0]

    def _page_id_of(key):
        return key[0] if join_key == "object_id" else page_ids.get(key[0])

    def _slot_of(key):
        return None if join_key == "object_id" else key[1]

    def _sort_key(key):
        return (str(key[0]), key[1])

    def _one(v):
        return v[0] if len(v) == 1 else v

    per_object_changes = []
    for key in sorted(set(t_idx) & set(r_idx), key=_sort_key):
        if t_idx[key] != r_idx[key]:
            per_object_changes.append({
                "join_key": join_key,
                "page_object_id": _page_id_of(key),
                "page_name": _page_of(key),
                "terminal_slot": _slot_of(key),
                "function_object_id": t_oids.get(key),
                "reference_function_object_id": r_oids.get(key),
                "reference_terminal": _one(r_idx[key]),
                "target_terminal": _one(t_idx[key]),
            })

    def _side_only(keys, idx, oids):
        return [{"join_key": join_key, "page_object_id": _page_id_of(k),
                 "page_name": _page_of(k), "terminal_slot": _slot_of(k),
                 "function_object_id": oids.get(k), "terminal": _one(idx[k])}
                for k in sorted(keys, key=_sort_key)]

    only_target = _side_only(set(t_idx) - set(r_idx), t_idx, t_oids)
    only_reference = _side_only(set(r_idx) - set(t_idx), r_idx, r_oids)

    # page-level multiset summary / fallback, keyed by PAGE NAME (page object
    # ids are not comparable across project copies - see _pick_terminal_join).
    target_roster = build_terminal_roster(target_designations)
    reference_roster = build_terminal_roster(reference_designations)
    page_roster_summary = []
    for page_name in sorted(set(target_roster) & set(reference_roster),
                            key=lambda p: p or ""):
        extra = target_roster[page_name] - reference_roster[page_name]
        missing = reference_roster[page_name] - target_roster[page_name]
        if extra or missing:
            page_roster_summary.append({
                "page_object_id": page_ids.get(page_name),
                "page_name": page_name,
                "extra_terminal_numbers": dict(sorted(extra.items())),
                "missing_terminal_numbers": dict(sorted(missing.items())),
            })

    has_differences = bool(per_object_changes or only_target or only_reference
                           or page_roster_summary)
    return {
        "has_differences": has_differences,
        "join_key": join_key,
        "per_object_change_count": len(per_object_changes),
        "per_object_changes": sorted(
            per_object_changes,
            key=lambda f: (f["page_name"] or "", f["terminal_slot"] or 0,
                           f["function_object_id"] or 0)),
        "objects_only_in_target": only_target,
        "objects_only_in_reference": only_reference,
        "page_roster_summary": page_roster_summary,
    }


# ---------------------------------------------------------------------------
# run_verification - the whole pipeline as one callable.
#
# Everything above is pure analysis; this is the only orchestration. It takes
# explicit paths (no module-level configuration), returns the report plus the
# intermediate artifacts in memory, and writes the four JSON files only when an
# out_dir is given - so a CLI, a web request handler or a test can all drive the
# same code. It never prints: callers that want progress pass a `progress`
# callback and render it however they like.
# ---------------------------------------------------------------------------

TOTAL_STAGES = 7


def _same_path(a, b):
    """True if two paths denote the same file/folder (case-insensitive on Windows)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))


def run_verification(source, points_csv, reference_source=None, out_dir=None,
                     progress=None):
    """
    Verify one EPLAN drawing against one points schedule.

    source            .edb folder or .zw1 backup to verify (see resolve_source)
    points_csv        the schedule, in the QTY/DI/DO/AI/AO/Total_* schema
    reference_source  optional known-good drawing of the SAME project; enables
                      the Phase E terminal ('TR') diff. Without it Phase E is
                      SKIPPED and says so - it is never silently reported as a
                      pass. Passing the same drawing as `source` is rejected,
                      because a self-comparison can only ever report "no
                      differences".
    out_dir           if given, the four JSON artifacts are written there
    progress          optional callable(stage:int, total:int, message:str)

    Returns {"report", "points", "excluded", "expected_points", "outputs"}.
    """
    def _step(n, msg):
        if progress:
            progress(n, TOTAL_STAGES, msg)

    if reference_source is not None and _same_path(source, reference_source):
        raise ValueError(
            "reference_source is the same drawing as source: a self-comparison "
            "can only ever report 'no differences'. Pass a known-good drawing "
            "of the same project, or None to skip the terminal ('TR') check.")

    # ---- [1/7] schedule: classify + generate the expected priority order -----
    # The CSV is the schedule for one (or more) specific DDCs. Classify every
    # point's I/O type straight from its DI/DO/AI/AO columns (never from its
    # name). Then GENERATE the expected priority order DI -> AI -> AO -> DO -
    # this is the expectation only; it does NOT verify that EPLAN allocates
    # channels in that order (a future phase).
    _step(1, "Classifying schedule + generating expected priority order")
    csv_real, csv_summary = load_csv_points(points_csv)
    ddc_nums = {n for n in (_ddc_num(r["DDC"]) for r in csv_real) if n}
    invalid_type_rows = [r for r in csv_real
                         if r["expected_io_type"] == "INVALID_EXCEL_TYPE"]
    totals_mismatch_rows = [r for r in csv_real if r["totals_consistent"] is False]
    expected_points = build_expected_points(csv_real)

    # ---- [2/7] read the source ----------------------------------------------
    _step(2, "Reading Function.eod + Page.eod from the source")
    with tempfile.TemporaryDirectory() as tmp:
        got = resolve_source(source, tmp)
        func_eod, page_eod = got["Function.eod"], got["Page.eod"]
        eod_sha, eod_size = _sha256_size(func_eod)
        page_sha, page_size = _sha256_size(page_eod)
        page_map, page_stats = build_page_map(page_eod)
        source_info = {
            "source": source,
            "source_kind": "edb" if os.path.isdir(source) else "zw1",
            "function_eod_size": eod_size, "function_eod_sha256": eod_sha,
            "page_eod_size": page_size, "page_eod_sha256": page_sha,
            "page_table": page_stats,
        }

        # ---- [3/7] extract raw point + terminal records ----------------------
        _step(3, "Extracting raw point + terminal records from EPLAN")
        raw_points = extract_points(func_eod, page_map=page_map,
                                    only_ddc_nums=ddc_nums or None)
        raw_terminals = extract_terminal_designations(
            func_eod, page_map=page_map, only_ddc_nums=ddc_nums or None)

    # ---- [4/7] scope to ACTIVE pages (points AND terminals) -----------------
    # ONE structural rule for both record classes.
    _step(4, "Scoping to ACTIVE pages (points AND terminals)")
    scoped_active = sum(1 for p in raw_points if _sid(p).get("page_active") is True)
    active_filter_applied = not (raw_points and scoped_active == 0)
    if not active_filter_applied:
        # safety net: the active-page flag did not mark any of this DDC's pages -
        # do NOT silently drop everything; keep all and flag it loudly.
        points, excluded = list(raw_points), []
        active_terminals, deleted_terminals = list(raw_terminals), []
    else:
        points, excluded = filter_by_active_page(raw_points)
        active_terminals, deleted_terminals = filter_by_active_page(raw_terminals)
        for p in excluded:  # annotate dropped points for the excluded file
            si = _sid(p)
            p["excluded_reason"] = (f"on deleted page {si.get('page_number')} "
                                    f"({si.get('page_name')})")

    # ---- [5/7] verify by multiplicity ---------------------------------------
    _step(5, "Verifying by multiplicity against 'Full Combined'")
    report = verify(points, csv_real)
    report["source"] = source_info
    variants = scope_diagnostics(points)
    report["schedule"] = {
        "points_csv": points_csv,
        "real_rows": len(csv_real),
        "summary_rows_ignored": len(csv_summary),
        "ddc_numbers": sorted(ddc_nums),
    }
    report["page_scoping"] = {
        "active_filter_applied": active_filter_applied,
        "points": {"raw": len(raw_points), "active": len(points),
                   "deleted": len(excluded)},
        "terminals": {"raw": len(raw_terminals), "active": len(active_terminals),
                      "deleted": len(deleted_terminals)},
    }
    report["scope_validation"] = {
        "page_current_marker": PAGE_CURRENT_MARKER,
        "validation_scope": PAGE_MARKER_VALIDATION_SCOPE,
        "validated_against_known_pages": PAGE_MARKER_VALIDATED_PAGES,
        "validation_mismatches": PAGE_MARKER_VALIDATION_MISMATCHES,
        "filter_applied": active_filter_applied,
    }
    report["excel_classification"] = {
        "invalid_type_count": len(invalid_type_rows),
        "invalid_type_rows": [r["Full Combined"] for r in invalid_type_rows],
        "totals_mismatch_count": len(totals_mismatch_rows),
        "totals_mismatch_rows": [{
            "full_combined": r["Full Combined"], "quantity": r["quantity"],
            "expected": r["totals_expected"], "actual": r["totals_actual"],
        } for r in totals_mismatch_rows],
    }

    # ---- [6/7] Phase D (uncertified) ----------------------------------------
    _step(6, "Phase D (uncertified): EPLAN page-family type-hint check")
    type_hint_findings = channel_type_hint_diagnostics(points, csv_real)
    report["uncertified_diagnostics"] = {
        "note": "Page-family type hints are a STRING INFERENCE (like "
                "derived_hints.module_name) - advisory only, never gates CERTIFIED.",
        "channel_type_hint_mismatch_count": len(type_hint_findings),
        "channel_type_hint_mismatches": type_hint_findings,
    }

    # ---- [7/7] Phase E: terminal diff against the reference -----------------
    _step(7, "Phase E: terminal-designation diff against the reference drawing")
    terminal_has_diff = False
    if reference_source is None:
        report["terminal_diagnostics"] = {
            "note": "No reference drawing supplied - terminal designations were "
                    "NOT checked. Supply a known-good drawing of the same "
                    "project to catch wrong/shifted terminal ('TR') numbers.",
            "reference_source": None,
            "skipped": True,
            "has_differences": None,
        }
    else:
        with tempfile.TemporaryDirectory() as tmp2:
            ref_got = resolve_source(reference_source, tmp2)
            ref_page_map, _ = build_page_map(ref_got["Page.eod"])
            ref_raw_terminals = extract_terminal_designations(
                ref_got["Function.eod"], page_map=ref_page_map,
                only_ddc_nums=ddc_nums or None)
        # SAME active-page rule on the reference, so we compare active-in-TARGET
        # vs active-in-REFERENCE only - a diff on a deleted old terminal page can
        # never manufacture a WRONG TR finding (the exact stale-record trap we
        # closed for points).
        ref_active_terminals, ref_deleted_terminals = filter_by_active_page(
            ref_raw_terminals)
        terminal_result = terminal_diagnostics(active_terminals, ref_active_terminals)
        terminal_has_diff = terminal_result["has_differences"]
        report["terminal_diagnostics"] = {
            "note": "REFERENCE-based diff of terminal ('TR') designations against "
                    "the reference drawing, ACTIVE pages only on both sides. "
                    "PRIMARY key is (page_object_id, function_object_id) -> "
                    "terminal_number (catches swaps); the page multiset is a "
                    "rollup/fallback.",
            "reference_source": reference_source,
            "skipped": False,
            "reference_terminals": {"raw": len(ref_raw_terminals),
                                    "active": len(ref_active_terminals),
                                    "deleted": len(ref_deleted_terminals)},
            "target_terminals": {"raw": len(raw_terminals),
                                 "active": len(active_terminals),
                                 "deleted": len(deleted_terminals)},
            **terminal_result,
        }

    # fail-closed certification: every scheduled point matched by multiplicity,
    # nothing missing/extra, every page reference resolved and consistent, the
    # active-page filter actually applied, no ambiguous legacy page-name
    # variants, the schedule itself is internally consistent (no
    # INVALID_EXCEL_TYPE rows, no QTY*flag/Total mismatches), and - when a
    # reference was supplied - no terminal-numbering differences.
    # The Phase D hint findings are advisory and do NOT gate certification.
    certified = (report["matched"] == report["excel_expected_total"]
                 and report["missing_from_eplan"] == 0
                 and report["extra_instances_in_eplan"] == 0
                 and report["page_unresolved"] == 0
                 and report["page_ref_inconsistent"] == 0
                 and active_filter_applied
                 and not variants
                 and not invalid_type_rows
                 and not totals_mismatch_rows
                 and not terminal_has_diff)
    report["legacy_ddc_page_variants"] = variants
    report["status"] = "CERTIFIED" if certified else "UNRESOLVED"

    outputs = {}
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        for name, payload in (("eplan_points.json", points),
                              ("eplan_points_excluded.json", excluded),
                              ("expected_points.json", expected_points),
                              ("verification_report.json", report)):
            path = os.path.join(out_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            outputs[name] = path
    report["outputs"] = outputs

    return {"report": report, "points": points, "excluded": excluded,
            "expected_points": expected_points, "outputs": outputs}
