# homelab-pim-tools

A small, deterministic tool server that gives an LLM front end (Open WebUI in my homelab) ground-truth access to a family calendar and task lists held in a CalDAV server (Radicale). Five operations, one container, no state of its own.

It exists because retrieval over personal data is the wrong tool for the job. An assistant asked "what's on Thursday" should call a function that reads the calendar and returns the events, not guess from context. Writes go the same way: "add milk to the shopping list" becomes one idempotent API call whose response is the stored item, and the model is instructed to report only what that response says.

## What it does

| Operation | Method | Path | Idempotent on |
|---|---|---|---|
| `list_tasks` | GET | `/tasks/{list}` | n/a |
| `add_task` | POST | `/tasks` | list + title (case-insensitive) + due date |
| `complete_task` | POST | `/tasks/complete` | n/a |
| `list_events` | GET | `/events?from=&to=` | n/a |
| `add_event` | POST | `/events` | title + start |

The OpenAPI document at `/openapi.json` is the integration surface. Open WebUI registers it as an external tool server and turns each `operationId` into a tool, so the operation ids and descriptions are written for the model, not for a human reader.

## Design decisions

- **Idempotency lives in the store layer, not the prompt.** A model that retries a failed turn must not create a second "Milk". `add_task` and `add_event` look for an existing open item with the same key and return it with `created: false`. The response shape makes the outcome explicit so the model can say "already on the list" instead of "added".
- **CalDAV is the store, not a database of my own.** Calendars and task lists stay in a standard format that the phones read natively (DAVx5, Tasks.org). The tool server is a second door onto the same data and can be deleted without losing anything.
- **One principal, calendars addressed by URL leaf.** Radicale exposes `/user/shopping/`, `/user/chores/`, `/user/personal/`. The leaf name is what the phone apps show, so it is the name the model uses. Display names are not trusted for lookup.
- **Store behind a `Protocol`, tested twice.** The HTTP layer is tested against an in-memory store (fast, no server). The CalDAV store has a separate round-trip test that runs only when `RADICALE_URL` is set, so CI stays hermetic and the live behaviour is still checked before a release.
- **No auth of its own in v1.** The server runs on a private network segment and can only reach Radicale with the credentials it is given. Per-user identity is the first thing to add if a second person gets chat access; that is a deliberate boundary, not an oversight.
- **Image built in CI and pinned by digest downstream.** The homelab has no registry, so GitHub Actions builds the image, Trivy scans it (HIGH/CRITICAL fail the build, unfixed ignored), and pushes to GHCR. The deployment repo references the image by digest, so a rebuild never changes what runs without a reviewed commit.

## Lessons learned

- **Symptom:** the Radicale endpoint answered 200 but the blackbox-exporter probe that would watch it reported failure. **Root cause:** Radicale's built-in server speaks HTTP/1.0 and the default `http_2xx` module only accepts 1.1 and 2.0. **Fix:** a dedicated probe module with `HTTP/1.0` in `valid_http_versions` (in the gitops repo). This server itself runs under uvicorn and answers HTTP/1.1, so its own probe uses the default module.
- **Symptom:** `ruff` flagged every FastAPI handler (`B008`, function call in argument default). **Root cause:** `Depends()` and `Query()` in defaults are the framework's contract, which bugbear cannot know. **Fix:** `extend-immutable-calls` for those two, rather than disabling the rule.

## Tracing

Set `OTEL_EXPORTER_OTLP_ENDPOINT` (for example `http://otel-collector.monitoring.svc:4318`) and every tool call becomes a server span in Tempo under `service.name=pim-tools`, with `/health` excluded. Unset, the SDK is never initialised, so tests and local runs stay silent. In the homelab this is the second OTLP producer after the NAS media pipeline, and the first in-cluster one.

## Running it

```bash
export RADICALE_URL=http://radicale.example:5232/ RADICALE_USER=alice RADICALE_PASSWORD=...
export RADICALE_CALENDAR=personal   # the events calendar; task lists are addressed by name
uvicorn pim_tools.app:app --host 0.0.0.0 --port 8000
```

Container: `ghcr.io/josephbannon-tech/homelab-pim-tools` (non-root, no writable state, `HEALTHCHECK` on `/health`).

Tests:

```bash
pip install -e ".[dev]"
pytest                       # API tests against the in-memory store
RADICALE_URL=... RADICALE_USER=... RADICALE_PASSWORD=... pytest tests/test_live_caldav.py
```

## Where it fits

Part of a single-node homelab run as production-shape infrastructure: GitOps with ArgoCD ([homelab-gitops](https://github.com/josephbannon-tech/homelab-gitops)), Prometheus/Loki/Tempo observability with SLOs ([homelab-observability](https://github.com/josephbannon-tech/homelab-observability)), and OpenTofu provisioning ([homelab-iac](https://github.com/josephbannon-tech/homelab-iac)). This service is deployed from the gitops repo with a Sealed Secret for the CalDAV credentials and a blackbox probe on `/health`.

## Roadmap

- Per-user identity (a second household member with chat access).
- Child spans for the CalDAV round trips (the `caldav` client sits on niquests, which is not auto-instrumented).
- Contacts (`list_contacts`, `add_contact`) over CardDAV on the same principal.
- Notes tools over a Syncthing-replicated markdown vault (`read_note`, `append_note`), once the vault exists.
