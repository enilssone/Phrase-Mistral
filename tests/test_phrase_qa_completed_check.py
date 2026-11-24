from datetime import datetime, timedelta, timezone

from worker.workflows.phrase_qa_completed_check import (
    MARKER,
    build_mistral_prompt,
    chunked,
    completed_parts,
    extra_rules_for_lang,
    find_completed_at,
    is_completed_pretranslation_event,
    is_within_window,
    parse_iso_ts,
    parse_violations,
    text_has_marker,
)


def test_relevant_completed_event():
    payload = {
        "event": "JOB_STATUS_CHANGED",
        "jobParts": [{"status": "COMPLETED", "workflowLevel": 1, "uid": "abc"}],
    }
    assert is_completed_pretranslation_event(payload)
    assert completed_parts(payload) == [{"status": "COMPLETED", "workflowLevel": 1, "uid": "abc"}]


def test_ignores_other_events_and_levels():
    assert not is_completed_pretranslation_event({"event": "PROJECT_CREATED"})
    assert not is_completed_pretranslation_event(
        {"event": "JOB_STATUS_CHANGED", "jobParts": [{"status": "COMPLETED", "workflowLevel": 2}]}
    )
    assert not is_completed_pretranslation_event(
        {"event": "JOB_STATUS_CHANGED", "jobParts": [{"status": "NEW", "workflowLevel": 1}]}
    )
    assert not is_completed_pretranslation_event({})


def test_prompt_contains_terms_rules_segments():
    prompt = build_mistral_prompt(
        "en_gb",
        ["help desk"],
        "# Rules\n- Use en-GB spelling",
        [{"id": "1", "source": "Click Help Desk", "target": "Click the helpdesk"}],
    )
    assert "en_gb" in prompt
    assert "help desk" in prompt
    assert "en-GB spelling" in prompt
    assert "1 | Click Help Desk | Click the helpdesk" in prompt
    assert '"violations"' in prompt


def test_parse_violations_plain_json():
    content = '{"violations": [{"segmentId": "7", "check": "terminology", "rule": "help desk", "message": "wrong term", "suggestion": "help desk"}]}'
    v = parse_violations(content)
    assert len(v) == 1
    assert v[0]["segmentId"] == "7"
    assert v[0]["check"] == "terminology"


def test_parse_violations_code_fence_and_prose():
    content = 'Here you go:\n```json\n{"violations": []}\n```'
    assert parse_violations(content) == []
    assert parse_violations("no json at all") == []
    assert parse_violations("") == []
    assert parse_violations('{"violations": "not-a-list"}') == []


def test_parse_violations_skips_malformed_entries():
    content = '{"violations": [{"segmentId": "1", "check": "consistency"}, {"bad": true}, null]}'
    v = parse_violations(content)
    assert len(v) == 1 and v[0]["check"] == "consistency"


def test_chunked():
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunked([], 3) == []


def test_extra_rules_en_gb_oxford_comma():
    rule = extra_rules_for_lang("en_gb")
    assert "British English" in rule
    assert "Oxford comma" in rule
    assert extra_rules_for_lang("en-GB") == rule
    assert extra_rules_for_lang("nl_nl") == ""
    assert extra_rules_for_lang("en_us") == ""


def test_prompt_en_gb_includes_no_oxford_comma():
    prompt = build_mistral_prompt(
        "en_gb", [], "- Some rule",
        [{"id": "1", "source": "Apples, bananas and cherries", "target": "Apples, bananas, and cherries"}],
    )
    assert "NO Oxford comma" in prompt
    assert "Apples, bananas, and cherries" in prompt
    assert '"check": "terminology|consistency|content-group|en-gb"' in prompt


def test_prompt_non_english_has_no_oxford_rule():
    prompt = build_mistral_prompt("nl_nl", [], "", [{"id": "1", "source": "a", "target": "b"}])
    assert "Oxford comma" not in prompt


def test_consistency_check_covers_terminology():
    prompt = build_mistral_prompt("en_gb", ["help desk"], "", [{"id": "1", "source": "a", "target": "b"}])
    assert "identical source text must have identical target text" in prompt
    assert "terminology must be used consistently" in prompt
    assert "same source term must always get the same target term" in prompt


def test_parse_iso_ts():
    assert parse_iso_ts("2026-11-24T10:00:00Z").tzinfo is not None
    assert parse_iso_ts("2026-11-24T10:00:00+00:00") is not None
    assert parse_iso_ts("geen datum") is None
    assert parse_iso_ts(None) is None


def test_find_completed_at_and_window():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    changes = {
        "statusChanges": [
            {"jobUid": "j1", "status": "NEW", "dateCreated": "2026-11-24T09:00:00Z"},
            {"jobUid": "j1", "status": "COMPLETED", "dateCreated": "2026-11-24T11,50".replace(",", ":") + ":00Z"},
            {"jobUid": "j2", "status": "COMPLETED", "dateCreated": "2026-11-24T08:00:00Z"},
        ]
    }
    ts = find_completed_at(changes, "j1")
    assert ts is not None
    assert is_within_window(ts, now, 20)
    ts2 = find_completed_at(changes, "j2")
    assert ts2 is not None and not is_within_window(ts2, now, 20)
    assert find_completed_at(changes, "onbekend") is None
    assert not is_within_window(None, now, 20)


def test_text_has_marker():
    assert text_has_marker(f'comment met {MARKER} erin')
    assert not text_has_marker("gewone comment")
