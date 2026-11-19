"""Realtime kwaliteitscheck direct na Phrase-voorvertaling (Taalcentrum-VU).

Getriggerd door de Mistral-webhook (Phrase Orchestrator event-webhook) bij
JOB_STATUS_CHANGED / COMPLETED op workflow level 1 (voorvertaling PLAI /
MT-profiel). Voert uit, zodra het project een contentgroep EN termbase heeft:

1. Terminologie uit de gekoppelde termbase toetsen op de targets.
2. Contentgroep-stijlregels (o.a. en-GB-conversie; UT-regels) toetsen.
3. Correcties terugschrijven naar Phrase (bilingual-file flow,
   saveToTransMemory=Confirmed, geen setCompleted, geen AIU's).
4. Excel-overzicht van wijzigingen -> SharePoint-map "Mistral QA voorvertaling"
   (Vertalen intern > Phrase).
5. Teams-melding in het kanaal "Phrase updates" (chat-target via env).

Alle Phrase-calls zijn gewone REST-calls met PHRASE_API_TOKEN (0 AIU's).
"""

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


def _api_base() -> str:
    return os.environ.get("PHRASE_API_BASE", "https://api.phrase.com/api2").rstrip("/")


def is_relevant_event(payload: dict) -> bool:
    """COMPLETED-voorvertalingen op workflow level 1."""
    if payload.get("event") != "JOB_STATUS_CHANGED":
        return False
    parts = payload.get("jobParts") or []
    return any(
        (p.get("status") == "COMPLETED") and int(p.get("workflowLevel") or 1) == 1
        for p in parts
    )


# --------------------------------------------------------------------------
# Controles (pure functies, unit-testbaar)
# --------------------------------------------------------------------------

# en-GB conversie: (Amerikaans, Brits). Alleen veilige, eenduidige gevallen.
EN_GB_PAIRS = [
    ("organize", "organise"), ("organized", "organised"), ("organizing", "organising"),
    ("organization", "organisation"), ("organizations", "organisations"),
    ("recognize", "recognise"), ("realize", "realise"), ("realization", "realisation"),
    ("analyze", "analyse"), ("analyzed", "analysed"), ("catalog", "catalogue"),
    ("dialog", "dialogue"), ("color", "colour"), ("colors", "colours"),
    ("behavior", "behaviour"), ("favor", "favour"), ("honor", "honour"),
    ("neighbor", "neighbour"), ("labor", "labour"),
    ("center", "centre"), ("centers", "centres"), ("theater", "theatre"),
    ("liter", "litre"), ("meter", "metre"), ("fiber", "fibre"),
    ("traveled", "travelled"), "traveling", ("labeled", "labelled"),
    ("counseling", "counselling"), ("enrollment", "enrolment"),
    ("defense", "defence"), ("offense", "offence"), ("pretense", "pretence"),
    ("fulfill", "fulfil"), ("fulfillment", "fulfilment"), ("gray", "grey"),
    ("aluminum", "aluminium"), ("jewelry", "jewellery"),
]
EN_GB_PAIRS = [p if isinstance(p, tuple) else (p, None) for p in EN_GB_PAIRS]
EN_GB_PAIRS = [(a, b) for a, b in EN_GB_PAIRS if b]

# UT-regels die toetsbaar zijn op zinsniveau.
UT_RULES = {
    "no_article_before_ut": re.compile(r"\bthe (UT)\b", re.IGNORECASE),
    "apostrophe_plurals": re.compile(r"\b(\d{4})'s\b"),
    "abbreviation_full_stops": re.compile(r"\b(U\.T\.|Ph\.D\.)\b"),
}


def apply_fixes(text: str, terms: list, target_lang: str) -> tuple[str, list[str]]:
    """Past termbase-termen, en-GB-conversie en UT-regels toe."""
    reasons: list[str] = []
    out = text

    # 1. Termbase-termen (case-insensitive, woordgrenzen).
    for term in terms:
        src = (term.get("terms") or {})[0] if isinstance(term, dict) else None
        if not src:
            continue
        pattern = re.compile(r"\b" + re.escape(src) + r"\b", re.IGNORECASE)
        if pattern.search(out):
            out = pattern.sub(src, out)
            reasons.append(f"terminologie: {src}")

    # 2. en-GB-conversie (alleen doeltaal en-GB / en_gb).
    if "en_gb" in (target_lang or "").lower() or target_lang == "en_gb":
        for us, gb in EN_GB_PAIRS:
            pattern = re.compile(r"\b" + us + r"\b", re.IGNORECASE)
            if pattern.search(out):
                out = pattern.sub(lambda m, gb=gb: gb if m.group(0).islower() else gb.capitalize(), out)
                reasons.append(f"en-GB: {us} -> {gb}")

    # 3. UT-regels.
    m = UT_RULES["no_article_before_ut"].search(out)
    if m:
        out = UT_RULES["no_article_before_ut"].sub(r"UT", out)
        reasons.append("UT-regel: geen lidwoord voor UT")
    m = UT_RULES["apostrophe_plurals"].search(out)
    if m:
        out = UT_RULES["apostrophe_plurals"].sub(r"\1s", out)
        reasons.append("apostrof in meervoud")
    m = UT_RULES["abbreviation_full_stops"].search(out)
    if m:
        fixed = m.group(1).replace(".", "")
        out = UT_RULES["abbreviation_full_stops"].sub(fixed, out)
        reasons.append("afkorting zonder punten")
    return out, reasons


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------

@workflows.activity(name="qa-filter-event")
async def qa_filter_event(event: TriggerEvent) -> dict:
    body = event.body
    return body if isinstance(body, dict) else {}


@workflows.activity(name="qa-fetch-project-info")
async def qa_fetch_project_info(payload: dict) -> dict:
    """Projectdetails, contentgroepen, termbases + termen ophalen."""
    import httpx

    project = payload.get("project") or {}
    project_uid = project.get("uid")
    if not project_uid:
        return {"check": False, "reason": "geen project-uid"}
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.get(
            f"{_api_base()}/api2/v2/projects/{project_uid}", headers={"Authorization": f"ApiToken {os.environ['PHRASE_API_TOKEN']}", "Accept": "application/json"}
        )
        r.raise_for_status()
        details = r.json()

        r = await http.get(
            f"{_api_base()}/api2/v1/projects/{project_uid}/termBases", headers={"Authorization": f"ApiToken {os.environ['PHRASE_API_TOKEN']}"}
        )
        term_bases = r.json().get("termBases", []) if r.status_code == 200 else []

        r = await http.get(
            f"{_api_base()}/api2/v1/projects/{project_uid}/contentGroups", headers={"Authorization": f"ApiToken {os.environ['PHRASE_API_TOKEN']}"}
        )
        content_groups = r.json().get("contentGroups", []) if r.status_code == 200 else []

    terms = []
    async with None.__class__.__mro__ and __import__("httpx").AsyncClient(timeout=60) as http:
        for tb in term_bases:
            uid = tb.get("id") or tb.get("uid")
            try:
                r = await http.get(
                    f"{_api_base()}/api2/v1/termBases/{uid}/terms", headers={"Authorization": f"ApiToken {os.environ['PHRASE_API_TOKEN']}"}
                )
                terms.extend((r.json().get("terms") or [])[:500])
            except Exception:
                pass

    return {
        "check": bool(content_groups) and bool(term_bases),
        "project": {"uid": project_uid, "name": details.get("name", project.get("name", "?"))},
        "contentGroups": [g.get("id") for g in content_groups],
        "termBases": [t.get("id") or t.get("uid") for t in term_bases],
        "terms": terms,
        "reason": "" if (content_groups and term_bases) else f"cg={bool(content_groups)} tb={bool(term_bases)}",
    }


@workflows.activity(name="qa-check-job")
async def qa_check_job(payload: dict, info: dict) -> dict:
    """Download bilingual file, pas fixes toe, schrijf terug, rapporteer."""
    import httpx

    token = os.environ["PHRASE_API_TOKEN"]
    auth = {"Authorization": f"ApiToken {token}", "Accept": "application/json"}
    project_uid = info["project"]["uid"]
    completed = [p for p in (payload.get("jobParts") or []) if p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1)) == 1] if False else [
        p for p in (payload.get("jobParts") or [])
        if p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1) == 1
    ]
    corrections: list[dict] = []
    async with httpx.AsyncClient(timeout=180) as http:
        for part in completed:
            part_uid = part.get("uid")
            part_id = next(iter(part_uid)) if isinstance(part_uid, dict) else part_uid
            target_lang = part.get("targetLang", "")
            try:
                r = await http.get(
                    f"{_api_base()}/api2/v1/projects/{project_uid}/jobs/{part_id}/bilingualFile?format=MXLF",
                    headers=auth,
                )
                if r.status_code != 200:
                    corrections.append({"part": part_id, "error": f"download {r.status_code}"})
                    continue
                xml = r.content.decode("utf-8", errors="replace")

                import re as _re
                fixed_xml = xml
                part_reasons: list[str] = []

                def _fix_target(m):
                    nonlocal part_reasons
                    original = m.group(1).replace("&amp;", "&")
                    fixed, reasons = apply_fixes(original, info["terms"], target_lang)
                    if reasons:
                        part_reasons.extend(reasons)
                        return m.group(0).replace(m.group(1), fixed.replace("&", "&amp;"))
                    return m.group(0)

                fixed_xml = _re.sub(r"<target>(.*?)</target>", _fix_target, xml, flags=_re.S)

                if part_reasons:
                    files = {"file": ("corrected.mxlf", fixed_xml.encode("utf-8"), "application/xml")}
                    ur = await http.post(
                        f"{_api_base()}/api2/v1/projects/{project_uid}/bilingualFiles",
                        headers={"Authorization": f"ApiToken {token}"},
                        files=files,
                        data={
                            "saveToTransMemory": "Confirmed",
                            "jobParts": part_id,
                        },
                    )
                    corrections.append({
                        "part": part_id,
                        "targetLang": target_lang,
                        "reasons": part_reasons,
                        "uploaded": ur.status_code in (200, 201),
                        "uploadStatus": ur.status_code,
                    })
                else:
                    corrections.append({
                        "part": part_id, "targetLang": target_lang, "reasons": [], "uploaded": False, "clean": True,
                    })
            except Exception as exc:
                corrections.append({"part": part_uid, "error": str(exc)})
    return {"project": info["project"], "results": corrections}


@workflows.activity(name="qa-build-xlsx")
async def qa_build_xlsx(report: dict) -> str:
    """Excel-overzicht bouwen met openpyxl."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Wijzigingen"
    ws.append(["Datum", "Project", "Job part", "Doeltaal", "Reden(en)", "Teruggeschreven"])
    for row in report["results"]:
        ws.append([
            report.get("project", {}).get("name", ""),
            row.get("part"), row.get("targetLang", ""),
            "; ".join(row.get("reasons", [])),
            "ja" if row.get("uploaded") else "nee",
        ])
    filename = "QA_voorvertaling_" + re.sub(r"[^A-Za-z0-9_-]+", "_", str(report.get("project", {}).get("name", "project")))[:60] + ".xlsx"
    wb.save(filename)
    return filename


@workflows.activity(name="qa-upload-sharepoint")
async def qa_upload_sharepoint(
    filename: str,
    sp: ToolCallClient = Depends(sharepoint_connector),
) -> str:
    with open(filename, "rb") as fh:
        content = fh.read()
    import base64
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


@workflows.activity(name="qa-notify-teams")
async def qa_notify_teams(
    report: dict,
    teams: ToolCallClient = Depends(teams_connector),
) -> str:
    p = report["project"]["name"]
    results = report.get("results", [])
    fixed = [r for r in results if r.get("uploaded")]
    clean = [r for r in results if r.get("clean")]
    reasons = sorted({reason for r in results for reason in r.get("reasons", [])})
    resp = await teams.call_tool(
        tool_name="teams_post_channel_message",
        arguments={
            "team_id": TEAMS_TEAM_ID,
            "channel_id": TEAMS_CHANNEL_ID,
            "subject": f"Kwaliteitscheck na voorvertaling: {p}",
            "content": (
                f"Kwaliteitscheck direct na voorvertaling uitgevoerd voor **{p}**."
                f"\n- Jobs gecontroleerd: {len(results)}"
                f"\n- Jobs met correcties (teruggeschreven naar Phrase): {len(fixed)}"
                f"\n- Jobs zonder bevindingen: {len(clean)}"
                f"\n- Soorten bevindingen: {', '.join(reasons) if reasons else 'geen'}"
                f"\n- Excel-overzicht: SharePoint > Vertalen intern > Phrase > Mistral QA voorvertaling."
            ),
        },
    )
    return str(resp)


@workflows.workflow.define(
    name="phrase-qa-voorvertaling",
    workflow_display_name="Kwaliteitscheck na voorvertaling",
    workflow_description=(
        "Realtime kwaliteitscheck direct na elke Phrase-voorvertaling "
        "(contentgroep + termbase, alle talen, 0 AIU's)."
    ),
)
@uses_connectors(teams_connector, sharepoint_connector)
class PhraseQaVoorvertalingWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, event: TriggerEvent) -> str:
        payload = await qa_filter_event(event)
        if not is_relevant_event(payload):
            return "genegeerd: geen COMPLETED-voorvertaling"
        info = await qa_fetch_project_info(payload)
        if not info.get("check"):
            return f"genegeerd: {info.get('reason')}"
        report = await qa_check_job(payload, info)
        filename = await qa_build_xlsx(report)
        await qa_upload_sharepoint(filename)
        return await qa_notify_teams(report)
