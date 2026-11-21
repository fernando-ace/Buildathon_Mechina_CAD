from __future__ import annotations

import hashlib
import shutil
import subprocess
import urllib.parse
from pathlib import Path

INPUT_BASE = "https://machina-input.joinbuilders.org"


def download_manifest(destination: Path, base_url: str = INPUT_BASE) -> Path:
    """Fetch the official manifest with curl, matching the track's own script."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is required to fetch the official input manifest")
    url = base_url.rstrip("/") + "/manifest.tsv"
    completed = subprocess.run(
        [
            curl,
            "-fS",
            "--retry",
            "5",
            "--retry-connrefused",
            "--compressed",
            "-o",
            str(temporary),
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Could not fetch the official manifest: " + completed.stderr[-600:])
    if not temporary.is_file() or temporary.stat().st_size < 32:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("The manifest download was empty or incomplete")
    temporary.replace(destination)
    return destination


def download_entry(
    base_url: str, url_path: str, destination: Path, expected_size: int, expected_md5: str | None
) -> str:
    """Download one manifest object, verify it, then atomically publish the scratch file."""

    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is required to fetch corpus objects")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    url = base_url.rstrip("/") + "/" + urllib.parse.quote(url_path, safe="/")
    temporary.unlink(missing_ok=True)
    completed = subprocess.run(
        [
            curl,
            "-fS",
            "--retry",
            "10",
            "--retry-connrefused",
            "--retry-delay",
            "2",
            "-o",
            str(temporary),
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Input download failed: " + completed.stderr[-400:])
    if not temporary.is_file() or temporary.stat().st_size != expected_size:
        actual = temporary.stat().st_size if temporary.exists() else -1
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Input size check failed ({actual} bytes; expected {expected_size})")
    if expected_md5:
        digest = hashlib.md5()
        with temporary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_md5:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Input MD5 check failed")
    temporary.replace(destination)
    return _sha256(destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
