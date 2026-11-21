from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import PurePosixPath

from .models import ManifestEntry


class ManifestError(ValueError):
    pass


def iter_manifest(path: str) -> Iterator[ManifestEntry]:
    """Read and validate the tab-separated challenge manifest."""

    with open(path, "r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, delimiter="\t")
        header_seen = False
        for line_number, row in enumerate(reader, start=1):
            if not row or row[0].startswith("#"):
                header_seen = True
                continue
            if len(row) != 4:
                raise ManifestError(
                    f"Manifest line {line_number} has {len(row)} columns, expected 4"
                )
            if not header_seen:
                raise ManifestError("Manifest header is missing")
            raw_path, raw_size, raw_md5, url_path = row
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ManifestError(f"Unsafe manifest path at line {line_number}")
            if "\\" in raw_path or "\x00" in raw_path:
                raise ManifestError(f"Invalid path encoding at line {line_number}")
            try:
                size = int(raw_size)
            except ValueError as exc:
                raise ManifestError(f"Invalid byte count at line {line_number}") from exc
            if size < 0:
                raise ManifestError(f"Negative byte count at line {line_number}")
            if raw_md5 and (
                len(raw_md5) != 32 or any(c not in "0123456789abcdefABCDEF" for c in raw_md5)
            ):
                raise ManifestError(f"Invalid MD5 at line {line_number}")
            if not url_path or url_path.startswith("/") or ".." in PurePosixPath(url_path).parts:
                raise ManifestError(f"Invalid URL path at line {line_number}")
            yield ManifestEntry(path, size, raw_md5.lower() or None, url_path, line_number)
