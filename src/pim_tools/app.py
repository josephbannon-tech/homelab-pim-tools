"""HTTP surface. Flat paths and explicit operationIds: Open WebUI turns each
operation into a tool named after its operationId, and the description is what
the model reads, so they are written for the model, not for a human."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi_mcp import FastApiMCP

from . import __version__
from .auth import BearerAuth
from .models import (
    AddEventRequest,
    AddEventResult,
    AddTaskRequest,
    AddTaskResult,
    AppendNoteRequest,
    AppendNoteResult,
    CancelEventResult,
    ChangeEventRequest,
    ChangeEventResult,
    CompleteTaskRequest,
    Event,
    EventList,
    EventRef,
    Note,
    NoteList,
    Task,
    TaskList,
)
from .notes import BadName, NoNotes, NoSuchNote, NoteFile, Notes
from .store import NotFound, Store, Unsupported
from .telemetry import configure as configure_telemetry

log = logging.getLogger("pim_tools")

# Lists holding repeating jobs, swept in the background. A job ticked on a
# phone goes straight to the CalDAV server and never passes through this
# service, so something has to notice it and schedule its next turn.
SWEEP_LISTS = tuple(
    s.strip() for s in os.environ.get("PIM_SWEEP_LISTS", "").split(",") if s.strip()
)
SWEEP_SECONDS = float(os.environ.get("PIM_SWEEP_SECONDS", "300"))


def sweep_once(store: Store, lists: tuple[str, ...] = SWEEP_LISTS) -> int:
    """One pass over the configured lists. A list that fails is logged and
    skipped; the next pass tries again."""
    rewritten = 0
    for name in lists:
        try:
            rewritten += store.sweep(name)
        except Exception:
            log.exception("sweep of %s failed", name)
    return rewritten


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    async def loop() -> None:
        while True:
            await asyncio.to_thread(sweep_once, get_store())
            await asyncio.sleep(SWEEP_SECONDS)

    task = asyncio.create_task(loop()) if SWEEP_LISTS else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(
    title="pim-tools",
    version=__version__,
    lifespan=lifespan,
    description=(
        "Ground-truth tools over a personal calendar and task lists. "
        "Every write returns the stored object; only report what it says."
    ),
)


@lru_cache(maxsize=1)
def _default_store() -> Store:
    from .store import CalDAVStore

    url = os.environ["RADICALE_URL"]
    return CalDAVStore(
        url=url,
        username=os.environ["RADICALE_USER"],
        password=os.environ["RADICALE_PASSWORD"],
        calendar=os.environ.get("RADICALE_CALENDAR", "personal"),
        extra_calendars=tuple(
            c.strip()
            for c in os.environ.get("RADICALE_EXTRA_CALENDARS", "").split(",")
            if c.strip()
        ),
    )


def get_store() -> Store:
    return _default_store()


@lru_cache(maxsize=1)
def _default_notes() -> Notes | None:
    root = os.environ.get("NOTES_DIR", "").strip()
    if not root:
        return None
    exclude = tuple(s.strip() for s in os.environ.get("NOTES_EXCLUDE", "").split(",") if s.strip())
    return Notes(root, exclude)


def get_notes() -> Notes:
    notes = _default_notes()
    if notes is None:
        raise NoNotes()
    return notes


def _no_such_list(name: NotFound, store: Store) -> HTTPException:
    """A 404 the model can recover from: it names the lists that do exist, so
    the next call can pick one or the reply can offer them."""
    lists = ", ".join(store.task_lists()) or "none"
    return HTTPException(
        404,
        f"no task list named {name}. The lists are: {lists}. "
        "Do not add to a different list without asking.",
    )


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get(
    "/tasks/{list_name}",
    operation_id="list_tasks",
    summary="List the open tasks on a task list",
    description=(
        "Returns the open items on a task list, by name (for example 'shopping'). "
        "An unknown name returns the names that exist. "
        "A list of repeating jobs (for example 'chores') returns only the jobs due now, "
        "in the order to do them: the first one is the next job. With include_completed, "
        "jobs that are done for now follow as completed."
    ),
    response_model=TaskList,
)
def list_tasks(
    list_name: str,
    include_completed: bool = Query(False, description="Also return completed tasks."),
    store: Store = Depends(get_store),
) -> TaskList:
    try:
        tasks = store.list_tasks(list_name, include_completed=include_completed)
    except NotFound as e:
        raise _no_such_list(e, store) from e
    return TaskList(list=list_name, open=sum(not t.completed for t in tasks), tasks=tasks)


@app.post(
    "/tasks",
    operation_id="add_task",
    summary="Add a task to a task list",
    description=(
        "Adds an item to a task list, by name (for example 'shopping' for groceries). "
        "Idempotent: if the same open item already exists, returns it with created=false."
    ),
    response_model=AddTaskResult,
)
def add_task(req: AddTaskRequest, store: Store = Depends(get_store)) -> AddTaskResult:
    try:
        task, created = store.add_task(req.list, req.title, req.due)
    except NotFound as e:
        raise _no_such_list(e, store) from e
    return AddTaskResult(task=task, created=created)


@app.post(
    "/tasks/complete",
    operation_id="complete_task",
    summary="Mark a task as done",
    description=(
        "Completes a task by uid (get the uid from list_tasks first). "
        "A repeating job is not closed: it comes back when it is next due."
    ),
    response_model=Task,
)
def complete_task(req: CompleteTaskRequest, store: Store = Depends(get_store)) -> Task:
    try:
        return store.complete_task(req.list, req.uid)
    except NotFound as e:
        raise HTTPException(404, f"no open task {e} on list {req.list}") from e


@app.get(
    "/events",
    operation_id="list_events",
    summary="List calendar events in a time range",
    description=(
        "Returns appointments on the personal calendar between two ISO 8601 timestamps. "
        "Defaults to the next 7 days. Recurring daily routine blocks (work, meals, "
        "exercise) are on a separate calendar: pass calendar='routine' to read those, "
        "or calendar='all' for both, which is what to use when checking if a time is free. "
        "Each event carries the uid, start and calendar that change_event and cancel_event need."
    ),
    response_model=EventList,
    response_model_by_alias=True,
)
def list_events(
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    calendar: str | None = Query(
        None, description="Omit for appointments only; 'routine' or 'all' to widen."
    ),
    store: Store = Depends(get_store),
) -> EventList:
    start = start or datetime.now(UTC)
    end = end or start + timedelta(days=7)
    if end <= start:
        raise HTTPException(422, "'to' must be after 'from'")
    try:
        events = store.list_events(start, end, calendar)
    except NotFound as e:
        raise HTTPException(404, f"no calendar named {e}; use 'routine' or 'all'") from e
    return EventList(calendar=calendar or "personal", **{"from": start}, to=end, events=events)


@app.post(
    "/events",
    operation_id="add_event",
    summary="Add a calendar event",
    description=(
        "Creates an event on the personal calendar. Give start as ISO 8601 with a timezone; "
        "end defaults to one hour later. Idempotent on (title, start)."
    ),
    response_model=AddEventResult,
)
def add_event(req: AddEventRequest, store: Store = Depends(get_store)) -> AddEventResult:
    if req.end and req.end <= req.start:
        raise HTTPException(422, "end must be after start")
    event, created = store.add_event(req.title, req.start, req.end, req.notes)
    return AddEventResult(event=event, created=created)


_SERIES = (
    "A whole repeating series cannot be changed or cancelled here, only single days: "
    "if the user wants every occurrence changed, say so and point them to their calendar app."
)


def _scope(event: Event) -> str:
    if event.recurring:
        return f"only {event.start:%A %d %B}; the rest of the repeating series is unchanged"
    return "this single event"


def _no_such_event(ref: EventRef, e: NotFound) -> HTTPException:
    return HTTPException(
        404,
        f"no event {ref.uid} starting {ref.start.isoformat()} on calendar {ref.calendar} ({e}). "
        "Call list_events with calendar='all' for that day and use the uid, start and "
        "calendar it returns.",
    )


@app.post(
    "/events/change",
    operation_id="change_event",
    summary="Change the time, title or notes of one event",
    description=(
        "Changes one event. Identify it by the uid, start and calendar that list_events "
        "returned. For a repeating event (recurring=true) this changes that one day only. "
        "Give new_start to move it (the length is kept), new_end to change its length, "
        "new_title or notes to reword it. " + _SERIES
    ),
    response_model=ChangeEventResult,
)
def change_event(req: ChangeEventRequest, store: Store = Depends(get_store)) -> ChangeEventResult:
    if (
        req.new_start is None
        and req.new_end is None
        and req.new_title is None
        and req.notes is None
    ):
        raise HTTPException(422, "nothing to change: give new_start, new_end, new_title or notes")
    try:
        event, changed = store.change_event(
            req.calendar, req.uid, req.start, req.new_start, req.new_end, req.new_title, req.notes
        )
    except NotFound as e:
        raise _no_such_event(req, e) from e
    except Unsupported as e:
        raise HTTPException(422, str(e)) from e
    return ChangeEventResult(event=event, changed=changed, scope=_scope(event))


@app.post(
    "/events/cancel",
    operation_id="cancel_event",
    summary="Cancel one event",
    description=(
        "Removes one event. Identify it by the uid, start and calendar that list_events "
        "returned. For a repeating event (recurring=true) this skips that one day only. "
        "Only cancel what the user named. " + _SERIES
    ),
    response_model=CancelEventResult,
)
def cancel_event(req: EventRef, store: Store = Depends(get_store)) -> CancelEventResult:
    try:
        gone = store.cancel_event(req.calendar, req.uid, req.start)
    except NotFound as e:
        raise _no_such_event(req, e) from e
    except Unsupported as e:
        raise HTTPException(422, str(e)) from e
    return CancelEventResult(cancelled=gone, scope=_scope(gone))


def _note(n: NoteFile) -> Note:
    return Note(name=n.name, modified=n.modified, size=n.size, text=n.text, truncated=n.truncated)


def _no_such_note(e: NoSuchNote) -> HTTPException:
    shown = ", ".join(f"'{k}'" for k in e.known[:40]) or "none"
    return HTTPException(
        404,
        f"no note named '{e.name}'. The notes are: {shown}. "
        "Do not add to a different note, or create one, without asking.",
    )


@app.exception_handler(NoNotes)
async def _notes_off(_, __):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "notes are not set up on this server"}, status_code=503)


@app.get(
    "/notes",
    operation_id="list_notes",
    summary="List the user's notes, or find the ones that mention something",
    description=(
        "Returns the names of the user's notes (markdown files, such as a packing list or "
        "reference notes). Give query to return only notes whose name or text contains it. "
        "Notes are free text; things to tick off live on task lists instead."
    ),
    response_model=NoteList,
)
def list_notes(
    query: str | None = Query(None, description="Words to look for in names and text."),
    notes: Notes = Depends(get_notes),
) -> NoteList:
    return NoteList(notes=[_note(n) for n in notes.list(query)])


@app.get(
    "/notes/read",
    operation_id="read_note",
    summary="Read one note",
    description="Returns a note's text, by name. An unknown name returns the names that exist.",
    response_model=Note,
)
def read_note(
    name: str = Query(description="The note's name, from list_notes."),
    notes: Notes = Depends(get_notes),
) -> Note:
    try:
        return _note(notes.read(name))
    except NoSuchNote as e:
        raise _no_such_note(e) from e


@app.post(
    "/notes/append",
    operation_id="append_note",
    summary="Add text to the end of a note",
    description=(
        "Appends text to an existing note. It cannot change or remove what is already "
        "there; tell the user to edit the note themselves for that. Use the note's own "
        "style: a checklist note takes a line like '- [ ] Item'."
    ),
    response_model=AppendNoteResult,
)
def append_note(req: AppendNoteRequest, notes: Notes = Depends(get_notes)) -> AppendNoteResult:
    try:
        note, created = notes.append(req.name, req.text, req.create)
    except NoSuchNote as e:
        raise _no_such_note(e) from e
    except BadName as e:
        raise HTTPException(422, f"'{e}' cannot be used as a note name") from e
    return AppendNoteResult(note=_note(note), created=created)


_ = Event  # re-exported for type checkers

# Second door: the same ten operations as MCP tools (Streamable HTTP at /mcp),
# generated from the routes above so there is one definition of each tool.
# Mounted after the routes, which fastapi-mcp reads at construction time.
FastApiMCP(app, name="pim-tools").mount_http()
# One gate for both doors; a no-op until PIM_TOOLS_TOKEN is set.
app.add_middleware(BearerAuth)

TRACING = configure_telemetry(app)
