"""Realtime kwaliteitscheck direct na Phrase-voorvertaling (Taalcentrum-VU).

Getriggerd door de Mistral-webhook (Phrase event-webhook) bij
JOB_STATUS_CHANGED / COMPLETED op workflow level 1 (voorvertaling PLAI /
MT-profiel). Voert uit, zodra het project een contentgroep EN termbase heeft:

1. Terminologie uit de gekoppelde termbase(s) toetsen op de targets.
2. Contentgroep-stijlregels toetsen (o.a. UT-regels; en-GB-conversie).
3. Correcties terugschrijven naar Phrase (bilingual-file flow,
   saveToTransMemory=Confirmed, geen setCompleted, geen AIU's).
4. Excel-overzicht van wijzigingen -> SharePoint-map "Mistral QA voorvertaling"
   (Vertalen intern > Phrase).
5. Teams-melding in het kanaal "Phrase updates".

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


def _auth() -> dict:
    return {
        "Authorization": f"ApiToken {os.environ['PHRASE_API_TOKEN']}",
        "Accept": "application/json",
    }


def is_relevant_event(payload: dict) -> bool:
    """COMPLETED-voorvertalingen op workflow level 1."""
    if payload.get("event") != "JOB_STATUS_CHANGED":
        return False
    parts = payload.get("jobParts") or []
    return any(
        p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1) == 1
        for p in parts
    )


# --------------------------------------------------------------------------
# Controles (pure functies, unit-testbaar)
# --------------------------------------------------------------------------

# en-GB conversie: (Amerikaans, Brits). Alleen veilige, eenduidige gevallen.
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

# UT-regels die op zinsniveau toetsbaar zijn.
UT_RE_THE_UT = re.compile(r"\bthe UT\b", re.IGNORECASE)
UT_RE_APOSTROPHE_PLURAL = re.compile(r"\b(\d{4})'s\b")
UT_RE_ABBR_DOTS = re.compile(r"\b(U\.T\.|Ph\.D\.)\b")


def _is_en_gb(target_lang: str) -> bool:
    return "en_gb" in (target_lang or "").lower() or (target_lang or "").lower() == "en-gb"


def extract_preferred_terms(terms: list) -> list[str]:
    """Haalt de voorkeurstermen (target-zijde) uit een termbase-response."""
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
                # Voorkeur: eerste term in de doeltaan van de entry.
                preferred.append(t["term"])
                break
    return [t for t in preferred if t]


def apply_fixes(text: str, preferred_terms: list[str], target_lang: str) -> tuple[str, list[str]]:
    """Past termbase-termen, en-GB-conversie en UT-regels toe."""
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
        r = await http.get(f"{_api_base()}/api2/v2/projects/{project_uid}", headers=_auth())
        r.raise_for_status()
        details = r.json()

        r = await http.get(
            f"{_api_base()}/api2/v1/projects/{project_uid}/termBases", headers=_auth()
        )
        term_bases = r.json().get("termBases", []) if r.status_code == 200 else []

        r = await http.get(
            f"{_api_base()}/api2/v1/projects/{project_uid}/contentGroups", headers=_auth()
        )
        content_groups = r.json().get("contentGroups", []) if r.status_code == 200 else []

        raw_terms: list = []
        for tb in term_bases:
            tb_uid = tb.get("id") or tb.get("uid")
            if not tb_uid:
                continue
            try:
                r = await http.get(
                    f"{_api_base()}/api2/v1/termBases/{tb_uid}/terms",
                    headers=_auth(),
                    params={"pageSize": 500},
                )
                if r.status_code == 200:
                    raw_terms.extend(r.json().get("terms") or [])
            except Exception:
                continue

    return {
        "check": bool(content_groups) and bool(term_bases),
        "project": {
            "uid": project_uid,
            "name": details.get("name") or project.get("name") or "?",
        },
        "contentGroups": [g.get("id") for g in content_groups],
        "termBases": [t.get("id") or t.get("uid") for t in term_bases],
        "preferredTerms": extract_preferred_terms(raw_terms),
        "reason": (
            ""
            if (content_groups and term_bases)
            else f"contentgroep={bool(content_groups)} termbase={bool(term_bases)}"
        ),
    }


@workflows.activity(name="qa-check-job")
async def qa_check_job(payload: dict, info: dict) -> dict:
    """Download bilingual file per afgeronde job, pas fixes toe, schrijf terug."""
    import httpx

    token = os.environ["PHRASE_API_TOKEN"]
    auth = _auth()
    project_uid = info["project"]["uid"]
    completed = [
        p
        for p in (payload.get("jobParts") or [])
        if p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1) == 1
    ]
    results: list[dict] = []
    terms = info.get("preferredTerms", [])

    async with httpx.AsyncClient(timeout=180) as http:
        for part in completed:
            part_uid = part.get("uid")
            part_id = part_uid.get("uid") if isinstance(part_uid, dict) else part_uid
            target_lang = part.get("targetLang", "")
            try:
                r = await http.get(
                    f"{_api_base()}/api2/v1/projects/{project_uid}/jobs/{part_id}/bilingualFile",
                    headers=auth,
                    params={"format": "MXLF"},
                )
                if r.status_code != 200:
                    results.append({"part": part_id, "error": f"download {r.status_code}"})
                    continue
                xml = r.content.decode("utf-8", errors="replace")

                part_reasons: list[str] = []
                changed_xml_holder = {"xml": xml}

                def _fix_target(m):
                    original = m.group(1)
                    unescaped = original.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                    fixed, reasons = apply_fixes(unescaped, terms, target_lang)
                    if reasons:
                        part_reasons.extend(reasons)
                        re_escaped = fixed.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        return "<target>" + re_escaped + "</target>"
                    return m.group(0)

                changed_xml_holder["xml"] = re.sub(
                    r"<target>(.*?)</target>", _fix_target, xml, flags=re.S
                )

                if part_reasons:
                    ur = await http.post(
                        f"{_api_base()}/api2/v1/projects/{project_uid}/bilingualFiles",
                        headers={"Authorization": f"ApiToken {token}"},
                        files={"file": ("corrected.mxlf", changed_xml_holder["xml"].encode("utf-8"), "application/xml")},
                        data={"saveToTransMemory": "Confirmed", "jobParts": part_id},
                    )
                    results.append({
                        "part": part_id,
                        "targetLang": target_lang,
                        "reasons": part_reasons,
                        "uploaded": ur.status_code in (200, 201),
                        "uploadStatus": ur.status_code,
                    })
                else:
                    results.append({
                        "part": part_id, "targetLang": target_lang,
                        "reasons": [], "uploaded": False, "clean": True,
                    })
            except Exception as exc:
                results.append({"part": part_id, "error": str(exc)})

    return {"project": info["project"], "results": results}


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
