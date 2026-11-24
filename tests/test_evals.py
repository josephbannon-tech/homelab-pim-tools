"""The eval harness itself: the case file is satisfiable, the scorer agrees
with a brain that does the right thing, and the gate fires on a wrong write.
No model is involved; real scored runs happen where a model is reachable."""

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from evals.brains import OracleBrain, openapi_tools
from evals.run import run
from evals.score import arg_matches, gate, load_cases, resolve, score_case
from evals.server import EvalServer, seeded_store
from pim_tools.app import app

TZ = ZoneInfo("Europe/London")
# A fixed day in summer time, so offsets are not accidentally UTC.
TODAY = date(2026, 6, 15)


@pytest.fixture(scope="module")
def server():
    with EvalServer() as s:
        yield s


@pytest.fixture(scope="module")
def cases():
    return load_cases(TODAY, TZ)


def test_placeholders_resolve():
    assert resolve("{date:+2} {dayname:+2} {nice:+2}", TODAY, TZ) == "2026-06-17 Wednesday 17 June"
    assert resolve("{dt:+1T09:30}", TODAY, TZ) == "2026-06-16T09:30:00+01:00"


def test_case_file_shape(cases):
    assert len(cases) >= 25
    tools = {t["function"]["name"] for t in openapi_tools(app.openapi())[0]}
    for case in cases:
        assert case["prompt"] and case["category"]
        assert "{" not in case["prompt"], f"{case['id']}: unresolved placeholder"
        for expect in case["expect"]:
            assert expect["tool"] in tools, f"{case['id']}: unknown tool {expect['tool']}"


def test_oracle_passes_every_case(server, cases):
    results = run(OracleBrain(server.url), server, cases, log=lambda _: None)
    assert [(r.id, r.reasons) for r in results if not r.passed] == []
    assert gate(results) == (True, 1.0, 0)


def test_one_bad_write_fails_the_run(server, cases):
    results = run(OracleBrain(server.url, rogue=True), server, cases[:1], log=lambda _: None)
    ok, _, bad = gate(results)
    assert not ok and bad == 1
    assert "bad write add_task" in " ".join(results[0].reasons)


def test_doing_nothing_fails_a_write_case(cases):
    case = next(c for c in cases if c["id"] == "add-task-simple")
    result = score_case(case, seeded_store(case["seed"]), "Added oat milk.")
    assert not result.passed and not result.bad_writes


def test_times_compare_as_instants():
    from datetime import datetime

    stored = datetime.fromisoformat("2026-06-17T09:00:00+00:00")
    assert arg_matches("2026-06-17T10:00:00+01:00", stored)
    assert not arg_matches("2026-06-17T10:00:00+00:00", stored)


def test_function_schemas_stand_alone():
    tools, routes = openapi_tools(app.openapi())
    assert set(routes) == {"list_tasks", "add_task", "complete_task", "list_events", "add_event"}
    add = next(t for t in tools if t["function"]["name"] == "add_task")["function"]
    assert set(add["parameters"]["required"]) == {"list", "title"}
    assert "$ref" not in str(tools)
