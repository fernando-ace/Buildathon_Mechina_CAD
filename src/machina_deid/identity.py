from __future__ import annotations

import base64
import hashlib
import heapq
import hmac
import os
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from itertools import groupby
from pathlib import Path

try:
    import ahocorasick
except ImportError:  # pragma: no cover - exercised only by dependency-light installs.
    ahocorasick = None

_ALIAS_BATCH_SIZE = 512

_LABELS = {
    "person": "Person",
    "company": "Company",
    "account": "Account",
    "domain": "Domain",
    "contact": "Contact",
    "address": "Address",
    "project": "Project",
    "part": "Part",
    "file": "File",
    "archive": "Archive",
}


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class IdentityLedger:
    """Stable type-scoped pseudonyms backed by a private SQLite ledger."""

    def __init__(self, db: sqlite3.Connection, key_path: Path) -> None:
        self.db = db
        self.key_path = key_path
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            self.key = key_path.read_bytes()
        else:
            self.key = os.urandom(32)
            key_path.write_bytes(self.key)
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
        if len(self.key) != 32:
            raise ValueError("Private pseudonym key must contain exactly 32 bytes")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS aliases (kind TEXT, normalized TEXT, pseudonym TEXT, "
            "PRIMARY KEY(kind, normalized))"
        )
        self.db.commit()
        self._alias_map: dict[str, str] = {}
        self._stored_aliases: set[tuple[str, str]] = set()
        for kind, normalized, pseudonym in self.db.execute(
            "SELECT kind,normalized,pseudonym FROM aliases"
        ):
            kind, normalized, pseudonym = str(kind), str(normalized), str(pseudonym)
            self._stored_aliases.add((kind, normalized))
            self._alias_map[normalized] = pseudonym
        self._pending_db_aliases: dict[tuple[str, str], str] = {}
        self._pending_aliases: dict[str, str] = {}
        self._pending_automaton = None
        self._alias_automaton = None
        self._alias_pattern: re.Pattern[str] | None = None
        self._full_pattern_dirty = True
        self._max_alias_length = 0
        self._compile_aliases()

    def pseudonym(self, kind: str, value: str) -> str:
        label = _LABELS.get(kind)
        normalized = normalize(value)
        if not label or not normalized:
            raise ValueError("A supported identity type and non-empty value are required")
        digest = hmac.new(self.key, f"{kind}\0{normalized}".encode(), hashlib.sha256).digest()
        token = base64.b32encode(digest[:10]).decode("ascii").rstrip("=")
        return f"{label}-{token}"

    def pseudonym_exact(self, kind: str, value: str) -> str:
        """Case-sensitive variant for filesystem paths whose case distinguishes files."""

        label = _LABELS.get(kind)
        normalized = " ".join(unicodedata.normalize("NFKC", value).split())
        if not label or not normalized:
            raise ValueError("A supported identity type and non-empty value are required")
        digest = hmac.new(self.key, f"{kind}\0{normalized}".encode(), hashlib.sha256).digest()
        token = base64.b32encode(digest[:10]).decode("ascii").rstrip("=")
        return f"{label}-{token}"

    def add_alias(self, kind: str, value: str, pseudonym: str) -> None:
        normalized = normalize(value)
        if normalized:
            alias_key = (kind, normalized)
            if alias_key not in self._stored_aliases:
                self._pending_db_aliases[alias_key] = pseudonym
            if alias_key not in self._stored_aliases:
                is_new_value = normalized not in self._alias_map
                self._alias_map[normalized] = pseudonym
                self._full_pattern_dirty = True
                if ahocorasick is None or not is_new_value:
                    return
                self._pending_aliases[normalized] = pseudonym
                self._max_alias_length = max(self._max_alias_length, len(normalized))
                self._pending_automaton = None
                if len(self._pending_aliases) >= _ALIAS_BATCH_SIZE:
                    self._compile_aliases()

    def flush_aliases(self) -> None:
        """Persist aliases in one short transaction after processing a source file."""
        if not self._pending_db_aliases:
            return
        rows = [
            (kind, normalized, pseudonym)
            for (kind, normalized), pseudonym in self._pending_db_aliases.items()
        ]
        self.db.executemany(
            "INSERT OR IGNORE INTO aliases(kind, normalized, pseudonym) VALUES (?, ?, ?)", rows
        )
        self.db.commit()
        self._stored_aliases.update(
            (kind, normalized) for kind, normalized, _ in rows
        )
        self._pending_db_aliases.clear()

    def _compile_aliases(self) -> None:
        if ahocorasick is None:
            self._alias_automaton = None
            self._pending_aliases.clear()
            self._pending_automaton = None
            return
        automaton = ahocorasick.Automaton()
        added = False
        for value, pseudonym in self._alias_map.items():
            if len(value) >= 3:
                automaton.add_word(value, (value, pseudonym))
                added = True
        self._max_alias_length = max(
            (len(value) for value in self._alias_map if len(value) >= 3), default=0
        )
        if added:
            automaton.make_automaton()
            self._alias_automaton = automaton
        else:
            self._alias_automaton = None
        self._pending_aliases.clear()
        self._pending_automaton = None
        self._alias_pattern = None
        self._full_pattern_dirty = True

    def _compile_pattern(self, aliases: Mapping[str, str]) -> re.Pattern[str] | None:
        values = sorted((value for value in aliases if len(value) >= 3), key=len, reverse=True)
        return (
            re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(value) for value in values) + r")(?!\w)",
                re.IGNORECASE,
            )
            if values
            else None
        )

    def _full_alias_pattern(self) -> re.Pattern[str] | None:
        if self._full_pattern_dirty:
            self._alias_pattern = self._compile_pattern(self._alias_map)
            self._full_pattern_dirty = False
        return self._alias_pattern

    def _pending_alias_automaton(self):
        if self._pending_automaton is None and self._pending_aliases and ahocorasick is not None:
            automaton = ahocorasick.Automaton()
            added = False
            for value, pseudonym in self._pending_aliases.items():
                if len(value) >= 3:
                    automaton.add_word(value, (value, pseudonym))
                    added = True
            if added:
                automaton.make_automaton()
                self._pending_automaton = automaton
        return self._pending_automaton

    def replace_known_aliases(self, text: str) -> str:
        if not text or not self._alias_map:
            return text
        if self._alias_automaton is None:
            pattern = self._full_alias_pattern()
            return (
                pattern.sub(
                    lambda match: self._alias_map.get(match.group(0).casefold(), match.group(0)),
                    text,
                )
                if pattern
                else text
            )

        folded = text.casefold()
        if len(folded) != len(text):
            # Full case folding can expand a small set of Unicode characters. Keep the
            # existing regex semantics for those rare strings so match offsets stay exact.
            pattern = self._full_alias_pattern()
            return (
                pattern.sub(
                    lambda match: self._alias_map.get(match.group(0).casefold(), match.group(0)),
                    text,
                )
                if pattern
                else text
            )

        # Python's regex engine slows down as the alias alternation grows. The Aho-Corasick
        # automaton scans large CAD XML and text payloads in one pass. Newly discovered aliases
        # are kept in a small regex until the next bounded automaton rebuild.
        if len(self._pending_aliases) >= _ALIAS_BATCH_SIZE:
            self._compile_aliases()
        best_by_start: dict[int, tuple[int, str]] = {}
        starts: list[int] = []
        output: list[str] = []
        cursor = 0

        def add_match(start: int, end: int, pseudonym: str) -> None:
            before = text[start - 1] if start else ""
            after = text[end] if end < len(text) else ""
            if (before and (before.isalnum() or before == "_")) or (
                after and (after.isalnum() or after == "_")
            ):
                return
            prior = best_by_start.get(start)
            if prior is None:
                best_by_start[start] = (end, pseudonym)
                heapq.heappush(starts, start)
            elif end > prior[0]:
                best_by_start[start] = (end, pseudonym)

        def flush_through(last_start: int | None) -> None:
            nonlocal cursor
            while starts and (last_start is None or starts[0] <= last_start):
                start = heapq.heappop(starts)
                end, pseudonym = best_by_start.pop(start)
                if start < cursor:
                    continue
                output.extend((text[cursor:start], pseudonym))
                cursor = end

        iterators = []
        if self._alias_automaton is not None:
            iterators.append(self._alias_automaton.iter(folded))
        pending = self._pending_alias_automaton()
        if pending is not None:
            iterators.append(pending.iter(folded))
        if not iterators:
            return text

        events = heapq.merge(*iterators)
        for end_index, same_end in groupby(events, key=lambda item: item[0]):
            for _, (alias, fallback) in same_end:
                start = end_index - len(alias) + 1
                add_match(start, end_index + 1, self._alias_map.get(alias, fallback))
            flush_through(end_index - self._max_alias_length + 1)
        flush_through(None)
        if not output:
            return text
        output.append(text[cursor:])
        return "".join(output)

    def known_aliases(self) -> list[tuple[str, str]]:
        return [
            (str(row[0]), str(row[1]))
            for row in self.db.execute(
                "SELECT normalized, pseudonym FROM aliases ORDER BY length(normalized) DESC"
            )
        ]

    def close(self) -> None:
        self.flush_aliases()
        self.db.commit()


class TextRedactor:
    """Conservative text recognizer for contact details and labeled identity fields."""

    EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,128}@(?:[\w-]+\.)+[A-Za-z]{2,63}(?![\w.-])")
    URL = re.compile(r"\b(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
    DOMAIN = re.compile(r"(?<![@\w.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}(?![\w.-])")
    PHONE = re.compile(
        r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?!\w)"
    )
    LABELED = re.compile(
        r"(?im)(?P<label>\b(?:author|creator|created\s+by|drawn\s+by|designer|engineer|checked\s+by|"
        r"approved\s+by|owner|contact|person|customer|supplier|vendor|company|organization|"
        r"organisation|project(?:\s+name)?|part(?:\s+(?:name|number|no\.?))?|account|"
        r"email|phone|address|location|domain|website)\b\s*[:=]\s*)"
        r"(?P<value>[^\r\n;<>]{2,160})"
    )
    PERSON_LABELS = frozenset(
        {
            "author",
            "creator",
            "created by",
            "drawn by",
            "designer",
            "engineer",
            "checked by",
            "approved by",
            "owner",
            "person",
            "contact",
        }
    )
    COMPANY_LABELS = frozenset(
        {"customer", "supplier", "vendor", "company", "organization", "organisation"}
    )
    PROJECT_LABELS = frozenset({"project", "project name"})
    PART_LABELS = frozenset({"part", "part name", "part number", "part no."})
    ADDRESS_LABELS = frozenset({"address", "location"})
    ACCOUNT_LABELS = frozenset({"account"})

    def __init__(self, ledger: IdentityLedger) -> None:
        self.ledger = ledger

    def _replace_labeled(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            label = re.sub(r"\s+", " ", match.group("label").strip().lower())
            value = match.group("value").strip().strip("\"'")
            if value.casefold() in {"unknown", "none", "n/a", "not applicable", "-"}:
                return match.group(0)
            if re.match(
                r"^(?:Person|Company|Account|Domain|Contact|Address|Project|Part|File|Archive)-[A-Z2-7]{16}$",
                value,
                re.IGNORECASE,
            ):
                return match.group(0)
            if label in self.PERSON_LABELS:
                kind = "person"
            elif label in self.COMPANY_LABELS:
                kind = "company"
            elif label in self.PROJECT_LABELS:
                kind = "project"
            elif label in self.PART_LABELS:
                kind = "part"
            elif label in self.ADDRESS_LABELS:
                kind = "address"
            elif label in self.ACCOUNT_LABELS or label == "email":
                kind = "account"
            elif label == "phone":
                kind = "contact"
            elif label == "domain":
                kind = "domain"
            else:
                kind = "contact"
            if label == "email":
                replacement = self._email(value)
                self.ledger.add_alias("account", value, replacement)
                return match.group("label") + replacement
            pseudo = self.ledger.pseudonym(kind, value)
            self.ledger.add_alias(kind, value, pseudo)
            return match.group("label") + pseudo

        return self.LABELED.sub(replace, text)

    def _email(self, value: str) -> str:
        if value.lower().endswith(".invalid") and "@domain-" in value.lower():
            return value
        _, separator, domain = value.rpartition("@")
        if not separator:
            return self.ledger.pseudonym("account", value)
        return (
            f"{self.ledger.pseudonym('account', value).lower()}@"
            f"{self.ledger.pseudonym('domain', domain).lower()}.invalid"
        )

    def redact(self, text: str) -> str:
        text = self._replace_labeled(text)
        text = self.ledger.replace_known_aliases(text)

        def email(match: re.Match[str]) -> str:
            return self._email(match.group(0))

        text = self.EMAIL.sub(email, text)

        def url(match: re.Match[str]) -> str:
            value = match.group(0).rstrip(").,;]")
            suffix = match.group(0)[len(value) :]
            host = re.sub(r"^https?://|^www\.", "", value, flags=re.IGNORECASE).split("/", 1)[0]
            if host.lower().endswith(".invalid"):
                return match.group(0)
            return f"https://{self.ledger.pseudonym('domain', host).lower()}.invalid/{self.ledger.pseudonym('project', value).lower()}{suffix}"

        text = self.URL.sub(url, text)
        common_tlds = {
            "com",
            "org",
            "net",
            "edu",
            "gov",
            "mil",
            "io",
            "co",
            "us",
            "uk",
            "de",
            "ca",
            "info",
            "biz",
            "app",
            "dev",
            "ai",
        }

        def domain(match: re.Match[str]) -> str:
            value = match.group(0)
            tld = value.rsplit(".", 1)[-1].lower()
            if tld not in common_tlds or value.lower().endswith(".invalid"):
                return value
            return f"{self.ledger.pseudonym('domain', value).lower()}.invalid"

        text = self.DOMAIN.sub(domain, text)
        text = self.PHONE.sub(
            lambda m: self.ledger.pseudonym("contact", re.sub(r"\D", "", m.group(0))), text
        )
        return text
