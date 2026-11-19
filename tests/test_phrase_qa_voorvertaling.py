from worker.workflows.phrase_qa_voorvertaling import (
    apply_fixes,
    extract_preferred_terms,
    is_relevant_event,
)


def test_relevant_event():
    assert is_relevant_event({"event": "JOB_STATUS_CHANGED", "jobParts": [{"status": "COMPLETED", "workflowLevel": 1}]})
    assert not is_relevant_event({"event": "PROJECT_CREATED"})


def test_apply_en_gb():
    fixed, reasons = apply_fixes("The center will organize the event.", [], "en_gb")
    assert "centre" in fixed and "organise" in fixed
    assert reasons


def test_ut_no_article():
    fixed, reasons = apply_fixes("Study at the UT campus.", [], "en_gb")
    assert "the UT" not in fixed
    assert any("UT" in r for r in reasons)


def test_no_false_positive():
    fixed, reasons = apply_fixes("The University of Twente is welcoming.", [], "en_gb")
    assert fixed == "The University of Twente is welcoming."
    assert reasons == []


def test_term_correction():
    fixed, reasons = apply_fixes("He works in the organisation.", ["organization"], "en_gb")
    assert "organization" in fixed
    assert reasons


def test_extract_preferred_terms():
    assert extract_preferred_terms(["help desk"]) == ["help desk"]
    assert extract_preferred_terms([{"terms": [{"term": "help desk", "language": "en"}]}]) == ["help desk"]
    assert extract_preferred_terms([]) == []
