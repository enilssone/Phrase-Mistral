"""Tests voor phrase_deadline_digest (pure helpers, met SDK-stub).

Deze tests importeren de workflow-module via een stub van
mistralai.workflows zodat ze ook draaien zonder het volledige SDK
geïnstalleerd (bijv. in een kale CI/sandbox). De pure helpers zijn
SDK-onafhankelijk; de stub zorgt dat de module-import lukt.
"""

import importlib.util
import sys
import types
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / (
    "apps/worker/src/worker/workflows/phrase_deadline_digest.py"
)


def _install_sdk_stub() -> None:
    if "mistralai" in sys.modules:
        return

    workflows_ns = types.ModuleType("mistralai.workflows")

    def _activity(name):
        def decorator(fn):
            return fn
        return decorator

    class _wf:
        @staticmethod
        def define(**kwargs):
            def decorator(cls):
                return cls
            return decorator

        @staticmethod
        def entrypoint(fn):
            return fn

    workflows_ns.activity = _activity
    workflows_ns.workflow = _wf
    workflows_ns.Depends = lambda dep: dep
    workflows_ns.discover_all_workflows_in_package = lambda pkg: []

    mistralai_ns = types.ModuleType("mistralai")
    mistralai_ns.workflows = workflows_ns

    connectors_ns = types.ModuleType(
        "mistralai.workflows.plugins.mistralai.connectors"
    )
    connectors_ns.connector = lambda name, run_as=None: name
    connectors_ns.uses_connectors = lambda *conns: (
        (lambda cls: cls) if conns else (lambda cls: cls)
    )
    connectors_ns.ToolCallClient = object

    triggers_ns = types.ModuleType("mistralai.workflows.plugins.triggers")
    triggers_ns.TriggerEvent = object

    plugin_mistralai_ns = types.ModuleType(
        "mistralai.workflows.plugins.mistralai"
    )
    plugin_mistralai_ns.connectors = connectors_ns
    plugins_ns = types.ModuleType("mistralai.workflows.plugins")
    plugins_ns.mistralai = plugin_mistralai_ns
    plugins_ns.triggers = triggers_ns
    workflows_ns.plugins = plugins_ns

    mistralai_ns.workflows = workflows_ns
    sys.modules["mistralai"] = mistralai_ns
    sys.modules["mistralai.workflows"] = workflows_ns
    sys.modules["mistralai.workflows.plugins"] = plugins_ns
    sys.modules["mistralai.workflows.plugins.mistralai"] = plugin_mistralai_ns
    sys.modules["mistralai.workflows.plugins.mistralai.connectors"] = connectors_ns
    sys.modules["mistralai.workflows.plugins.triggers"] = triggers_ns


def _load_module():
    _install_sdk_stub()
    spec = importlib.util.spec_from_file_location(
        "phrase_deadline_digest", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["phrase_deadline_digest"] = module
    spec.loader.exec_module(module)
    return module


digest = _load_module()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECT_DUE_TODAY = {
    "name": "2026/2656 nieuwsbericht PFAS - Geneesmiddelen",
    "dateDue": "2026-11-24T17:00:00+0000",
    "archived": False,
    "status": "NEW",
    "client": {"name": "RIVM"},
    "owner": {"firstName": "Vivian", "lastName": "van Klaarbergen"},
    "targetLangs": ["en_gb"],
    "progress": {"totalCount": 2, "finishedCount": 0},
}

PROJECT_DUE_TODAY_LATE_UTC = {
    "name": "Late UTC deadline",
    # 23:30Z op 24-11 is 00:30 op 25-11 in Amsterdam: niet vandaag.
    "dateDue": "2026-11-24T23:30:00+0000",
    "archived": False,
    "status": "NEW",
    "client": {"name": "X"},
    "targetLangs": ["de_de"],
    "progress": {"totalCount": 1, "finishedCount": 0},
}

PROJECT_DUE_TOMORROW = {
    "name": "NIFP - boekvertaling persoon van de verdachte PCB",
    "dateDue": "2026-11-25T16:00:00+0000",
    "archived": False,
    "status": "ASSIGNED",
    "client": {"name": "NIFP"},
    "targetLangs": ["en_gb"],
    "progress": {"totalCount": 26, "finishedCount": 18},
}

PROJECT_ARCHIVED = {
    "name": "Archiefproject",
    "dateDue": "2026-11-24T12:00:00+0000",
    "archived": True,
    "client": {"name": "Y"},
}

PROJECT_NO_DUE = {
    "name": "Zonder deadline",
    "dateDue": None,
    "archived": False,
    "client": {"name": "Z"},
}


# ---------------------------------------------------------------------------
# parse_date_due
# ---------------------------------------------------------------------------

def test_parse_date_due_basic():
    dt = digest.parse_date_due("2026-11-24T16:00:00+0000")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 11 and dt.day == 24


def test_parse_date_due_invalid():
    assert digest.parse_date_due(None) is None
    assert digest.parse_date_due("") is None
    assert digest.parse_date_due("geen datum") is None


# ---------------------------------------------------------------------------
# select_deadline_projects
# ---------------------------------------------------------------------------

def test_select_deadline_projects_filters():
    projects = [
        PROJECT_DUE_TODAY,
        PROJECT_DUE_TODAY_LATE_UTC,
        PROJECT_DUE_TOMORROW,
        PROJECT_ARCHIVED,
        PROJECT_NO_DUE,
    ]
    matches = digest.select_deadline_projects(projects, "2026-11-24")
    assert [p["name"] for p in matches] == [PROJECT_DUE_TODAY["name"]]


def test_select_deadline_projects_amsterdam_boundary():
    # 23:30Z op 24-11 = 00:30 op 25-11 Amsterdam -> deadline valt op de 25e.
    matches = digest.select_deadline_projects(
        [PROJECT_DUE_TODAY_LATE_UTC], "2026-11-25"
    )
    assert len(matches) == 1
    assert digest.select_deadline_projects(
        [PROJECT_DUE_TODAY_LATE_UTC], "2026-11-24"
    ) == []


def test_select_deadline_projects_empty_input():
    assert digest.select_deadline_projects([], "2026-11-24") == []
    assert digest.select_deadline_projects(None, "2026-11-24") == []


# ---------------------------------------------------------------------------
# format_message
# ---------------------------------------------------------------------------

def test_format_message_content():
    msg = digest.format_message([PROJECT_DUE_TODAY, PROJECT_DUE_TOMORROW], "2026-11-24")
    assert msg is not None
    assert msg["subject"] == "Phrase-projecten met deadline dinsdag 2026-11-24"
    assert "Overzicht van niet-gearchiveerde Phrase-projecten" in msg["content"]
    assert "RIVM" in msg["content"]
    assert "Vivian van Klaarbergen" in msg["content"]
    assert "0/2 jobs afgerond" in msg["content"]


def test_format_message_no_matches():
    assert digest.format_message([], "2026-11-24") is None


def test_format_message_sorts_by_deadline_time():
    later = dict(PROJECT_DUE_TODAY, name="Later", dateDue="2026-11-24T17:00:00+0000")
    earlier = dict(PROJECT_DUE_TOMORROW, name="Eerder",
                   dateDue="2026-11-24T12:00:00+0000")
    msg = digest.format_message([later, earlier], "2026-11-24")
    assert msg["content"].index("Eerder") < msg["content"].index("Later")


# ---------------------------------------------------------------------------
# weekday_nl
# ---------------------------------------------------------------------------

def test_weekday_nl():
    assert digest.weekday_nl("2026-11-24") == "dinsdag"
    assert digest.weekday_nl("2026-11-21") == "zaterdag"
    assert digest.weekday_nl("geen datum") == "geen datum"


# ---------------------------------------------------------------------------
# _unwrap_projects_tool_result
# ---------------------------------------------------------------------------

def test_unwrap_tool_result_response_wrapper():
    resp = {"response": {"totalPages": 2, "content": [{"a": 1}]}}
    assert digest._unwrap_projects_tool_result(resp)["content"] == [{"a": 1}]


def test_unwrap_tool_result_plain_page():
    resp = {"totalPages": 1, "content": [{"b": 2}]}
    assert digest._unwrap_projects_tool_result(resp)["content"] == [{"b": 2}]


def test_unwrap_tool_result_unexpected():
    assert digest._unwrap_projects_tool_result(["loose"]) is None
    assert digest._unwrap_projects_tool_result(None) is None
