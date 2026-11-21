from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import PurePosixPath

from .identity import IdentityLedger
from .models import ManifestEntry

NEUTRAL_DIRS = {
    "_rels",
    "3d",
    "archive",
    "archives",
    "assets",
    "cad",
    "charts",
    "comments",
    "customxml",
    "data",
    "docprops",
    "docs",
    "drawings",
    "embeddings",
    "export",
    "exports",
    "files",
    "images",
    "input",
    "models",
    "parts",
    "references",
    "source",
    "meta-inf",
    "metadata",
    "media",
    "notesmasters",
    "notesslides",
    "ppt",
    "slidelayouts",
    "slidemasters",
    "slides",
    "sources",
    "step",
    "stl",
    "tables",
    "test",
    "tests",
    "theme",
    "v1",
    "v2",
    "word",
    "xl",
}
NEUTRAL_NAMES = {
    "readme",
    "license",
    "licence",
    "notice",
    "changelog",
    "thumbs",
    "desktop",
    "manifest",
    "index",
    "makefile",
    "dockerfile",
    "document",
    "guidocument",
    "workbook",
    "presentation",
    "sharedstrings",
    "styles",
    "settings",
    "theme1",
    "core",
    "app",
    "custom",
    "3dmodel",
    "[content_types]",
    "content_types",
    "rels",
    "thumbnail",
    "thumbnail1",
}
_REFERENCE_DELIMITERS = frozenset(
    ("\"", "'", "<", ">", "(", ")", "[", "]", ",", ";", ":", "=")
)
_REFERENCE_SUFFIX = re.compile(
    r"\.[A-Za-z][A-Za-z0-9]{0,11}(?=$|[\s\"'<>()[\],;:=])"
)


def _has_file_extension(value: str) -> bool:
    if "." not in value:
        return False
    filename = value.replace("\\", "/").rsplit("/", 1)[-1]
    extension = filename.rpartition(".")[2]
    return bool(
        extension
        and len(extension) <= 12
        and extension[0].isascii()
        and extension[0].isalpha()
        and extension.isascii()
        and extension.isalnum()
    )


def rewrite_reference_text(
    text: str,
    file_map: Mapping[str, str],
    component_map: dict[str, str] | None = None,
) -> str:
    """Replace referenced paths using indexed exact-name lookups."""

    component_map = component_map or {}

    @lru_cache(maxsize=8192)
    def lookup(value: str) -> str | None:
        direct = file_map.get(value)
        if direct is not None:
            return direct
        if "/" not in value and "\\" not in value:
            return None
        normalized = value.replace("\\", "/")
        direct = file_map.get(normalized)
        if direct is not None:
            return direct
        parts = PurePosixPath(normalized).parts
        if not parts:
            return None
        filename = file_map.get(parts[-1])
        if filename is None:
            return None
        mapped = [component_map.get(part, part) for part in parts[:-1]]
        mapped.append(filename)
        return PurePosixPath(*mapped).as_posix()

    replacements: list[tuple[int, int, str]] = []
    index = 0
    while index < len(text):
        single_quote = text.find("'", index)
        double_quote = text.find('"', index)
        quote = min(
            (position for position in (single_quote, double_quote) if position >= 0),
            default=-1,
        )
        suffix = _REFERENCE_SUFFIX.search(text, index)
        if quote >= 0 and (suffix is None or quote <= suffix.start()):
            closing = text.find(text[quote], quote + 1)
            if closing >= 0:
                value = text[quote + 1 : closing]
                if _has_file_extension(value):
                    replacement = lookup(value)
                    if replacement is not None:
                        replacements.append((quote + 1, closing, replacement))
                index = closing + 1
                continue
            index = quote + 1
            continue
        if suffix is None:
            break
        start, end = suffix.span()
        while start > index and not (
            text[start - 1].isspace() or text[start - 1] in _REFERENCE_DELIMITERS
        ):
            start -= 1
        value = text[start:end]
        if _has_file_extension(value):
            replacement = lookup(value)
            if replacement is not None:
                replacements.append((start, end, replacement))
        index = end
    if not replacements:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        parts.append(text[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


class PathMapper:
    """Create a stable path map before processing so references can follow renames."""

    def __init__(self, ledger: IdentityLedger, entries: list[ManifestEntry]) -> None:
        self.by_path: dict[str, str] = {}
        self.by_filename: dict[str, str] = {}
        self.by_component: dict[str, str] = {}
        used_targets: dict[str, str] = {}
        for entry in entries:
            original = entry.path.as_posix()
            mapped_parts: list[str] = []
            for index, component in enumerate(entry.path.parts):
                if index < len(entry.path.parts) - 1:
                    if component.casefold() in NEUTRAL_DIRS:
                        mapped_parts.append(component)
                    else:
                        mapped = ledger.pseudonym("project", component)
                        mapped_parts.append(mapped)
                        self.by_component[component] = mapped
                    continue
                suffix = "".join(entry.path.suffixes)
                stem = entry.path.name[: -len(suffix)] if suffix else entry.path.name
                if stem.casefold() in NEUTRAL_NAMES:
                    mapped_name = stem + suffix
                else:
                    mapped_name = ledger.pseudonym("part", stem) + suffix
                mapped_parts.append(mapped_name)
                self.by_filename[entry.path.name] = mapped_name
            mapped_path = PurePosixPath(*mapped_parts).as_posix()
            prior = used_targets.get(mapped_path)
            if prior is not None and prior != original:
                filename = mapped_parts[-1]
                suffix = "".join(entry.path.suffixes)
                stem = filename[: -len(suffix)] if suffix else filename
                mapped_parts[-1] = f"{stem}-{ledger.pseudonym_exact('file', original)}{suffix}"
                mapped_path = PurePosixPath(*mapped_parts).as_posix()
                self.by_filename[entry.path.name] = mapped_parts[-1]
            used_targets[mapped_path] = original
            self.by_path[original] = mapped_path
        self._reference_map = {**self.by_filename, **self.by_path}

    def output_path(self, original: str | PurePosixPath) -> PurePosixPath:
        key = original.as_posix() if isinstance(original, PurePosixPath) else original
        return PurePosixPath(self.by_path[key])

    def rewrite_references(self, text: str) -> str:
        return rewrite_reference_text(
            text,
            self._reference_map,
            self.by_component,
        )
