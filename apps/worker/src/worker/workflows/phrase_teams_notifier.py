"""Phrase TMS -> Teams notifier.

Starts from a Mistral webhook trigger (custom provider = Phrase TMS).
The trigger passes the whole HTTP request to the workflow; the Phrase event
payload is in ``event.body``, e.g.::

    {"event": "PROJECT_CREATED", "project": {...}, "eventUid": ..., "timestamp": ...}
    {"event": "JOB_CREATED", "project": {...}, "jobParts": [...], ...}

For PROJECT_CREATED the workflow optionally enriches the message with live
Phrase API data (workflow steps, MT engine, created jobs) when the
``PHRASE_API_TOKEN`` secret is bound to the deployment.
"""

import os
from typing import Any, Optional

import httpx

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)
from mistralai.workflows.plugins.triggers import TriggerEvent

teams_connector = connector("teams")

TEAM_ID = os.environ.get("TEAMS_TEAM_ID", "60f99958-3d93-415c-a2ff-7e62f5da25a2")
CHANNEL_ID = os.environ.get(
    "TEAMS_CHANNEL_ID", "19:f12365030ece4194a88701a4efcb7b0b@thread.tacv2"
)
PHRASE_API_BASE = os.environ.get(
    "PHRASE_API_BASE", "https://api.phrase.com"
).rstrip("/")
PHRASE_API_TOKEN = os.environ.get("PHRASE_API_TOKEN", "")

SUPPORTED_EVENTS = {"PROJECT_CREATED", "JOB_CREATED", "JOB_STATUS_CHANGED"}


# ---------------------------------------------------------------------------
# Pure formatting helpers (unit-tested)
# ---------------------------------------------------------------------------

def _fmt_dt(value: Any) -> str:
    return str(value) if value else "-"


def _owner_name(project: dict) -> str:
    owner = project.get("owner") or {}
    name = f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
    if owner.get("userName"):
        name = f"{name} ({owner['userName']})" if name else owner["userName"]
    return name or "-"


def _workflow_steps_text(project: dict, details: dict) -> str:
    steps = (details.get("workflowSteps") or []) or (project.get("workflowSteps") or [])
    if not steps:
        return "-"
    steps = sorted(steps, key=lambda s: s.get("workflowLevel", 0))
    return " → ".join(
        f"{s.get('workflowLevel')}) {s.get('name')} ({s.get('abbreviation')})" for s in steps
    )


def _mt_engine_text(project: dict, details: dict) -> str:
    mt = details.get("mtSettings")
    if isinstance(mt, dict) and mt.get("machineTranslateSettings"):
        m = mt["machineTranslateSettings"]
        return f"{m.get('name', '?')} ({m.get('type', '?')})"
    lst = project.get("mtSettingsPerLanguageList") or []
    if lst:
        names = sorted(
            {m.get("machineTranslateSettings", {}).get("name", "?") for m in lst}
        )
        return ", ".join(names)
    return "geen MT"


def _jobs_text(project: dict, details: dict, payload: dict) -> str:
    jobs = details.get("jobs")
    if jobs is None:
        jobs = payload.get("jobParts") or []
    if not jobs:
        return "nog geen jobs"
    lines = []
    for j in jobs:
        step = (j.get("workflowStep") or {}).get("name", "?")
        assigned = ", ".join(
            f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
            for p in j.get("providers") or []
        )
        line = (
            f"• {j.get('filename', '?')} — stap: {step}, taal: {j.get('targetLang', '?')}, "
            f"status: {j.get('status', '?')}, aangemaakt: {_fmt_dt(j.get('dateCreated'))}"
        )
        if assigned:
            line += f", toegewezen: {assigned}"
        lines.append(line)
    return "\n".join(lines)


def format_message(payload: dict, details: dict) -> Optional[dict]:
    """Build the Teams message for a Phrase event. Returns None to skip posting."""
    event_name = payload.get("event", "")
    if event_name not in SUPPORTED_EVENTS:
        return None
    project = payload.get("project") or {}
    pname = project.get("name", "?")
    pid = project.get("internalId", project.get("uid", "?"))
    client = (project.get("client") or {}).get("name") or "-"
    langs = f"{project.get('sourceLang', '?')} → {', '.join(project.get('targetLangs') or [])}"

    if event_name == "PROJECT_CREATED":
        subject = f"🎉 Nieuw Phrase-project: {pname}"
        content = (
            f"🎉 **Nieuw project aangemaakt in Phrase TMS**\n\n"
            f"📄 **Project:** {pname}\n"
            f"🆔 **ID:** {pid} (uid: {project.get('uid', '?')})\n"
            f"👤 **Owner:** {_owner_name(project)}\n"
            f"🏢 **Klant:** {client}\n"
            f"🌐 **Talen:** {langs}\n"
            f"📅 **Aangemaakt:** {_fmt_dt(project.get('dateCreated'))} · "
            f"Deadline: {_fmt_dt(project.get('dateDue'))}\n"
            f"⚙️ **Status:** {project.get('status', '?')}\n"
            f"🔁 **Workflow stappen:** {_workflow_steps_text(project, details)}\n"
            f"🤖 **MT engine:** {_mt_engine_text(project, details)}\n"
            f"📦 **Jobs aangemaakt:**\n{_jobs_text(project, details, payload)}"
        )
    elif event_name == "JOB_CREATED":
        subject = f"🔔 Nieuwe job(s) in Phrase: {pname}"
        content = (
            f"🔔 **Nieuwe job aangemaakt in Phrase TMS**\n\n"
            f"📄 **Project:** {pname} (ID {pid})\n"
            f"👤 **Owner:** {_owner_name(project)}\n"
            f"📦 **Jobs:**\n{_jobs_text(project, details, payload)}"
        )
    else:  # JOB_STATUS_CHANGED
        subject = f"🔁 Jobstatus gewijzigd: {pname}"
        content = (
            f"🔁 **Jobstatus gewijzigd in Phrase TMS**\n\n"
            f"📄 **Project:** {pname} (ID {pid})\n"
            f"📦 **Jobs:**\n{_jobs_text(project, details, payload)}"
        )
    return {"subject": subject, "content": content}


# ---------------------------------------------------------------------------
# Activities (side effects live here)
# ---------------------------------------------------------------------------

@workflows.activity(name="extract-event")
async def extract_event(event: TriggerEvent) -> dict:
    body = event.body
    return body if isinstance(body, dict) else {}


@workflows.activity(name="enrich-project-details")
async def enrich_project_details(payload: dict) -> dict:
    """Fetch live project details (workflow steps, MT settings, jobs) from Phrase."""
    if not PHRASE_API_TOKEN:
        return {}
    project = payload.get("project") or {}
    uid = project.get("uid")
    if not uid:
        return {}
    headers = {"Authorization": f"ApiToken {PHRASE_API_TOKEN}", "Accept": "application/json"}
    details: dict = {}
    async with httpx.AsyncClient(timeout=30) as http:
        for key, path in {
            "project": f"/api2/v1/projects/{uid}",
            "workflowSteps": f"/api2/v2/projects/{uid}/workflowSteps",
            "mtSettings": f"/api2/v1/projects/{uid}/mtSettings",
        }.items():
            try:
                resp = await http.get(PHRASE_API_BASE + path, headers=headers)
                resp.raise_for_status()
                details[key] = resp.json()
            except Exception:
                details[key] = None
        jobs: list = []
        for level in (1, 2, 3, 4):
            try:
                resp = await http.get(
                    f"{PHRASE_API_BASE}/api2/v2/projects/{uid}/jobs",
                    headers=headers,
                    params={"pageSize": 50, "workflowLevel": level},
                )
                resp.raise_for_status()
                jobs.extend(resp.json().get("content", []))
            except Exception:
                pass
        details["jobs"] = jobs
    return details


@workflows.activity(name="build-teams-message")
async def build_teams_message(payload: dict, details: dict) -> Optional[dict]:
    return format_message(payload, details)


@workflows.activity(name="post-to-teams")
async def post_to_teams(
    message: dict,
    teams: ToolCallClient = Depends(teams_connector),
) -> str:
    resp = await teams.call_tool(
        tool_name="teams_post_channel_message",
        arguments={
            "team_id": TEAM_ID,
            "channel_id": CHANNEL_ID,
            "subject": message["subject"],
            "content": message["content"],
        },
    )
    return str(resp)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@workflows.workflow.define(
    name="phrase-teams-notifier",
    workflow_display_name="Phrase → Teams notifier",
    workflow_description=(
        "Post Phrase TMS events (PROJECT_CREATED, JOB_CREATED, JOB_STATUS_CHANGED) "
        "as messages to the Teams channel 'Phrase updates'."
    ),
)
@uses_connectors(teams_connector)
class PhraseTeamsNotifierWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, event: TriggerEvent) -> str:
        payload = await extract_event(event)
        details = await enrich_project_details(payload)
        message = await build_teams_message(payload, details)
        if message is None:
            return "ignored: unsupported event"
        return await post_to_teams(message)
