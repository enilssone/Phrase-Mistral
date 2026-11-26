"""Diagnose: welke EU-Phrase-host accepteert de PHRASE_API_TOKEN-secret?

Test alle EU-hosts (cloud.memsource.com en eu.phrase.com) met ApiToken- en
Bearer-schemas. Retorna alléén HTTP-statuscodes. Geen US-hosts.
"""

import json
import os

import mistralai.workflows as workflows


@workflows.activity(name="qa-hostcheck")
async def qa_hostcheck(payload: dict) -> dict:
    import httpx

    token = os.environ["PHRASE_API_TOKEN"].strip()
    project = "mm3dMmy2fQU5aVDi2kj9s2"
    combos = [
        ("memsource_web_apitoken", f"https://cloud.memsource.com/web/api2/v1/projects/{project}", f"ApiToken {token}"),
        ("euphrase_web_apitoken", f"https://eu.phrase.com/web/api2/v1/projects/{project}", f"ApiToken {token}"),
        ("euphrase_web_bearer", f"https://eu.phrase.com/web/api2/v1/projects/{project}", f"Bearer {token}"),
        ("euphrase_api2_apitoken", f"https://eu.phrase.com/api2/v1/projects/{project}", f"ApiToken {token}"),
        ("euphrase_api2_bearer", f"https://eu.phrase.com/api2/v1/projects/{project}", f"Bearer {token}"),
    ]
    out = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
        for label, url, auth_value in combos:
            try:
                r = await http.get(url, headers={"Authorization": auth_value, "Accept": "application/json"})
                out[label] = r.status_code
            except Exception as exc:
                out[label] = str(exc)[:100]
    out["token_len"] = len(token)
    out["token_prefix"] = token[:3] + "..." if len(token) > 3 else "?"
    return out


@workflows.workflow.define(
    name="phrase-qa-hostcheck",
    workflow_display_name="QA hostcheck (diagnose)",
    workflow_description="Diagnosticeert welke EU-Phrase-host het secret-token accepteert.",
)
class PhraseQaHostcheckWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, event: dict) -> str:
        result = await qa_hostcheck({})
        return json.dumps(result)
