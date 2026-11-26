"""Realtime kwaliteitscheck direct na Phrase-voorvertaling (Taalcentrum-VU).

Getriggerd bij JOB_STATUS_CHANGED / COMPLETED op workflow level 1
(voorvertaling PLAI / MT-profiel / custom MT). Voert uit, zodra het project
een contentgroep EN termbase heeft:

1. Terminologie: termbase-termen + MT-glossary (PLAI-profiel, indien leesbaar).
2. Contentgroep: vaststellen via /contentGroup; stijlgidsen ophalen.
3. Consistentie: dezelfde brontekst met verschillende targets signaleren.
4. Stijl via Mistral (workflow-tokens, 0 AIU's in Phrase): domein-/subdomein-
   metadata, stijlgids-namen en terminologie in de prompt; en-GB-controles
   (Amerikaans -> Brits, geen Oxfordkomma) rule-based afgedwongen.
5. Correcties terugschrijven naar Phrase (bilingual-file flow,
   saveToTransMemory=Confirmed, geen setCompleted, geen AIU's).
6. Excel-overzicht -> SharePoint-map "Mistral QA voorvertaling".
7. Teams-melding in kanaal "Phrase updates" + persoonlijk bericht aan de
   project-owner (indien een 1-op-1 chat bestaat).
"""

import base64
import json
import os
import re

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai import (
    ChatCompletionRequest,
    UserMessage,
    mistralai_chat_complete,
)
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
LLM_MODEL = os.environ.get("QA_LLM_MODEL", "mistral-small-latest")
LLM_MAX_SEGMENTS = int(os.environ.get("QA_LLM_MAX_SEGMENTS", "80"))


def _api_base() -> str:
    return os.environ.get("PHRASE_API_BASE", "https://api.phrase.com/api2").rstrip("/")


def _auth() -> dict:
    return {
        "Authorization": f"ApiToken {os.environ['PHRASE_API_TOKEN']}",
        "Accept": "application/json",
    }


def is_relevant_event(payload: dict) -> bool:
    """COMPLETED-voorvertalingen op workflow level 1 (PLAI/MT-profiel/custom MT)."""
    if payload.get("event") != "JOB_STATUS_CHANGED":
        return False
    parts = payload.get("jobParts") or []
    return any(
        p.get("status") == "COMPLETED" and int(p.get("workflowLevel") or 1) == 1
        for p in parts
    )


# --------------------------------------------------------------------------
# Rule-based controles (pure functies, unit-testbaar)
# --------------------------------------------------------------------------

# en-GB conversie: (Amerikaans, Brits). Alleen veilige, eenduidige gevallen.
EN_GB_PAIRS = [
    ("organize", "organise"), ("organized", "organised"), ("organizing", "organising"),
    ("organization", "organisation"), ("organizations", "organisations"),
    ("recognize", "recognise"), ("realize", "realise"), ("realization", "realisation"),
    ("analyze", "analyse"), ("analyzed", "analysed"), ("catalog", "catalogue"),
    ("dialog", "dialogue"), ("color", "colour"), ("colors", "colours"),
    ("colored", "coloured"), ("colorful", "colourful"), ("coloring", "colouring"),
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
# Oxfordkomma: komma direct voor and/or/nor (voor het laatste item)
OXFORD_COMMA_RE = re.compile(r",\s+(?=(?:and|or|nor)\b)", re.IGNORECASE)


def _is_en_gb(target_lang: str) -> bool:
    tl = (target_lang or "").lower().replace("-", "_")
    return "en_gb" in tl or tl == "en-gb"


def extract_preferred_terms(terms: list) -> list:
    """Haalt de voorkeurstermen (target-zijde) uit een termbase-response."""
    preferred = []
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


def apply_fixes(text: str, preferred_terms: list, target_lang: str):
    """Past termbase-termen, en-GB-conversie, Oxfordkomma en UT-regels toe."""
    reasons = []
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
        if OXFORD_COMMA_RE.search(out):
            out = OXFORD_COMMA_RE.sub(" ", out)
            reasons.append("en-GB: Oxfordkomma verwijderd")

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


def consistency_issues(pairs: list, limit: int = 20):
    """Zelfde brontekst met afwijkende targets signaleren."""
    by_source = {}
    for src, tgt in pairs:
        key = re.sub(r"\s+", " ", (src or "").strip()).lower()
        if key:
            by_source.setdefault(key, {"src": src.strip(), "variants": []})
            if tgt.strip() not in by_source[key]["variants"]:
                by_source[key]["variants"].append(tgt.strip())
    issues = []
    for item in by_source.values():
        if len(item["variants"]) > 1:
            issues.append(
                f"consistentie: seg '{item['src'][:50]}' heeft {len(item['variants'])} vertalingen"
            )
            if len(issues) >= limit:
                break
    return issues


def _unescape(text: str) -> str:
    return (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&apos;", "'")
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------

@workflows.activity(name="qa-filter-event")
async def qa_filter_event(event: TriggerEvent) -> dict:
    body = event.body
    return body if isinstance(body, dict) else {}


@workflows.activity(name="qa-fetch-project-info")
async def qa_fetch_project_info(payload: dict) -> dict:
    """Projectdetails incl. domein/owner, contentgroep, stijlgidsen, termbases, MT-glossary."""
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

        # Correcte endpoint is /contentGroup (enkelvoud); antwoord is een object.
        content_groups = []
        content_group_name = None
        try:
            r = await http.get(
                f"{_api_base()}/api2/v1/projects/{project_uid}/contentGroup",
                headers=_auth(),
            )
            if r.status_code == 200:
                cg = r.json()
                if isinstance(cg, dict) and cg.get("id"):
                    content_groups = [cg]
                    content_group_name = cg.get("name")
        except Exception:
            content_groups = []

        # Stijlgidsen van het project (via de gekoppelde contentgroep)
        style_guides = []
        try:
            r = await http.get(
                f"{_api_base()}/api2/v1/projects/{project_uid}/styleGuides",
                headers=_auth(),
            )
            if r.status_code == 200:
                body = r.json()
                style_guides = [
                    g.get("name") for g in (body.get("styleGuides") or []) if isinstance(g, dict)
                ]
        except Exception:
            style_guides = []

        # MT-glossary: glossaries gekoppeld aan het PLAI-profiel (best effort)
        glossaries = []
        for mts in details.get("mtSettingsPerLanguageList") or []:
            settings = mts.get("machineTranslateSettings") or {}
            uid = settings.get("uid")
            if not uid:
                continue
            try:
                r = await http.get(
                    f"{_api_base()}/api2/v2/memsourceTranslateProfiles/{uid}/glossaries",
                    headers=_auth(),
                )
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("glossaries") or data.get("content") or []
                    for g in items:
                        if isinstance(g, dict) and g.get("name"):
                            glossaries.append(g["name"])
            except Exception:
                continue

        raw_terms = []
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

    domain = details.get("domain") or {}
    sub_domain = details.get("subDomain") or {}
    owner = details.get("owner") or {}
    return {
        "check": bool(content_groups) and bool(term_bases),
        "project": {
            "uid": project_uid,
            "name": details.get("name") or project.get("name") or "?",
        },
        "owner": {
            "email": owner.get("email"),
            "name": (owner.get("firstName") or "") + " " + (owner.get("lastName") or ""),
        },
        "domain": domain.get("name"),
        "subDomain": sub_domain.get("name"),
        "contentGroup": content_group_name,
        "contentGroups": [g.get("id") for g in content_groups],
        "styleGuides": style_guides,
        "glossaries": glossaries,
        "termBases": [t.get("id") or t.get("uid") for t in term_bases],
        "preferredTerms": extract_preferred_terms(raw_terms),
        "reason": (
            ""
            if (content_groups and term_bases)
            else f"contentgroep={bool(content_groups)} termbase={bool(term_bases)}"
        ),
    }


def _build_prompt(info: dict, target_lang: str) -> str:
    terms = info.get("preferredTerms", [])
    guides = info.get("styleGuides", [])
    return (
        "Je bent een QA-revisor voor machinevertalingen van Taalcentrum-VU. "
        "Corrigeer uitsluitend objectieve fouten: strijd met de terminologie, "
        "stijlregels of lokale conventies, of duidelijke vertaalfouten. "
        "WIJZIG NIETS als de target al goed is. "
        "Bij Brits Engels geldt: geen Oxfordkomma. "
        f"Doeltaal: {target_lang}. "
        f"Domein (metadata): {info.get('domain')} / {info.get('subDomain')}. "
        f"Contentgroep: {info.get('contentGroup')}. "
        f"Stijlgidsen: {'; '.join(guides) if guides else 'geen'}. "
        f"Verplichte terminologie: {'; '.join(terms[:200]) if terms else 'geen'}. "
        "MT-glossary: " + ('; '.join(info.get('glossaries') or []) if info.get('glossaries') else 'geen') + ". "
        "Antwoord ALLEEN met een JSON-array. Elk element: "
        '{"i": <segmentindex>, "t": "<gecorrigeerde target>", "r": "<korte reden>"}. '
        "Lege array [] als er niets te corrigeren valt."
    )


def _parse_llm_json(content: str):
    try:
        start = content.find("[")
        end = content.rfind("]")
        if start == -1 or end == -1:
            return []
        return json.loads(content[start : end + 1])
    except Exception:
        return []


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
    results = []
    terms = info.get("preferredTerms", [])

    async with httpx.AsyncClient(timeout=180) as http:
        for part in completed:
            part_uid = part.get("uid")
            part_id = part_uid.get("uid") if isinstance(part_uid, dict) else part_uid
            target_lang = part.get("targetLang", "")
            row = {
                "part": part_id,
                "targetLang": target_lang,
                "reasons": [],
                "consistency": [],
                "uploaded": False,
                "clean": False,
            }
            try:
                r = await http.get(
                    f"{_api_base()}/api2/v1/projects/{project_uid}/jobs/{part_id}/bilingualFile",
                    headers=auth,
                    params={"format": "MXLF"},
                )
                if r.status_code != 200:
                    row["error"] = f"download {r.status_code}"
                    results.append(row)
                    continue
                xml = r.content.decode("utf-8", errors="replace")

                sources = re.findall(r"<source>(.*?)</source>", xml, flags=re.S)
                targets = re.findall(r"<target>(.*?)</target>", xml, flags=re.S)
                if len(sources) != len(targets):
                    row["error"] = (
                        f"MXLF structuur: {len(sources)} sources vs {len(targets)} targets"
                    )
                    sources = None

                seg_count = len(targets)
                new_targets = list(targets)
                seg_reasons = []

                # 1. Rule-based fixes per segment
                for i, raw in enumerate(targets):
                    unescaped = _unescape(raw)
                    fixed, reasons = apply_fixes(unescaped, terms, target_lang)
                    if reasons:
                        new_targets[i] = _escape(fixed)
                        seg_reasons.extend(reasons[:3])

                # 2. Consistentie: zelfde bron, andere target
                if sources:
                    pairs = [
                        (_unescape(s), _unescape(t))
                        for s, t in zip(sources, new_targets)
                    ]
                    row["consistency"] = consistency_issues(pairs)
                    seg_reasons.extend(row["consistency"])

                # 3. Mistral-stijlcheck (0 AIU's, gebruikt workflow-tokens)
                if sources and seg_count:
                    try:
                        payload_segments = [
                            {"i": i, "s": _unescape(sources[i])[:1500], "t": _unescape(new_targets[i])[:1500]}
                            for i in range(min(seg_count, LLM_MAX_SEGMENTS))
                        ]
                        req = ChatCompletionRequest(
                            model=LLM_MODEL,
                            messages=[
                                UserMessage(
                                    content=_build_prompt(info, target_lang)
                                    + " Segments (JSON): "
                                    + json.dumps(payload_segments, ensure_ascii=False)
                                )
                            ],
                        )
                        completion = await mistralai_chat_complete(req)
                        content = completion.choices[0].message.content
                        for item in _parse_llm_json(content):
                            try:
                                i = int(item.get("i"))
                                new_t = item.get("t")
                                reason = item.get("r") or "stijlcorrectie Mistral"
                                if 0 <= i < seg_count and new_t and _unescape(new_targets[i]) != new_t:
                                    new_targets[i] = _escape(str(new_t))
                                    seg_reasons.append(f"Mistral-stijl: {reason} (segment {i + 1})")
                            except Exception:
                                continue
                    except Exception as exc:
                        seg_reasons.append(f"Waarschuwing: Mistral-stijlcheck overgeslagen ({exc})")

                # 4. XML heropbouwen als er correcties zijn
                if seg_reasons:
                    counter = {"i": 0}

                    def _repl(m):
                        idx = counter["i"]
                        counter["i"] += 1
                        if idx < seg_count and new_targets[idx] != targets[idx]:
                            return "<target>" + new_targets[idx] + "</target>"
                        return m.group(0)

                    changed_xml = re.sub(r"<target>(.*?)</target>", _repl, xml, flags=re.S)

                    ur = await http.post(
                        f"{_api_base()}/api2/v1/projects/{project_uid}/bilingualFiles",
                        headers={"Authorization": f"ApiToken {token}"},
                        files={"file": ("corrected.mxlf", changed_xml.encode("utf-8"), "application/xml")},
                        data={"saveToTransMemory": "Confirmed", "jobParts": part_id},
                    )
                    row["uploaded"] = ur.status_code in (200, 201)
                    row["uploadStatus"] = ur.status_code
                else:
                    row["clean"] = True
                row["reasons"] = seg_reasons
                results.append(row)
            except Exception as exc:
                row["error"] = str(exc)
                results.append(row)

    return {"project": info["project"], "info": info, "results": results}


@workflows.activity(name="qa-build-xlsx")
async def qa_build_xlsx(report: dict) -> str:
    """Excel-overzicht bouwen met openpyxl."""
    from openpyxl import Workbook

    info = report.get("info") or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "Wijzigingen"
    ws.append([
        "Project", "Job part", "Doeltaal", "Domein", "Stijlgidsen",
        "Correcties (redenen)", "Consistentie-issues", "Teruggeschreven",
    ])
    for row in report["results"]:
        ws.append([
            report.get("project", {}).get("name", ""),
            row.get("part"),
            row.get("targetLang", ""),
            f"{info.get('domain') or '-'} / {info.get('subDomain') or '-'}",
            "; ".join(info.get("styleGuides") or []) or "-",
            "; ".join(row.get("reasons", [])) or row.get("error", ""),
            "; ".join(row.get("consistency", [])) or "-",
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
        content = fh.read()
    resp = await sp.call_tool(
        tool_name="create_binary_file",
        arguments={
            "drive_id": SHAREPOINT_DRIVE_ID,
            "folder_id": SHAREPOINT_FOLDER_ID,
            "filename": filename,
            "base64_content": base64.b64encode(content).decode(),
        },
    )
    return str(resp)


def _teams_summary(report: dict) -> str:
    p = report["project"]["name"]
    info = report.get("info") or {}
    results = report.get("results", [])
    fixed = [r for r in results if r.get("uploaded")]
    clean = [r for r in results if r.get("clean")]
    reasons = sorted({reason for r in results for reason in r.get("reasons", [])
                       if not reason.startswith("Waarschuwing")})
    cons = sum(len(r.get("consistency") or []) for r in results)
    return (
        f"Kwaliteitscheck direct na voorvertaling uitgevoerd voor **{p}**."
        f"\n- Jobs gecontroleerd: {len(results)}"
        f"\n- Jobs met correcties (teruggeschreven naar Phrase): {len(fixed)}"
        f"\n- Jobs zonder bevindingen: {len(clean)}"
        f"\n- Consistentie-issues: {cons}"
        f"\n- Soorten bevindingen: {', '.join(reasons[:10]) if reasons else 'geen'}"
        f"\n- Domein: {info.get('domain') or '-'} / {info.get('subDomain') or '-'}"
        f"\n- Contentgroep: {info.get('contentGroup') or '-'}"
        f"\n- Stijlgidsen: {'; '.join(info.get('styleGuides') or []) or '-'}"
        f"\n- Excel: SharePoint > Vertalen intern > Phrase > Mistral QA voorvertaling."
    )


@workflows.activity(name="qa-notify-teams")
async def qa_notify_teams(
    report: dict,
    teams: ToolCallClient = Depends(teams_connector),
) -> str:
    summary = _teams_summary(report)

    # 1. Melding in het kanaal "Phrase updates"
    try:
        await teams.call_tool(
            tool_name="post_channel_message",
            arguments={
                "team_id": TEAMS_TEAM_ID,
                "channel_id": TEAMS_CHANNEL_ID,
                "subject": f"Kwaliteitscheck na voorvertaling: {report['project']['name']}",
                "content": summary,
            },
        )
        channel_ok = "kanaalmelding geplaatst"
    except Exception as exc:
        channel_ok = f"kanaalmelding mislukt: {exc}"

    # 2. Persoonlijk bericht aan de project-owner (indien 1-op-1 chat bestaat)
    owner = (report.get("info") or {}).get("owner") or {}
    owner_email = owner.get("email")
    dm_ok = "geen owner-DM (geen 1-op-1 chat gevonden of geen owner-email)"
    if owner_email:
        try:
            chats = await teams.call_tool(tool_name="list_chats", arguments={"limit": 50})
            chat_id = None
            for chat in (chats.get("chats") or []):
                members = chat.get("members") or []
                if chat.get("chat_type") == "oneOnOne" and members:
                    emails = [m.get("email") for m in members if isinstance(m, dict)]
                    if owner_email in emails:
                        chat_id = chat.get("id")
                        break
            if chat_id:
                await teams.call_tool(
                    tool_name="post_chat_message",
                    arguments={"chat_id": chat_id, "content": summary},
                )
                dm_ok = f"owner-DM verstuurd naar {owner_email}"
        except Exception as exc:
            dm_ok = f"owner-DM mislukt: {exc}"

    return f"{channel_ok}; {dm_ok}"


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
