from worker.workflows.phrase_qa_completed_check import (
    build_mistral_prompt,
    chunked,
    completed_parts,
    is_completed_pretranslation_event,
    parse_violations,
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
