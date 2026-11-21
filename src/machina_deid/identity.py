from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

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
        self._alias_map = {
            str(normalized): str(pseudonym)
            for normalized, pseudonym in self.db.execute("SELECT normalized,pseudonym FROM aliases")
        }
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
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO aliases(kind, normalized, pseudonym) VALUES (?, ?, ?)",
                (kind, normalized, pseudonym),
            )
            self.db.commit()
            if cursor.rowcount:
                self._alias_map[normalized] = pseudonym
                self._compile_aliases()

    def _compile_aliases(self) -> None:
        values = sorted(
            (value for value in self._alias_map if len(value) >= 3), key=len, reverse=True
        )
        self._alias_values = {value.casefold(): self._alias_map[value] for value in values}
        self._alias_pattern = (
            re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(v) for v in values) + r")(?!\w)", re.IGNORECASE
            )
            if values
            else None
        )

    def replace_known_aliases(self, text: str) -> str:
        if self._alias_pattern is None:
            return text
        return self._alias_pattern.sub(
            lambda match: self._alias_values.get(match.group(0).casefold(), match.group(0)), text
        )

    def known_aliases(self) -> list[tuple[str, str]]:
        return [
            (str(row[0]), str(row[1]))
            for row in self.db.execute(
                "SELECT normalized, pseudonym FROM aliases ORDER BY length(normalized) DESC"
            )
        ]

    def close(self) -> None:
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
