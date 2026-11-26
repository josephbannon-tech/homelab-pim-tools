"""Recipes: read anything, change only by narrow edits the files survive, and
put a plan's or a week's shopping on the shopping list."""

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import DEFAULT_TOOLSETS, app, get_recipes, get_store, selected_routes
from pim_tools.recipes import Recipes, shopping_items
from pim_tools.store import MemoryStore

KEEMA = """---
name: Lamb Keema
protein: lamb
cuisine: indian
effort: standard        # ~45 min
kcal_per_serving: 670
serves: 4
scales_well: yes
freezes_well: yes
---

# Lamb Keema

Spiced mince with peas.

## Ingredients

- 500 g lamb mince
- 1 tin chopped tomatoes

## Method

1. **T-18: Tomatoes.** Simmer uncovered **15 minutes**.
2. **T-0: Serve.**

## Notes

- Better the next day.
"""

COD = """---
name: Cod Curry
protein: fish
cuisine: indian
effort: quick           # ~30 min
kcal_per_serving: 520
serves: 2
---

# Cod Curry

## Ingredients

- 2 cod loins

## Method

1. **T-0: Serve.**
"""

INDEX = """# Recipe Index

| Recipe | Protein | Cuisine | Effort | kcal/serving | File |
|---|---|---|---|---|---|
| Lamb Keema | lamb | indian | standard | ~670 | [lamb-keema.md](lamb-keema.md) |
| Cod Curry | fish | indian | quick | ~520 | [cod-curry.md](cod-curry.md) |
"""

WEEK = """# Shopping List: Week of Mon 2 Nov

## This week's dinners

| Recipe | Serves |
|---|---|
| Lamb Keema | 4 |

## Produce

- 4 onions
- potatoes, 1.8 kg (800 g chips for one night, the rest roasted)

## Meat & fish

- 500 g lamb mince: one pack, 20% fat

## Frozen

- nothing this week

## Check you have

- sunflower oil, olive oil
- stock (about 950 ml across the week: lamb 600 ml, duck 250 ml)

## Lunches covered

- 2 lunches
"""

PLAN = """# Steak Night

## Shopping

Meat

- 2 sirloin steaks
- 1 pack beef dripping (have)

Check you have

- salt, pepper

## Method

1. Cook.
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "recipes").mkdir()
    (tmp_path / "recipes" / "lamb-keema.md").write_text(KEEMA)
    (tmp_path / "recipes" / "cod-curry.md").write_text(COD)
    (tmp_path / "recipes" / "INDEX.md").write_text(INDEX)
    (tmp_path / "shopping-lists").mkdir()
    (tmp_path / "shopping-lists" / "2026-11-02.md").write_text(WEEK)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "steak-night.md").write_text(PLAN)
    (tmp_path / "profile.md").write_text("Loves: lamb. Won't eat: liver.\n")
    return tmp_path


@pytest.fixture
def store():
    s = MemoryStore()
    s.tasks["shopping"] = []
    return s


@pytest.fixture
def client(root, store):
    app.dependency_overrides[get_recipes] = lambda: Recipes(root)
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_default_deployment_serves_no_recipe_tools():
    kept = selected_routes(app.router.routes, DEFAULT_TOOLSETS.split(","))
    ops = {getattr(r, "operation_id", None) for r in kept}
    assert "list_tasks" in ops and "read_recipe" not in ops
    assert {getattr(r, "path", "") for r in kept} >= {"/health"}


def test_list_search_and_read(client):
    names = {r["name"]: r["kind"] for r in client.get("/recipes").json()["recipes"]}
    assert names == {
        "Cod Curry": "recipe",
        "Lamb Keema": "recipe",
        "Steak Night": "plan",
        "2026-11-02": "weekly list",
    }
    found = client.get("/recipes", params={"query": "quick fish"}).json()["recipes"]
    assert [r["name"] for r in found] == ["Cod Curry"]
    r = client.get("/recipes/read", params={"name": "keema"}).json()
    assert r["effort"] == "standard" and "500 g lamb mince" in r["text"]


def test_unknown_name_names_the_close_ones(client):
    r = client.get("/recipes/read", params={"name": "Lamb Kheema"})
    assert r.status_code == 404 and "'Lamb Keema'" in r.json()["detail"]


def test_guide(client):
    assert "liver" in client.get("/recipes/guide").json()["text"]


def test_note_lands_in_notes(client, root):
    r = client.post("/recipes/note", json={"name": "Lamb Keema", "text": "Double the chilli."})
    assert r.status_code == 200
    text = (root / "recipes" / "lamb-keema.md").read_text()
    assert text.endswith("- Better the next day.\n- Double the chilli.\n")


def test_note_makes_a_notes_section_when_missing(client, root):
    client.post("/recipes/note", json={"name": "Cod Curry", "text": "Loins, not fillets."})
    assert (
        (root / "recipes" / "cod-curry.md")
        .read_text()
        .endswith("## Notes\n\n- Loins, not fillets.\n")
    )


def test_change_one_exact_passage(client, root):
    r = client.post(
        "/recipes/change",
        json={"name": "Lamb Keema", "old_text": "**15 minutes**", "new_text": "**20 minutes**"},
    )
    assert r.status_code == 200
    assert "**20 minutes**" in (root / "recipes" / "lamb-keema.md").read_text()


@pytest.mark.parametrize(
    "old,new,why",
    [
        ("**25 minutes**", "x", "word for word"),
        ("**T-", "**T+", "appears 2 times"),
        ("## Method", "## Steps", "Method section"),
        ("name: Lamb Keema", "name: Keema", "renamed"),
        ("kcal_per_serving: 670\n", "", "kcal_per_serving tag"),
        ("---\n\n# Lamb", "\n# Lamb", "tag block"),
    ],
)
def test_change_refuses_what_the_file_would_not_survive(client, root, old, new, why):
    before = (root / "recipes" / "lamb-keema.md").read_text()
    r = client.post(
        "/recipes/change", json={"name": "Lamb Keema", "old_text": old, "new_text": new}
    )
    assert r.status_code == 422 and why in r.json()["detail"]
    assert (root / "recipes" / "lamb-keema.md").read_text() == before


def test_tag_keeps_its_comment_and_moves_the_index(client, root):
    r = client.post(
        "/recipes/tag", json={"name": "Cod Curry", "tag": "effort", "value": "Standard"}
    )
    assert r.status_code == 200 and r.json()["index_updated"] is True
    assert "effort: standard           # ~30 min" in (root / "recipes" / "cod-curry.md").read_text()
    assert (
        "| Cod Curry | fish | indian | standard | ~520 |"
        in (root / "recipes" / "INDEX.md").read_text()
    )
    r = client.post(
        "/recipes/tag", json={"name": "Cod Curry", "tag": "kcal_per_serving", "value": "~560"}
    )
    assert "| standard | ~560 |" in (root / "recipes" / "INDEX.md").read_text()
    r = client.post(
        "/recipes/tag", json={"name": "Cod Curry", "tag": "freezes_well", "value": "yes"}
    )
    assert r.json()["index_updated"] is False
    assert "freezes_well: yes" in (root / "recipes" / "cod-curry.md").read_text()


@pytest.mark.parametrize(
    "tag,value", [("effort", "slow"), ("serves", "two"), ("protein", "beef"), ("name", "X")]
)
def test_tag_refuses_bad_values_and_other_tags(client, tag, value):
    r = client.post("/recipes/tag", json={"name": "Cod Curry", "tag": tag, "value": value})
    assert r.status_code == 422


def test_save_plan_never_overwrites_and_needs_shopping(client, root):
    body = "## Shopping\n\n- 2 duck breasts\n\n## Method\n\n1. Cook.\n"
    r = client.post("/plans", json={"title": "Duck for Two", "text": body})
    assert r.status_code == 200 and r.json()["kind"] == "plan"
    assert (
        (root / "tmp" / "duck-for-two.md").read_text().startswith("# Duck for Two\n\n## Shopping")
    )
    assert client.post("/plans", json={"title": "Duck for Two", "text": body}).status_code == 409
    r = client.post("/plans", json={"title": "No List", "text": "## Method\n\n1. Cook it all."})
    assert r.status_code == 422


def test_plan_to_shopping(client, store):
    r = client.post("/shopping/from", json={"name": "Steak Night"}).json()
    assert r["added"] == ["2 sirloin steaks", "Check: salt", "Check: pepper"]
    assert r["have"] == ["1 pack beef dripping"]
    again = client.post("/shopping/from", json={"name": "Steak Night"}).json()
    assert again["added"] == [] and len(again["already_there"]) == 3
    assert len(store.tasks["shopping"]) == 3


def test_week_to_shopping(client):
    r = client.post("/shopping/from", json={"name": "2026-11-02"}).json()
    assert r["added"] == [
        "4 onions",
        "potatoes, 1.8 kg",
        "500 g lamb mince",
        "Check: sunflower oil",
        "Check: olive oil",
        "Check: stock",
    ]


def test_recipes_have_no_shopping_section(client):
    assert client.post("/shopping/from", json={"name": "Cod Curry"}).status_code == 404


def test_symlinks_out_of_the_folder_are_invisible(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.md").write_text(KEEMA.replace("Lamb Keema", "Secret"))
    (root / "recipes" / "secret.md").symlink_to(outside / "secret.md")
    assert "Secret" not in {d.name for d in Recipes(root).list()}


def test_continuation_lines_join_their_item():
    text = "## Check you have\n\n- smoked paprika, sweet paprika,\n  turmeric\n"
    assert shopping_items(text, "weekly list").items == [
        "Check: smoked paprika",
        "Check: sweet paprika",
        "Check: turmeric",
    ]


def test_recipes_off_without_a_folder():
    from pim_tools.app import _default_recipes

    _default_recipes.cache_clear()
    assert TestClient(app).get("/recipes").status_code == 503
