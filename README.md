# homelab-pim-tools

A small, deterministic tool server that gives an LLM front end (Open WebUI in my homelab) ground-truth access to my calendar and task lists held in a CalDAV server (Radicale). Five operations, two doors (OpenAPI and MCP), one container, no state of its own.

It exists because retrieval over personal data is the wrong tool for the job. An assistant asked "what's on Thursday" should call a function that reads the calendar and returns the events, not guess from context. Writes go the same way: "add milk to the shopping list" becomes one idempotent API call whose response is the stored item, and the model is instructed to report only what that response says.

## What it does

| Operation | Method | Path | Idempotent on |
|---|---|---|---|
| `list_tasks` | GET | `/tasks/{list}` | n/a |
| `add_task` | POST | `/tasks` | list + title (case-insensitive) + due date |
| `complete_task` | POST | `/tasks/complete` | n/a |
| `list_events` | GET | `/events?from=&to=` | n/a |
| `add_event` | POST | `/events` | title + start |
| `change_event` | POST | `/events/change` | the values asked for (`changed: false` when already so) |
| `cancel_event` | POST | `/events/cancel` | n/a (a second call is a 404) |
| `move_event` | POST | `/events/move` | refused when already on that calendar (422) |
| `list_calendars` | GET | `/calendars` | n/a |
| `list_notes` | GET | `/notes?query=` | n/a |
| `read_note` | GET | `/notes/read?name=` | n/a |
| `append_note` | POST | `/notes/append` | no: each call adds its text |
| `list_recipes` | GET | `/recipes?query=` | n/a |
| `read_recipe` | GET | `/recipes/read?name=` | n/a |
| `read_cooking_guide` | GET | `/recipes/guide` | n/a |
| `add_recipe_note` | POST | `/recipes/note` | no: each call adds a line |
| `change_recipe_text` | POST | `/recipes/change` | yes in effect: the old passage is gone after the first call |
| `set_recipe_tag` | POST | `/recipes/tag` | yes |
| `save_meal_plan` | POST | `/plans` | refused the second time (409) |
| `add_to_shopping` | POST | `/shopping/from` | yes: items already on the list are skipped |

Tools come in groups (`calendar`, `tasks`, `notes`, `recipes`) and `PIM_TOOLSETS` picks which ones an instance serves, so one image runs as two services: the cluster one over CalDAV and the notes vault, and one on the host that holds the recipe files.

The OpenAPI document at `/openapi.json` is the integration surface. Open WebUI registers it as an external tool server and turns each `operationId` into a tool, so the operation ids and descriptions are written for the model, not for a human reader.

The same operations are served as MCP tools at `/mcp` (Streamable HTTP) for clients that speak MCP rather than OpenAPI, which in the homelab is Claude Code running headless behind the chat. The tools are generated from the routes above, so there is one definition of each operation and two doors onto it.

With `PIM_TOOLS_TOKEN` set, both doors require `Authorization: Bearer <token>` on every request except `/health` and `/openapi.json`. Unset, the server is open, which is the posture for in-cluster callers only.

## Design decisions

- **Idempotency lives in the store layer, not the prompt.** A model that retries a failed turn must not create a second "Milk". `add_task` and `add_event` look for an existing open item with the same key and return it with `created: false`. The response shape makes the outcome explicit so the model can say "already on the list" instead of "added".
- **CalDAV is the store, not a database of my own.** Calendars and task lists stay in a standard format that the phones read natively (DAVx5, Tasks.org). The tool server is a second door onto the same data and can be deleted without losing anything.
- **One principal, calendars addressed by URL leaf.** Radicale exposes `/user/shopping/`, `/user/chores/`, `/user/personal/`. The leaf name is what the phone apps show, so it is the name the model uses. Display names are not trusted for lookup.
- **Routine blocks on their own calendar.** My calendar is mostly time-blocking: a dozen recurring blocks a day and a few real appointments a month. Read straight, "what's on tomorrow?" returns twelve routine entries with the one appointment buried in them. So appointments and routine live in two CalDAV calendars (the phone overlays them), `list_events` reads appointments unless asked for `routine` or `all`, and the tool description tells the model to use `all` when the question is whether a time is free. New events never go on the routine calendar.
- **Colour comes from the calendar, not the event.** iCalendar has a per-event `COLOR` property and DAVx5 passes it to Android, but Google Calendar ignores it on any account that is not Google's, and showed a test event in its calendar's colour. Calendar colours do show. So each kind of event (social, health, work, travel, home) is its own calendar with its own colour, `add_event` takes the calendar, `move_event` moves an event between them, and `list_calendars` gives the model each calendar's colour, so "make it red" resolves to the calendar that is red. Every calendar but the routine counts as appointments, so a plain "what's on" still reads them all.
- **The chat can change one day, never a series.** Time-blocking only works if a day can bend: a shorter nap, dinner an hour early. `change_event` and `cancel_event` take the uid, start and calendar that `list_events` returned, and for a repeating event they touch that day alone, the way the format intends: a moved day is a second VEVENT with a `RECURRENCE-ID`, a skipped day is an `EXDATE` on the master. No code path rewrites an `RRULE`, and the only one that deletes a repeating resource is `move_event` with `whole_series`, which writes the series to its new calendar first and deletes the old copy only after that succeeds; a whole routine series never moves. So the worst a wrong call can do is one day, which shows on the phone and is fixed by hand. "Move it every day from now on" is answered with a pointer to the calendar app, and an eval case holds the model to that. Identifying the day by its current start means a day already moved is found where it now is, and the existing override is edited instead of a second one being stacked on it.
- **Repeating jobs count from when they were last done.** A calendar rule repeats on fixed dates; a household job is due a week after it was last done, whenever that was. A task that carries an interval (`X-CHORE-EVERY`) and a rank is a repeating job: ticking it does not close it, it sets the next due time and hides the task until then with a future `DTSTART`, which task apps read as "hide until". `list_tasks` returns only the jobs due now, in the order to do them, so the question "what next?" has one answer. Two links cover the cases a single interval cannot: `after` (hang the washing out an hour after the wash went on) and `resets` (a deep clean restarts the weekly one). The model gets no new tools for any of this, only two sentences in existing descriptions.
- **Late jobs climb, measured against their own interval.** Jobs are shown in one agreed rank order, decided in advance so nothing is decided at the time. Rank alone would starve the quarterly jobs, because the weekly ones keep returning above them. So a job's rank shrinks in proportion to how late it is, and reaches the top when it is a whole interval late: a weekly job a week late and a quarterly job three months late have both been skipped once.
- **The server sweeps, because the phone does not ask.** A job ticked in a phone app is written straight to the CalDAV server and never passes through this service. A background pass every five minutes finds ticked jobs, reschedules them and follows their links. Display order (`X-APPLE-SORT-ORDER`) is rewritten only when the relative order of the visible jobs is wrong, not on every pass, so an idle list costs no writes and no phone resyncs. Writes carry the ETag; if the phone edited the task in between, the phone's version stands and the next pass settles it.
- **Notes are a folder of markdown files, and the assistant can only add to them.** The notes live in a folder that Syncthing keeps on the phone, where any markdown editor opens them; this service mounts the same folder. `append_note` adds text at the end and nothing else: no overwrite, no rename, no delete, so the worst a wrong call can do is a stray line the owner removes. Appending to a name that matches no note is refused with the names that exist, unless the call says to create one, because a misheard name should not quietly become a new file. Writes go to a temporary file that is swapped in, so the sync tool never ships half a note.
- **A private folder the tools cannot see.** Folders named in `NOTES_EXCLUDE` are not listed, searched, read or written, and the refusal does not admit they exist. Everything the assistant reads leaves the house in a prompt, so the line between what it may see and what it may not is a folder the owner controls from the phone, and it is enforced here, not in the prompt. Two eval cases hold it: a question about a private note must not surface its contents.
- **Recipes get edits a file survives, not a file editor.** The recipe folder belongs to a meal-planning project that a desktop editor also works on, so the tools allow four narrow writes: a line added to a recipe's Notes, one exact passage replaced (it must occur once, and the result must still carry its tags and its Ingredients and Method sections, or nothing is saved), one tag set to an allowed value with the index table kept in step, and a new one-off meal plan that never overwrites. Nothing is deleted or renamed. A general file-editing tool would have been quicker to wire to one model; these work the same for any model behind the front end, and the eval set measures them.
- **Shopping is parsed, not generated.** `add_to_shopping` reads a plan's `## Shopping` section, or every aisle of a weekly list, and adds each item through the same idempotent `add_task`. Store-cupboard items go on as `Check: ...`, items marked `(have)` are skipped, and long asides in brackets are trimmed so a line fits a phone. The model only names the plan; a weak model cannot drop or invent items, and running it twice adds nothing.
- **Store behind a `Protocol`, tested twice.** The HTTP layer is tested against an in-memory store (fast, no server). The CalDAV store has a separate round-trip test that runs only when `RADICALE_URL` is set, so CI stays hermetic and the live behaviour is still checked before a release.
- **MCP generated from the OpenAPI routes, not written twice.** fastapi-mcp turns each route into a tool named after its `operationId`, with the descriptions and schemas the OpenAPI door already exposes, and invokes the route in-process with the caller's bearer forwarded. A hand-written MCP server would be a second copy of the tool contract, and copies drift.
- **A shared-secret bearer token, not identity.** v1 had no auth and stayed on a ClusterIP for that reason. The token (v0.3) lets the service sit on a NodePort for an MCP client on another host without letting anything else on the LAN write to the calendar. It says a caller is trusted, not who they are, which is all a single-user tool needs. The gate is a pure ASGI middleware because Starlette's `BaseHTTPMiddleware` buffers response bodies and the MCP transport streams.
- **Image built in CI and pinned by digest downstream.** The homelab has no registry, so GitHub Actions builds the image, Trivy scans it (HIGH/CRITICAL fail the build, unfixed ignored), and pushes to GHCR. The deployment repo references the image by digest, so a rebuild never changes what runs without a reviewed commit.

## Lessons learned

- **Symptom:** the Radicale endpoint answered 200 but the blackbox-exporter probe that would watch it reported failure. **Root cause:** Radicale's built-in server speaks HTTP/1.0 and the default `http_2xx` module only accepts 1.1 and 2.0. **Fix:** a dedicated probe module with `HTTP/1.0` in `valid_http_versions` (in the gitops repo). This server itself runs under uvicorn and answers HTTP/1.1, so its own probe uses the default module.
- **Symptom:** `ruff` flagged every FastAPI handler (`B008`, function call in argument default). **Root cause:** `Depends()` and `Query()` in defaults are the framework's contract, which bugbear cannot know. **Fix:** `extend-immutable-calls` for those two, rather than disabling the rule.
- **Symptom:** `FastApiMCP(app)` raised `Server.__init__() takes 2 positional arguments but 3 were given` on a fresh install. **Root cause:** fastapi-mcp 0.4 targets the 1.x MCP SDK and the SDK's 2.0 release changed the `Server` constructor; pip resolved the newest SDK. **Fix:** pin `mcp>=1.12,<2` beside `fastapi-mcp` with a comment naming the ceiling, plus an end-to-end test that drives `/mcp` with the SDK's own client, so the next bump fails in CI rather than in the chat.

## Eval set

[`evals/`](evals/) holds 61 scripted chat turns that gate any change to who or what sits in front of these tools. A front end that runs its own tool loop returns only text, so the harness scores at the tool server: the real app on a loopback port over a recording store, seeded fresh for each case. A run passes at 90% of cases with zero writes that a case did not ask for. Method, coverage and results are in [evals/README.md](evals/README.md).

## Tracing

Set `OTEL_EXPORTER_OTLP_ENDPOINT` (for example `http://otel-collector.monitoring.svc:4318`) and every tool call becomes a server span in Tempo under `service.name=pim-tools`, with `/health` excluded. Unset, the SDK is never initialised, so tests and local runs stay silent. In the homelab this is the second OTLP producer after the NAS media pipeline, and the first in-cluster one.

## Running it

```bash
export RADICALE_URL=http://radicale.example:5232/ RADICALE_USER=alice RADICALE_PASSWORD=...
export RADICALE_CALENDAR=personal   # the events calendar; task lists are addressed by name
export RADICALE_EXTRA_CALENDARS=social,health,routine   # optional: more event calendars (kinds, each with its own colour)
export RADICALE_ROUTINE_CALENDARS=routine   # the default: read only when asked, never written to by add_event or move_event
export PIM_SWEEP_LISTS=chores       # optional: task lists holding repeating jobs, swept every
export PIM_SWEEP_SECONDS=300        # this many seconds for jobs ticked on a phone
export NOTES_DIR=/vault             # optional: a folder of markdown notes (the notes tools answer 503 without it)
export NOTES_EXCLUDE=Private        # optional: top-level folders the notes tools must never see
export RECIPES_DIR=/srv/nutrition   # optional: recipes/, tmp/ (plans), shopping-lists/, profile.md
export PIM_TOOLSETS=calendar,tasks,notes   # the default; add or pick 'recipes'
export PIM_TOOLS_TOKEN=...          # optional; omit for the open, in-cluster-only posture
uvicorn pim_tools.app:app --host 0.0.0.0 --port 8000
```

An MCP client such as Claude Code reaches the second door with:

```json
{"mcpServers": {"pim": {"type": "http", "url": "http://pim-tools.example:8000/mcp",
                        "headers": {"Authorization": "Bearer ..."}}}}
```

Container: `ghcr.io/josephbannon-tech/homelab-pim-tools` (non-root, no writable state, `HEALTHCHECK` on `/health`).

Tests:

```bash
pip install -e ".[dev]"
pytest                       # API, auth and MCP tests against the in-memory store
                             # (the MCP test runs a real uvicorn on a loopback port)
RADICALE_URL=... RADICALE_USER=... RADICALE_PASSWORD=... pytest tests/test_live_caldav.py
python -m evals.run oracle   # the eval harness end to end, no model needed
```

## Where it fits

Part of a single-node homelab run as production-shape infrastructure: GitOps with ArgoCD ([homelab-gitops](https://github.com/josephbannon-tech/homelab-gitops)), Prometheus/Loki/Tempo observability with SLOs ([homelab-observability](https://github.com/josephbannon-tech/homelab-observability)), and OpenTofu provisioning ([homelab-iac](https://github.com/josephbannon-tech/homelab-iac)). This service is deployed from the gitops repo with a Sealed Secret for the CalDAV credentials and a blackbox probe on `/health`.

## Roadmap

- Child spans for the CalDAV round trips (the `caldav` client sits on niquests, which is not auto-instrumented).
- Contacts (`list_contacts`, `add_contact`) over CardDAV on the same principal.
