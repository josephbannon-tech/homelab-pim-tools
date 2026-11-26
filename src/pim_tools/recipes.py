"""Recipes: a folder of markdown files kept by a meal-planning project.

Layout, under one root:

- `recipes/<slug>.md`: one recipe each, with a frontmatter block of tags
  (`name`, `protein`, `cuisine`, `effort`, `kcal_per_serving`, `serves`, ...)
  and `## Ingredients`, `## Method`, `## Notes` sections. `recipes/INDEX.md`
  is a table of them, kept in step when a tag changes.
- a plans folder (`tmp/` by default): one-off meal plans, each with a
  `## Shopping` section.
- a lists folder (`shopping-lists/` by default): one generated shopping list
  per planned week, one `##` section per supermarket aisle.
- guide files (`profile.md` by default): what the household eats, read
  before planning a meal.

The files belong to the person; a desktop editor works on the same folder.
So an assistant gets narrow edits only, each of which the files survive:

- add a line to a recipe's Notes;
- replace one exact passage, which must occur once, and the result must still
  carry its tags and its Ingredients and Method sections;
- set one tag to an allowed value;
- save a new plan, never over an old one.

Nothing is deleted or renamed, and an unknown name is refused with the names
that are close, so a misheard name does not land on the wrong recipe.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

MAX_READ = 40_000

# Tags an assistant may set, and what each accepts.
TAGS: dict[str, tuple[str, ...] | type] = {
    "effort": ("quick", "standard", "weekend"),
    "kcal_per_serving": int,
    "serves": int,
    "scales_well": ("yes", "no"),
    "freezes_well": ("yes", "no"),
}
REQUIRED = ("name", "protein", "cuisine", "effort", "kcal_per_serving", "serves")
# INDEX.md column for each tag that the index shows.
INDEX_COLUMNS = {"effort": "effort", "kcal_per_serving": "kcal"}
# Sections of a weekly list that are not things to buy.
NOT_SHOPPING = ("this week", "lunches covered", "banked")
CHECK = "check you have"
# A comma-separated piece that is only an amount ("300 g", "1 tsp", "½ bunch")
# belongs to the item before it, not a second item.
_AMOUNT = re.compile(
    r"\A(?:[\d½¼¾⅓⅔.,/\-– ]+|a few|a little|a pinch|a knob|a splash|half a?)\s*"
    r"(?:g|kg|ml|l|litres?|tsp|tbsp|teaspoons?|tablespoons?|cups?|tins?|cans?|jars?|"
    r"packs?|bunch(?:es)?|pinch(?:es)?|cloves?|large|small|medium|of (?:it|them))?\.?\Z",
    re.I,
)

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


class NoRecipes(Exception):
    """No recipes folder is configured."""


class NoSuchRecipe(Exception):
    def __init__(self, name: str, close: list[str]):
        super().__init__(name)
        self.name, self.close = name, close


class Refused(Exception):
    """A change the files would not survive, or one the rules do not allow."""


class Exists(Exception):
    pass


@dataclass
class Doc:
    name: str
    kind: str  # recipe | plan | weekly list
    path: Path
    tags: dict[str, str] = field(default_factory=dict)
    retired: bool = False
    modified: datetime | None = None
    text: str | None = None
    truncated: bool = False


@dataclass
class ShoppingItems:
    items: list[str]
    have: list[str]  # marked "(have)" in the plan, so not bought


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[-_]", " ", s).split()).casefold()


def tags_of(text: str) -> dict[str, str] | None:
    """The frontmatter as {key: value}, comments after '#' dropped."""
    m = _FRONT.match(text)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "#")):
            key, value = line.split(":", 1)
            out[key.strip()] = value.split("#", 1)[0].strip()
    return out


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")


def _write(path: Path, text: str) -> None:
    """Write beside the file and swap it in, so a reader or sync tool never
    sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pim-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)


def _clean_item(line: str) -> str:
    """A shopping line as a short task title: the explanation after ': ' and
    any long aside in brackets go; quantities stay."""

    def aside(m: re.Match) -> str:
        return "" if len(m.group(1)) > 20 or ";" in m.group(1) else m.group(0)

    s = re.sub(r"\s*\(([^()]*)\)", aside, line)  # before the ': ' cut, which may sit inside one
    s = s.split(": ", 1)[0]
    return " ".join(s.split()).rstrip(".")


def _split_outside_brackets(s: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def shopping_items(text: str, kind: str) -> ShoppingItems:
    """The things to buy in a plan's `## Shopping` section, or in every aisle
    section of a weekly list. Items under "Check you have" come back as
    'Check: <item>', one per comma-separated entry; an entry that is only an
    amount stays with the item before it."""
    items: list[str] = []
    have: list[str] = []
    in_scope = False
    group = ""
    bullets: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            heading = _norm(line[3:])
            if kind == "plan":
                in_scope = heading == "shopping"
                group = ""
            else:
                in_scope = not heading.startswith(NOT_SHOPPING)
                group = heading
            continue
        if not in_scope or not line.strip():
            continue
        if line.startswith("### "):
            group = _norm(line[4:])
        elif line.startswith("- "):
            bullets.append((group, line[2:].strip()))
        elif line.startswith((" ", "\t")) and bullets:
            g, prev = bullets[-1]
            bullets[-1] = (g, prev + " " + line.strip())
        else:
            group = _norm(line)  # a plain label line names the group below it
    for g, item in bullets:
        if "(have)" in item.casefold():
            have.append(_clean_item(item.replace("(have)", "")))
            continue
        if g.startswith(CHECK):
            parts: list[str] = []
            for p in _split_outside_brackets(item):
                if parts and _AMOUNT.match(p.strip()):
                    parts[-1] = f"{parts[-1]}, {p.strip()}"
                else:
                    parts.append(p)
            items += [f"Check: {_clean_item(p)}" for p in parts]
            continue
        title = _clean_item(item)
        if title and not title.casefold().startswith("nothing"):
            items.append(title)
    seen, unique = set(), []
    for i in items:
        if _norm(i) not in seen:
            seen.add(_norm(i))
            unique.append(i)
    return ShoppingItems(unique, have)


class Recipes:
    def __init__(
        self,
        root: str | os.PathLike,
        plans: str = "tmp",
        lists: str = "shopping-lists",
        guides: tuple[str, ...] = ("profile.md",),
    ):
        self.root = Path(root)
        self.recipes_dir = self.root / "recipes"
        self.plans_dir = self.root / plans
        self.lists_dir = self.root / lists
        self.guides = guides

    # -- which files count
    def _inside(self, path: Path) -> bool:
        return path.resolve().is_relative_to(self.root.resolve())

    def _ok(self, path: Path) -> bool:
        return path.is_file() and self._inside(path) and not path.name.startswith(".")

    def _docs(self) -> list[Doc]:
        docs = []
        for p in sorted(self.recipes_dir.glob("*.md")):
            if not self._ok(p) or p.name.casefold() == "index.md":
                continue
            text = p.read_text(errors="replace")
            tags = tags_of(text) or {}
            docs.append(
                Doc(
                    tags.get("name") or p.stem,
                    "recipe",
                    p,
                    tags,
                    tags.get("retired", "").casefold() == "yes",
                )
            )
        for p in sorted(self.plans_dir.rglob("*.md")):
            if self._ok(p) and not any(part.startswith(".") for part in p.parts):
                docs.append(Doc(_title(p.read_text(errors="replace"), p.stem), "plan", p))
        for p in sorted(self.lists_dir.glob("*.md")):
            if self._ok(p):
                docs.append(Doc(p.stem, "weekly list", p))
        for d in docs:
            d.modified = datetime.fromtimestamp(d.path.stat().st_mtime, UTC)
        return docs

    def _find(self, name: str, kinds: tuple[str, ...] | None = None) -> Doc:
        docs = [d for d in self._docs() if kinds is None or d.kind in kinds]
        want = _norm(name.removesuffix(".md"))
        for d in docs:
            if want in (_norm(d.name), _norm(d.path.stem)):
                return d
        part = [d for d in docs if want and (want in _norm(d.name) or want in _norm(d.path.stem))]
        if len(part) == 1:
            return part[0]
        names = [d.name for d in docs]
        close = [d.name for d in part] or difflib.get_close_matches(name, names, n=8, cutoff=0.6)
        raise NoSuchRecipe(name, close[:8])

    def _load(self, doc: Doc) -> Doc:
        text = doc.path.read_text(errors="replace")
        doc.text, doc.truncated = text[:MAX_READ], len(text) > MAX_READ
        doc.modified = datetime.fromtimestamp(doc.path.stat().st_mtime, UTC)
        return doc

    # -- reading
    def list(self, query: str | None = None) -> list[Doc]:
        docs = self._docs()
        if not query:
            return docs
        words = _norm(query).split()
        out = []
        for d in docs:
            hay = _norm(
                " ".join([d.name, d.kind, *d.tags.values(), d.path.read_text(errors="replace")])
            )
            if all(w in hay for w in words):
                out.append(d)
        return out

    def read(self, name: str) -> Doc:
        return self._load(self._find(name))

    def guide(self) -> str:
        parts = []
        for g in self.guides:
            p = self.root / g
            if self._ok(p):
                parts.append(p.read_text(errors="replace"))
        return "\n\n".join(parts)[:MAX_READ]

    # -- the narrow edits
    def _save_recipe(self, doc: Doc, before: str, after: str) -> Doc:
        old, new = tags_of(before) or {}, tags_of(after)
        if new is None:
            raise Refused("that change breaks the recipe's tag block; nothing was saved")
        missing = [k for k in REQUIRED if not new.get(k)]
        if missing:
            raise Refused(f"that change removes the {', '.join(missing)} tag; nothing was saved")
        if new.get("name") != old.get("name"):
            raise Refused("a recipe cannot be renamed here; nothing was saved")
        for heading in ("## Ingredients", "## Method"):
            if heading not in after:
                raise Refused(f"that change removes the {heading[3:]} section; nothing was saved")
        _write(doc.path, after)
        return self._load(doc)

    def add_note(self, name: str, text: str) -> Doc:
        doc = self._load(self._find(name, ("recipe",)))
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        if not lines:
            raise Refused("the note is empty")
        add = "\n".join(ln if ln.startswith("- ") else f"- {ln}" for ln in lines)
        before = doc.path.read_text(errors="replace")
        m = re.search(r"^## Notes[ \t]*$", before, re.M)
        if m:
            nxt = re.search(r"^## ", before[m.end() :], re.M)
            end = m.end() + nxt.start() if nxt else len(before)
            head, tail = before[:end].rstrip("\n"), before[end:]
            after = head + "\n" + add + "\n" + ("\n" + tail if tail else "")
        else:
            after = before.rstrip("\n") + "\n\n## Notes\n\n" + add + "\n"
        return self._save_recipe(doc, before, after)

    def change_text(self, name: str, old: str, new: str) -> Doc:
        doc = self._find(name, ("recipe",))
        before = doc.path.read_text(errors="replace")
        if not old.strip():
            raise Refused("old_text is empty")
        count = before.count(old)
        if count == 0:
            raise Refused(
                "old_text is not in the recipe word for word. Read the recipe and copy the "
                "exact passage, including its punctuation and ** marks."
            )
        if count > 1:
            raise Refused(f"old_text appears {count} times; include more of the surrounding words")
        return self._save_recipe(doc, before, before.replace(old, new, 1))

    def set_tag(self, name: str, tag: str, value: str) -> tuple[Doc, bool]:
        """Returns (recipe, whether INDEX.md was updated)."""
        if tag not in TAGS:
            raise Refused(f"'{tag}' cannot be set here; the tags are: {', '.join(TAGS)}")
        rule, value = TAGS[tag], str(value).strip().casefold()
        if rule is int:
            value = value.lstrip("~")
            if not value.isdigit():
                raise Refused(f"{tag} takes a whole number")
        elif value not in rule:  # type: ignore[operator]
            raise Refused(f"{tag} takes one of: {', '.join(rule)}")  # type: ignore[arg-type]
        doc = self._find(name, ("recipe",))
        before = doc.path.read_text(errors="replace")
        m = _FRONT.match(before)
        if not m:
            raise Refused("the recipe has no tag block")
        block = m.group(1)
        line = re.compile(rf"^({re.escape(tag)}:[ \t]*)([^#\n]*?)([ \t]*#.*)?$", re.M)
        if line.search(block):
            block = line.sub(lambda x: x.group(1) + value + (x.group(3) or ""), block, count=1)
        else:
            block += f"\n{tag}: {value}"
        after = before[: m.start(1)] + block + before[m.end(1) :]
        saved = self._save_recipe(doc, before, after)
        return saved, self._update_index(doc.path.name, tag, value)

    def _update_index(self, filename: str, tag: str, value: str) -> bool:
        column = INDEX_COLUMNS.get(tag)
        index = self.recipes_dir / "INDEX.md"
        if not column or not self._ok(index):
            return False
        lines = index.read_text(errors="replace").splitlines(keepends=True)
        header = next((i for i, ln in enumerate(lines) if ln.startswith("|")), None)
        if header is None:
            return False
        cols = [_norm(c) for c in lines[header].strip().strip("|").split("|")]
        at = next((i for i, c in enumerate(cols) if c.startswith(column)), None)
        if at is None:
            return False
        for i, ln in enumerate(lines):
            if ln.startswith("|") and f"({filename})" in ln:
                cells = ln.rstrip("\n").strip().strip("|").split("|")
                cells[at] = f" {'~' if tag == 'kcal_per_serving' else ''}{value} "
                lines[i] = "|" + "|".join(cells) + "|\n"
                _write(index, "".join(lines))
                return True
        return False

    def save_plan(self, title: str, text: str) -> Doc:
        title = " ".join(title.split())
        slug = _slug(title)
        if not slug:
            raise Refused("the plan needs a title")
        if not re.search(r"^## Shopping[ \t]*$", text, re.M | re.I):
            raise Refused(
                "the plan needs a '## Shopping' section listing what to buy, so it can go "
                "on the shopping list"
            )
        path = self.plans_dir / f"{slug}.md"
        if not self._inside(path.parent) and path.parent.exists():
            raise Refused("the plans folder is outside the recipes folder")
        if path.exists():
            raise Exists(path.stem)
        body = text.strip() + "\n"
        if not body.startswith("# "):
            body = f"# {title}\n\n{body}"
        _write(path, body)
        return self._load(Doc(_title(body, title), "plan", path))

    def shopping(self, name: str) -> tuple[Doc, ShoppingItems]:
        doc = self._find(name, ("plan", "weekly list"))
        return doc, shopping_items(doc.path.read_text(errors="replace"), doc.kind)
