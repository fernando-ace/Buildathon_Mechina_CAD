from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One row from the official corpus manifest."""

    path: PurePosixPath
    size: int
    md5: str | None
    url_path: str
    line_number: int


@dataclass(slots=True)
class FileResult:
    status: str
    notes: list[str] = field(default_factory=list)
