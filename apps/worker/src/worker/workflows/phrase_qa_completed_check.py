"""Kwaliteitscontrole na voorvertaling (Completed) — checks via Mistral, 0 AIU's in Phrase.

Connectors: `teams` en `sharepoint_mcp` (run_as=deployment). Phrase-data loopt via
plain REST met PHRASE_API_TOKEN uit een workspace secret (de user-scope connector
`phrase_mistral_768b` is niet bruikbaar voor unattended runs; als die later op
workspace-niveau gedeeld wordt, kan `_rest` omgewisseld worden voor de connector).
0 AIU's: alle Phrase-calls zijn gewone REST; de checks zelf worden door een
Mistral-model uitgevoerd (MISTRAL_API_KEY workspace secret).

Twee modi:
- event: Phrase event-webhook (JOB_STATUS_CHANGED) zodra een job op workflow
  level 1 (voorvertaling) op COMPLETED staat;
- poll (default bij scheduled run, géén webhook nodig): zoekt in Phrase TMS naar
  jobs op level 1 die zojuist COMPLETED zijn geworden (statusChanges binnen het
  venster) en controleert die. Een marker-comment in Phrase voorkomt dubbele
  verwerking.

Checks per job: consistentie (incl. consistente terminologie), termbase-
terminologie en contentgroep-regels (+ en-GB: Brits Engels zonder Oxford comma).
Rapport (Excel) -> SharePoint, melding in Teams.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)
from mistralai.workflows.plugins.triggers import TriggerEvent

# auto_auth=False: de preflight faalt hard als de deployment nog geen
# workspace-credential heeft; rapportgage (Teams/SharePoint) is best-effort en
# mag de QA-core (poll + Mistral-checks + marker) nooit blokkeren.
teams_connector = connector("teams", run_as="deployment", auto_auth=False)
sharepoint_connector = connector("sharepoint_mcp", run_as="deployment", auto_auth=False)

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

MISTRAL_API_BASE = os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai/v1").rstrip("/")
MISTRAL_QA_MODEL = os.environ.get("MISTRAL_QA_MODEL", "mistral-large-latest")

# Microsoft Graph (voor persoonlijke Teams-DM naar de project-eigenaar).
# Maak in Azure een app-registratie met application permission Chat.ReadWrite.All
# (+ admin consent) en zet tenant/client id/secret als workspace secrets.
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MS_GRAPH_TENANT_ID = os.environ.get("MS_GRAPH_TENANT_ID", "")
MS_GRAPH_CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "")
MS_GRAPH_CLIENT_SECRET = os.environ.get("MS_GRAPH_CLIENT_SECRET", "")
MS_GRAPH_TOKEN = os.environ.get("MS_GRAPH_TOKEN", "")  # alternatief: pre-issued token


async def _graph_token() -> str:
    if MS_GRAPH_TOKEN:
        return MS_GRAPH_TOKEN
    import httpx

    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"https://login.microsoftonline.com/{MS_GRAPH_TENANT_ID}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": MS_GRAPH_CLIENT_ID,
                "client_secret": MS_GRAPH_CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        r.raise_for_status()
        return r.json()["access_token"]


async def _graph(method: str, path: str, body=None):
    import httpx

    token = await _graph_token()
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.request(
            method, f"{MS_GRAPH_BASE}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json() if r.content else {}

# Limieten om promptgrootte behapbaar te houden.
MAX_SEGMENTS_PER_CALL = 40
MAX_SEGMENTS_TOTAL = 400
# Poll-venster in minuten (module-niveau: os.environ mag niet in de workflow-body).
QC_POLL_WINDOW_MINUTES = int(os.environ.get("QC_POLL_WINDOW_MINUTES", "20"))
# Vaste Phrase-map (project) waarin elk QA-rapport als bestand wordt geplaatst.
REPORT_PROJECT_NAME = os.environ.get("PHRASE_REPORT_PROJECT_NAME", "QA-checks na voorvertaling (Mistral)")
REPORT_PROJECT_UID = os.environ.get("PHRASE_REPORT_PROJECT_UID", "")


def _phrase_token() -> str:
    """Phrase API-token uit workspace secret (PHRASE_API_TOKEN of phrase_api_token)."""
    return os.environ.get("PHRASE_API_TOKEN") or os.environ["phrase_api_token"]


def _rest_headers() -> dict:
    return {"Authorization": f"ApiToken {_phrase_token()}", "Accept": "application/json"}


async def _rest(method: str, path: str, query_params: Optional[dict] = None, body=None):
    """Plain Phrase REST-call (0 AIU's) met PHRASE_API_TOKEN uit workspace secret."""
    import httpx

    base = os.environ.get("PHRASE_API_BASE", "https://api_phrase.com").rstrip("/")
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.request(
            method,
            f"{base}{path}",
            params=query_params,
            json=body,
            headers=_rest_headers(),
        )
        r.raise_for_status()
        if "json" in r.headers.get("content-type", ""):
            return r.json()
        return {"content": r.text}


async def _rest_upload(path: str, filename: str, content: bytes, memsource: dict):
    """Multipart upload naar Phrase (bijv. rapportbestand als job)."""
    import httpx

    base = os.environ.get("PHRASE_API_BASE", "https://api_phrase.com").rstrip("/")
    headers = _rest_headers()
    headers["Memsource"] = json.dumps(memsource)
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(
            f"{base}{path}",
            headers=headers,
            files={"file": (filename, content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        r.raise_for_status()
        return r.json() if "json" in r.headers.get("content-type", "") else {"content": r.text}


def is_completed_pretranslation_event(payload: dict) -> bool:
    """True bij JOB_STATUS_CHANGED met een job part COMPLETED op workflow level 1."""
    if not isinstance(payload, dict) or payload.get("event") != "JOB_STATUS_CHANGED":
        return False
    parts = payload.get("jobParts") or []
    return any(
        p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1) == 1
        for p in parts
    )


def completed_parts(payload: dict) -> list[dict]:
    return [
        p
        for p in (payload.get("jobParts") or [])
        if p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1) == 1
    ]


# Marker-comment in Phrase om verwerkte jobs te onthouden (dedupe bij polling).
MARKER = "[mistral-qa-completed-check]"


def parse_iso_ts(value) -> Optional[datetime]:
    """Parseert een ISO-8601 timestamp (Z of offset) naar aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


_CHANGE_DATE_FIELDS = ("changedDate", "dateCreated", "createdAt", "timestamp", "date")


def _iter_changes(changes, job_uid: str):
    """Levert (status, timestamp) tuples voor een job uit diverse response-vormen."""
    if isinstance(changes, dict) and isinstance(changes.get("projects"), list):
        # batch-vorm: {"projects": [{"jobs": [{"jobUid": ..., "statusChanges": [...]}]}]}
        for proj in changes["projects"]:
            for job in proj.get("jobs", []) or []:
                if str(job.get("jobUid") or "") != str(job_uid):
                    continue
                for c in job.get("statusChanges", []) or []:
                    yield c
        return
    items = []
    if isinstance(changes, dict):
        items = changes.get("statusChanges") or changes.get("changes") or []
        if isinstance(items, dict):
            items = items.get(job_uid) or []
    elif isinstance(changes, list):
        items = changes
    for c in items:
        if not isinstance(c, dict):
            continue
        uid = c.get("jobUid") or c.get("uid")
        if not uid and isinstance(c.get("job"), dict):
            uid = c["job"].get("uid")
        if uid and str(uid) != str(job_uid):
            continue
        yield c


def find_completed_at(changes, job_uid: str) -> Optional[datetime]:
    """Vindt de meest recente COMPLETED-status change voor een job (tolerant formaat)."""
    best: Optional[datetime] = None
    for c in _iter_changes(changes, job_uid):
        if str(c.get("status") or c.get("newStatus") or "").upper() != "COMPLETED":
            continue
        ts = None
        for field in _CHANGE_DATE_FIELDS:
            ts = parse_iso_ts(c.get(field))
            if ts:
                break
        if ts and (best is None or ts > best):
            best = ts
    return best


def is_within_window(ts: Optional[datetime], now: datetime, window_minutes: int) -> bool:
    if ts is None:
        return False
    return (now - ts).total_seconds() <= window_minutes * 60


def text_has_marker(text: str) -> bool:
    return MARKER in (text or "")


# --------------------------------------------------------------------------
# Mistral-prompt en -antwoord (pure functies, unit-testbaar)
# --------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a localization quality-assurance engine for machine-translated content. "
    "You check target segments against an approved termbase and a set of content-group "
    "rules, and you check consistency across segments. "
    "Answer ONLY with a JSON object, no prose."
)


def extra_rules_for_lang(target_lang: str) -> str:
    """Extra stijlregels per doeltaal (nu: en-GB = Brits Engels zonder Oxford comma)."""
    lang = (target_lang or "").lower().replace("_", "-")
    if lang == "en-gb":
        return (
            "- en-GB style: use British English spelling (organise, colour, centre, ...) "
            "and NO Oxford comma in lists ('a, b and c', never 'a, b, and c')."
        )
    return ""


def build_mistral_prompt(
    target_lang: str,
    terms: list[str],
    rules_md: str,
    segments: list[dict],
) -> str:
    """Bouwt de user-prompt met termbase, regels en segmenten."""
    seg_lines = []
    for s in segments:
        src = (s.get("source") or "").replace("\n", " ").strip()
        tgt = (s.get("target") or "").replace("\n", " ").strip()
        seg_lines.append(f"{s.get('id')} | {src} | {tgt}")
    rules_block = rules_md.strip()
    extra = extra_rules_for_lang(target_lang)
    if extra:
        rules_block = f"{rules_block}\n{extra}" if rules_block else extra
    return (
        f"Target language: {target_lang}\n\n"
        f"APPROVED TERMBASE TERMS (use these in the target; flag other variants):\n"
        f"{chr(10).join('- ' + t for t in terms) or '(none)'}\n\n"
        f"CONTENT GROUP RULES (evaluate each rule per segment):\n"
        f"{rules_block or '(none)'}\n\n"
        f"SEGMENTS (id | source | target):\n" + "\n".join(seg_lines) + "\n\n"
        "CHECKS:\n"
        "1. terminology: the target must use the approved termbase terms; flag non-approved "
        "or forbidden variants.\n"
        "2. consistency: segments with identical source text must have identical target "
        "text, AND terminology must be used consistently across all segments — the same "
        "source term must always get the same target term; flag inconsistent term usage "
        "and mismatched duplicates alike.\n"
        "3. content-group: evaluate every rule above against each segment; flag clear "
        "violations only (segment-local verdicts).\n\n"
        'OUTPUT (JSON only): {"violations": [{"segmentId": "<id>", '
        '"check": "terminology|consistency|content-group|en-gb", "rule": "<rule or term>", '
        '"message": "<what is wrong>", "suggestion": "<preferred wording, if any>"}]}'
    )


def parse_violations(content: str) -> list[dict]:
    """Parst het Mistral-antwoord (JSON, evt. in code fence) naar violations."""
    if not content:
        return []
    text = content.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    violations = data.get("violations") if isinstance(data, dict) else None
    if not isinstance(violations, list):
        return []
    out = []
    for v in violations:
        if isinstance(v, dict) and v.get("segmentId") and v.get("check"):
            out.append(
                {
                    "segmentId": str(v.get("segmentId")),
                    "check": str(v.get("check")),
                    "rule": str(v.get("rule") or ""),
                    "message": str(v.get("message") or ""),
                    "suggestion": str(v.get("suggestion") or ""),
                }
            )
    return out


def chunked(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------

@workflows.activity(name="qc-filter-event")
async def qc_filter_event(event: Any) -> dict:
    body = getattr(event, "body", event)
    return body if isinstance(body, dict) else {}


@workflows.activity(name="qc-fetch-context")
async def qc_fetch_context(payload: dict) -> dict:
    """Haalt project, termbase-termen, contentgroep-regels en segmenten op (0 AIU)."""
    project = payload.get("project") or {}
    project_uid = project.get("uid")
    if not project_uid:
        return {"check": False, "reason": "geen project-uid"}

    parts = completed_parts(payload)

    details = await _rest("GET", f"/api2/v2/projects/{project_uid}")

    term_bases_resp = await _rest("GET", f"/api2/v1/projects/{project_uid}/termBases")
    term_bases = term_bases_resp.get("termBases", []) if isinstance(term_bases_resp, dict) else []

    raw_terms: list = []
    for tb in term_bases:
        tb_uid = tb.get("id") or tb.get("uid")
        if not tb_uid:
            continue
        try:
            terms_resp = await _rest(
                "GET", f"/api2/v1/termBases/{tb_uid}/terms", query_params={"pageSize": 500}
            )
            for entry in (terms_resp.get("terms") or []) if isinstance(terms_resp, dict) else []:
                items = entry.get("terms") if isinstance(entry, dict) else None
                if isinstance(items, list):
                    for t in items:
                        if isinstance(t, dict) and t.get("term"):
                            raw_terms.append(t["term"])
                        elif isinstance(t, str):
                            raw_terms.append(t)
        except Exception:
            continue

    cg_resp = await _rest("GET", f"/api2/v1/projects/{project_uid}/contentGroups")
    content_groups = cg_resp.get("contentGroups", []) if isinstance(cg_resp, dict) else []

    rules_md_parts: list[str] = []
    for g in content_groups:
        gid = g.get("id") or g.get("uid")
        if not gid:
            continue
        try:
            export = await _rest("GET", f"/api/v1/rules/{gid}/export")
            text = export.get("content") if isinstance(export, dict) else str(export)
            if text and text.strip():
                rules_md_parts.append(text.strip())
        except Exception:
            continue

    segments: list[dict] = []
    for part in parts:
        part_uid = part.get("uid")
        part_id = part_uid.get("uid") if isinstance(part_uid, dict) else part_uid
        target_lang = part.get("targetLang", "")
        try:
            seg_resp = await _rest(
                "GET",
                f"/api2/v1/projects/{project_uid}/jobs/{part_id}/segments",
                query_params={"beginIndex": 0, "endIndex": MAX_SEGMENTS_TOTAL - 1},
            )
            records = []
            if isinstance(seg_resp, dict):
                records = seg_resp.get("segments") or seg_resp.get("content") or []
            for rec in records:
                segments.append(
                    {
                        "id": str(rec.get("segmentId") or rec.get("id") or len(segments)),
                        "part": str(part_id),
                        "targetLang": target_lang,
                        "source": rec.get("source") or "",
                        "target": rec.get("translation") or rec.get("target") or "",
                    }
                )
        except Exception:
            continue

    segments = [s for s in segments if s["source"] and s["target"]][:MAX_SEGMENTS_TOTAL]
    owner = details.get("owner") if isinstance(details, dict) else None
    owner_info = None
    if isinstance(owner, dict):
        owner_info = {
            "email": owner.get("email"),
            "userName": owner.get("userName"),
            "name": " ".join(x for x in [owner.get("firstName"), owner.get("lastName")] if x) or owner.get("userName"),
        }
    return {
        "check": bool(segments),
        "project": {
            "uid": project_uid,
            "name": (details.get("name") if isinstance(details, dict) else None) or project.get("name") or "?",
            "owner": owner_info,
        },
        "parts": [
            {
                "uid": (p.get("uid").get("uid") if isinstance(p.get("uid"), dict) else p.get("uid")),
                "targetLang": p.get("targetLang", ""),
            }
            for p in parts
        ],
        "terms": sorted(set(raw_terms)),
        "contentGroups": [g.get("id") or g.get("uid") for g in content_groups],
        "rulesMd": "\n\n".join(rules_md_parts),
        "segments": segments,
        "reason": "" if segments else "geen segmenten met bron+target",
    }


@workflows.activity(name="qc-poll-completed")
async def qc_poll_completed(window_minutes: int) -> list[dict]:
    """Zoekt jobs op level 1 die binnen het venster COMPLETED zijn geworden (poll-modus)."""
    now = datetime.now(timezone.utc)
    payloads: list[dict] = []

    projects: list[dict] = []
    for page in range(10):  # org heeft 300+ projecten; met semaphore is dit snel genoeg
        try:
            resp = await _rest(
                "GET", "/api2/v1/projects",
                query_params={
                    "pageNumber": page,
                    "pageSize": 50,
                    "includeArchived": "false",
                    "sort": "ID",
                    "order": "DESC",  # nieuwste projecten eerst
                },
            )
        except Exception:
            break
        content = resp.get("content", []) if isinstance(resp, dict) else []
        projects.extend(content)
        if len(content) < 50:
            break

    import asyncio

    candidates: list[tuple[dict, dict]] = []
    sem = asyncio.Semaphore(10)

    async def _jobs_for(p: dict) -> list[tuple[dict, dict]]:
        puid = p.get("uid")
        if not puid:
            return []
        async with sem:
            try:
                resp = await _rest(
                    "GET", f"/api2/v2/projects/{puid}/jobs",
                    query_params={"workflowLevel": 1, "status": ["COMPLETED"], "pageSize": 50},
                )
                return [
                    ({"uid": puid, "name": p.get("name") or puid}, j)
                    for j in (resp.get("content", []) if isinstance(resp, dict) else [])
                ]
            except Exception:
                return []

    for result in await asyncio.gather(*[_jobs_for(p) for p in projects]):
        candidates.extend(result)

    for i in range(0, len(candidates), 100):
        batch = candidates[i : i + 100]
        try:
            changes = await _rest(
                "POST", "/api2/v2/jobs/statusChanges",
                query_params={"order": "DESC"},
                body={"jobs": [{"uid": j.get("uid")} for _, j in batch]},
            )
        except Exception:
            changes = {}
        for proj, j in batch:
            juid = j.get("uid")
            completed_at = find_completed_at(changes, juid)
            if not is_within_window(completed_at, now, window_minutes):
                continue
            try:
                conv = await _rest("GET", f"/api2/v1/jobs/{juid}/conversations/plains")
                if text_has_marker(json.dumps(conv)):
                    continue
            except Exception:
                pass
            payloads.append(
                {
                    "project": proj,
                    "jobParts": [
                        {
                            "uid": juid,
                            "status": "COMPLETED",
                            "workflowLevel": 1,
                            "targetLang": j.get("targetLang", ""),
                        }
                    ],
                }
            )
    return payloads


@workflows.activity(name="qc-mark-processed")
async def qc_mark_processed(job_uid: str) -> bool:
    """Plaatst de marker-comment op de job zodat polling niet dubbel verwerkt."""
    try:
        await _rest(
            "POST", f"/api2/v3/jobs/{job_uid}/conversations/plains",
            body={
                "references": [],
                "comment": {"text": f"{MARKER} kwaliteitscontrole na voorvertaling (Completed) uitgevoerd via Mistral, 0 AIU's."},
            },
        )
        return True
    except Exception:
        return False


@workflows.activity(name="qc-mistral-check")
async def qc_mistral_check(context: dict) -> dict:
    """Voert de QA-checks uit via Mistral (chat completions, JSON-output)."""
    import httpx

    api_key = os.environ.get("MISTRAL_API_KEY") or os.environ.get("PHRASE_MISTRAL_API_KEY")
    if not api_key:
        return {"error": "MISTRAL_API_KEY ontbreekt", "violations": []}

    segments = context.get("segments", [])
    violations: list[dict] = []
    async with httpx.AsyncClient(timeout=180) as http:
        # groepeer per doeltaal zodat de prompt eenduidig is
        by_lang: dict[str, list[dict]] = {}
        for s in segments:
            by_lang.setdefault(s.get("targetLang") or "?", []).append(s)
        for lang, segs in by_lang.items():
            for batch in chunked(segs, MAX_SEGMENTS_PER_CALL):
                prompt = build_mistral_prompt(
                    lang, context.get("terms", []), context.get("rulesMd", ""), batch
                )
                r = await http.post(
                    f"{MISTRAL_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": MISTRAL_QA_MODEL,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                    },
                )
                if r.status_code != 200:
                    violations.append(
                        {
                            "segmentId": "-",
                            "check": "engine",
                            "rule": "mistral-call",
                            "message": f"HTTP {r.status_code}: {r.text[:200]}",
                            "suggestion": "",
                        }
                    )
                    continue
                content = r.json()["choices"][0]["message"]["content"]
                for v in parse_violations(content):
                    v["targetLang"] = lang
                    violations.append(v)

    return {
        "project": context["project"],
        "segmentCount": len(segments),
        "violationCount": len(violations),
        "violations": violations,
    }


@workflows.activity(name="qc-build-xlsx")
async def qc_build_xlsx(report: dict) -> str:
    """Excel-rapport met alle bevindingen."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Bevindingen"
    ws.append(["Project", "Doeltaal", "Segment", "Check", "Regel/term", "Bevinding", "Suggestie"])
    for v in report.get("violations", []):
        ws.append([
            v.get("project") or report.get("project", {}).get("name", ""),
            v.get("targetLang", ""),
            v.get("segmentId", ""),
            v.get("check", ""),
            v.get("rule", ""),
            v.get("message", ""),
            v.get("suggestion", ""),
        ])
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(report.get("project", {}).get("name", "qc")))[:60]
    filename = f"QA_completed_{safe_name}.xlsx"
    wb.save(filename)
    return filename


@workflows.activity(name="qc-store-report-in-phrase")
async def qc_store_report_in_phrase(filename: str) -> str:
    """Plaatst het QA-rapport als bestand in de vaste Phrase-map (rapportproject)."""
    # 1. bestaand rapportproject opzoeken (of aanmaken)
    project_uid = REPORT_PROJECT_UID
    if not project_uid:
        resp = await _rest(
            "GET", "/api2/v1/projects",
            query_params={"nameOrInternalId": REPORT_PROJECT_NAME, "pageSize": 5},
        )
        content = resp.get("content", []) if isinstance(resp, dict) else []
        for p in content:
            if (p.get("name") or "") == REPORT_PROJECT_NAME:
                project_uid = p.get("uid")
                break
    if not project_uid:
        created = await _rest(
            "POST", "/api2/v3/projects",
            body={
                "name": REPORT_PROJECT_NAME,
                "sourceLang": "nl",
                "targetLangs": ["en_gb"],
                "note": "Rapporten van de automatische kwaliteitscontrole na voorvertaling (Mistral, 0 AIU's). Elke check plaatst hier een Excel-rapport.",
            },
        )
        project_uid = created.get("uid") if isinstance(created, dict) else None
    if not project_uid:
        return "rapportproject niet gevonden/aangemaakt"

    # 2. rapport uploaden als job in dat project
    with open(filename, "rb") as fh:
        content = fh.read()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    job_name = f"QA_rapport_{ts}.xlsx"
    resp = await _rest_upload(
        f"/api2/v1/projects/{project_uid}/jobs",
        job_name,
        content,
        {"targetLangs": ["en_gb"], "path": "QA-rapporten"},
    )
    jobs = resp.get("jobs", []) if isinstance(resp, dict) else []
    job_uid = jobs[0].get("uid") if jobs else "?"
    return f"Phrase-map '{REPORT_PROJECT_NAME}' ({project_uid}), job {job_uid} ({job_name})"


@workflows.activity(name="qc-upload-sharepoint")
async def qc_upload_sharepoint(
    filename: str,
    sp: ToolCallClient = Depends(sharepoint_connector),
) -> str:
    import base64

    with open(filename, "rb") as fh:
        content = fh.read()
    resp = await sp.call_tool(
        tool_name="upload_binary_file",
        arguments={
            "drive_id": SHAREPOINT_DRIVE_ID,
            "folder_id": SHAREPOINT_FOLDER_ID,
            "filename": filename,
            "content_base64": base64.b64encode(content).decode(),
        },
    )
    text = str(resp)
    m = re.search(r"https://[^\"'\s\\]+", text)
    return m.group(0) if m else text


@workflows.activity(name="qc-dm-owner")
async def qc_dm_owner(owner_email: str, project_name: str, vcount: int, report_url: str) -> str:
    """Persoonlijk Teams-bericht (DM) naar de project-eigenaar via Microsoft Graph."""
    if not owner_email:
        return "geen owner-email"
    if not (MS_GRAPH_TOKEN or (MS_GRAPH_TENANT_ID and MS_GRAPH_CLIENT_ID and MS_GRAPH_CLIENT_SECRET)):
        return "geen MS Graph credentials (secrets ms_graph_*)"
    user = await _graph("GET", f"/users/{owner_email}")
    if not user or not user.get("id"):
        return f"gebruiker niet gevonden in Entra: {owner_email}"
    chat = await _graph(
        "POST", "/chats",
        {
            "chatType": "oneOnOne",
            "members": [
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{MS_GRAPH_BASE}/users/{user['id']}",
                }
            ],
        },
    )
    chat_id = (chat or {}).get("id")
    if not chat_id:
        return "chat aanmaken mislukt"
    content = (
        f"De voorvertaling van project {project_name} is klaar en de kwaliteitscontroles zijn door "
        f"Mistral uitgevoerd. Je kunt de voorvertaling nu naar de post-editor sturen.\n"
        f"Bevindingen: {vcount}."
    )
    if report_url and report_url.startswith("http"):
        content += f"\nRapport met wijzigingen (SharePoint): {report_url}"
    await _graph(
        "POST", f"/chats/{chat_id}/messages",
        {"body": {"contentType": "text", "content": content}},
    )
    return f"DM naar {owner_email} (project {project_name})"


@workflows.activity(name="qc-notify-teams")
async def qc_notify_teams(
    report: dict,
    owners: list[dict],
    report_url: str = "",
    teams: ToolCallClient = Depends(teams_connector),
) -> str:
    p = report["project"]["name"]
    vcount = report.get("violationCount", 0)
    scount = report.get("segmentCount", 0)
    by_check: dict[str, int] = {}
    for v in report.get("violations", []):
        by_check[v.get("check", "?")] = by_check.get(v.get("check", "?"), 0) + 1
    breakdown = ", ".join(f"{k}: {n}" for k, n in sorted(by_check.items())) or "geen"

    # Owners als mention toevoegen (bericht naar owner via @-mention in 'Phrase updates')
    mentions: list[dict] = []
    owner_lines: list[str] = []
    for o in owners:
        email = o.get("email") or ""
        name = o.get("name") or o.get("userName") or email
        proj = o.get("project") or "?"
        line = (
            f"- De voorvertaling van project **{proj}** is klaar en de kwaliteitscontroles zijn door "
            f"Mistral uitgevoerd. Je kunt de voorvertaling nu naar de post-editor sturen."
        )
        if email:
            try:
                members = await teams.call_tool(
                    tool_name="list_team_members",
                    arguments={"team_id": TEAMS_TEAM_ID, "query": email, "limit": 5},
                )
                for m in (members.get("members") or []) if isinstance(members, dict) else []:
                    if m.get("email") and m["email"].lower() == email.lower() and m.get("user_id"):
                        mentions.append({"user_id": m["user_id"], "display_name": m.get("display_name") or name})
                        line += f" — @{m.get('display_name') or name} ({email})"
                        break
                else:
                    line += f" — owner {name} ({email})"
            except Exception:
                line += f" — owner {name} ({email})"
        owner_lines.append(line)

    content = (
        f"De voorvertaling van project **{p}** is klaar en de kwaliteitscontroles zijn door Mistral uitgevoerd. "
        f"Je kunt de voorvertaling nu naar de post-editor sturen."
        f"\n- Segmenten gecontroleerd: {scount}"
        f"\n- Bevindingen: {vcount} ({breakdown})"
        f"\n- Checks: consistentie (incl. terminologie), termbase-terminologie, contentgroep-regels, en-GB zonder Oxford comma."
        f"\n- Rapport: Phrase-project 'QA-checks na voorvertaling (Mistral)' (map QA-rapporten)."
    )
    if report_url and report_url.startswith("http"):
        content += f"\n- Rapport met wijzigingen (SharePoint): {report_url}"
    if owner_lines:
        content += "\n\nEigenaren om te informeren:\n" + "\n".join(owner_lines)

    resp = await teams.call_tool(
        tool_name="post_channel_message",
        arguments={
            "team_id": TEAMS_TEAM_ID,
            "channel_id": TEAMS_CHANNEL_ID,
            "subject": f"Kwaliteitscontrole na voorvertaling (Completed): {p}",
            "content": content,
            "mentions": mentions or None,
        },
    )
    return str(resp)


@workflows.workflow.define(
    name="phrase-qa-completed-check",
    workflow_display_name="Kwaliteitscontrole na voorvertaling (Completed) — Mistral",
    workflow_description=(
        "Bij elke voorvertaling die op Completed staat (JOB_STATUS_CHANGED of poll op workflow level 1): "
        "kwaliteitscontrole via Mistral — consistentie (incl. terminologie), termbase-terminologie, "
        "contentgroep-regels en en-GB zonder Oxford comma. Phrase-data via plain REST (0 AIU's); "
        "Teams/SharePoint via connectors."
    ),
)
@uses_connectors(teams_connector, sharepoint_connector)
class PhraseQaCompletedCheckWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, event: Any = None) -> str:
        payloads: list[dict] = []
        raw = getattr(event, "body", event) if event is not None else None
        if isinstance(raw, dict) and raw:
            payload = await qc_filter_event(raw)
            if payload:
                if not is_completed_pretranslation_event(payload):
                    return "genegeerd: geen COMPLETED-voorvertaling op level 1"
                payloads = [payload]
        if not payloads:
            # poll-modus (scheduled run, zonder webhook)
            payloads = await qc_poll_completed(QC_POLL_WINDOW_MINUTES)
            if not payloads:
                return "geen nieuwe COMPLETED-voorvertalingen (level 1) gevonden"

        all_violations: list[dict] = []
        segment_count = 0
        processed: list[str] = []
        owners: list[dict] = []
        for payload in payloads:
            context = await qc_fetch_context(payload)
            if not context.get("check"):
                continue
            report = await qc_mistral_check(context)
            if report.get("error"):
                return f"fout: {report['error']}"
            segment_count += report.get("segmentCount", 0)
            pname = context["project"]["name"]
            owner = context["project"].get("owner") or {}
            owners.append({"project": pname, **owner})
            for v in report.get("violations", []):
                v["project"] = pname
                all_violations.append(v)
            for part in context.get("parts", []):
                await qc_mark_processed(part["uid"])
                processed.append(part["uid"])

        agg = {
            "project": {"name": f"{len(processed)} voorvertaling-job(s)"},
            "segmentCount": segment_count,
            "violationCount": len(all_violations),
            "violations": all_violations,
        }
        notes: list[str] = []
        filename = await qc_build_xlsx(agg)
        try:
            phrase_loc = await qc_store_report_in_phrase(filename)
            notes.append(f"rapport in Phrase: {phrase_loc}")
        except Exception as exc:
            notes.append(f"rapport naar Phrase mislukt: {exc}")
        report_url = ""
        try:
            report_url = await qc_upload_sharepoint(filename)
        except Exception as exc:
            notes.append(f"sharepoint-upload mislukt (workspace-credential?): {exc}")
        # 1) persoonlijk DM naar elke owner via Microsoft Graph (als credentials er zijn)
        for o in owners:
            if o.get("email"):
                try:
                    dm = await qc_dm_owner(o["email"], o.get("project") or "?", len(all_violations), report_url)
                    notes.append(dm)
                except Exception as exc:
                    notes.append(f"DM naar {o.get('email')} mislukt: {exc}")
        # 2) fallback/kanaal: melding in 'Phrase updates' met owner-mentions
        try:
            result = await qc_notify_teams(agg, owners, report_url)
        except Exception as exc:
            notes.append(f"teams-kanaalmelding mislukt (workspace-credential?): {exc}")
            result = "rapport niet gemeld via Teams-kanaal"
        if notes:
            return result + " | " + "; ".join(notes)
        return result
