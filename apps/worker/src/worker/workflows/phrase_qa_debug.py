"""Tijdelijke debug-workflow: controleert de Phrase-token van het deployment."""

import os

import mistralai.workflows as workflows
from mistralai.workflows.plugins.triggers import TriggerEvent


@workflows.activity(name="qa-debug-probe")
async def qa_debug_probe(payload: dict) -> dict:
    import httpx

    token = os.environ.get("PHRASE_API_TOKEN", "")
    base = "https://api.eu.phrase.com"
    headers = {"Authorization": f"ApiToken {token}", "Accept": "application/json"}
    checks = {}
    async with httpx.AsyncClient(timeout=30) as http:
        for name, path in [
            ("me", "/api2/v1/me"),
            ("users", "/api2/v1/users?limit=1"),
            ("project_v1", "/api2/v1/projects/mm3dMmy2fQU5aVDi2kj9s2"),
        ]:
            try:
                r = await http.get(base + path, headers=headers)
                body = r.text[:200]
                checks[name] = f"{r.status_code}: {body}"
            except Exception as exc:
                checks[name] = f"ERR: {exc}"
    return {
        "token_length": len(token),
        "token_prefix": token[:4] if token else "",
        "token_suffix": token[-4:] if token else "",
        "token_has_space": " " in token,
        "token_has_quote": any(q in token for q in ["\"", "'"]),
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
