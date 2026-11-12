"""Tests for the phrase-teams-notifier workflow (pure parts + registration)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "worker", "src"))

import mistralai.workflows as workflows  # noqa: E402
from worker.workflows import phrase_teams_notifier as mod  # noqa: E402

PROJECT = {
    "uid": "abc123",
    "internalId": 323,
    "name": "2026/2559: Valpreventie",
    "owner": {"firstName": "Vivian", "lastName": "van Klaarbergen", "userName": "vvanklaarbergen_1"},
    "client": {"name": "RIVM"},
    "sourceLang": "nl_nl",
    "targetLangs": ["en_gb"],
    "status": "NEW",
    "dateCreated": "2026-11-10T09:13:28+0000",
    "dateDue": "2026-11-12T17:00:00+0000",
    "mtSettingsPerLanguageList": [],
    "workflowSteps": [
        {"workflowLevel": 1, "name": "Translation", "abbreviation": "T"},
        {"workflowLevel": 2, "name": "Revision", "abbreviation": "R"},
    ],
}


def test_workflow_is_discovered():
    classes = workflows.discover_all_workflows_in_package("worker.workflows")
    names = {c.__name__ for c in classes}
    assert "PhraseTeamsNotifierWorkflow" in names


def test_project_created_message_contains_all_fields():
    payload = {"event": "PROJECT_CREATED", "project": PROJECT, "timestamp": 1794492641}
    msg = mod.format_message(payload, {"jobs": [
        {"filename": "bestand.docx", "workflowStep": {"name": "Translation"},
         "targetLang": "en_gb", "status": "NEW", "dateCreated": "2026-11-10T09:13:58+0000",
         "providers": [{"firstName": "Anna", "lastName": "Jaffe"}]},
    ]})
    assert msg is not None
    for needle in ["2026/2559", "323", "Vivian van Klaarbergen", "RIVM", "nl_nl → en_gb",
                   "Translation", "Revision", "geen MT", "bestand.docx", "Anna Jaffe"]:
        assert needle in msg["content"], needle
    assert "Nieuw Phrase-project" in msg["subject"]


def test_job_created_message():
    payload = {"event": "JOB_CREATED", "project": PROJECT,
               "jobParts": [{"filename": "f.docx", "workflowStep": {"name": "Translation"},
                            "targetLang": "en_gb", "status": "NEW"}]}
    msg = mod.format_message(payload, {})
    assert msg is not None
    assert "Nieuwe job" in msg["subject"]
    assert "f.docx" in msg["content"]


def test_unsupported_event_is_skipped():
    assert mod.format_message({"event": "PROJECT_DELETED", "project": PROJECT}, {}) is None


def test_mt_engine_from_details():
    details = {"mtSettings": {"machineTranslateSettings": {"name": "Phrase Language AI", "type": "MEMSOURCE_TRANSLATE_SETTINGS"}}}
    assert "Phrase Language AI" in mod._mt_engine_text(PROJECT, details)
