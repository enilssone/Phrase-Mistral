"""Kwaliteitscontrole na voorvertaling (Completed) — checks via Mistral, 0 AIU's in Phrase.

Getriggerd door de Phrase event-webhook (JOB_STATUS_CHANGED) zodra een job op
workflow level 1 (voorvertaling) op COMPLETED staat.

Alle data (segmenten, termbase, contentgroep-regels) komt uit gewone Phrase
REST-calls met PHRASE_API_TOKEN (0 AIU's). De checks zelf worden uitgevoerd
door een Mistral-model (alleen Mistral, geen Phrase-AI):

1. consistentie: identieke bronsegmenten moeten identieke vertalingen hebben;
2. terminologie: goedgekeurde termbase-termen moeten consistent worden gebruikt;
3. contentgroep: alle regels van de gekoppelde content group worden per
   segment geëvalueerd.

Rapport (Excel) -> SharePoint-map "Mistral QA voorvertaling", melding in Teams.
"""

import json
import os
import re
from typing import Optional

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)
from mistralai.workflows.plugins.triggers import TriggerEvent

teams_connector = connector("teams", run_as="deployment")
sharepoint_connector = connector("sharepoint_mcp", run_as="deployment")

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

# Limieten om promptgrootte behapbaar te houden.
MAX_SEGMENTS_PER_CALL = 40
MAX_SEGMENTS_TOTAL = 400


def _tms_base() -> str:
    return os.environ.get("PHRASE_API_BASE", "https://api.phrase.com").rstrip("/")


def _auth() -> dict:
    return {
        "Authorization": f"ApiToken {os.environ['PHRASE_API_TOKEN']}",
        "Accept": "application/json",
    }


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
async def qc_filter_event(event: TriggerEvent) -> dict:
    body = event.body
    return body if isinstance(body, dict) else {}


@workflows.activity(name="qc-fetch-context")
async def qc_fetch_context(payload: dict) -> dict:
    """Haalt project, termbase-termen, contentgroep-regels en segmenten op (0 AIU)."""
    import httpx

    project = payload.get("project") or {}
    project_uid = project.get("uid")
    if not project_uid:
        return {"check": False, "reason": "geen project-uid"}

    base = _tms_base()
    auth = _auth()
    parts = completed_parts(payload)

    async with httpx.AsyncClient(timeout=120) as http:
        r = await http.get(f"{base}/api2/v2/projects/{project_uid}", headers=auth)
        r.raise_for_status()
        details = r.json()

        r = await http.get(f"{base}/api2/v1/projects/{project_uid}/termBases", headers=auth)
        term_bases = r.json().get("termBases", []) if r.status_code == 200 else []

        raw_terms: list = []
        for tb in term_bases:
            tb_uid = tb.get("id") or tb.get("uid")
            if not tb_uid:
                continue
            try:
                r = await http.get(
                    f"{base}/api2/v1/termBases/{tb_uid}/terms",
                    headers=auth,
                    params={"pageSize": 500},
                )
                if r.status_code == 200:
                    for entry in r.json().get("terms") or []:
                        terms = entry.get("terms") if isinstance(entry, dict) else None
                        if isinstance(terms, list):
                            for t in terms:
                                if isinstance(t, dict) and t.get("term"):
                                    raw_terms.append(t["term"])
                                elif isinstance(t, str):
                                    raw_terms.append(t)
            except Exception:
                continue

        r = await http.get(f"{base}/api2/v1/projects/{project_uid}/contentGroups", headers=auth)
        content_groups = r.json().get("contentGroups", []) if r.status_code == 200 else []

        rules_md_parts: list[str] = []
        for g in content_groups:
            gid = g.get("id") or g.get("uid")
            if not gid:
                continue
            try:
                r = await http.get(f"{base}/api/v1/rules/{gid}/export", headers=auth)
                if r.status_code == 200 and r.text.strip():
                    rules_md_parts.append(r.text.strip())
            except Exception:
                continue

        segments: list[dict] = []
        for part in parts:
            part_uid = part.get("uid")
            part_id = part_uid.get("uid") if isinstance(part_uid, dict) else part_uid
            target_lang = part.get("targetLang", "")
            try:
                r = await http.get(
                    f"{base}/api2/v1/projects/{project_uid}/jobs/{part_id}/segments",
                    headers=auth,
                    params={"beginIndex": 0, "endIndex": MAX_SEGMENTS_TOTAL - 1},
                )
                if r.status_code == 200:
                    for rec in r.json().get("segments") or r.json().get("content") or []:
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
    return {
        "check": bool(segments),
        "project": {
            "uid": project_uid,
            "name": details.get("name") or project.get("name") or "?",
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
            report.get("project", {}).get("name", ""),
            v.get("targetLang", ""),
            v.get("segmentId", ""),
            v.get("check", ""),
            v.get("rule", ""),
            v.get("message", ""),
            v.get("suggestion", ""),
        ])
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(report.get("project", {}).get("name", "project")))[:60]
    filename = f"QA_completed_{safe_name}.xlsx"
    wb.save(filename)
    return filename


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
    return str(resp)


@workflows.activity(name="qc-notify-teams")
async def qc_notify_teams(
    report: dict,
    teams: ToolCallClient = Depends(teams_connector),
) -> str:
    p = report["project"]["name"]
    vcount = report.get("violationCount", 0)
    scount = report.get("segmentCount", 0)
    by_check: dict[str, int] = {}
    for v in report.get("violations", []):
        by_check[v.get("check", "?")] = by_check.get(v.get("check", "?"), 0) + 1
    breakdown = ", ".join(f"{k}: {n}" for k, n in sorted(by_check.items())) or "geen"
    resp = await teams.call_tool(
        tool_name="teams_post_channel_message",
        arguments={
            "team_id": TEAMS_TEAM_ID,
            "channel_id": TEAMS_CHANNEL_ID,
            "subject": f"Kwaliteitscontrole na voorvertaling (Completed): {p}",
            "content": (
                f"Voorvertaling van **{p}** staat op Completed — kwaliteitscontrole uitgevoerd "
                f"via Mistral (0 AIU's in Phrase)."
                f"\n- Segmenten gecontroleerd: {scount}"
                f"\n- Bevindingen: {vcount} ({breakdown})"
                f"\n- Checks: consistentie, termbase-terminologie, contentgroep-regels."
                f"\n- Detail: SharePoint > Vertalen intern > Phrase > Mistral QA voorvertaling."
            ),
        },
    )
    return str(resp)


@workflows.workflow.define(
    name="phrase-qa-completed-check",
    workflow_display_name="Kwaliteitscontrole na voorvertaling (Completed) — Mistral",
    workflow_description=(
        "Bij elke voorvertaling die op Completed staat (JOB_STATUS_CHANGED, workflow level 1): "
        "kwaliteitscontrole via Mistral — consistentie, termbase-terminologie en contentgroep-regels. "
        "Alleen plain Phrase REST voor data (0 AIU's); checks worden door Mistral uitgevoerd."
    ),
)
@uses_connectors(teams_connector, sharepoint_connector)
class PhraseQaCompletedCheckWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, event: TriggerEvent) -> str:
        payload = await qc_filter_event(event)
        if not is_completed_pretranslation_event(payload):
            return "genegeerd: geen COMPLETED-voorvertaling op level 1"
        context = await qc_fetch_context(payload)
        if not context.get("check"):
            return f"genegeerd: {context.get('reason')}"
        report = await qc_mistral_check(context)
        if report.get("error"):
            return f"fout: {report['error']}"
        filename = await qc_build_xlsx(report)
        await qc_upload_sharepoint(filename)
        return await qc_notify_teams(report)
