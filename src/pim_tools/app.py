"""HTTP surface. Flat paths and explicit operationIds: Open WebUI turns each
operation into a tool named after its operationId, and the description is what
the model reads, so they are written for the model, not for a human."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException, Query

from . import __version__
from .models import (
    AddEventRequest,
    AddEventResult,
    AddTaskRequest,
    AddTaskResult,
    CompleteTaskRequest,
    Event,
    EventList,
    Task,
    TaskList,
)
from .store import NotFound, Store

app = FastAPI(
    title="pim-tools",
    version=__version__,
    description=(
        "Ground-truth tools over the family calendar and task lists. "
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
    )


def get_store() -> Store:
    return _default_store()


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get(
    "/tasks/{list_name}",
    operation_id="list_tasks",
    summary="List the open tasks on a task list",
    description="Returns the open items on a task list such as 'shopping' or 'chores'.",
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
        raise HTTPException(404, f"no task list named {e}") from e
    return TaskList(list=list_name, open=sum(not t.completed for t in tasks), tasks=tasks)


@app.post(
    "/tasks",
    operation_id="add_task",
    summary="Add a task to a task list",
    description=(
        "Adds an item to a task list ('shopping' for groceries, 'chores' for jobs). "
        "Idempotent: if the same open item already exists, returns it with created=false."
    ),
    response_model=AddTaskResult,
)
def add_task(req: AddTaskRequest, store: Store = Depends(get_store)) -> AddTaskResult:
    try:
        task, created = store.add_task(req.list, req.title, req.due)
    except NotFound as e:
        raise HTTPException(404, f"no task list named {e}") from e
    return AddTaskResult(task=task, created=created)


@app.post(
    "/tasks/complete",
    operation_id="complete_task",
    summary="Mark a task as done",
    description="Completes a task by uid (get the uid from list_tasks first).",
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
        "Returns events on the personal calendar between two ISO 8601 timestamps. "
        "Defaults to the next 7 days."
    ),
    response_model=EventList,
    response_model_by_alias=True,
)
def list_events(
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    store: Store = Depends(get_store),
) -> EventList:
    start = start or datetime.now(UTC)
    end = end or start + timedelta(days=7)
    if end <= start:
        raise HTTPException(422, "'to' must be after 'from'")
    events = store.list_events(start, end)
    return EventList(calendar="personal", **{"from": start}, to=end, events=events)


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


_ = Event  # re-exported for type checkers
