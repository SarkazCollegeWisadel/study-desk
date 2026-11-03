"""Read-only legacy unit-log import, kept separate from confirmed learning.

The legacy API has date/head/body but no actor or event timestamp. It must never
inflate confirmed learning totals. Content-addressed local notes retain facts
without inventing clocks, durations, provenance, or mastery.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .core import InvalidEvent, Ledger, atomic_write, canonical, now_utc, valid_day


DEFAULT_SOURCE = "http://127.0.0.1:8765/api/unit/log?n=1000"
MAX_RESPONSE = 8 * 1024 * 1024
# Require explicit provenance/generation markers; medical test questions or an
# incidental mention of AI inside a study note must not trigger this filter.
EXCLUDED_MARKER = re.compile(
    r"(?im)^(?:actor|执行者|记录者|来源类型)\s*[:：]\s*(?:agent|assistant|codex|dsh|system)(?:\s|$)"
    r"|^(?:source_kind|origin|event_type|action)\s*[:：]\s*(?:agent|generated|retrieval_test|audio_generated|draft_generated|synthetic)(?:\s|$)"
    r"|^\[(?:agent-generated|synthetic|agent-test)\]"
    r"|^(?:自动生成记录|Agent测试记录|音频制作记录|稿件生成记录)\s*[:：]"
)
UNKNOWN_HEADS = {"", "未知", "未命名", "学习日志", "学习记录", "单元记录"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_source(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.fragment
            or parsed.path != "/api/unit/log"):
        raise InvalidEvent("sync source must be the loopback HTTP /api/unit/log read endpoint without credentials")
    _ = parsed.port  # Force malformed port validation.
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"n"} or ("n" in query and len(query["n"]) != 1):
        raise InvalidEvent("sync source only accepts one n query parameter")
    limit_text = query.get("n", ["5"])[0]
    if not re.fullmatch(r"\d+", limit_text) or not 1 <= int(limit_text) <= 10000:
        raise InvalidEvent("sync n must be an integer from 1 to 10000")
    return int(limit_text)


def fetch_records(url=DEFAULT_SOURCE):
    limit = validate_source(url)
    # Only literal loopback, no auth, no proxy and no redirects; GET only.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with opener.open(request, timeout=5) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise InvalidEvent("legacy source response exceeds 8 MiB; import was not performed")
    payload = json.loads(body.decode("utf-8-sig"))
    return payload, limit


def _note(record, source, historical):
    if not isinstance(record, dict):
        raise InvalidEvent("legacy record must be an object")
    raw = {field: record.get(field, "") for field in ("date", "head", "body")}
    if not all(isinstance(value, str) for value in raw.values()):
        raise InvalidEvent("legacy date/head/body must be strings")
    if len(raw["head"]) > 2048 or len(raw["body"]) > 1_048_576 or len(raw["date"]) > 64:
        raise InvalidEvent("legacy record field exceeds local import limits")
    fingerprint = hashlib.sha256(canonical(raw).encode("utf-8")).hexdigest()
    if EXCLUDED_MARKER.search(raw["head"] + "\n" + raw["body"]):
        return fingerprint, None, "explicit_agent_generated_or_test"
    pending = []
    head = raw["head"].strip()
    if head in UNKNOWN_HEADS:
        pending.append("missing_explicit_topic")
    try:
        valid_day(raw["date"])
    except ValueError:
        pending.append("invalid_source_date")
    # The API does not expose a native record ID. Preserve this as unknown;
    # fingerprint is a derived content identifier, not an invented source ID.
    note = {
        "schema_version": 1, "record_type": "legacy_note", "actor": "unknown",
        "date": raw["date"], "date_precision": "day", "occurred_at": None,
        "source_event_id": None, "source_record_sha256": fingerprint,
        "source": {"kind": "unit_log_api", "endpoint": source,
                   "locator": "sha256:" + fingerprint},
        "head": raw["head"], "body": raw["body"],
        "import_class": "historical_import" if historical else "newly_observed_note",
        "status": "pending_index" if pending else "unverified_legacy_note",
        "pending_reasons": pending, "count_as_confirmed_learning": False,
        "memory_statement": None if pending else "既有学习记录记载：" + head + "。是否实际学习未核实。",
        "external_memory_imported": False,
    }
    return fingerprint, note, None


def import_records(ledger: Ledger, payload, source=DEFAULT_SOURCE, source_limit=1000):
    """Import a fetched response; tests pass synthetic responses directly.

    No writes to the source, no writes to events.sqlite3, no daily summaries.
    The source may have no stable IDs: changed bodies become separate immutable
    content versions, without claiming which historical note was edited.
    """
    validate_source(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise InvalidEvent("legacy response must have a records array")
    records = payload["records"]
    if len(records) > 10000:
        raise InvalidEvent("legacy response exceeds 10000 records")
    ledger.home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ledger.home / "legacy.sqlite3", timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS legacy_notes (
                fingerprint TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS import_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        con.execute("BEGIN IMMEDIATE")
        historical = not con.execute("SELECT 1 FROM import_meta WHERE key='initial_import_at'").fetchone()
        inserted = existing = excluded = pending = invalid = 0
        encountered = set()
        for record in records:
            try:
                fingerprint, note, reason = _note(record, source, historical)
            except InvalidEvent:
                invalid += 1
                continue
            if fingerprint in encountered:
                continue
            encountered.add(fingerprint)
            if reason:
                excluded += 1
                continue
            if note["pending_reasons"]:
                pending += 1
            previous = con.execute("SELECT 1 FROM legacy_notes WHERE fingerprint=?", (fingerprint,)).fetchone()
            if previous:
                existing += 1
            else:
                con.execute("INSERT INTO legacy_notes VALUES (?, ?, ?)", (fingerprint, canonical(note), now_utc()))
                inserted += 1
        if historical:
            con.execute("INSERT INTO import_meta VALUES ('initial_import_at', ?)", (now_utc(),))
        con.commit()
        # Serialize projections with import writers and always export current
        # committed state. Missing projections repair without changing identity.
        con.execute("BEGIN IMMEDIATE")
        folder = ledger.home / "legacy_notes"
        folder.mkdir(exist_ok=True)
        index = []
        for row in con.execute("SELECT * FROM legacy_notes ORDER BY fingerprint"):
            note = json.loads(row["payload_json"])
            atomic_write(folder / (row["fingerprint"] + ".json"), row["payload_json"] + "\n")
            index.append({key: note[key] for key in (
                "source_record_sha256", "date", "date_precision", "head", "status",
                "pending_reasons", "import_class", "memory_statement")})
        atomic_write(folder / "index.json", canonical({"record_type": "legacy_note_index",
                     "actor": "unknown", "count_as_confirmed_learning": False, "notes": index}) + "\n")
        result = {
            "status": "synced", "first_import": historical, "fetched_records": len(records),
            "new_or_changed_notes": inserted, "already_seen_notes": existing,
            "explicitly_excluded_notes": excluded, "pending_notes_in_response": pending,
            "invalid_records": invalid, "total_legacy_notes": len(index),
            "source_window_may_be_truncated": len(records) >= source_limit,
            "confirmed_learning_imported": 0, "daily_summaries_generated": 0,
            "external_memory_imported": False,
        }
        atomic_write(ledger.home / "import_status.json", canonical({**result, "checked_at": now_utc()}) + "\n")
        con.commit()
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def sync(ledger, source=DEFAULT_SOURCE):
    payload, limit = fetch_records(source)
    return import_records(ledger, payload, source, limit)
