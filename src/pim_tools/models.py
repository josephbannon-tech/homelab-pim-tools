"""Wire models. Every response echoes the stored object so the model can quote it."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class Task(BaseModel):
    uid: str
    list: str = Field(description="Task list name, e.g. 'shopping'.")
    title: str
    due: date | None = None
    completed: bool = False
    repeats: str | None = Field(
        default=None,
        description=(
            "Set on a repeating job, e.g. 'every 7 days'. Completing it schedules its next turn."
        ),
    )


class TaskList(BaseModel):
    list: str
    open: int
    tasks: list[Task]


class AddTaskRequest(BaseModel):
    list: str = Field(description="Task list name, e.g. 'shopping' or 'todo'.")
    title: str = Field(min_length=1, max_length=200, description="What to add, e.g. 'Milk'.")
    due: date | None = Field(default=None, description="Optional due date, ISO 8601 (YYYY-MM-DD).")


class AddTaskResult(BaseModel):
    task: Task
    created: bool = Field(description="False when an identical open task already existed.")


class CompleteTaskRequest(BaseModel):
    list: str
    uid: str = Field(description="The task uid from list_tasks.")


class Event(BaseModel):
    uid: str
    title: str
    start: datetime
    end: datetime | None = None
    notes: str | None = None
    calendar: str = Field(default="personal", description="Which calendar the event is on.")
    recurring: bool = Field(
        default=False,
        description="True for one day of a repeating series. Only that day can be changed here.",
    )


class EventList(BaseModel):
    calendar: str
    from_: datetime = Field(alias="from")
    to: datetime
    events: list[Event]

    model_config = {"populate_by_name": True}


class AddEventRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start: datetime = Field(description="ISO 8601 with timezone, e.g. 2026-10-02T18:00:00+01:00.")
    end: datetime | None = Field(default=None, description="Defaults to start + 1 hour.")
    notes: str | None = None


class AddEventResult(BaseModel):
    event: Event
    created: bool = Field(description="False when an identical event already existed.")


class EventRef(BaseModel):
    uid: str = Field(description="The event uid from list_events.")
    start: datetime = Field(
        description="The event's current start, exactly as list_events returned it. "
        "For a repeating event this picks the day."
    )
    calendar: str = Field(
        default="personal", description="The calendar the event is on, from list_events."
    )


class ChangeEventRequest(EventRef):
    new_start: datetime | None = Field(
        default=None, description="New start, ISO 8601 with timezone. The length is kept."
    )
    new_end: datetime | None = Field(default=None, description="New end, to change the length.")
    new_title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, description="Replaces the event's notes.")


class ChangeEventResult(BaseModel):
    event: Event = Field(description="The event as it now stands.")
    changed: bool = Field(description="False when it already had these values.")
    scope: str = Field(description="What was affected: one day of a series, or a single event.")


class CancelEventResult(BaseModel):
    cancelled: Event = Field(description="The event that was removed.")
    scope: str = Field(description="What was affected: one day of a series, or a single event.")
