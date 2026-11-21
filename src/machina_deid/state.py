from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from pathlib import Path


class RunState:
    """Private, durable checkpoint state. Original names never leave this database."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=120)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=120000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS files (attempt INTEGER, source_path TEXT, output_path TEXT, "
            "status TEXT, input_sha256 TEXT, output_sha256 TEXT, notes TEXT DEFAULT '', "
            "PRIMARY KEY(attempt, source_path))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS issues (attempt INTEGER, output_path TEXT, code TEXT, "
            "detail TEXT, PRIMARY KEY(attempt, output_path, code))"
        )
        self.db.commit()

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def status(self, attempt: int, source_path: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM files WHERE attempt=? AND source_path=?", (attempt, source_path)
        ).fetchone()
        return str(row[0]) if row else None

    def record(
        self,
        attempt: int,
        source_path: str,
        output_path: str,
        status: str,
        input_sha256: str | None = None,
        output_sha256: str | None = None,
        notes: Iterable[str] = (),
    ) -> None:
        self.db.execute(
            "INSERT INTO files(attempt,source_path,output_path,status,input_sha256,output_sha256,notes) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(attempt,source_path) DO UPDATE SET "
            "output_path=excluded.output_path,status=excluded.status,input_sha256=excluded.input_sha256,"
            "output_sha256=excluded.output_sha256,notes=excluded.notes",
            (
                attempt,
                source_path,
                output_path,
                status,
                input_sha256,
                output_sha256,
                "\n".join(notes),
            ),
        )
        self.db.commit()

    def record_issue(self, attempt: int, output_path: str, code: str, detail: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO issues(attempt,output_path,code,detail) VALUES(?,?,?,?)",
            (attempt, output_path, code, detail),
        )
        self.db.commit()

    def staged(self, attempt: int) -> list[tuple[str, str, str | None, str | None, str]]:
        rows = self.db.execute(
            "SELECT source_path,output_path,input_sha256,output_sha256,notes FROM files "
            "WHERE attempt=? AND status='staged' ORDER BY source_path",
            (attempt,),
        )
        return [(str(a), str(b), c, d, str(e or "")) for a, b, c, d, e in rows]

    def mark_uploaded(self, attempt: int, source_paths: Iterable[str]) -> None:
        self.db.executemany(
            "UPDATE files SET status='uploaded' WHERE attempt=? AND source_path=?",
            [(attempt, path) for path in source_paths],
        )
        self.db.commit()

    def issue_rows(self, attempt: int) -> list[tuple[str, str, str]]:
        return [
            (str(path), str(code), str(detail))
            for path, code, detail in self.db.execute(
                "SELECT output_path,code,detail FROM issues WHERE attempt=? ORDER BY output_path,code",
                (attempt,),
            )
        ]

    def counts(self, attempt: int) -> dict[str, int]:
        return {
            str(status): int(count)
            for status, count in self.db.execute(
                "SELECT status,COUNT(*) FROM files WHERE attempt=? GROUP BY status", (attempt,)
            )
        }

    def close(self) -> None:
        self.db.commit()
        self.db.close()
