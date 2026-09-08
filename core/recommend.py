"""
AI review of the findings queue (optional enrichment, Google Gemini).

WHAT THIS IS
    core/findings.py already turns a verification report into a flat queue of
    findings, and pairs a MISSING point with a look-alike EXTRA_INSTANCE as an
    UNCERTIFIED text-similarity HINT (`likely_pair`). This module takes that
    queue, asks Gemini to decide the ACTUAL engineering relationship behind each
    candidate pair, and returns concrete, per-finding recommendations.

    A high similarity score is only a candidate: Gemini may confirm a typo, flag
    a semantic mismatch (Pump 2 vs Pump 3), or split the pair back into a real
    missing point plus a real extra point. Deterministic validation then checks
    the shape of every answer before it is trusted.

WHERE IT SITS
    Strictly on TOP of the deterministic verifier, never inside it. It never
    changes a finding's certified status, severity or the failure count - it
    only annotates. The caller (api/jobs.py) runs it best-effort: if the key is
    unset, the package is missing, or Gemini fails, the job still completes with
    findings and simply reports ai_status="unavailable".

    Unlike the rest of core/, this module reaches an external, paid API and sends
    point NAMES and page/channel identifiers (never credentials or file bytes)
    to Google. Nothing here imports google.genai until analyze_findings runs, so
    importing this module is always safe.

CONFIG (environment only - never hard-code a key)
    GEMINI_API_KEY   required to actually call Gemini
    GEMINI_MODEL     default "gemini-3.5-flash"
    GEMINI_TIMEOUT   per-request timeout in seconds (default 60)
"""

import json
import os
import sys
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
MAX_GEMINI_ATTEMPTS = 2


class AIReviewUnavailable(RuntimeError):
    """Expected, non-alarming reasons the AI review cannot run: no API key, or
    the google-genai package is not installed. The caller logs these as a plain
    one-liner rather than a traceback - they are configuration, not a fault."""


def _timeout_seconds():
    try:
        return float(os.environ.get("GEMINI_TIMEOUT", "60"))
    except ValueError:
        return 60.0


# ============================================================
# Structured Gemini output
# ============================================================

RelationshipType = Literal[
    "SAME_POINT_TYPO",
    "SAME_POINT_NAMING_VARIATION",
    "POSSIBLE_MATCH_NEEDS_REVIEW",
    "UNRELATED",
    "STANDALONE",
    "UNCERTAIN",
]

IssueClassification = Literal[
    "TYPO_OR_NAMING_ERROR",
    "NAMING_VARIATION",
    "SEMANTIC_MISMATCH",
    "ACTUAL_MISSING",
    "ACTUAL_EXTRA",
    "UNCERTAIN",
]


class IssueRecommendation(BaseModel):
    classification: IssueClassification
    source_finding_ids: list[str]
    summary: str
    recommended_action: str
    suggested_correct_name: Optional[str] = None
    safe_to_auto_fix: bool
    reason: str


class GroupAnalysis(BaseModel):
    group_id: str
    relationship: RelationshipType
    confidence: float = Field(ge=0.0, le=1.0)
    relationship_reason: str
    issues: list[IssueRecommendation]


class AnalysisResult(BaseModel):
    groups: list[GroupAnalysis]


# ============================================================
# Findings -> analyzer records (replaces the old report.txt regex parser)
#
# core/findings.py hands us richer, already-structured findings than the text
# report ever had, so we map straight from them. `evidence[0]` carries the
# page/channel/object identifiers the old regex used to scrape out of the body.
# ============================================================

def _first_evidence(finding: dict) -> dict:
    evidence = finding.get("evidence") or []
    return evidence[0] if evidence else {}


def records_from_findings(findings: list[dict]) -> list[dict]:
    records = []
    for f in findings:
        ev = _first_evidence(f)
        pair = f.get("likely_pair") or {}
        records.append({
            "id": f["id"],
            "type": f.get("category"),
            "name": f.get("title"),
            "expected": f.get("expected"),
            "found": f.get("found"),
            "page": ev.get("page_name") or f.get("page_name"),
            "page_name": ev.get("page_name") or f.get("page_name"),
            "channel": ev.get("channel_raw"),
            "function_object_id": ev.get("function_object_id"),
            "likely_pair_id": pair.get("id"),
            "likely_pair_title": pair.get("title"),
            "similarity": pair.get("similarity"),
            "pair_status": "hint" if pair else None,
        })
    return records


# ============================================================
# Candidate grouping
# ============================================================

def _compact_finding(record: dict) -> dict:
    return {
        "id": record["id"],
        "type": record["type"],
        "name": record["name"],
        "expected": record["expected"],
        "found": record["found"],
        "page": record["page"],
        "page_name": record["page_name"],
        "channel": record["channel"],
        "function_object_id": record["function_object_id"],
    }


def build_candidate_groups(records: list[dict]) -> list[dict]:
    """
    The fuzzy matcher creates candidate relationships only.

    A missing + extra candidate pair is NOT automatically one real issue.
    Gemini can classify it as UNRELATED, in which case the pair becomes:
      1 ACTUAL_MISSING issue
      1 ACTUAL_EXTRA issue
    """
    records_by_id = {r["id"]: r for r in records}
    used_ids: set[str] = set()
    groups: list[dict] = []
    group_number = 1

    def _pair(missing: dict, extra: dict, group_id: str) -> dict:
        return {
            "group_id": group_id,
            "kind": "CANDIDATE_MISSING_EXTRA_PAIR",
            "missing": _compact_finding(missing),
            "extra": _compact_finding(extra),
            "matcher": {
                "missing_to_extra_similarity": missing.get("similarity"),
                "missing_to_extra_status": missing.get("pair_status"),
                "extra_to_missing_similarity": extra.get("similarity"),
                "extra_to_missing_status": extra.get("pair_status"),
                "reciprocal_pair": (
                    extra.get("likely_pair_id") == missing["id"]
                ),
            },
        }

    # First build candidates from MISSING -> EXTRA links.
    for record in records:
        if record["type"] != "MISSING_POINT" or record["id"] in used_ids:
            continue
        paired = records_by_id.get(record.get("likely_pair_id"))
        if (paired and paired["type"] == "EXTRA_INSTANCE"
                and paired["id"] not in used_ids):
            groups.append(_pair(record, paired, f"pair-{group_number:04d}"))
            group_number += 1
            used_ids.add(record["id"])
            used_ids.add(paired["id"])

    # Fallback: EXTRA -> MISSING links not already consumed.
    for record in records:
        if record["type"] != "EXTRA_INSTANCE" or record["id"] in used_ids:
            continue
        paired = records_by_id.get(record.get("likely_pair_id"))
        if (paired and paired["type"] == "MISSING_POINT"
                and paired["id"] not in used_ids):
            groups.append(_pair(paired, record, f"pair-{group_number:04d}"))
            group_number += 1
            used_ids.add(record["id"])
            used_ids.add(paired["id"])

    # Everything else remains a standalone issue.
    for record in records:
        if record["id"] in used_ids:
            continue
        groups.append({
            "group_id": f"issue-{group_number:04d}",
            "kind": "STANDALONE",
            "finding": _compact_finding(record),
        })
        group_number += 1
        used_ids.add(record["id"])

    return groups


# ============================================================
# Gemini prompt
# ============================================================

SYSTEM_INSTRUCTION = """
You are reviewing an EPLAN / BMS / control-system point comparison report.

The comparison system compares a schedule against an engineering drawing.

The Python program has grouped some MISSING_POINT and EXTRA_INSTANCE
findings because a fuzzy similarity matcher suggested they MIGHT be related.

CRITICAL PRINCIPLE:

A CANDIDATE_MISSING_EXTRA_PAIR is only a candidate relationship.
It is NOT proof that the missing and extra findings are the same point.

Your job is to decide the actual engineering relationship and then produce
the correct actionable issue or issues.

============================================================
OUTPUT STRUCTURE
============================================================

For every input group, return exactly ONE GroupAnalysis object using the
same group_id.

However, GroupAnalysis.issues may contain ONE OR TWO issues.

A candidate pair can therefore become:

- ONE issue when both raw findings are two sides of the same discrepancy.
- TWO issues when the fuzzy pairing is misleading and the findings really
  represent one actual missing point and one actual extra point.

============================================================
RELATIONSHIP TYPES
============================================================

1. SAME_POINT_TYPO

Use when the schedule name and drawing name clearly represent the same
engineering point and the difference is an obvious typo, spelling mistake,
accidental repeated character, or similarly trivial text error.

Examples:
- On/Of vs On/Off
- Systam vs System
- Incominge vs Incoming
- Dampersss vs Damper

For SAME_POINT_TYPO:
- return exactly ONE issue
- classification = TYPO_OR_NAMING_ERROR
- source_finding_ids must contain BOTH the missing and extra IDs
- safe_to_auto_fix may be true only if the correction is extremely obvious
- suggested_correct_name should normally be the schedule name


2. SAME_POINT_NAMING_VARIATION

Use when both clearly refer to the same engineering point and the difference
is a harmless naming convention or wording variation rather than a typo.

Examples can include harmless punctuation, spacing, abbreviation, or
word-order differences when equipment identity and function are unchanged.

For SAME_POINT_NAMING_VARIATION:
- return exactly ONE issue
- classification = NAMING_VARIATION
- source_finding_ids must contain BOTH IDs
- safe_to_auto_fix may be true only when there is no engineering ambiguity


3. POSSIBLE_MATCH_NEEDS_REVIEW

Use when the two findings are strongly related, but the differing token may
change engineering meaning, equipment identity, function, state, command,
direction, or point type.

Examples:
- Pump 2 vs Pump 3
- AHU-01 vs AHU-02
- Supply vs Return
- Start vs Stop
- Open vs Close
- High vs Low
- Alarm vs Status
- Command vs Feedback
- Enable vs Disable

For POSSIBLE_MATCH_NEEDS_REVIEW:
- return exactly ONE issue
- classification = SEMANTIC_MISMATCH
- source_finding_ids must contain BOTH IDs
- safe_to_auto_fix = false
- explain exactly what meaningful token differs
- recommend engineering verification before changing schedule or drawing


4. UNRELATED

Use when the fuzzy matcher appears to have paired two different engineering
points and there is no good basis for treating one as the naming correction
of the other.

For UNRELATED:
- return exactly TWO issues

Issue A:
- classification = ACTUAL_MISSING
- source_finding_ids contains ONLY the missing finding ID
- safe_to_auto_fix = false

Issue B:
- classification = ACTUAL_EXTRA
- source_finding_ids contains ONLY the extra finding ID
- safe_to_auto_fix = false

Do not hide a real missing point or real extra point merely because the
similarity score is high.


5. UNCERTAIN

Use when there is not enough evidence to decide whether the pair is one
discrepancy or two independent discrepancies.

For UNCERTAIN:
- return exactly ONE issue
- classification = UNCERTAIN
- source_finding_ids must contain BOTH IDs
- safe_to_auto_fix = false
- state what an engineer needs to verify


6. STANDALONE

Use only for an input group whose kind is STANDALONE.

For standalone MISSING_POINT:
- return exactly ONE ACTUAL_MISSING issue

For standalone EXTRA_INSTANCE:
- return exactly ONE ACTUAL_EXTRA issue

For any other standalone finding that cannot be classified safely:
- return exactly ONE UNCERTAIN issue

============================================================
ENGINEERING SAFETY RULES
============================================================

1. Similarity score alone must NEVER prove that two findings are the same
   engineering point.

2. A reciprocal fuzzy match is stronger evidence of relatedness, but it is
   still not proof that the points are identical.

3. Treat numbers, equipment identifiers, tags, directions, commands,
   states, and functional words as meaningful.

4. Never auto-fix a difference that could change:
   - physical equipment identity
   - equipment number
   - system identity
   - signal direction
   - command meaning
   - alarm/status meaning
   - start/stop meaning
   - open/close meaning
   - high/low meaning
   - supply/return meaning

5. Do not decide that Pump 2 and Pump 3 are the same equipment merely
   because the rest of the text matches.

6. Do not automatically choose UNRELATED for every number difference either.
   If it could realistically be a wrong equipment identifier in one source,
   POSSIBLE_MATCH_NEEDS_REVIEW is appropriate.

7. Use UNRELATED when the evidence indicates the matcher has connected two
   genuinely distinct points.

8. For an obvious typo, the recommended action should identify the drawing
   point and tell the reviewer exactly what to rename.

9. For an actual missing point, recommend verifying/adding/locating the
   required schedule point in the drawing.

10. For an actual extra point, recommend verifying whether the drawing point
    should be added to the schedule or removed/corrected in the drawing.

11. Preserve all group IDs and source finding IDs exactly as supplied.

12. Keep summaries and actions concise, practical, and engineering-focused.
"""


def build_prompt(groups: list[dict],
                 validation_feedback: Optional[str] = None) -> str:
    prompt = (
        "Analyze the candidate groups below.\n\n"
        f"Input group count: {len(groups)}\n"
        f"Return exactly {len(groups)} GroupAnalysis objects.\n\n"
        "IMPORTANT: the number of FINAL issues does NOT have to equal "
        "the number of input groups.\n"
        "A group classified as UNRELATED must produce TWO issues.\n\n"
        "Candidate groups:\n"
        + json.dumps(groups, indent=2, ensure_ascii=False)
    )
    if validation_feedback:
        prompt += (
            "\n\n"
            "Your previous response violated deterministic validation rules.\n"
            "Correct the response using this validation error:\n"
            f"{validation_feedback}\n"
        )
    return prompt


# ============================================================
# Gemini call
# ============================================================

def _call_gemini(client, groups, model_name, validation_feedback=None):
    from google.genai import types

    response = client.models.generate_content(
        model=model_name,
        contents=build_prompt(groups, validation_feedback),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=AnalysisResult,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        ),
    )
    if response.parsed is not None:
        if isinstance(response.parsed, AnalysisResult):
            return response.parsed
        return AnalysisResult.model_validate(response.parsed)
    if not response.text:
        raise RuntimeError("Gemini returned no structured response.")
    return AnalysisResult.model_validate_json(response.text)


# ============================================================
# Deterministic validation
# ============================================================

def _group_source_ids(group: dict) -> set[str]:
    if group["kind"] == "CANDIDATE_MISSING_EXTRA_PAIR":
        return {group["missing"]["id"], group["extra"]["id"]}
    return {group["finding"]["id"]}


def validate_analysis_result(result: AnalysisResult, groups: list[dict]) -> None:
    expected_ids = [group["group_id"] for group in groups]
    returned_ids = [item.group_id for item in result.groups]

    if len(returned_ids) != len(expected_ids):
        raise ValueError(
            f"Expected {len(expected_ids)} GroupAnalysis objects, "
            f"got {len(returned_ids)}.")
    if len(set(returned_ids)) != len(returned_ids):
        raise ValueError("Gemini returned duplicate group IDs.")
    if set(returned_ids) != set(expected_ids):
        raise ValueError(
            "Gemini changed, omitted, or invented group IDs. "
            f"Expected {expected_ids}, got {returned_ids}.")

    groups_by_id = {group["group_id"]: group for group in groups}

    for group_result in result.groups:
        group = groups_by_id[group_result.group_id]
        valid_ids = _group_source_ids(group)

        if not group_result.issues:
            raise ValueError(f"{group_result.group_id} returned no issues.")

        for issue in group_result.issues:
            issue_ids = set(issue.source_finding_ids)
            if not issue.source_finding_ids:
                raise ValueError(
                    f"{group_result.group_id} has an issue with no source IDs.")
            if len(issue_ids) != len(issue.source_finding_ids):
                raise ValueError(
                    f"{group_result.group_id} has duplicate source IDs.")
            if not issue_ids.issubset(valid_ids):
                raise ValueError(
                    f"{group_result.group_id} used invalid source IDs "
                    f"{sorted(issue_ids)}.")
            if (issue.safe_to_auto_fix and issue.classification
                    not in {"TYPO_OR_NAMING_ERROR", "NAMING_VARIATION"}):
                raise ValueError(
                    f"{group_result.group_id}: "
                    f"{issue.classification} cannot be auto-fixed.")

        if group["kind"] == "STANDALONE":
            finding = group["finding"]
            issue = group_result.issues[0]
            if group_result.relationship != "STANDALONE":
                raise ValueError(f"{group_result.group_id} must use STANDALONE.")
            if len(group_result.issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} must return one issue.")
            if issue.source_finding_ids != [finding["id"]]:
                raise ValueError(
                    f"{group_result.group_id} must reference only "
                    f"{finding['id']}.")
            if (finding["type"] == "MISSING_POINT"
                    and issue.classification != "ACTUAL_MISSING"):
                raise ValueError(
                    f"{group_result.group_id} must be ACTUAL_MISSING.")
            if (finding["type"] == "EXTRA_INSTANCE"
                    and issue.classification != "ACTUAL_EXTRA"):
                raise ValueError(
                    f"{group_result.group_id} must be ACTUAL_EXTRA.")
            if issue.safe_to_auto_fix:
                raise ValueError(
                    f"{group_result.group_id} cannot be auto-fixed.")
            continue

        missing_id = group["missing"]["id"]
        extra_id = group["extra"]["id"]
        both_ids = {missing_id, extra_id}

        if group_result.relationship == "STANDALONE":
            raise ValueError(
                f"{group_result.group_id} is a pair, not standalone.")

        if group_result.relationship in {"SAME_POINT_TYPO",
                                          "SAME_POINT_NAMING_VARIATION",
                                          "POSSIBLE_MATCH_NEEDS_REVIEW",
                                          "UNCERTAIN"}:
            expected_class = {
                "SAME_POINT_TYPO": "TYPO_OR_NAMING_ERROR",
                "SAME_POINT_NAMING_VARIATION": "NAMING_VARIATION",
                "POSSIBLE_MATCH_NEEDS_REVIEW": "SEMANTIC_MISMATCH",
                "UNCERTAIN": "UNCERTAIN",
            }[group_result.relationship]
            if len(group_result.issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} {group_result.relationship} "
                    "must return one issue.")
            issue = group_result.issues[0]
            if issue.classification != expected_class:
                raise ValueError(
                    f"{group_result.group_id} must use {expected_class}.")
            if set(issue.source_finding_ids) != both_ids:
                raise ValueError(
                    f"{group_result.group_id} must reference both pair IDs.")
            if (group_result.relationship in {"POSSIBLE_MATCH_NEEDS_REVIEW",
                                              "UNCERTAIN"}
                    and issue.safe_to_auto_fix):
                raise ValueError(
                    f"{group_result.group_id} cannot be auto-fixed.")

        elif group_result.relationship == "UNRELATED":
            if len(group_result.issues) != 2:
                raise ValueError(
                    f"{group_result.group_id} UNRELATED must return two issues.")
            missing_issues = [i for i in group_result.issues
                              if i.classification == "ACTUAL_MISSING"]
            extra_issues = [i for i in group_result.issues
                            if i.classification == "ACTUAL_EXTRA"]
            if len(missing_issues) != 1 or len(extra_issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} UNRELATED must contain exactly "
                    "one ACTUAL_MISSING and one ACTUAL_EXTRA.")
            if missing_issues[0].source_finding_ids != [missing_id]:
                raise ValueError(
                    f"{group_result.group_id} ACTUAL_MISSING must reference "
                    f"only {missing_id}.")
            if extra_issues[0].source_finding_ids != [extra_id]:
                raise ValueError(
                    f"{group_result.group_id} ACTUAL_EXTRA must reference "
                    f"only {extra_id}.")
            if (missing_issues[0].safe_to_auto_fix
                    or extra_issues[0].safe_to_auto_fix):
                raise ValueError(
                    f"{group_result.group_id} actual missing/extra "
                    "cannot be auto-fixed.")
        else:
            raise ValueError(
                f"{group_result.group_id} returned unsupported relationship "
                f"{group_result.relationship}.")


def reorder_result_to_input(result: AnalysisResult,
                            groups: list[dict]) -> AnalysisResult:
    by_id = {item.group_id: item for item in result.groups}
    result.groups = [by_id[group["group_id"]] for group in groups]
    return result


# Transient Gemini errors worth a short backoff: server busy (503), quota spike
# (429), server error (500). Everything else (auth, bad model) is permanent.
_TRANSIENT_CODES = {429, 500, 503}
_MAX_TRANSIENT_RETRIES = 3


def _call_gemini_with_retry(client, groups, model_name, validation_feedback,
                            genai_errors):
    delay = 2.0
    for attempt in range(1, _MAX_TRANSIENT_RETRIES + 1):
        try:
            return _call_gemini(client, groups, model_name, validation_feedback)
        except genai_errors.APIError as exc:
            code = getattr(exc, "code", None)
            if code in _TRANSIENT_CODES and attempt < _MAX_TRANSIENT_RETRIES:
                sys.stdout.write(
                    f"Gemini {code} (busy); retrying in {delay:.0f}s...\n")
                sys.stdout.flush()
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise


def analyze_groups_with_gemini(groups: list[dict],
                               model_name: str = MODEL_NAME) -> AnalysisResult:
    # GEMINI_API_KEY env var wins; otherwise the key hard-coded here is used.
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise AIReviewUnavailable("GEMINI_API_KEY is not set")

    try:
        from google import genai
        from google.genai import types
        from google.genai import errors as genai_errors
    except ImportError as exc:
        raise AIReviewUnavailable("google-genai is not installed") from exc

    validation_feedback: Optional[str] = None
    last_error: Optional[Exception] = None

    http_options = types.HttpOptions(timeout=int(_timeout_seconds() * 1000))
    with genai.Client(api_key=api_key, http_options=http_options) as client:
        for attempt in range(1, MAX_GEMINI_ATTEMPTS + 1):
            try:
                result = _call_gemini_with_retry(
                    client, groups, model_name, validation_feedback,
                    genai_errors)
            except genai_errors.APIError as exc:
                # permanent config problem (leaked/invalid key, unknown model):
                # not a bug, log one clean line rather than a traceback.
                code = getattr(exc, "code", "?")
                message = getattr(exc, "message", str(exc))
                raise AIReviewUnavailable(
                    f"Gemini API error {code}: {message}") from exc
            try:
                validate_analysis_result(result, groups)
                return reorder_result_to_input(result, groups)
            except ValueError as exc:
                last_error = exc
                if attempt >= MAX_GEMINI_ATTEMPTS:
                    break
                validation_feedback = str(exc)

    raise RuntimeError(
        "Gemini could not produce a response that satisfies "
        "the deterministic relationship rules."
    ) from last_error


# ============================================================
# Flatten + export
# ============================================================

def _find_group(groups: list[dict], group_id: str) -> dict:
    for group in groups:
        if group["group_id"] == group_id:
            return group
    raise KeyError(f"Unknown group_id: {group_id}")


def _context_for_issue(group: dict, issue: IssueRecommendation) -> dict:
    source_ids = set(issue.source_finding_ids)
    context = {
        "schedule_name": None,
        "drawing_name": None,
        "page": None,
        "page_name": None,
        "channel": None,
        "function_object_id": None,
    }

    if group["kind"] == "CANDIDATE_MISSING_EXTRA_PAIR":
        missing, extra = group["missing"], group["extra"]
        if missing["id"] in source_ids:
            context["schedule_name"] = missing["name"]
        if extra["id"] in source_ids:
            context["drawing_name"] = extra["name"]
            context["page"] = extra["page"]
            context["page_name"] = extra["page_name"]
            context["channel"] = extra["channel"]
            context["function_object_id"] = extra["function_object_id"]
        return context

    finding = group["finding"]
    if finding["type"] == "MISSING_POINT":
        context["schedule_name"] = finding["name"]
    elif finding["type"] == "EXTRA_INSTANCE":
        context["drawing_name"] = finding["name"]
        context["page"] = finding["page"]
        context["page_name"] = finding["page_name"]
        context["channel"] = finding["channel"]
        context["function_object_id"] = finding["function_object_id"]
    else:
        context["schedule_name"] = finding["name"]
        context["page"] = finding["page"]
        context["page_name"] = finding["page_name"]
        context["channel"] = finding["channel"]
        context["function_object_id"] = finding["function_object_id"]
    return context


def flatten_issues(result: AnalysisResult, groups: list[dict]) -> list[dict]:
    flattened: list[dict] = []
    for group_result in result.groups:
        group = _find_group(groups, group_result.group_id)
        for issue in group_result.issues:
            flattened.append({
                "group_id": group_result.group_id,
                "relationship": group_result.relationship,
                "relationship_confidence": group_result.confidence,
                "relationship_reason": group_result.relationship_reason,
                "classification": issue.classification,
                "source_finding_ids": issue.source_finding_ids,
                "safe_to_auto_fix": issue.safe_to_auto_fix,
                "suggested_correct_name": issue.suggested_correct_name,
                "summary": issue.summary,
                "recommended_action": issue.recommended_action,
                "reason": issue.reason,
                **_context_for_issue(group, issue),
            })
    return flattened


def build_export_payload(records: list[dict], groups: list[dict],
                         result: AnalysisResult) -> dict:
    actionable = flatten_issues(result, groups)
    return {
        "summary": {
            "raw_findings": len(records),
            "candidate_groups": len(groups),
            "final_actionable_issues": len(actionable),
            "safe_auto_fixes": sum(1 for i in actionable
                                   if i["safe_to_auto_fix"]),
            "manual_or_review_issues": sum(1 for i in actionable
                                           if not i["safe_to_auto_fix"]),
            "split_candidate_pairs": sum(
                1 for g in result.groups if g.relationship == "UNRELATED"),
        },
        "group_analysis": result.model_dump(),
        "actionable_issues": actionable,
    }


# ============================================================
# Public entry point
# ============================================================

def analyze_findings(findings: list[dict],
                     model_name: str = MODEL_NAME) -> dict:
    """Turn the findings queue into per-finding AI recommendations.

    Raises RuntimeError if the key is unset or Gemini cannot produce a valid
    answer - the caller runs this best-effort and degrades to "unavailable".
    Returns the export payload: {"summary", "group_analysis", "actionable_issues"}.
    """
    records = records_from_findings(findings)
    groups = build_candidate_groups(records)
    result = analyze_groups_with_gemini(groups, model_name=model_name)
    return build_export_payload(records, groups, result)
