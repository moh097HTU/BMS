import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


# ============================================================
# Configuration
# ============================================================

# Put your NEW Gemini API key here.
# Do not reuse a key that you have already exposed publicly.
GEMINI_API_KEY = "AIzaSyC_klwZWZ8FSh7vNTJrcS6QqWI89CfRJlM"

MODEL_NAME = "gemini-3.8-flash"
REPORT_FILE = Path("report.txt")
OUTPUT_FILE = Path("recommendations.json")
MAX_GEMINI_ATTEMPTS = 2


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
# Report parsing
# ============================================================

FINDING_START_RE = re.compile(
    r"^\s*\[(?P<id>[^\]]+)\]\s+"
    r"(?P<type>[A-Z_]+)\s+"
    r"(?P<title>.+?)\s*$"
)

LIKELY_PAIR_RE = re.compile(
    r"likely pair\s*->\s*"
    r"\[(?P<id>[^\]]+)\]\s*"
    r"(?P<title>.*?)\s*"
    r"\((?P<score>\d+(?:\.\d+)?),\s*(?P<status>[^)]+)\)",
    re.IGNORECASE,
)

EXPECTED_RE = re.compile(r"\bexpected=(\d+)")
FOUND_RE = re.compile(r"\bfound=(\d+)")
PAGE_RE = re.compile(r"\bpage=([^\s]+)")
PAGE_NAME_RE = re.compile(r"\bpage_name=([^\s]+)")
CHANNEL_RE = re.compile(r"\bchannel_raw=([^\s]+)")
FUNCTION_OBJECT_ID_RE = re.compile(r"\bfunction_object_id=([^\s]+)")


def _search(regex: re.Pattern, text: str) -> Optional[str]:
    match = regex.search(text)
    return match.group(1) if match else None


def parse_findings(report_text: str) -> list[dict]:
    lines = report_text.splitlines()
    findings: list[dict] = []
    index = 0

    while index < len(lines):
        start_match = FINDING_START_RE.match(lines[index])

        if not start_match:
            index += 1
            continue

        finding_id = start_match.group("id")
        finding_type = start_match.group("type")
        title = start_match.group("title").strip()

        body_lines: list[str] = []
        index += 1

        while index < len(lines):
            next_finding = FINDING_START_RE.match(lines[index])

            if next_finding:
                break

            line = lines[index].strip()

            if not re.match(r"^--\s+[A-Z]+", line):
                if line:
                    body_lines.append(line)

            index += 1

        body = "\n".join(body_lines)
        pair_match = LIKELY_PAIR_RE.search(body)

        finding = {
            "id": finding_id,
            "type": finding_type,
            "title": title,
            "expected": _search(EXPECTED_RE, body),
            "found": _search(FOUND_RE, body),
            "page": _search(PAGE_RE, body),
            "page_name": _search(PAGE_NAME_RE, body),
            "channel": _search(CHANNEL_RE, body),
            "function_object_id": _search(FUNCTION_OBJECT_ID_RE, body),
            "likely_pair_id": None,
            "likely_pair_title": None,
            "similarity": None,
            "pair_status": None,
        }

        if pair_match:
            finding["likely_pair_id"] = pair_match.group("id")
            finding["likely_pair_title"] = pair_match.group("title").strip()
            finding["similarity"] = float(pair_match.group("score"))
            finding["pair_status"] = pair_match.group("status").strip()

        findings.append(finding)

    return findings


# ============================================================
# Candidate grouping
# ============================================================

def _compact_finding(finding: dict) -> dict:
    return {
        "id": finding["id"],
        "type": finding["type"],
        "name": finding["title"],
        "expected": finding["expected"],
        "found": finding["found"],
        "page": finding["page"],
        "page_name": finding["page_name"],
        "channel": finding["channel"],
        "function_object_id": finding["function_object_id"],
    }


def build_candidate_groups(findings: list[dict]) -> list[dict]:
    """
    The fuzzy matcher creates candidate relationships only.

    A missing + extra candidate pair is NOT automatically one real issue.
    Gemini can classify it as UNRELATED, in which case the pair becomes:
      1 ACTUAL_MISSING issue
      1 ACTUAL_EXTRA issue
    """

    findings_by_id = {
        finding["id"]: finding
        for finding in findings
    }

    used_ids: set[str] = set()
    groups: list[dict] = []
    group_number = 1

    # First build candidates from MISSING -> EXTRA links.
    for finding in findings:
        if finding["type"] != "MISSING_POINT":
            continue

        if finding["id"] in used_ids:
            continue

        pair_id = finding.get("likely_pair_id")
        paired_finding = findings_by_id.get(pair_id)

        if (
            paired_finding
            and paired_finding["type"] == "EXTRA_INSTANCE"
            and paired_finding["id"] not in used_ids
        ):
            group_id = f"pair-{group_number:04d}"
            group_number += 1

            groups.append(
                {
                    "group_id": group_id,
                    "kind": "CANDIDATE_MISSING_EXTRA_PAIR",
                    "missing": _compact_finding(finding),
                    "extra": _compact_finding(paired_finding),
                    "matcher": {
                        "missing_to_extra_similarity": finding.get("similarity"),
                        "missing_to_extra_status": finding.get("pair_status"),
                        "extra_to_missing_similarity": paired_finding.get("similarity"),
                        "extra_to_missing_status": paired_finding.get("pair_status"),
                        "reciprocal_pair": (
                            paired_finding.get("likely_pair_id")
                            == finding["id"]
                        ),
                    },
                }
            )

            used_ids.add(finding["id"])
            used_ids.add(paired_finding["id"])

    # Fallback: EXTRA -> MISSING links not already consumed.
    for finding in findings:
        if finding["type"] != "EXTRA_INSTANCE":
            continue

        if finding["id"] in used_ids:
            continue

        pair_id = finding.get("likely_pair_id")
        paired_finding = findings_by_id.get(pair_id)

        if (
            paired_finding
            and paired_finding["type"] == "MISSING_POINT"
            and paired_finding["id"] not in used_ids
        ):
            group_id = f"pair-{group_number:04d}"
            group_number += 1

            groups.append(
                {
                    "group_id": group_id,
                    "kind": "CANDIDATE_MISSING_EXTRA_PAIR",
                    "missing": _compact_finding(paired_finding),
                    "extra": _compact_finding(finding),
                    "matcher": {
                        "missing_to_extra_similarity": paired_finding.get("similarity"),
                        "missing_to_extra_status": paired_finding.get("pair_status"),
                        "extra_to_missing_similarity": finding.get("similarity"),
                        "extra_to_missing_status": finding.get("pair_status"),
                        "reciprocal_pair": (
                            paired_finding.get("likely_pair_id")
                            == finding["id"]
                        ),
                    },
                }
            )

            used_ids.add(finding["id"])
            used_ids.add(paired_finding["id"])

    # Everything else remains a standalone issue.
    for finding in findings:
        if finding["id"] in used_ids:
            continue

        group_id = f"issue-{group_number:04d}"
        group_number += 1

        groups.append(
            {
                "group_id": group_id,
                "kind": "STANDALONE",
                "finding": _compact_finding(finding),
            }
        )

        used_ids.add(finding["id"])

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


def build_prompt(
    groups: list[dict],
    validation_feedback: Optional[str] = None,
) -> str:
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
# Gemini
# ============================================================

def _call_gemini(
    client: genai.Client,
    groups: list[dict],
    model_name: str,
    validation_feedback: Optional[str] = None,
) -> AnalysisResult:
    response = client.models.generate_content(
        model=model_name,
        contents=build_prompt(
            groups=groups,
            validation_feedback=validation_feedback,
        ),
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
        return {
            group["missing"]["id"],
            group["extra"]["id"],
        }

    return {group["finding"]["id"]}


def validate_analysis_result(
    result: AnalysisResult,
    groups: list[dict],
) -> None:
    expected_ids = [group["group_id"] for group in groups]
    returned_ids = [item.group_id for item in result.groups]

    if len(returned_ids) != len(expected_ids):
        raise ValueError(
            f"Expected {len(expected_ids)} GroupAnalysis objects, "
            f"got {len(returned_ids)}."
        )

    if len(set(returned_ids)) != len(returned_ids):
        raise ValueError("Gemini returned duplicate group IDs.")

    if set(returned_ids) != set(expected_ids):
        raise ValueError(
            "Gemini changed, omitted, or invented group IDs. "
            f"Expected {expected_ids}, got {returned_ids}."
        )

    groups_by_id = {
        group["group_id"]: group
        for group in groups
    }

    for group_result in result.groups:
        group = groups_by_id[group_result.group_id]
        valid_ids = _group_source_ids(group)

        if not group_result.issues:
            raise ValueError(
                f"{group_result.group_id} returned no issues."
            )

        for issue in group_result.issues:
            issue_ids = set(issue.source_finding_ids)

            if not issue.source_finding_ids:
                raise ValueError(
                    f"{group_result.group_id} has an issue with no source IDs."
                )

            if len(issue_ids) != len(issue.source_finding_ids):
                raise ValueError(
                    f"{group_result.group_id} has duplicate source IDs."
                )

            if not issue_ids.issubset(valid_ids):
                raise ValueError(
                    f"{group_result.group_id} used invalid source IDs "
                    f"{sorted(issue_ids)}."
                )

            if (
                issue.safe_to_auto_fix
                and issue.classification
                not in {
                    "TYPO_OR_NAMING_ERROR",
                    "NAMING_VARIATION",
                }
            ):
                raise ValueError(
                    f"{group_result.group_id}: "
                    f"{issue.classification} cannot be auto-fixed."
                )

        if group["kind"] == "STANDALONE":
            finding = group["finding"]
            issue = group_result.issues[0]

            if group_result.relationship != "STANDALONE":
                raise ValueError(
                    f"{group_result.group_id} must use STANDALONE."
                )

            if len(group_result.issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} must return one issue."
                )

            if issue.source_finding_ids != [finding["id"]]:
                raise ValueError(
                    f"{group_result.group_id} must reference only "
                    f"{finding['id']}."
                )

            if (
                finding["type"] == "MISSING_POINT"
                and issue.classification != "ACTUAL_MISSING"
            ):
                raise ValueError(
                    f"{group_result.group_id} must be ACTUAL_MISSING."
                )

            if (
                finding["type"] == "EXTRA_INSTANCE"
                and issue.classification != "ACTUAL_EXTRA"
            ):
                raise ValueError(
                    f"{group_result.group_id} must be ACTUAL_EXTRA."
                )

            if issue.safe_to_auto_fix:
                raise ValueError(
                    f"{group_result.group_id} cannot be auto-fixed."
                )

            continue

        missing_id = group["missing"]["id"]
        extra_id = group["extra"]["id"]
        both_ids = {missing_id, extra_id}

        if group_result.relationship == "STANDALONE":
            raise ValueError(
                f"{group_result.group_id} is a pair, not standalone."
            )

        if group_result.relationship == "SAME_POINT_TYPO":
            if len(group_result.issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} SAME_POINT_TYPO "
                    "must return one issue."
                )

            issue = group_result.issues[0]

            if issue.classification != "TYPO_OR_NAMING_ERROR":
                raise ValueError(
                    f"{group_result.group_id} must use TYPO_OR_NAMING_ERROR."
                )

            if set(issue.source_finding_ids) != both_ids:
                raise ValueError(
                    f"{group_result.group_id} must reference both pair IDs."
                )

        elif group_result.relationship == "SAME_POINT_NAMING_VARIATION":
            if len(group_result.issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} naming variation "
                    "must return one issue."
                )

            issue = group_result.issues[0]

            if issue.classification != "NAMING_VARIATION":
                raise ValueError(
                    f"{group_result.group_id} must use NAMING_VARIATION."
                )

            if set(issue.source_finding_ids) != both_ids:
                raise ValueError(
                    f"{group_result.group_id} must reference both pair IDs."
                )

        elif group_result.relationship == "POSSIBLE_MATCH_NEEDS_REVIEW":
            if len(group_result.issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} review pair "
                    "must return one issue."
                )

            issue = group_result.issues[0]

            if issue.classification != "SEMANTIC_MISMATCH":
                raise ValueError(
                    f"{group_result.group_id} must use SEMANTIC_MISMATCH."
                )

            if set(issue.source_finding_ids) != both_ids:
                raise ValueError(
                    f"{group_result.group_id} must reference both pair IDs."
                )

            if issue.safe_to_auto_fix:
                raise ValueError(
                    f"{group_result.group_id} cannot be auto-fixed."
                )

        elif group_result.relationship == "UNCERTAIN":
            if len(group_result.issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} UNCERTAIN must return one issue."
                )

            issue = group_result.issues[0]

            if issue.classification != "UNCERTAIN":
                raise ValueError(
                    f"{group_result.group_id} must use UNCERTAIN."
                )

            if set(issue.source_finding_ids) != both_ids:
                raise ValueError(
                    f"{group_result.group_id} must reference both pair IDs."
                )

            if issue.safe_to_auto_fix:
                raise ValueError(
                    f"{group_result.group_id} cannot be auto-fixed."
                )

        elif group_result.relationship == "UNRELATED":
            if len(group_result.issues) != 2:
                raise ValueError(
                    f"{group_result.group_id} UNRELATED must return two issues."
                )

            missing_issues = [
                issue
                for issue in group_result.issues
                if issue.classification == "ACTUAL_MISSING"
            ]

            extra_issues = [
                issue
                for issue in group_result.issues
                if issue.classification == "ACTUAL_EXTRA"
            ]

            if len(missing_issues) != 1 or len(extra_issues) != 1:
                raise ValueError(
                    f"{group_result.group_id} UNRELATED must contain exactly "
                    "one ACTUAL_MISSING and one ACTUAL_EXTRA."
                )

            if missing_issues[0].source_finding_ids != [missing_id]:
                raise ValueError(
                    f"{group_result.group_id} ACTUAL_MISSING must reference "
                    f"only {missing_id}."
                )

            if extra_issues[0].source_finding_ids != [extra_id]:
                raise ValueError(
                    f"{group_result.group_id} ACTUAL_EXTRA must reference "
                    f"only {extra_id}."
                )

            if (
                missing_issues[0].safe_to_auto_fix
                or extra_issues[0].safe_to_auto_fix
            ):
                raise ValueError(
                    f"{group_result.group_id} actual missing/extra "
                    "cannot be auto-fixed."
                )

        else:
            raise ValueError(
                f"{group_result.group_id} returned unsupported relationship "
                f"{group_result.relationship}."
            )


def reorder_result_to_input(
    result: AnalysisResult,
    groups: list[dict],
) -> AnalysisResult:
    by_id = {
        item.group_id: item
        for item in result.groups
    }

    result.groups = [
        by_id[group["group_id"]]
        for group in groups
    ]

    return result


def analyze_groups_with_gemini(
    groups: list[dict],
    model_name: str = MODEL_NAME,
) -> AnalysisResult:
    if (
        not GEMINI_API_KEY
        or GEMINI_API_KEY == "PASTE_YOUR_NEW_GEMINI_API_KEY_HERE"
    ):
        raise RuntimeError(
            "Set GEMINI_API_KEY at the top of recommendation_analyzer.py."
        )

    validation_feedback: Optional[str] = None
    last_error: Optional[Exception] = None

    with genai.Client(api_key=GEMINI_API_KEY) as client:
        for attempt in range(1, MAX_GEMINI_ATTEMPTS + 1):
            result = _call_gemini(
                client=client,
                groups=groups,
                model_name=model_name,
                validation_feedback=validation_feedback,
            )

            try:
                validate_analysis_result(
                    result=result,
                    groups=groups,
                )

                return reorder_result_to_input(
                    result=result,
                    groups=groups,
                )

            except ValueError as exc:
                last_error = exc

                if attempt >= MAX_GEMINI_ATTEMPTS:
                    break

                validation_feedback = str(exc)

                print(
                    "Gemini response failed deterministic validation; "
                    "retrying once..."
                )

    raise RuntimeError(
        "Gemini could not produce a response that satisfies "
        "the deterministic relationship rules."
    ) from last_error


# ============================================================
# Report analysis
# ============================================================

def analyze_report(
    report_text: str,
    model_name: str = MODEL_NAME,
) -> tuple[list[dict], list[dict], AnalysisResult]:
    findings = parse_findings(report_text)

    if not findings:
        raise ValueError(
            "No findings were found in the supplied report."
        )

    groups = build_candidate_groups(findings)

    print(f"Raw findings       : {len(findings)}")
    print(f"Candidate groups   : {len(groups)}")

    result = analyze_groups_with_gemini(
        groups=groups,
        model_name=model_name,
    )

    return findings, groups, result


# ============================================================
# Output helpers
# ============================================================

def _find_group(groups: list[dict], group_id: str) -> dict:
    for group in groups:
        if group["group_id"] == group_id:
            return group

    raise KeyError(f"Unknown group_id: {group_id}")


def _context_for_issue(
    group: dict,
    issue: IssueRecommendation,
) -> dict:
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
        missing = group["missing"]
        extra = group["extra"]

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


def flatten_issues(
    result: AnalysisResult,
    groups: list[dict],
) -> list[dict]:
    flattened: list[dict] = []

    for group_result in result.groups:
        group = _find_group(
            groups,
            group_result.group_id,
        )

        for issue in group_result.issues:
            flattened.append(
                {
                    "group_id": group_result.group_id,
                    "relationship": group_result.relationship,
                    "relationship_confidence": group_result.confidence,
                    "relationship_reason": group_result.relationship_reason,
                    "issue": issue,
                    "context": _context_for_issue(
                        group,
                        issue,
                    ),
                }
            )

    return flattened


def print_result(
    result: AnalysisResult,
    groups: list[dict],
) -> None:
    final_issues = flatten_issues(
        result=result,
        groups=groups,
    )

    classification_counts = Counter(
        item["issue"].classification
        for item in final_issues
    )

    safe_fix_count = sum(
        1
        for item in final_issues
        if item["issue"].safe_to_auto_fix
    )

    review_count = len(final_issues) - safe_fix_count

    split_pair_count = sum(
        1
        for group_result in result.groups
        if group_result.relationship == "UNRELATED"
    )

    print()
    print("=" * 80)
    print("JOB REVIEW")
    print("=" * 80)
    print(f"Candidate groups         : {len(groups)}")
    print(f"Final actionable issues  : {len(final_issues)}")
    print(f"Safe automatic fixes     : {safe_fix_count}")
    print(f"Review/manual action     : {review_count}")
    print(f"Pairs split into 2 issues: {split_pair_count}")

    if classification_counts:
        print()
        print("Issue types:")

        for classification, count in sorted(
            classification_counts.items()
        ):
            print(f"  {classification:<24}: {count}")

    print()
    print("=" * 80)
    print(f"ACTIONABLE ISSUES ({len(final_issues)})")
    print("=" * 80)

    for index, item in enumerate(
        final_issues,
        start=1,
    ):
        issue: IssueRecommendation = item["issue"]
        context = item["context"]

        if issue.safe_to_auto_fix:
            action_label = "SAFE CORRECTION"
        elif issue.classification in {
            "ACTUAL_MISSING",
            "ACTUAL_EXTRA",
        }:
            action_label = "MANUAL ACTION REQUIRED"
        else:
            action_label = "ENGINEERING REVIEW REQUIRED"

        print()
        print(f"{index}. {action_label}")
        print(f"   Classification : {issue.classification}")
        print(f"   Group          : {item['group_id']}")
        print(f"   Relationship   : {item['relationship']}")
        print(
            f"   Confidence     : "
            f"{item['relationship_confidence']:.2f}"
        )
        print(
            "   Source finding : "
            + ", ".join(issue.source_finding_ids)
        )

        if context["schedule_name"]:
            print(
                f"   Schedule       : "
                f"{context['schedule_name']}"
            )

        if context["drawing_name"]:
            print(
                f"   Drawing        : "
                f"{context['drawing_name']}"
            )

        if context["page"]:
            print(
                f"   Page           : "
                f"{context['page']}"
            )

        if context["channel"]:
            print(
                f"   Channel        : "
                f"{context['channel']}"
            )

        if context["function_object_id"]:
            print(
                f"   Object ID      : "
                f"{context['function_object_id']}"
            )

        print(
            f"   Auto-fix       : "
            f"{'YES' if issue.safe_to_auto_fix else 'NO'}"
        )

        if issue.suggested_correct_name:
            print(
                f"   Correct name   : "
                f"{issue.suggested_correct_name}"
            )

        print(f"   Summary        : {issue.summary}")
        print(f"   Action         : {issue.recommended_action}")
        print(f"   Reason         : {issue.reason}")
        print(
            f"   Pair reasoning : "
            f"{item['relationship_reason']}"
        )


# ============================================================
# JSON output
# ============================================================

def build_export_payload(
    findings: list[dict],
    groups: list[dict],
    result: AnalysisResult,
) -> dict:
    final_issues = flatten_issues(
        result=result,
        groups=groups,
    )

    exported_issues: list[dict] = []

    for item in final_issues:
        issue: IssueRecommendation = item["issue"]

        exported_issues.append(
            {
                "group_id": item["group_id"],
                "relationship": item["relationship"],
                "relationship_confidence": item["relationship_confidence"],
                "relationship_reason": item["relationship_reason"],
                "classification": issue.classification,
                "source_finding_ids": issue.source_finding_ids,
                "safe_to_auto_fix": issue.safe_to_auto_fix,
                "suggested_correct_name": issue.suggested_correct_name,
                "summary": issue.summary,
                "recommended_action": issue.recommended_action,
                "reason": issue.reason,
                **item["context"],
            }
        )

    return {
        "summary": {
            "raw_findings": len(findings),
            "candidate_groups": len(groups),
            "final_actionable_issues": len(exported_issues),
            "safe_auto_fixes": sum(
                1
                for issue in exported_issues
                if issue["safe_to_auto_fix"]
            ),
            "manual_or_review_issues": sum(
                1
                for issue in exported_issues
                if not issue["safe_to_auto_fix"]
            ),
            "split_candidate_pairs": sum(
                1
                for group_result in result.groups
                if group_result.relationship == "UNRELATED"
            ),
        },
        "group_analysis": result.model_dump(),
        "actionable_issues": exported_issues,
    }


def save_json(
    findings: list[dict],
    groups: list[dict],
    result: AnalysisResult,
    output_file: Path = OUTPUT_FILE,
) -> None:
    payload = build_export_payload(
        findings=findings,
        groups=groups,
        result=result,
    )

    output_file.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ============================================================
# Main
# ============================================================

def main() -> None:
    if not REPORT_FILE.exists():
        raise FileNotFoundError(
            f"{REPORT_FILE} was not found.\n"
            "Paste the checker output into report.txt "
            "and run the script again."
        )

    report_text = REPORT_FILE.read_text(
        encoding="utf-8-sig"
    )

    findings, groups, result = analyze_report(
        report_text
    )

    print_result(
        result=result,
        groups=groups,
    )

    save_json(
        findings=findings,
        groups=groups,
        result=result,
    )

    print()
    print(
        f"Structured result written to: "
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
