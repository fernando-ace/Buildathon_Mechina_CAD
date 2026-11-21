from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from .formats import HandlerContext, process_file
from .identity import IdentityLedger, TextRedactor
from .input_source import INPUT_BASE, InputNotFound, download_entry, download_manifest
from .manifest import iter_manifest
from .models import ManifestEntry
from .paths import PathMapper
from .state import RunState
from .uploader import BuildersUploader, TokenSource


def stratified_sample(entries: list[ManifestEntry], limit: int) -> list[ManifestEntry]:
    """Select a reproducible sample across the most common file extensions."""

    if limit <= 0 or limit >= len(entries):
        return entries
    groups: dict[str, list[ManifestEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.path.suffix.lower() or "[none]"].append(entry)
    counts = Counter({extension: len(items) for extension, items in groups.items()})
    extension_order = [extension for extension, _ in counts.most_common(min(40, limit))]
    selected: list[ManifestEntry] = []
    cursors = {extension: 0 for extension in extension_order}
    while len(selected) < limit:
        made_progress = False
        for extension in extension_order:
            index = cursors[extension]
            if index < len(groups[extension]) and len(selected) < limit:
                selected.append(groups[extension][index])
                cursors[extension] += 1
                made_progress = True
        if not made_progress:
            break
    order = {entry.path.as_posix(): index for index, entry in enumerate(entries)}
    return sorted(selected, key=lambda entry: order[entry.path.as_posix()])


def _manifest_entries(work_dir: Path, refresh: bool) -> list[ManifestEntry]:
    path = work_dir / "manifest.tsv"
    if refresh or not path.exists():
        download_manifest(path)
    return list(iter_manifest(str(path)))


def _tree_bytes(path: Path) -> int:
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _unlink_output(path: Path) -> None:
    path.unlink(missing_ok=True)
    parent = path.parent
    while parent.name and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _write_readme(
    path: Path, attempt: int, counts: dict[str, int], issues: list[tuple[str, str, str]]
) -> None:
    lines = [
        "Machina CAD De-ID submission",
        "",
        f"Attempt: {attempt}",
        "Pipeline: machina-cad-deid 0.1.0",
        "",
        "Run instructions (Ubuntu / WSL2, Python 3.11+):",
        "  python3 -m venv .venv",
        "  source .venv/bin/activate",
        "  python -m pip install -r requirements.lock",
        "  python -m pip install --no-deps -e .",
        "  machina-deid run --work-dir .state --scratch-dir .scratch --attempt N \\",
        "    --scratch-limit-gb 10 --dest gs://builders-machina-submissions-1120/teams/TEAM_ID/attempt-N/",
        "Get the upload token from the official track page and set MACHINA_UPLOAD_TOKEN.",
        "The work directory contains the private pseudonym key and checkpoint database; do not submit it.",
        "",
        "Processing counts:",
    ]
    lines.extend(f"  {status}: {count}" for status, count in sorted(counts.items()))
    lines.extend(
        [
            "",
            "Known limitations and exceptions (final paths only):",
        ]
    )
    if issues:
        lines.extend(f"  {output_path} [{code}]: {detail}" for output_path, code, detail in issues)
    else:
        lines.append("  None recorded by the pipeline.")
    lines.extend(
        [
            "",
            "Format notes:",
            "  Text-based CAD fields, contact details, names in labeled fields, metadata, file paths,",
            "  ZIP/TAR members, PDF text and metadata, and OCR-readable raster text are processed.",
            "  OCR requires Tesseract. Unsupported proprietary CAD binaries are preserved intact and",
            "  reported; their internal identifying properties may remain. Review these exceptions.",
            "  Geometry values and non-identifying engineering data are not intentionally rewritten.",
            "",
            "No credentials, raw input, private identity mappings, or temporary files are included.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _source_files(root: Path) -> list[tuple[Path, str]]:
    selected: list[tuple[Path, str]] = []
    for relative in ("pyproject.toml", "requirements.lock", "README.md"):
        path = root / relative
        if path.is_file():
            selected.append((path, f"code/{relative}"))
    for folder in ("src", "tests"):
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and ".pyc" not in path.suffix and "__pycache__" not in path.parts:
                selected.append((path, "code/" + path.relative_to(root).as_posix()))
    return selected


def run_pipeline(
    *,
    work_dir: Path,
    scratch_dir: Path,
    attempt: int,
    dest: str | None,
    token: str | None,
    token_file: Path | None,
    scratch_limit_gb: float,
    batch_size_gb: float,
    max_files: int | None,
    retry_omitted: bool,
    refresh_manifest: bool,
    workers: int,
    shard_index: int = 0,
    shard_count: int = 1,
) -> int:
    if attempt < 1:
        raise ValueError("Attempt numbers start at 1")
    if scratch_limit_gb <= 0 or batch_size_gb <= 0 or batch_size_gb > scratch_limit_gb:
        raise ValueError("Require 0 < batch size <= scratch limit")
    if max_files is not None and max_files < 1:
        raise ValueError("--max-files must be positive")
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("Require 0 <= --shard-index < --shard-count and --shard-count >= 1")
    if max_files is not None and dest:
        raise ValueError("A bounded sample is not a complete scored corpus; do not upload it")
    if max_files is None and not dest:
        raise ValueError(
            "A full run needs the private Builders destination to keep local staging bounded"
        )
    uploader = None
    if dest:
        if not dest.rstrip("/").endswith(f"attempt-{attempt}"):
            raise ValueError(f"Destination must end with attempt-{attempt}/")
        uploader = BuildersUploader(dest, TokenSource(token, token_file), workers=workers)

    work_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = scratch_dir / ("sample-corpus" if max_files is not None else "corpus")
    raw_dir = scratch_dir / "raw"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    entries = _manifest_entries(work_dir, refresh_manifest)
    sample = stratified_sample(entries, max_files or 0)
    if shard_count > 1:
        sample = sample[shard_index::shard_count]
    print(
        f"Manifest: {len(entries):,} files; selected: {len(sample):,} "
        f"(shard {shard_index + 1}/{shard_count}).",
        flush=True,
    )

    state = RunState(work_dir / "run.sqlite3")
    identity_db = sqlite3.connect(work_dir / "run.sqlite3", timeout=120)
    identity_db.execute("PRAGMA busy_timeout=120000")
    ledger = IdentityLedger(identity_db, work_dir / "pseudonym.key")
    path_map = PathMapper(ledger, entries)
    redactor = TextRedactor(ledger)
    context = HandlerContext(ledger, redactor, path_map, scratch_dir)
    scratch_limit = int(scratch_limit_gb * 1024**3)
    batch_limit = int(batch_size_gb * 1024**3)
    scratch_bytes = _tree_bytes(corpus_dir) + _tree_bytes(raw_dir)
    completed = 0
    failed = 0

    def flush_batch() -> None:
        nonlocal scratch_bytes
        staged = state.staged(attempt)
        upload_items: list[tuple[Path, str]] = []
        source_paths: list[str] = []
        for source_path, output_path, _, _, _ in staged:
            local = corpus_dir.joinpath(*PurePosixPath(output_path).parts)
            if local.is_file():
                upload_items.append((local, "corpus/" + output_path))
                source_paths.append(source_path)
        if not upload_items:
            return
        if uploader is None:
            return
        byte_count = uploader.upload_many(upload_items)
        state.mark_uploaded(attempt, source_paths)
        for local, _ in upload_items:
            try:
                scratch_bytes -= local.stat().st_size
            except OSError:
                pass
            _unlink_output(local)
        print(f"Uploaded {len(upload_items):,} files ({byte_count / 1e9:.2f} GB).", flush=True)

    try:
        for entry in sample:
            source_path = entry.path.as_posix()
            output_path = path_map.output_path(entry.path).as_posix()
            current_status = state.status(attempt, source_path)
            if current_status == "uploaded" or (current_status == "omitted" and not retry_omitted):
                continue
            local_output = corpus_dir.joinpath(*PurePosixPath(output_path).parts)
            if current_status == "staged" and local_output.is_file():
                if uploader and scratch_bytes >= batch_limit:
                    flush_batch()
                continue

            estimated_working = entry.size * 2 + 2 * 1024 * 1024
            if scratch_bytes and scratch_bytes + estimated_working > scratch_limit:
                if uploader:
                    flush_batch()
                if scratch_bytes + estimated_working > scratch_limit:
                    state.record_issue(
                        attempt,
                        output_path,
                        "scratch_limit",
                        "File exceeds the configured local scratch budget.",
                    )
                    state.record(
                        attempt, source_path, output_path, "omitted", notes=["scratch_limit"]
                    )
                    failed += 1
                    continue
            if estimated_working > scratch_limit:
                state.record_issue(
                    attempt,
                    output_path,
                    "scratch_limit",
                    "File exceeds the configured local scratch budget.",
                )
                state.record(attempt, source_path, output_path, "omitted", notes=["scratch_limit"])
                failed += 1
                continue

            temp_name = hashlib.sha256(source_path.encode("utf-8")).hexdigest() + ".input"
            local_input = raw_dir / temp_name
            prior_input_size = local_input.stat().st_size if local_input.is_file() else 0
            prior_output_size = local_output.stat().st_size if local_output.is_file() else 0
            try:
                input_digest = download_entry(
                    INPUT_BASE, entry.url_path, local_input, entry.size, entry.md5
                )
            except InputNotFound:
                detail = "The complete official mirror returned HTTP 404 for this manifest-listed file."
                state.record_issue(attempt, output_path, "source_missing", detail)
                state.record(attempt, source_path, output_path, "omitted", notes=["source_missing"])
                failed += 1
                continue
            except (OSError, RuntimeError, ValueError) as exc:
                state.record_issue(attempt, output_path, "input_unavailable", type(exc).__name__)
                state.record(
                    attempt, source_path, output_path, "failed", notes=["input_unavailable"]
                )
                failed += 1
                continue
            scratch_bytes += entry.size - prior_input_size
            result = process_file(local_input, local_output, context, logical_name=entry.path.name)
            local_input.unlink(missing_ok=True)
            scratch_bytes -= entry.size
            if result.status == "omitted":
                if local_output.is_file():
                    scratch_bytes -= local_output.stat().st_size
                    local_output.unlink(missing_ok=True)
                state.record(
                    attempt,
                    source_path,
                    output_path,
                    "omitted",
                    input_sha256=input_digest,
                    notes=result.notes,
                )
                for code in result.notes:
                    state.record_issue(
                        attempt, output_path, code, "The file was omitted by a format-safety check."
                    )
                failed += 1
                continue
            output_size = local_output.stat().st_size
            scratch_bytes += output_size - prior_output_size
            if scratch_bytes > scratch_limit:
                local_output.unlink(missing_ok=True)
                scratch_bytes -= output_size
                state.record(
                    attempt,
                    source_path,
                    output_path,
                    "omitted",
                    input_sha256=input_digest,
                    notes=["scratch_limit"],
                )
                state.record_issue(
                    attempt,
                    output_path,
                    "scratch_limit",
                    "Transformed output exceeded the configured local scratch budget.",
                )
                failed += 1
                continue
            output_digest = RunState.sha256(local_output)
            status = "staged" if uploader else "complete_local"
            state.record(
                attempt,
                source_path,
                output_path,
                status,
                input_digest,
                output_digest,
                result.notes,
            )
            for code in result.notes:
                state.record_issue(
                    attempt, output_path, code, "See the format notes in README.txt."
                )
            completed += 1
            if uploader and scratch_bytes >= batch_limit:
                flush_batch()
            if completed % 50 == 0:
                print(
                    f"Processed {completed:,} files; {failed:,} omitted or unavailable.", flush=True
                )

        if uploader:
            flush_batch()
            status_counts = state.counts(attempt)
            pending = status_counts.get("failed", 0) + status_counts.get("staged", 0)
            if pending and shard_count == 1:
                print(
                    f"Attempt is incomplete: {pending:,} files failed or remain staged; code was not packaged.",
                    file=sys.stderr,
                )
                return 2
            if shard_count == 1:
                issues = state.issue_rows(attempt)
                readme = work_dir / f"README-attempt-{attempt}.txt"
                _write_readme(readme, attempt, status_counts, issues)
                root = Path(__file__).resolve().parents[2]
                source_items = _source_files(root)
                source_items.append((readme, "README.txt"))
                byte_count = uploader.upload_many(source_items)
                print(f"Uploaded source and README ({byte_count / 1e6:.1f} MB).", flush=True)
                print(f"Complete attempt folder: {dest}")
                print("Review the folder, then submit it on the official track page.")
            else:
                print(
                    f"Shard {shard_index + 1}/{shard_count} finished; run a single-shard "
                    "final pass after all workers exit to validate completeness and package code."
                )
        elif max_files is None:
            print(
                "Local output has no remote destination; stop and use a bounded sample or configure upload."
            )
        counts = state.counts(attempt)
        print(f"Attempt {attempt} counts: {counts}; issues: {len(state.issue_rows(attempt))}.")
        return 0 if failed == 0 else 1
    finally:
        ledger.close()
        identity_db.close()
        state.close()
