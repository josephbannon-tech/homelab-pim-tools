"""Wire models. Every response echoes the stored object so the model can quote it."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class Task(BaseModel):
    uid: str
    list: str = Field(description="Task list name, e.g. 'shopping' or 'chores'.")
    title: str
    due: date | None = None
    completed: bool = False


class TaskList(BaseModel):
    list: str
    open: int
    tasks: list[Task]


class AddTaskRequest(BaseModel):
    list: str = Field(description="Task list name: 'shopping' or 'chores'.")
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
