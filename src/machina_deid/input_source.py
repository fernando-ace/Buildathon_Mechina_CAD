from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

INPUT_BASE = "https://machina-input.joinbuilders.org"


class InputNotFound(RuntimeError):
    """The manifest lists a file that the complete input mirror cannot serve."""


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
    # The official manifest's URL-path column is already percent-encoded.
    # Keep it byte-for-byte, as get-input.sh does; quoting it again breaks
    # paths containing escapes and returns 404 for valid mirror objects.
    url = base_url.rstrip("/") + "/" + url_path
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
            "-w",
            "\n%{http_code}",
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
        status = completed.stdout.strip().splitlines()[-1:] or [""]
        if status[0] == "404":
            raise InputNotFound("The official input mirror returned HTTP 404.")
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
