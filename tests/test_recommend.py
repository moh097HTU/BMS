"""
Offline tests for the AI review layer (core/recommend.py).

Nothing here touches the network: the adapter, the candidate grouping, the
deterministic validator, and the best-effort fallback in api/jobs.py are all
pure or key-gated. The one thing that WOULD call Gemini (analyze_groups_with_
gemini) is exercised only through its no-key guard.

Run:  .venv/Scripts/python -m pytest tests/test_recommend.py -q
      .venv/Scripts/python tests/test_recommend.py     (no pytest needed)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.recommend import (                                    # noqa: E402
    AnalysisResult,
    build_candidate_groups,
    records_from_findings,
    validate_analysis_result,
)


def _findings():
    """A tiny findings queue in core/findings.py shape: one typo-style pair and
    one unpaired extra."""
    return [
        {
            "id": "missing-0006", "category": "MISSING_POINT",
            "title": "Access Control Panel On/Off Status",
            "expected": 1, "found": 0, "page_name": None,
            "evidence": [], "detail": {},
            "likely_pair": {"id": "extra-0001",
                            "title": "Access Control Panel On/Of Status",
                            "similarity": 0.9877, "certified": False, "note": "x"},
        },
        {
            "id": "extra-0001", "category": "EXTRA_INSTANCE",
            "title": "Access Control Panel On/Of Status",
            "expected": 0, "found": 1, "page_name": None,
            "evidence": [{"function_object_id": "F123", "page_name": "P-01",
                          "channel_raw": "DI3"}],
            "detail": {},
            "likely_pair": {"id": "missing-0006",
                            "title": "Access Control Panel On/Off Status",
                            "similarity": 0.9877, "certified": False, "note": "x"},
        },
        {
            "id": "extra-0009", "category": "EXTRA_INSTANCE",
            "title": "Standalone Extra Point", "expected": 0, "found": 1,
            "page_name": None,
            "evidence": [{"function_object_id": "F999", "page_name": "P-09",
                          "channel_raw": "DO1"}],
            "detail": {},
        },
    ]


def _pair_result(missing_id, extra_id, group_id):
    return AnalysisResult.model_validate({
        "groups": [{
            "group_id": group_id,
            "relationship": "SAME_POINT_TYPO",
            "confidence": 0.95,
            "relationship_reason": "One dropped 'f' - clearly the same point.",
            "issues": [{
                "classification": "TYPO_OR_NAMING_ERROR",
                "source_finding_ids": [missing_id, extra_id],
                "summary": "Typo On/Of -> On/Off.",
                "recommended_action": "Rename the drawing point to On/Off.",
                "suggested_correct_name": "Access Control Panel On/Off Status",
                "safe_to_auto_fix": True,
                "reason": "Single obvious character error.",
            }],
        }, {
            "group_id": "issue-0002",
            "relationship": "STANDALONE",
            "confidence": 0.9,
            "relationship_reason": "No look-alike schedule point.",
            "issues": [{
                "classification": "ACTUAL_EXTRA",
                "source_finding_ids": [extra_id_standalone := "extra-0009"],
                "summary": "Drawing has a point the schedule does not.",
                "recommended_action": "Confirm whether to add it to the schedule.",
                "safe_to_auto_fix": False,
                "reason": "Genuinely extra.",
            }],
        }],
    })


def test_records_from_findings_maps_pair_and_evidence():
    records = records_from_findings(_findings())
    by_id = {r["id"]: r for r in records}

    assert by_id["missing-0006"]["type"] == "MISSING_POINT"
    assert by_id["missing-0006"]["name"] == "Access Control Panel On/Off Status"
    assert by_id["missing-0006"]["likely_pair_id"] == "extra-0001"
    assert by_id["missing-0006"]["similarity"] == 0.9877
    # page/channel/object come out of evidence[0]
    assert by_id["extra-0001"]["channel"] == "DI3"
    assert by_id["extra-0001"]["function_object_id"] == "F123"
    assert by_id["extra-0001"]["page_name"] == "P-01"
    print("PASS  records_from_findings maps pair + evidence")


def test_build_candidate_groups_pairs_and_standalone():
    groups = build_candidate_groups(records_from_findings(_findings()))
    kinds = [g["kind"] for g in groups]

    assert kinds.count("CANDIDATE_MISSING_EXTRA_PAIR") == 1
    assert kinds.count("STANDALONE") == 1

    pair = next(g for g in groups if g["kind"] == "CANDIDATE_MISSING_EXTRA_PAIR")
    assert pair["missing"]["id"] == "missing-0006"
    assert pair["extra"]["id"] == "extra-0001"
    assert pair["matcher"]["reciprocal_pair"] is True

    standalone = next(g for g in groups if g["kind"] == "STANDALONE")
    assert standalone["finding"]["id"] == "extra-0009"
    print("PASS  build_candidate_groups pairs + standalone")


def test_validate_accepts_wellformed_and_rejects_bad():
    groups = build_candidate_groups(records_from_findings(_findings()))
    pair_group = next(g for g in groups
                      if g["kind"] == "CANDIDATE_MISSING_EXTRA_PAIR")
    result = _pair_result(pair_group["missing"]["id"],
                          pair_group["extra"]["id"], pair_group["group_id"])

    # well-formed: no exception
    validate_analysis_result(result, groups)

    # break it: a typo issue must reference BOTH ids
    bad = result.model_copy(deep=True)
    bad.groups[0].issues[0].source_finding_ids = [pair_group["missing"]["id"]]
    try:
        validate_analysis_result(bad, groups)
    except ValueError:
        print("PASS  validate accepts good, rejects bad")
    else:
        raise AssertionError("validator accepted a malformed result")


def test_attach_ai_review_degrades_without_key():
    """api/jobs.py must never fail a job on the AI step: no key -> unavailable."""
    from api.jobs import _attach_ai_review

    # Set empty (not popped): load_dotenv won't override an existing var, so this
    # keeps the test hermetic even when a real key sits in the project .env.
    saved = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = ""
    try:
        findings = {"summary": {}, "findings": _findings()}
        _attach_ai_review("test-job", findings)     # must not raise
        assert findings["summary"]["ai_status"] == "unavailable"
        assert "recommendations" not in findings    # findings untouched
        # empty queue is 'skipped', still no crash
        empty = {"summary": {}, "findings": []}
        _attach_ai_review("test-job", empty)
        assert empty["summary"]["ai_status"] == "skipped"
        print("PASS  _attach_ai_review degrades gracefully without a key")
    finally:
        if saved is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = saved


def run_all():
    test_records_from_findings_maps_pair_and_evidence()
    test_build_candidate_groups_pairs_and_standalone()
    test_validate_accepts_wellformed_and_rejects_bad()
    test_attach_ai_review_degrades_without_key()
    print("\nAll recommend tests passed.")


if __name__ == "__main__":
    run_all()
