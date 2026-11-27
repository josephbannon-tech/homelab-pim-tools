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
    location: str | None = Field(default=None, description="Where the event is.")
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
    location: str | None = Field(
        default=None,
        max_length=200,
        description="Where it is, when the user says: the place or address in their words, "
        "e.g. 'Steam, Swindon'. The phone shows it as a link to the maps app. Put the place "
        "here, not in notes or the title. Never look one up or guess one: omit it when the "
        "user names no place.",
    )
    calendar: str | None = Field(
        default=None,
        description="Which calendar to put it on, by name from list_calendars. Each calendar "
        "is a kind of event with its own colour on the phone: use the one whose kind the "
        "event is. Omit for the default calendar when none fits.",
    )


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
    location: str | None = Field(
        default=None,
        max_length=200,
        description="Replaces where the event is, in the user's words. Omit to keep it.",
    )


class MoveEventRequest(EventRef):
    to_calendar: str = Field(description="The calendar to move it to, from list_calendars.")
    whole_series: bool = Field(
        default=False,
        description="For a repeating event: true moves every day of the series, false only "
        "the day given by start.",
    )


class MoveEventResult(BaseModel):
    event: Event = Field(description="The event as it now stands, on its new calendar.")
    from_calendar: str
    scope: str = Field(description="What moved: one event, a whole series, or one day of it.")


class CalendarInfo(BaseModel):
    name: str
    colour: str | None = Field(default=None, description="#rrggbb as the phone shows it.")
    routine: bool = Field(
        default=False,
        description="The daily routine: one colour, read only when asked or with calendar='all'.",
    )
    default: bool = Field(default=False, description="Where add_event puts an event by default.")


class CalendarList(BaseModel):
    calendars: list[CalendarInfo]


class ChangeEventResult(BaseModel):
    event: Event = Field(description="The event as it now stands.")
    changed: bool = Field(description="False when it already had these values.")
    scope: str = Field(description="What was affected: one day of a series, or a single event.")


class CancelEventResult(BaseModel):
    cancelled: Event = Field(description="The event that was removed.")
    scope: str = Field(description="What was affected: one day of a series, or a single event.")


class Note(BaseModel):
    name: str = Field(description="The note's name, e.g. 'Festival packing list'.")
    modified: datetime
    size: int = Field(description="Size in bytes.")
    text: str | None = Field(default=None, description="The note's text (markdown).")
    truncated: bool = Field(
        default=False, description="True when the note was too long to return whole."
    )


class NoteList(BaseModel):
    notes: list[Note]


class AppendNoteRequest(BaseModel):
    name: str = Field(description="The note to add to, by name from list_notes.")
    text: str = Field(min_length=1, max_length=4000, description="The text to add at the end.")
    create: bool = Field(
        default=False,
        description="Set true only when the user asked for a new note. "
        "Otherwise a name that matches no note is refused.",
    )


class AppendNoteResult(BaseModel):
    note: Note = Field(description="The note as it now stands (its last part, if long).")
    created: bool = Field(description="True when this call made a new note.")


class Recipe(BaseModel):
    name: str = Field(description="The recipe or plan's name, e.g. 'Lamb Keema with Rice'.")
    kind: str = Field(description="recipe, plan (a one-off meal plan) or weekly list.")
    file: str = Field(description="File name without .md.")
    protein: str | None = None
    cuisine: str | None = None
    effort: str | None = Field(default=None, description="quick, standard or weekend.")
    kcal_per_serving: str | None = None
    serves: str | None = None
    retired: bool = Field(default=False, description="True for a recipe taken out of planning.")
    modified: datetime | None = None
    text: str | None = Field(default=None, description="The full text (markdown).")
    truncated: bool = False


class RecipeList(BaseModel):
    recipes: list[Recipe]


class CookingGuide(BaseModel):
    text: str = Field(description="The household's eating profile: likes, dislikes, habits.")


class RecipeNoteRequest(BaseModel):
    name: str = Field(description="The recipe, by name from list_recipes.")
    text: str = Field(
        min_length=1,
        max_length=2000,
        description="What to add to the recipe's Notes, as a short instruction for next time, "
        "e.g. 'Double the green chilli.'",
    )


class RecipeChangeRequest(BaseModel):
    name: str = Field(description="The recipe, by name from list_recipes.")
    old_text: str = Field(
        min_length=1,
        description="A passage copied word for word from read_recipe, long enough to occur "
        "only once.",
    )
    new_text: str = Field(description="What replaces it, in the same style.")


class RecipeTagRequest(BaseModel):
    name: str = Field(description="The recipe, by name from list_recipes.")
    tag: str = Field(description="effort, kcal_per_serving, serves, scales_well or freezes_well.")
    value: str = Field(
        description="effort: quick, standard or weekend. kcal_per_serving and serves: a "
        "whole number. scales_well and freezes_well: yes or no."
    )


class RecipeEditResult(BaseModel):
    recipe: Recipe = Field(description="The recipe as it now stands.")
    index_updated: bool | None = Field(
        default=None, description="For a tag change: whether the recipe index was updated too."
    )


class MealPlanRequest(BaseModel):
    title: str = Field(min_length=3, max_length=120, description="e.g. 'Steak Night for Two'.")
    text: str = Field(
        min_length=20,
        max_length=20000,
        description="The plan in markdown. Must include a '## Shopping' section: one '- item' "
        "line per thing to buy with its quantity, '(have)' after anything already in, and a "
        "'Check you have' label above store-cupboard items.",
    )


class ShoppingRequest(BaseModel):
    name: str = Field(
        description="A meal plan's name, or a weekly list by the date of its Monday "
        "(YYYY-MM-DD), from list_recipes."
    )


class ShoppingResult(BaseModel):
    task_list: str = Field(description="The task list the items went to.")
    source: str = Field(description="The plan or weekly list they came from.")
    added: list[str] = Field(description="Items put on the list.")
    already_there: list[str] = Field(description="Items that were on the list already.")
    have: list[str] = Field(description="Items the plan marks as already in, not added.")
