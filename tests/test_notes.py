"""Notes: read and append over a folder of markdown files, never overwrite,
never see the private folder."""

import os

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import app, get_notes
from pim_tools.notes import BadName, NoSuchNote, Notes


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Festival packing list.md").write_text("# Festival\n\n- [ ] Tent\n- [ ] Mallet")
    (tmp_path / "Recipes").mkdir()
    (tmp_path / "Recipes" / "Pad krapow.md").write_text("Holy basil, chillies, pork mince.\n")
    (tmp_path / "Private").mkdir()
    (tmp_path / "Private" / "Health.md").write_text("the secret word is heliotrope\n")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "workspace.md").write_text("editor state\n")
    (tmp_path / "photo.jpg").write_text("not a note")
    return tmp_path


@pytest.fixture
def client(vault):
    app.dependency_overrides[get_notes] = lambda: Notes(vault, exclude=("Private",))
    yield TestClient(app)
    app.dependency_overrides.clear()


def names(r):
    return [n["name"] for n in r.json()["notes"]]


def test_lists_notes_and_nothing_private_or_hidden(client):
    assert names(client.get("/notes")) == ["Festival packing list", "Recipes/Pad krapow"]


def test_query_searches_names_and_text(client):
    assert names(client.get("/notes", params={"query": "holy basil"})) == ["Recipes/Pad krapow"]
    assert names(client.get("/notes", params={"query": "FESTIVAL"})) == ["Festival packing list"]
    assert names(client.get("/notes", params={"query": "heliotrope"})) == []


def test_read_by_name_case_blind_and_by_leaf(client):
    r = client.get("/notes/read", params={"name": "festival packing list"}).json()
    assert r["name"] == "Festival packing list" and "- [ ] Tent" in r["text"]
    assert client.get("/notes/read", params={"name": "pad krapow.md"}).json()["name"] == (
        "Recipes/Pad krapow"
    )


def test_private_and_hidden_notes_cannot_be_read(client):
    for name in ("Private/Health", "Health", ".obsidian/workspace", "../etc/passwd"):
        r = client.get("/notes/read", params={"name": name})
        assert r.status_code == 404 and "heliotrope" not in r.text
        assert "Health" not in r.json()["detail"].split("The notes are:")[1]


def test_append_adds_a_line_and_keeps_what_was_there(client, vault):
    body = {"name": "Festival packing list", "text": "- [ ] Camping chair"}
    r = client.post("/notes/append", json=body).json()
    assert r["created"] is False
    text = (vault / "Festival packing list.md").read_text()
    assert text == "# Festival\n\n- [ ] Tent\n- [ ] Mallet\n- [ ] Camping chair\n"
    assert oct((vault / "Festival packing list.md").stat().st_mode)[-3:] == "664"
    assert [p.name for p in vault.iterdir() if p.name.startswith(".pim-")] == []


def test_append_to_a_missing_note_names_the_ones_that_exist(client, vault):
    r = client.post("/notes/append", json={"name": "Holiday", "text": "Passport"})
    assert r.status_code == 404 and "Festival packing list" in r.json()["detail"]
    assert not (vault / "Holiday.md").exists()


def test_create_makes_a_note_only_when_asked(client, vault):
    body = {"name": "Trips/Holiday", "text": "Passport", "create": True}
    assert client.post("/notes/append", json=body).json()["created"] is True
    assert (vault / "Trips" / "Holiday.md").read_text() == "Passport\n"


@pytest.mark.parametrize("name", ["Private/Diary", "../outside", ".obsidian/x", "", "a\nb"])
def test_create_refuses_private_hidden_and_escaping_names(client, vault, name):
    r = client.post("/notes/append", json={"name": name, "text": "x", "create": True})
    assert r.status_code == 422
    assert not (vault / "Private" / "Diary.md").exists()
    assert not (vault.parent / "outside.md").exists()


def test_private_note_cannot_be_appended_to(client, vault):
    r = client.post("/notes/append", json={"name": "Health", "text": "x"})
    assert r.status_code == 404
    assert (vault / "Private" / "Health.md").read_text() == "the secret word is heliotrope\n"


def test_a_long_note_is_cut_and_says_so(client, vault):
    (vault / "Long.md").write_text("x" * 30_000)
    r = client.get("/notes/read", params={"name": "Long"}).json()
    assert r["truncated"] is True and len(r["text"]) == 20_000


def test_without_a_notes_folder_the_tools_say_so():
    os.environ.pop("NOTES_DIR", None)
    r = TestClient(app).get("/notes")
    assert r.status_code == 503 and "not set up" in r.json()["detail"]


def test_symlinks_out_of_the_folder_are_invisible(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("the secret word is heliotrope\n")
    (vault / "Link.md").symlink_to(outside / "secret.md")
    (vault / "Elsewhere").symlink_to(outside)
    notes = Notes(vault)
    assert notes.list() == []
    with pytest.raises(NoSuchNote):
        notes.read("Link")
    with pytest.raises(BadName):
        notes.append("Elsewhere/New", "x", create=True)
    assert sorted(p.name for p in outside.iterdir()) == ["secret.md"]
