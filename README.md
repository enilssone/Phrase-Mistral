# Phrase-Mistral

Mistral Workflow die Phrase TMS events (`PROJECT_CREATED`, `JOB_CREATED`, `JOB_STATUS_CHANGED`)
via een webhook-trigger vertaalt naar berichten in het Teams-kanaal **Phrase updates**
(team *Vertaalbureau*).

## Structuur
- `apps/worker/` - Python worker (Mistral Workflows SDK)
  - `src/worker/workflows/phrase_teams_notifier.py` - de workflow `phrase-teams-notifier`
  - `src/worker/entrypoints/worker.py` - worker entrypoint
- `deploy/docker/Dockerfile.worker` - image voor de managed deployment
- `tests/` - unit tests voor de opmaak en workflow-registratie
