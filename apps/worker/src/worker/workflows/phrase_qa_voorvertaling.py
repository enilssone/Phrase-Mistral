"""Kwaliteitscheck direct na Phrase-voorvertaling (Taalcentrum-VU).

Volledig via MCP-connectors (geen losse API-token):
- phrase_mistral_768b: Phrase TMS lezen en schrijven (tms_read_api,
  tms_write_api, request_upload_url).
- sharepoint_mcp: Excel-overzicht opslaan in de map "Mistral QA voorvertaling".
- teams: melding in het kanaal "Phrase updates".

Phrase-webhooks kunnen niet rechtstreeks bij Mistral-webhooks komen (Mistral
vereist een HMAC-handtekening, Phrase stuurt die niet). Daarom stuurt de
Phrase-webhook (v2, secretTokenType=AUTHORIZATION, secretToken
"Bearer <Mistral-API-key>") de event-body rechtstreeks naar
POST /v1/workflows/phrase-qa-voorvertaling/execute. De workflow draait dan
on_behalf_of de triggerende user (Else), dus met de user-connectors
(Phrase, SharePoint, Teams). In de run: filter op COMPLETED op workflow
level 1; alleen als het project een contentgroep EN een termbase heeft:
terminologie + stijlregels toetsen, correcties terug naar Phrase
(saveToTransMemory=Confirmed, geen setCompleted, 0 AIU's), Excel naar
SharePoint, melding met @owner in Teams.

De workflow accepteert óók een enkel event-body (dict) voor handmatige tests.
"""

import base64
import os
import re
from typing import Any

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)
from mistralai.workflows.plugins.triggers import TriggerEvent

phrase_connector = connector("phrase_mistral_768b", run_as="auto")
teams_connector = connector("teams", run_as="auto")
sharepoint_connector = connector("sharepoint_mcp", run_as="auto")

SHAREPOINT_DRIVE_ID = os.environ.get(
    "SHAREPOINT_DRIVE_ID",
    "b!wtpK_VfkVUqsUpZb30cZmwFAQxwrHpdHr8faqcHYMPXW8n3X01D9R4grQUSiahnK",
)
SHAREPOINT_FOLDER_ID = os.environ.get(
    "SHAREPOINT_FOLDER_ID", "013PVE4APMLJAUE6EYSBAYKCLKTU67CFZF"
)
TEAMS_TEAM_ID = os.environ.get(
    "TEAMS_TEAM_ID", "60f99958-3d93-415c-a2ff-7e62f5da25a2"
)
TEAMS_CHANNEL_ID = os.environ.get(
    "TEAMS_CHANNEL_ID", "19:f12365030ece4194a88701a4efcb7b0b@thread.tacv2"
)
PHRASE_WEBHOOK_UID = os.environ.get(
    "PHRASE_WEBHOOK_UID", "9c07dcca-a480-4a3d-938c-baea3e24e161"
)
POLL_STATE_ITEM_ID = os.environ.get(
    "POLL_STATE_ITEM_ID", "013PVE4AJ3M462ISRXKFGYWX55YDOPQPCV"
)
POLL_STATE_FILENAME = "qa-poller-state.json"


def _unwrap(resp: Any) -> Any:
    """ToolCallClient-responses netjes uitpakken naar JSON-data."""
    if isinstance(resp, dict):
        if "content" in resp and isinstance(resp["content"], list):
            parts = []
            for item in resp["content"]:
                if isinstance(item, dict) and item.get("text"):
                    try:
                        import json
                        parts.append(json.loads(item["text"]))
                    except Exception:
                        parts.append(item["text"])
                elif isinstance(item, dict):
                    parts.append(item.get("json", item))
            if len(parts) == 1:
                return parts[0]
            if parts:
                return parts
        return resp
    return resp


def is_relevant_event(payload: dict) -> bool:
    if payload.get("event") != "JOB_STATUS_CHANGED":
        return False
    parts = payload.get("jobParts") or []
    return any(
        p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1) == 1
        for p in parts
    )


EN_GB_PAIRS: list[tuple[str, str]] = [
    ("organize", "organise"), ("organized", "organised"), ("organizing", "organising"),
    ("organization", "organisation"), ("organizations", "organisations"),
    ("recognize", "recognise"), ("realize", "realise"), ("realization", "realisation"),
    ("analyze", "analyse"), ("analyzed", "analysed"), ("catalog", "catalogue"),
    ("dialog", "dialogue"), ("color", "colour"), ("colors", "colours"),
    ("behavior", "behaviour"), ("favor", "favour"), ("honor", "honour"),
    ("neighbor", "neighbour"), ("labor", "labour"),
    ("center", "centre"), ("centers", "centres"), ("theater", "theatre"),
    ("liter", "litre"), ("fiber", "fibre"),
    ("traveled", "travelled"), ("traveling", "travelling"), ("labeled", "labelled"),
    ("counseling", "counselling"), ("enrollment", "enrolment"),
    ("defense", "defence"), ("offense", "offence"), ("pretense", "pretence"),
    ("fulfill", "fulfil"), ("fulfillment", "fulfilment"),
    ("gray", "grey"), ("aluminum", "aluminium"), ("jewelry", "jewellery"),
]

UT_RE_THE_UT = re.compile(r"\bthe UT\b", re.IGNORECASE)
UT_RE_APOSTROPHE_PLURAL = re.compile(r"\b(\d{4})'s\b")
UT_RE_ABBR_DOTS = re.compile(r"\b(U\.T\.|Ph\.D\.)\b")


def _is_en_gb(target_lang: str) -> bool:
    return "en_gb" in (target_lang or "").lower() or (target_lang or "").lower() == "en-gb"


def extract_preferred_terms(terms: list) -> list[str]:
    preferred: list[str] = []
    for entry in terms or []:
        if isinstance(entry, str):
            preferred.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        items = entry.get("terms") or []
        if items and isinstance(items[0], str):
            preferred.append(items[0])
            continue
        for t in items:
            if isinstance(t, dict) and t.get("term"):
                preferred.append(t["term"])
                break
    return [t for t in preferred if t]


def apply_fixes(text: str, preferred_terms: list[str], target_lang: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    out = text

    for term in preferred_terms:
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        m = pattern.search(out)
        if m and m.group(0) != term:
            out = pattern.sub(term, out)
            reasons.append(f"terminologie: '{m.group(0)}' -> '{term}'")

    if _is_en_gb(target_lang):
        for us, gb in EN_GB_PAIRS:
            pattern = re.compile(r"\b" + us + r"\b", re.IGNORECASE)
            m = pattern.search(out)
            if m:
                repl = gb if m.group(0).islower() else gb.capitalize()
                out = pattern.sub(repl, out)
                reasons.append(f"en-GB: '{us}' -> '{gb}'")

    if UT_RE_THE_UT.search(out):
        out = UT_RE_THE_UT.sub("UT", out)
        reasons.append("UT-regel: geen lidwoord voor UT")
    m = UT_RE_APOSTROPHE_PLURAL.search(out)
    if m:
        out = UT_RE_APOSTROPHE_PLURAL.sub(r"\1s", out)
        reasons.append("apostrof in meervoud")
    m = UT_RE_ABBR_DOTS.search(out)
    if m:
        fixed = m.group(1).replace(".", "")
        out = UT_RE_ABBR_DOTS.sub(fixed, out)
        reasons.append(f"afkorting zonder punten: '{m.group(1)}' -> '{fixed}'")
    return out, reasons


@workflows.activity(name="qa-filter-event")
async def qa_filter_event(event: Any) -> dict:
    """Accepteert een TriggerEvent (webhook) of een gewone dict (schema/input)."""
    if isinstance(event, dict):
        return event
    body = getattr(event, "body", None)
    if isinstance(body, dict):
        return body
    return {}


def _parse_created_at(value: Any):
    """Parse '2026-11-27T12:40:11+0000' naar een bewuste datetime."""
    from datetime import datetime

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("+0000", "+00:00"))
    except Exception:
        return None


@workflows.activity(name="qa-poll-events")
async def qa_poll_events(
    lookback_minutes: int,
    phrase: ToolCallClient = Depends(phrase_connector),
) -> list:
    """Leest recente JOB_STATUS_CHANGED-leveringen via de Phrase-API."""
    from datetime import datetime, timedelta, timezone

    resp = _unwrap(await phrase.call_tool(
        tool_name="tms_read_api",
        arguments={
            "method": "GET",
            "path": "/api2/v1/webhooksCalls",
            "query_params": {
                "webhookUid": PHRASE_WEBHOOK_UID,
                "events": "JOB_STATUS_CHANGED",
                "pageSize": "50",
            },
        },
    ))
    content: list = []
    if isinstance(resp, dict):
        content = resp.get("content") or (resp.get("response") or {}).get("content") or []
    elif isinstance(resp, list):
        content = resp

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=int(lookback_minutes or 8))
    candidates: list[dict] = []
    for call in content:
        if not isinstance(call, dict) or call.get("forced"):
            continue
        created = _parse_created_at(call.get("createdAt"))
        if not created or created < cutoff:
            continue
        try:
            import json
            body = json.loads(call.get("body") or "{}")
        except Exception:
            continue
        if not isinstance(body, dict) or body.get("event") != "JOB_STATUS_CHANGED":
            continue
        parts = [
            p for p in (body.get("jobParts") or [])
            if isinstance(p, dict)
            and p.get("status") == "COMPLETED"
            and int(p.get("workflowLevel") or 1) == 1
        ]
        if not parts:
            continue
        meta = ((body.get("metadata") or {}).get("project") or {})
        project_uid = meta.get("uid") or ((parts[0].get("project") or {}).get("uid"))
        if not project_uid:
            continue
        candidates.append({
            "eventUid": body.get("eventUid") or call.get("uid"),
            "payload": {
                "event": "JOB_STATUS_CHANGED",
                "project": {"uid": project_uid, "name": meta.get("name") or ""},
                "jobParts": parts,
            },
        })
    return candidates


@workflows.activity(name="qa-load-state")
async def qa_load_state(
    sp: ToolCallClient = Depends(sharepoint_connector),
) -> dict:
    """Statusbestand (verwerkte events/jobparts) uit SharePoint lezen."""
    import json

    resp = _unwrap(await sp.call_tool(
        tool_name="read_binary_file",
        arguments={"drive_id": SHAREPOINT_DRIVE_ID, "item_id": POLL_STATE_ITEM_ID},
    ))
    file_path = None
    if isinstance(resp, dict):
        file_path = resp.get("file_path") or resp.get("file_name")
    if not file_path or not os.path.exists(file_path):
        return {"processedEvents": [], "processedParts": []}
    try:
        with open(file_path, "r") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"processedEvents": [], "processedParts": []}


@workflows.activity(name="qa-save-state")
async def qa_save_state(
    state: dict,
    sp: ToolCallClient = Depends(sharepoint_connector),
) -> str:
    """Statusbestand (verwerkte events/jobparts) in SharePoint overschrijven."""
    import json

    state["processedEvents"] = (state.get("processedEvents") or [])[-500:]
    state["processedParts"] = (state.get("processedParts") or [])[-2000:]
    local = "/tmp/qa-poller-state.json"
    with open(local, "w") as fh:
        json.dump(state, fh)
    with open(local, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    resp = await sp.call_tool(
        tool_name="create_binary_file",
        arguments={
            "drive_id": SHAREPOINT_DRIVE_ID,
            "folder_id": SHAREPOINT_FOLDER_ID,
            "filename": POLL_STATE_FILENAME,
            "base64_content": b64,
        },
    )
    return str(resp)


@workflows.activity(name="qa-fetch-project-info")
async def qa_fetch_project_info(
    payload: dict,
    phrase: ToolCallClient = Depends(phrase_connector),
) -> dict:
    """Project, contentgroepen en termbases + termen via de Phrase-connector."""
    project = payload.get("project") or {}
    project_uid = project.get("uid")
    if not project_uid:
        return {"check": False, "reason": "geen project-uid"}

    details = _unwrap(await phrase.call_tool(
        tool_name="tms_read_api",
        arguments={"method": "GET", "path": f"/api2/v1/projects/{project_uid}"},
    ))
    if isinstance(details, dict) and details.get("uid") is None and "id" in details:
        pass  # sommige antwoorden hebben alleen id
    if not isinstance(details, dict):
        details = {}

    tb_resp = _unwrap(await phrase.call_tool(
        tool_name="tms_read_api",
        arguments={"method": "GET", "path": f"/api2/v1/projects/{project_uid}/termBases"},
    ))
    term_bases = []
    if isinstance(tb_resp, dict):
        term_bases = tb_resp.get("termBases") or tb_resp.get("content") or []

    cg_resp = await phrase.call_tool(
        tool_name="tms_read_api",
        arguments={"method": "GET", "path": f"/api2/v1/projects/{project_uid}/contentGroup"},
    )
    cg = _unwrap(cg_resp)
    content_groups = []
    if isinstance(cg, dict) and cg.get("id"):
        content_groups = [cg]

    raw_terms: list = []
    for tb in term_bases:
        tb_uid = tb.get("id") or tb.get("uid")
        if not tb_uid:
            continue
        try:
            t = _unwrap(await phrase.call_tool(
                tool_name="tms_read_api",
                arguments={
                    "method": "GET",
                    "path": f"/api2/v1/termBases/{tb_uid}/terms",
                    "query_params": {"pageSize": 500},
                }
            ))
            if isinstance(t, dict):
                raw_terms.extend(t.get("terms") or t.get("content") or [])
        except Exception:
            continue

    return {
        "check": bool(content_groups) and bool(term_bases),
        "project": {
            "uid": project_uid,
            "name": details.get("name") or project.get("name") or "?",
        },
        "owner": {
            "email": ((details.get("owner") or {}).get("email")) or ((project.get("owner") or {}).get("email")) or "",
            "display_name": " ".join(filter(None, [((details.get("owner") or {}).get("firstName") or ""), ((details.get("owner") or {}).get("lastName") or "")])).strip(),
        },
        "contentGroups": [g.get("id") for g in content_groups if isinstance(g, dict)],
        "termBases": [t.get("id") or t.get("uid") for t in term_bases],
        "preferredTerms": extract_preferred_terms(raw_terms),
        "reason": (
            ""
            if (content_groups and term_bases)
            else f"contentgroep={bool(content_groups)} termbase={bool(term_bases)}"
        ),
    }


def _extract_url(value: Any) -> str | None:
    """Zoekt een downloadbare URL in een tool-respons (resource link)."""
    if isinstance(value, dict):
        for key in ("resource_link", "url", "download_url", "web_url", "uri"):
            v = value.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in value.values():
            found = _extract_url(v)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _extract_url(item)
            if found:
                return found
    elif isinstance(value, str) and value.startswith("http") and " " not in value:
        return value
    return None


def _extract_text(value: Any) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), str):
            return value["content"]
        for v in value.values():
            found = _extract_text(v)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _extract_text(item)
            if found:
                return found
    elif isinstance(value, str) and len(value) > 100 and "<mrk" in value:
        return value
    return None


@workflows.activity(name="qa-check-job")
async def qa_check_job(
    payload: dict,
    info: dict,
    phrase: ToolCallClient = Depends(phrase_connector),
) -> dict:
    """Bilingual file downloaden (via connector), fixes, upload (via connector)."""
    import httpx

    project_uid = info["project"]["uid"]
    completed = [
        p
        for p in (payload.get("jobParts") or [])
        if p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1) == 1
    ]
    results: list[dict] = []
    terms = info.get("preferredTerms", [])

    for part in completed:
        part_uid = part.get("uid")
        part_id = part_uid.get("uid") if isinstance(part_uid, dict) else part_uid
        target_lang = part.get("targetLang", "")
        try:
            resp = _unwrap(await phrase.call_tool(
                tool_name="tms_read_api",
                arguments={
                    "method": "GET",
                    "path": f"/api2/v1/projects/{project_uid}/jobs/{part_id}/bilingualFile",
                    "query_params": {"format": "MXLF"},
                },
            ))
            xml = _extract_text(resp)
            if not xml:
                url = _extract_url(resp)
                if url:
                    async with httpx.AsyncClient(timeout=120) as http:
                        r = await http.get(url)
                        r.raise_for_status()
                        xml = r.text
            if not xml:
                results.append({"part": part_id, "error": "geen bilingual-inhoud via connector"})
                continue

            part_reasons: list[str] = []
            holder = {"xml": xml}

            def _fix_target(m):
                original = m.group(1)
                unescaped = original.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                fixed, reasons = apply_fixes(unescaped, terms, target_lang)
                if reasons:
                    part_reasons.extend(reasons)
                    re_escaped = fixed.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    return "<target>" + re_escaped + "</target>"
                return m.group(0)

            holder["xml"] = re.sub(r"<target>(.*?)</target>", _fix_target, xml, flags=re.S)

            if not part_reasons:
                results.append({
                    "part": part_id, "targetLang": target_lang,
                    "reasons": [], "uploaded": False, "clean": True,
                })
                continue

            # Upload via out-of-band file-handle flow van de Phrase-connector.
            up = _unwrap(await phrase.call_tool(
                tool_name="request_upload_url",
                arguments={
                    "filename": "corrected.mxlf",
                    "content_type": "application/xml",
                },
            ))
            upload_url = _extract_url(up) or (up.get("upload_url") if isinstance(up, dict) else None)
            file_handle = None
            if isinstance(up, dict):
                file_handle = up.get("file_handle") or up.get("fileHandle")
            if not upload_url:
                results.append({"part": part_id, "error": "geen upload-url van connector", "reasons": part_reasons})
                continue
            async with httpx.AsyncClient(timeout=120) as http:
                r = await http.put(upload_url, content=holder["xml"].encode("utf-8"))
                if r.status_code not in (200, 201):
                    results.append({"part": part_id, "error": f"upload bytes {r.status_code}", "reasons": part_reasons})
                    continue
                if not file_handle:
                    try:
                        file_handle = r.json().get("file_handle")
                    except Exception:
                        file_handle = None

            ur = _unwrap(await phrase.call_tool(
                tool_name="tms_write_api",
                arguments={
                    "method": "POST",
                    "path": f"/api2/v1/projects/{project_uid}/bilingualFiles",
                    "file_handle": file_handle,
                    "body": {
                        "saveToTransMemory": "Confirmed",
                        "jobParts": [part_id],
                    },
                },
            ))
            ok = isinstance(ur, dict) and ur.get("error") is None
            results.append({
                "part": part_id,
                "targetLang": target_lang,
                "reasons": part_reasons,
                "uploaded": ok,
                "response": str(ur)[:300],
            })
        except Exception as exc:
            results.append({"part": part_id, "error": str(exc)})

    return {"project": info["project"], "results": results}


@workflows.activity(name="qa-build-xlsx")
async def qa_build_xlsx(report: dict) -> str:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Wijzigingen"
    ws.append(["Datum", "Project", "Job part", "Doeltaal", "Reden(en)", "Teruggeschreven"])
    for row in report["results"]:
        ws.append([
            report.get("project", {}).get("name", ""),
            row.get("part"),
            row.get("targetLang", ""),
            "; ".join(row.get("reasons", [])) or row.get("error", ""),
            "ja" if row.get("uploaded") else "nee",
        ])
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(report.get("project", {}).get("name", "project")))[:60]
    filename = f"QA_voorvertaling_{safe_name}.xlsx"
    wb.save(filename)
    return filename


@workflows.activity(name="qa-upload-sharepoint")
async def qa_upload_sharepoint(
    filename: str,
    sp: ToolCallClient = Depends(sharepoint_connector),
) -> str:
    with open(filename, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    resp = await sp.call_tool(
        tool_name="create_binary_file",
        arguments={
            "drive_id": SHAREPOINT_DRIVE_ID,
            "folder_id": SHAREPOINT_FOLDER_ID,
            "filename": filename,
            "base64_content": b64,
        },
    )
    return str(resp)


@workflows.activity(name="qa-notify-teams")
async def qa_notify_teams(
    report: dict,
    teams: ToolCallClient = Depends(teams_connector),
) -> str:
    p = report["project"]["name"]
    owner = report.get("owner") or {}
    owner_name = (owner.get("display_name") or "").strip()
    owner_email = (owner.get("email") or "").strip()
    results = report.get("results", [])
    fixed = [r for r in results if r.get("uploaded")]
    clean = [r for r in results if r.get("clean")]
    reasons = sorted({reason for r in results for reason in r.get("reasons", [])})
    content = (
        f"Kwaliteitscheck direct na voorvertaling uitgevoerd voor **{p}**."
        f"\n- Jobs gecontroleerd: {len(results)}"
        f"\n- Jobs met correcties (teruggeschreven naar Phrase): {len(fixed)}"
        f"\n- Jobs zonder bevindingen: {len(clean)}"
        f"\n- Soorten bevindingen: {', '.join(reasons) if reasons else 'geen'}"
        f"\n- Excel-overzicht: SharePoint > Vertalen intern > Phrase > Mistral QA voorvertaling."
    )
    mentions = None
    if owner_name:
        content = f"@{owner_name} " + content
        try:
            members = _unwrap(await teams.call_tool(
                tool_name="list_team_members",
                arguments={"team_id": TEAMS_TEAM_ID, "query": owner_email or owner_name},
            ))
            candidates = members if isinstance(members, list) else ((members or {}).get("members") or (members or {}).get("content") or [])
            member = None
            for m in candidates:
                if isinstance(m, dict) and owner_email and (m.get("email") or "").lower() == owner_email.lower():
                    member = m
                    break
            if member is None:
                for m in candidates:
                    if isinstance(m, dict) and owner_name.lower() in ((m.get("display_name") or "")).lower():
                        member = m
                        break
            if member and member.get("user_id"):
                mentions = [{
                    "user_id": member["user_id"],
                    "display_name": member.get("display_name") or owner_name,
                }]
        except Exception:
            mentions = None
    args = {
        "team_id": TEAMS_TEAM_ID,
        "channel_id": TEAMS_CHANNEL_ID,
        "subject": f"Kwaliteitscheck na voorvertaling: {p}",
        "content": content,
    }
    if mentions:
        args["mentions"] = mentions
    resp = await teams.call_tool(
        tool_name="post_channel_message",
        arguments=args,
    )
    return str(resp)


@workflows.workflow.define(
    name="phrase-qa-voorvertaling",
    workflow_display_name="Kwaliteitscheck na voorvertaling",
    workflow_description=(
        "Kwaliteitscheck direct na elke Phrase-voorvertaling: realtime via de "
        "Phrase-webhook (JOB_STATUS_CHANGED, COMPLETED, level 1), volledig via "
        "MCP-connectors (Phrase, SharePoint, Teams); contentgroep + termbase, "
        "alle talen, 0 AIU's."
    ),
    on_behalf_of=True,
)
@uses_connectors(phrase_connector, teams_connector, sharepoint_connector)
class PhraseQaVoorvertalingWorkflow:
    @workflows.workflow.entrypoint
    async def run(
        self,
        jobParts: Any = None,
        metadata: Any = None,
        event: Any = None,
        timestamp: Any = None,
        eventUid: Any = None,
    ) -> str:
        # Phrase-webhook v2 stuurt de event-body als raw JSON; de keys mapten
        # direct op deze parameters. Handmatige/poll-aanroepen kunnen ook
        # {"event": {...}} of de losse keys sturen.
        if isinstance(event, dict) and not jobParts:
            payload = await qa_filter_event(event)
        else:
            payload = await qa_filter_event(
                {
                    "jobParts": jobParts,
                    "metadata": metadata,
                    "event": event,
                    "timestamp": timestamp,
                    "eventUid": eventUid,
                }
            )
        if payload.get("poll"):
            lookback = int(payload.get("lookback_minutes") or 8)
            candidates = await qa_poll_events(lookback)
            if not candidates:
                return "poll: geen nieuwe COMPLETED-voorvertalingen"
            state = await qa_load_state()
            done_events = set(state.get("processedEvents") or [])
            done_parts = set(state.get("processedParts") or [])
            summaries: list[str] = []
            changed = False
            for cand in candidates:
                ev = cand.get("eventUid")
                if ev and ev in done_events:
                    continue
                pl = cand["payload"]
                parts = [p for p in pl.get("jobParts", []) if p.get("uid") not in done_parts]
                if not parts:
                    continue
                pl = dict(pl, jobParts=parts)
                if ev:
                    done_events.add(ev)
                for p in parts:
                    done_parts.add(p.get("uid"))
                changed = True
                try:
                    summaries.append(await self._process_one(pl))
                except Exception as exc:
                    summaries.append(f"fout bij verwerken: {exc}")
                state["processedEvents"] = sorted(done_events)
                state["processedParts"] = sorted(done_parts)
                await qa_save_state(state)
            if not changed:
                return "poll: niets nieuws te verwerken"
            return "poll: " + " | ".join(summaries)
        return await self._process_one(payload)

    async def _process_one(self, payload: dict) -> str:
        if not is_relevant_event(payload):
            return "genegeerd: geen COMPLETED-voorvertaling"
        info = await qa_fetch_project_info(payload)
        if not info.get("check"):
            return f"genegeerd: {info.get('reason')}"
        report = await qa_check_job(payload, info)
        report["owner"] = info.get("owner") or {}
        filename = await qa_build_xlsx(report)
        await qa_upload_sharepoint(filename)
        return await qa_notify_teams(report)
