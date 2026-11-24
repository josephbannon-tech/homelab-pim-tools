"""Case loading and scoring.

A case passes when every required tool call reached the store with matching
arguments, no write happened that the case did not ask for, the final store
state is as expected, and the reply quotes what it should and nothing it
should not. Extra reads are free: a model that lists before it adds is being
careful, not wrong.

Dates in the case file are relative to the day of the run, because the chat
front end tells the model the real date. Placeholders, in any string:

  {date:+2}        ISO date two days from today          2026-11-25
  {dayname:+2}     that day's weekday name               Wednesday
  {nice:+2}        that day as people write it           25 November
  {dt:+2T10:00}    ISO datetime, local offset            2026-11-25T10:00:00+00:00
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .server import WRITES, Call, RecordingStore

CASES = Path(__file__).with_name("cases.toml")
PASS_RATE = 0.90  # the gate: this share of cases, and zero bad writes

_PLACEHOLDER = re.compile(r"\{(date|dayname|nice|dt):([+-]?\d+)(?:T(\d\d):(\d\d))?\}")


def resolve(value: Any, today: date, tz: ZoneInfo) -> Any:
    """Expand placeholders in every string of a nested structure."""
    if isinstance(value, dict):
        return {k: resolve(v, today, tz) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, today, tz) for v in value]
    if not isinstance(value, str):
        return value

    def sub(m: re.Match) -> str:
        kind, day = m.group(1), today + timedelta(days=int(m.group(2)))
        if kind == "date":
            return day.isoformat()
        if kind == "dayname":
            return f"{day:%A}"
        if kind == "nice":
            return f"{day.day} {day:%B}"
        if m.group(3) is None:
            raise ValueError(f"{m.group(0)}: dt needs a time, e.g. {{dt:+1T10:00}}")
        at = time(int(m.group(3)), int(m.group(4)))
        return datetime.combine(day, at, tzinfo=tz).isoformat()

    return _PLACEHOLDER.sub(sub, value)


def load_cases(today: date, tz: ZoneInfo, path: Path = CASES) -> list[dict[str, Any]]:
    doc = tomllib.loads(path.read_text())
    default_seed = doc.get("seed", {})
    cases = []
    for raw in doc["case"]:
        case = resolve(raw, today, tz)
        case.setdefault("seed", resolve(default_seed, today, tz))
        case.setdefault("expect", [])
        cases.append(case)
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'")
    return " ".join(s.split()).casefold()


def _instant(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # the server reads naive as UTC


def arg_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if expected.get("absent"):
            return actual is None
        if "any" in expected:
            return any(arg_matches(e, actual) for e in expected["any"])
        if "contains" in expected:
            return actual is not None and _norm(expected["contains"]) in _norm(str(actual))
        raise ValueError(f"unknown matcher {expected}")
    if actual is None:
        return False
    if isinstance(actual, datetime):
        return _instant(actual) == _instant(datetime.fromisoformat(expected))
    if isinstance(actual, date):
        return actual == date.fromisoformat(expected)
    if isinstance(actual, str):
        return _norm(actual) == _norm(str(expected))
    return actual == expected


def _uid_of(store: RecordingStore, list_name: str, title: str) -> str | None:
    for t in store.tasks.get(list_name, []):
        if _norm(t.title) == _norm(title):
            return t.uid
    return None


def call_matches(expect: dict[str, Any], call: Call, store: RecordingStore) -> bool:
    if call.tool != expect["tool"] or not call.ok:
        return False
    args = dict(expect.get("args") or {})
    if "task" in expect:  # complete_task names the seeded task by title
        args["uid"] = _uid_of(store, args.get("list", ""), expect["task"])
    if "covers" in expect:  # list_events: the queried window includes this moment
        t = _instant(datetime.fromisoformat(expect["covers"]))
        if not _instant(call.args["from"]) <= t < _instant(call.args["to"]):
            return False
    return all(arg_matches(v, call.args.get(k)) for k, v in args.items())


@dataclass
class Result:
    id: str
    category: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    bad_writes: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    reply: str = ""
    seconds: float = 0.0


def score_case(
    case: dict[str, Any], store: RecordingStore, reply: str, error: str | None = None
) -> Result:
    reasons: list[str] = []
    if error:
        reasons.append(f"brain error: {error}")

    for expect in case["expect"]:
        if expect.get("optional"):
            continue
        if not any(call_matches(expect, c, store) for c in store.calls):
            want = {k: v for k, v in expect.items() if k != "tool"}
            reasons.append(f"missing call {expect['tool']} {want}")

    bad = [
        c.as_json()
        for c in store.calls
        if c.tool in WRITES
        and c.changed
        and not any(call_matches(e, c, store) for e in case["expect"])
    ]
    for b in bad:
        reasons.append(f"bad write {b['tool']} {b['args']}")

    state = case.get("state") or {}
    for list_name, titles in (state.get("open") or {}).items():
        have = sorted(_norm(t.title) for t in store.tasks.get(list_name, []) if not t.completed)
        if have != sorted(_norm(t) for t in titles):
            reasons.append(f"state: open on {list_name} is {have}")
    if "events" in state and len(store.events) != state["events"]:
        reasons.append(f"state: {len(store.events)} events, expected {state['events']}")

    checks = case.get("reply") or {}
    text = _norm(reply)
    for s in checks.get("contains", []):
        if _norm(s) not in text:
            reasons.append(f"reply lacks {s!r}")
    if any_of := checks.get("contains_any"):
        if not any(_norm(s) in text for s in any_of):
            reasons.append(f"reply has none of {any_of}")
    for s in checks.get("excludes", []):
        if _norm(s) in text:
            reasons.append(f"reply mentions {s!r}")

    return Result(
        id=case["id"],
        category=case["category"],
        passed=not reasons,
        reasons=reasons,
        bad_writes=bad,
        calls=[c.as_json() for c in store.calls],
        reply=reply,
    )


def gate(results: list[Result]) -> tuple[bool, float, int]:
    """(passed, pass rate, bad write count). One bad write fails the run."""
    rate = sum(r.passed for r in results) / len(results) if results else 0.0
    bad = sum(len(r.bad_writes) for r in results)
    return bad == 0 and rate >= PASS_RATE, rate, bad
