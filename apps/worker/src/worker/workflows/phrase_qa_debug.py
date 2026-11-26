"""Tijdelijke debug-workflow: controleert de Phrase-tokens van het deployment."""

import os

import mistralai.workflows as workflows
from mistralai.workflows.plugins.triggers import TriggerEvent


@workflows.activity(name="qa-debug-probe")
async def qa_debug_probe(payload: dict) -> dict:
    import httpx

    proj = "mm3dMmy2fQU5aVDi2kj9s2"
    out = {}
    for var in ["PHRASE_API_TOKEN", "PHRASE_TOKEN_ALT"]:
        tok = os.environ.get(var, "")
        out[var] = {
            "len": len(tok),
            "prefix": tok[:4],
            "suffix": tok[-4:],
        }
    token = os.environ.get("PHRASE_API_TOKEN", "")
    alt = os.environ.get("PHRASE_TOKEN_ALT", "")
    headers1 = {"Authorization": f"ApiToken {token}"}
    headers2 = {"Authorization": f"ApiToken {alt}"}
    checks = {}
    async with httpx.AsyncClient(timeout=30) as http:
        for name, hdrs in [("primary_eu", headers1), ("alt_eu", headers2)]:
            try:
                r = await http.get("https://cloud.memsource.com/web/api2/v1/projects/" + proj, headers=hdrs)
                checks[name] = f"{r.status_code}: {r.text[:120]}"
            except Exception as exc:
                checks[name] = f"ERR: {exc}"
    return {"tokens": out, "checks": checks}


@workflows.workflow.define(
    name="phrase-qa-debug",
    workflow_display_name="QA debug",
    workflow_description="Tijdelijke diagnostic workflow voor Phrase-token.",
)
class PhraseQaDebugWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, event: TriggerEvent) -> str:
        import json

        body = event.body if isinstance(event.body, dict) else {}
        result = await qa_debug_probe(body)
        return json.dumps(result, ensure_ascii=False)
