"""Tijdelijke debug-workflow: controleert de Phrase-token van het deployment."""

import os

import mistralai.workflows as workflows
from mistralai.workflows.plugins.triggers import TriggerEvent


@workflows.activity(name="qa-debug-probe")
async def qa_debug_probe(payload: dict) -> dict:
    import httpx

    token = os.environ.get("PHRASE_API_TOKEN", "")
    proj = "mm3dMmy2fQU5aVDi2kj9s2"
    variants = [
        ("eu_apitoken", "https://cloud.memsource.com/web/api2/v1/projects/" + proj, {"Authorization": f"ApiToken {token}"}),
        ("eu_bearer", "https://cloud.memsource.com/web/api2/v1/projects/" + proj, {"Authorization": f"Bearer {token}"}),
        ("eu_bid", "https://cloud.memsource.com/web/api2/v1/projects/" + proj, {"Api-Token": token}),
        ("phrase_us_bearer", "https://api.phrase.com/api2/v1/projects/" + proj, {"Authorization": f"Bearer {token}"}),
        ("phrase_us_apitoken", "https://api.phrase.com/api2/v1/projects/" + proj, {"Authorization": f"ApiToken {token}"}),
        ("phrase_eu_app", "https://eu.app.phrase.com/web/api2/v1/projects/" + proj, {"Authorization": f"ApiToken {token}"}),
    ]
    checks = {}
    async with httpx.AsyncClient(timeout=30) as http:
        for name, url, headers in variants:
            try:
                r = await http.get(url, headers=headers)
                checks[name] = f"{r.status_code}: {r.text[:150]}"
            except Exception as exc:
                checks[name] = f"ERR: {exc}"
    return {
        "token_length": len(token),
        "token_prefix": token[:4] if token else "",
        "token_suffix": token[-4:] if token else "",
        "checks": checks,
    }


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
