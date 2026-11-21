from __future__ import annotations

import getpass
import http.client
import os
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


class UploadError(RuntimeError):
    pass


class TokenSource:
    def __init__(self, token: str | None, token_file: Path | None) -> None:
        self.token = (token or os.environ.get("MACHINA_UPLOAD_TOKEN", "")).strip()
        self.token_file = token_file
        self.lock = threading.Lock()
        if not self.token and token_file and token_file.is_file():
            self.token = token_file.read_text(encoding="utf-8").strip()
        if not self.token:
            raise UploadError(
                "Get an upload token from the track page and pass --token or --token-file"
            )

    def current(self) -> str:
        with self.lock:
            return self.token

    def renew(self, stale: str) -> str:
        with self.lock:
            if self.token != stale:
                return self.token
            print(
                "The Builders upload token expired. Generate a new token on the track page.",
                flush=True,
            )
            if self.token_file:
                print(
                    f"Write the new token to {self.token_file}; waiting up to two hours.",
                    flush=True,
                )
                deadline = time.monotonic() + 2 * 60 * 60
                while time.monotonic() < deadline:
                    try:
                        fresh = self.token_file.read_text(encoding="utf-8").strip()
                    except OSError:
                        fresh = ""
                    if fresh and fresh != stale:
                        self.token = fresh
                        return fresh
                    time.sleep(3)
            elif os.isatty(0):
                fresh = getpass.getpass("Paste the replacement token: ").strip()
                if fresh and fresh != stale:
                    self.token = fresh
                    return fresh
            raise UploadError("Token expired; rerun the command with a fresh upload token")


class BuildersUploader:
    """Small dependency-free uploader for a team's private Builders GCS folder."""

    HOST = "storage.googleapis.com"

    def __init__(self, destination: str, tokens: TokenSource, workers: int = 4) -> None:
        if not destination.startswith("gs://"):
            raise ValueError("Destination must start with gs://")
        bucket, sep, prefix = destination[5:].partition("/")
        if not sep or not bucket:
            raise ValueError("Destination must include a bucket and attempt folder")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/"
        self.tokens = tokens
        self.workers = max(1, min(workers, 8))

    def upload(self, local_path: Path, object_path: str) -> int:
        key = f"{self.prefix}{object_path.lstrip('/')}"
        encoded = "/".join(
            urllib.parse.quote(part, safe="") for part in f"{self.bucket}/{key}".split("/")
        )
        request_path = "/" + encoded
        for attempt in range(5):
            token = self.tokens.current()
            conn = http.client.HTTPSConnection(self.HOST, timeout=180)
            try:
                conn.putrequest("PUT", request_path)
                conn.putheader("Authorization", f"Bearer {token}")
                conn.putheader("Content-Length", str(local_path.stat().st_size))
                conn.putheader("Content-Type", "application/octet-stream")
                conn.endheaders()
                with local_path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        conn.send(chunk)
                response = conn.getresponse()
                body = response.read()
                if 200 <= response.status < 300:
                    return local_path.stat().st_size
                if response.status == 401 or (
                    response.status in (400, 403) and b"xpired" in body.lower()
                ):
                    self.tokens.renew(token)
                    continue
                if response.status >= 500 or response.status == 429:
                    time.sleep(min(8, 0.5 * (2**attempt)))
                    continue
                raise UploadError(
                    f"Builders upload failed ({response.status}): "
                    + body.decode("utf-8", "replace")[:300]
                )
            except (OSError, http.client.HTTPException) as exc:
                if attempt == 4:
                    raise UploadError(f"Builders upload failed after retries: {exc}") from exc
                time.sleep(min(8, 0.5 * (2**attempt)))
            finally:
                conn.close()
        raise UploadError("Builders upload failed after retries")

    def upload_many(self, files: list[tuple[Path, str]]) -> int:
        if not files:
            return 0
        total = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self.upload, local, rel) for local, rel in files]
            for future in as_completed(futures):
                total += future.result()
        return total
