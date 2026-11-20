"""Helpers for the rewrite's tests: a writable copy of the content pack to patch."""
from __future__ import annotations

import shutil
from pathlib import Path

from cat.content import CONTENT_DIR


class Pack:
    def __init__(self, root: Path):
        self.root = root

    def path(self, rel: str) -> Path:
        return self.root / rel

    def read(self, rel: str) -> str:
        return self.path(rel).read_text()

    def write(self, rel: str, text: str) -> None:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def replace(self, rel: str, old: str, new: str) -> None:
        text = self.read(rel)
        assert old in text, f"{old!r} not in {rel}"
        self.write(rel, text.replace(old, new, 1))

    def line_of(self, rel: str, needle: str) -> int:
        for i, line in enumerate(self.read(rel).splitlines(), 1):
            if needle in line:
                return i
        raise AssertionError(f"{needle!r} not in {rel}")


def copy_pack(tmp_path: Path) -> Pack:
    dest = tmp_path / "content"
    shutil.copytree(CONTENT_DIR, dest)
    return Pack(dest)
