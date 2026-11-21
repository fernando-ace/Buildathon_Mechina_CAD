from __future__ import annotations

import argparse
from pathlib import Path

from .runner import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="machina-deid", description="Machina CAD De-ID pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Process a stratified sample or the complete manifest")
    run.add_argument(
        "--work-dir", type=Path, default=Path(".state"), help="Private checkpoint directory"
    )
    run.add_argument(
        "--scratch-dir", type=Path, default=Path(".scratch"), help="Bounded local staging directory"
    )
    run.add_argument("--attempt", type=int, default=1, help="Scored attempt number")
    run.add_argument("--dest", help="Private Builders folder ending in attempt-N/")
    run.add_argument(
        "--token", help="Short-lived Builders upload token; defaults to MACHINA_UPLOAD_TOKEN"
    )
    run.add_argument("--token-file", type=Path, help="File to update when the upload token expires")
    run.add_argument(
        "--workers", type=int, default=4, help="Concurrent private-folder uploads (1-8)"
    )
    run.add_argument(
        "--scratch-limit-gb", type=float, default=10, help="Maximum staged local bytes in GiB"
    )
    run.add_argument(
        "--batch-size-gb", type=float, default=1, help="Upload after this much staged output"
    )
    run.add_argument(
        "--max-files", type=int, help="Process a stratified sample; cannot be uploaded"
    )
    run.add_argument(
        "--retry-omitted", action="store_true", help="Retry files previously recorded as omitted"
    )
    run.add_argument("--refresh-manifest", action="store_true", help="Download a fresh manifest")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        code = run_pipeline(
            work_dir=args.work_dir.resolve(),
            scratch_dir=args.scratch_dir.resolve(),
            attempt=args.attempt,
            dest=args.dest,
            token=args.token,
            token_file=args.token_file.resolve() if args.token_file else None,
            scratch_limit_gb=args.scratch_limit_gb,
            batch_size_gb=args.batch_size_gb,
            max_files=args.max_files,
            retry_omitted=args.retry_omitted,
            refresh_manifest=args.refresh_manifest,
            workers=args.workers,
        )
        raise SystemExit(code)


if __name__ == "__main__":
    main()
