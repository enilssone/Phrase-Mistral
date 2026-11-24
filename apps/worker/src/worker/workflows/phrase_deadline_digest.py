"""Phrase deadline digest (Taalcentrum-VU).

Geplande workflow (schedule, dagelijks 09:00 Europe/Amsterdam) die:

1. Alle niet-gearchiveerde Phrase TMS-projecten ophaalt via de
   Phrase-connector (workspace credential, run_as="deployment").
2. De projecten filtert waarvan de deadline (dateDue, omgerekend naar
   Europe/Amsterdam) op de dag van uitvoering valt.
3. Een overzicht post in het Teams-kanaal "Phrase updates" via een
   Incoming Webhook (workspace secret TEAMS_WEBHOOK_URL).

Input (optioneel, gebruikt door de schedule en handmatige runs):
    {}  of  {"date": "2026-11-24", "dry_run": true}

- ``date``: overschrijft de "vandaag" waarop gefilterd wordt (ISO-datum).
- ``dry_run``: bericht bouwen maar niet posten (voor smoke-tests).
"""

import os
from datetime import datetime, timezone
from typing import Any, Optional

# Module-level imports van mistralai-workflows zijn OK (geen urllib-keten);
# alle httpx-/zoneinfo-/env-IO staat BINNEN activities (Temporal-sandbox).
import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

phrase_connector = connector("phrase_mistral_768b", run_as="deployment")

CHANNEL_LABEL = "Phrase updates"


# ---------------------------------------------------------------------------
# Pure helpers (unit-testbaar, geen IO)
# ---------------------------------------------------------------------------

def parse_date_due(value: Any) -> Optional[datetime]:
    """Parse een Phrase dateDue ("2026-11-24T16:00:00+0000") naar UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def select_deadline_projects(projects: list, today: str) -> list:
    """Niet-gearchiveerde projecten waarvan dateDue (Amsterdam) op today valt."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Amsterdam")
    matches = []
    for project in projects or []:
        if not isinstance(project, dict) or project.get("archived"):
            continue
        due = parse_date_due(project.get("dateDue"))
        if due is None:
            continue
        if due.astimezone(timezone.utc).astimezone(tz).date().isoformat() == today:
            matches.append(project)
    return matches


def _owner_name(project: dict) -> str:
    owner = project.get("owner") or {}
    name = f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
    return name or "-"


def _progress_text(project: dict) -> str:
    progress = project.get("progress") or {}
    total = progress.get("totalCount", 0)
    finished = progress.get("finishedCount", 0)
    return f"{finished}/{total} jobs afgerond"


def _due_time_text(project: dict) -> str:
    from zoneinfo import ZoneInfo

    due = parse_date_due(project.get("dateDue"))
    if due is None:
        return "?"
    return due.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%H:%M")


def weekday_nl(date_iso: str) -> str:
    """Nederlandse weekdagnaam voor een ISO-datum (2026-11-24)."""
    names = [
        "maandag", "dinsdag", "woensdag", "donderdag", "vrijdag",
        "zaterdag", "zondag",
    ]
    try:
        d = datetime.fromisoformat(date_iso)
    except (TypeError, ValueError):
        return date_iso
    return names[d.weekday()]


def format_message(matches: list, today: str) -> Optional[dict]:
    """Bouw het Teams-bericht. None/lege dict als er geen matches zijn."""
    if not matches:
        return None
    lines = []
    for index, project in enumerate(
        sorted(matches, key=lambda p: _due_time_text(p) or ""), start=1
    ):
        client = (project.get("client") or {}).get("name") or "-"
        targets = ", ".join(project.get("targetLangs") or [])
        lines.append(
            f"{index}. {project.get('name', '?')} - {client} - "
            f"deadline {_due_time_text(project)} - doeltaal {targets or '?'} - "
            f"status: {project.get('status', '?')} ({_progress_text(project)}) - "
            f"projectmanager: {_owner_name(project)}"
        )
    subject = f"Phrase-projecten met deadline {weekday_nl(today)} {today}"
    content = (
        f"Overzicht van niet-gearchiveerde Phrase-projecten met deadline "
        f"{weekday_nl(today)} {today} (tijden lokale tijd):\n\n" + "\n".join(lines)
    )
    return {"subject": subject, "content": content}


# ---------------------------------------------------------------------------
# Activities (side effects live hier)
# ---------------------------------------------------------------------------

@workflows.activity(name="digest-resolve-today")
async def digest_resolve_today(payload: Optional[dict]) -> str:
    """ISO-datum van vandaag in Europe/Amsterdam (of de override uit de input)."""
    from zoneinfo import ZoneInfo

    if isinstance(payload, dict) and payload.get("date"):
        return str(payload["date"])
    return datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()


def _unwrap_projects_tool_result(resp: Any) -> Optional[dict]:
    """Normaliseer de mogelijke vormen van het tool-resultaat naar een page-dict."""
    if isinstance(resp, dict):
        if isinstance(resp.get("response"), dict):
            return resp["response"]
        if "content" in resp and "totalPages" in resp:
            return resp
        if "content" in resp and "pageNumber" in resp:
            return resp
    if isinstance(resp, list) and resp and isinstance(resp[0], dict):
        # resource_link-vorm: [(...)] - kan hier niet gevolgd worden, overslaan.
        return None
    if isinstance(resp, dict):
        return resp
    return None


@workflows.activity(name="digest-fetch-projects")
async def digest_fetch_projects(
    phrase: ToolCallClient = Depends(phrase_connector),
) -> list:
    """Alle niet-gearchiveerde projecten via GET /api2/v1/projects (gepagineerd)."""
    projects: list = []
    page = 0
    total_pages = 1
    first_error: Optional[str] = None
    while page < max(total_pages, 1):
        resp = await phrase.call_tool(
            tool_name="tms_read_api",
            arguments={
                "method": "GET",
                "path": "/api2/v1/projects",
                "query_params": {"pageNumber": page, "pageSize": 50},
            },
        )
        page_data = _unwrap_projects_tool_result(resp)
        if page_data is None and page == 0:
            first_error = f"onverwachte tool-vorm: {str(resp)[:300]}"
            break
        if page_data is None:
            break
        total_pages = int(page_data.get("totalPages") or 1)
        projects.extend(page_data.get("content") or [])
        page += 1
        if page >= total_pages:
            break
    if first_error:
        raise RuntimeError(f"Phrase API-resultaat kon niet gelezen worden: {first_error}")
    return projects


@workflows.activity(name="digest-build-message")
async def digest_build_message(projects: list, today: str) -> Optional[dict]:
    return format_message(select_deadline_projects(projects, today), today)


@workflows.activity(name="digest-post-teams")
async def digest_post_teams(message: dict) -> str:
    """Post het bericht via de Incoming Webhook (secret TEAMS_WEBHOOK_URL)."""
    import httpx

    url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not url:
        raise RuntimeError("TEAMS_WEBHOOK_URL is niet gezet (workspace secret ontbreekt?)")
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            url,
            json={"title": message["subject"], "text": message["content"]},
        )
        resp.raise_for_status()
    return f"geposted: {message['subject']}"


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@workflows.workflow.define(
    name="phrase-deadline-digest",
    workflow_display_name="Phrase deadline-overzicht (dagelijks 09:00)",
    workflow_description=(
        "Post dagelijks (09:00 Europe/Amsterdam) een overzicht van "
        "Phrase-projecten met die dag als deadline in de Teams-kanalen "
        "'Phrase updates'."
    ),
)
@uses_connectors(phrase_connector)
class PhraseDeadlineDigestWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, payload: Optional[dict] = None) -> str:
        today = await digest_resolve_today(payload)
        projects = await digest_fetch_projects()
        message = await digest_build_message(projects, today)
        if isinstance(payload, dict) and payload.get("dry_run"):
            return f"dry-run: {message if message else 'geen deadlines vandaag'}"
        if not message:
            return f"geen projecten met deadline {today}"
        return await digest_post_teams(message)
