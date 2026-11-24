# Phrase-Mistral

Mistral Workflow die Phrase TMS events (`PROJECT_CREATED`, `JOB_CREATED`, `JOB_STATUS_CHANGED`)
via een webhook-trigger vertaalt naar berichten in het Teams-kanaal **Phrase updates**
(team *Vertaalbureau*).

## Structuur
- `apps/worker/` - Python worker (Mistral Workflows SDK)
  - `src/worker/workflows/phrase_teams_notifier.py` - de workflow `phrase-teams-notifier`
  - `src/worker/workflows/phrase_deadline_digest.py` - de workflow `phrase-deadline-digest`
    (gepland: dagelijks 09:00 Europe/Amsterdam overzicht van projecten met die dag als deadline -> Teams "Phrase updates")
  - `src/worker/entrypoints/worker.py` - worker entrypoint
- `deploy/docker/Dockerfile.worker` - image voor de managed deployment
- `tests/` - unit tests voor de opmaak en workflow-registratie

## phrase-deadline-digest
- Leest alle niet-gearchiveerde projecten via de Phrase-connector (workspace credential, `run_as="deployment"`).
- Filtert op deadline (dateDue, Europe/Amsterdam) gelijk aan de uitvoeringsdag.
- Post het overzicht via de Incoming Webhook van het kanaal "Phrase updates"
  (workspace secret `TEAMS_WEBHOOK_URL`).
- Input-opties voor handmatige runs: `{"date": "2026-11-24", "dry_run": true}`.
