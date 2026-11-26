"""Notes: markdown files in a folder that a file-sync tool keeps on the phone.

The folder is the store. A note is `Name.md`, or `Folder/Name.md`; its name is
that path without the extension. Nothing here is a database, so the notes stay
usable by any editor and survive this service being deleted.

Three rules keep an assistant from doing damage or seeing too much:

- It can read and append. It cannot overwrite, rename or delete; rewriting a
  note is a job for the person, in their editor.
- Appending to a note that does not exist is refused unless `create` is set,
  and the refusal names the notes that do exist. A misheard name must not
  quietly become a new file.
- Folders named in `exclude` (and anything starting with a dot) do not exist
  as far as these calls are concerned: not listed, not searched, not readable,
  not writable. That is where private notes go.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_READ = 20_000  # characters returned by one read; a longer note is cut and says so


class NoNotes(Exception):
    """No notes folder is configured."""


class NoSuchNote(Exception):
    def __init__(self, name: str, known: list[str]):
        super().__init__(name)
        self.name, self.known = name, known


class BadName(Exception):
    pass


@dataclass
class NoteFile:
    name: str
    modified: datetime
    size: int
    text: str | None = None
    truncated: bool = False


def _norm(s: str) -> str:
    return " ".join(s.replace("\\", "/").split()).casefold()


class Notes:
    def __init__(self, root: str | os.PathLike, exclude: tuple[str, ...] = ()):
        self.root = Path(root)
        self.exclude = {e.casefold() for e in exclude}

    # -- which files count
    def _hidden(self, rel: Path) -> bool:
        parts = rel.parts
        return any(p.startswith(".") for p in parts) or (
            len(parts) > 1 and parts[0].casefold() in self.exclude
        )

    def _inside(self, path: Path) -> bool:
        """False for anything that resolves outside the folder: a symlinked
        note or folder must not become a way to read or write other files."""
        return path.resolve().is_relative_to(self.root.resolve())

    def _files(self) -> dict[str, Path]:
        """{name: path} for every visible note."""
        found = {}
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root)
            if path.is_file() and not self._hidden(rel) and self._inside(path):
                found[rel.with_suffix("").as_posix()] = path
        return found

    def _info(self, name: str, path: Path) -> NoteFile:
        st = path.stat()
        return NoteFile(name, datetime.fromtimestamp(st.st_mtime, UTC), st.st_size)

    def _find(self, name: str) -> tuple[str, Path]:
        files = self._files()
        want = _norm(name.removesuffix(".md"))
        for known, path in files.items():
            if _norm(known) == want:
                return known, path
        # a bare name also finds a note inside a folder, when only one has it
        leaf = [(k, p) for k, p in files.items() if _norm(k.rsplit("/", 1)[-1]) == want]
        if len(leaf) == 1:
            return leaf[0]
        raise NoSuchNote(name, sorted(files))

    # -- the three operations
    def list(self, query: str | None = None) -> list[NoteFile]:
        out = []
        for name, path in self._files().items():
            if query:
                q = _norm(query)
                if q not in _norm(name) and q not in _norm(path.read_text(errors="replace")):
                    continue
            out.append(self._info(name, path))
        return out

    def read(self, name: str) -> NoteFile:
        known, path = self._find(name)
        text = path.read_text(errors="replace")
        note = self._info(known, path)
        note.text, note.truncated = text[:MAX_READ], len(text) > MAX_READ
        return note

    def append(self, name: str, text: str, create: bool = False) -> tuple[NoteFile, bool]:
        """Add text at the end of a note. Returns (note, created)."""
        try:
            known, path = self._find(name)
            created = False
        except NoSuchNote:
            if not create:
                raise
            known, path, created = *self._new(name), True
        before = path.read_text(errors="replace") if not created else ""
        if before and not before.endswith("\n"):
            before += "\n"
        after = before + text.strip("\n") + "\n"
        # Write beside the note and swap it in, so the sync tool never ships a
        # half-written file. Group-writable: the sync tool runs as another user.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pim-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(after)
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
        note = self._info(known, path)
        note.text = after[-MAX_READ:]
        return note, created

    def _new(self, name: str) -> tuple[str, Path]:
        clean = name.replace("\\", "/").strip().removesuffix(".md").strip("/")
        rel = Path(clean + ".md")
        bad = (
            not clean
            or rel.is_absolute()
            or ".." in rel.parts
            or any(c in clean for c in '<>:"|?*\n')
        )
        path = self.root / rel
        if bad or self._hidden(rel) or not self._inside(path.parent):
            raise BadName(name)
        return clean, path
