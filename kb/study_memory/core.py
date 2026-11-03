"""Standard-library event ledger and deterministic daily learning summaries.

SQLite is authoritative. Markdown/JSON are repairable projections, not evidence
that an external memory system has imported anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


SCHEMA_VERSION = 1
TIMEZONE_NAME = "Asia/Singapore"
# Singapore has used UTC+08:00 since 1982; this ledger only accepts modern
# learning events, avoiding a tzdata installation on Windows.
LOCAL_TZ = timezone(timedelta(hours=8), TIMEZONE_NAME)
DEFAULT_HOME = Path(__file__).resolve().parent.parent / ".local" / "study_memory"
ACTIONS = {"browse": "查阅", "read": "阅读", "study": "学习", "practice": "练习", "answer": "作答", "review": "复习"}
EVIDENCE_KINDS = {"explicit_user_statement", "direct_user_action"}
EVENT_KEYS = {"event_id", "occurred_at", "actor", "action", "topic", "evidence", "source_refs", "note", "unresolved"}
SOURCE_KEYS = {"source_id", "title", "locator", "uri", "content_sha256"}


class InvalidEvent(ValueError):
    pass


class EventConflict(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def text_field(value, name, maximum=280):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise InvalidEvent(f"{name} must be a non-empty string of at most {maximum} characters")
    # Keep exported Markdown and terminal messages single-line and safe from
    # embedded ANSI escapes. Content itself remains local and never logged.
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvalidEvent(f"{name} must not contain control characters")
    return value.strip()


def normalized_event(value):
    if not isinstance(value, dict) or set(value) - EVENT_KEYS:
        raise InvalidEvent("event must be an object with only documented fields")
    event_id = text_field(value.get("event_id"), "event_id", 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", event_id):
        raise InvalidEvent("event_id must use ASCII letters, digits, dot, underscore, colon or hyphen")
    if value.get("actor") != "user":
        raise InvalidEvent("only actor=user is accepted; agent activity is not a learning event")
    action = value.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise InvalidEvent("action must be browse, read, study, practice, answer or review")
    timestamp = text_field(value.get("occurred_at"), "occurred_at", 64)
    # Require an actual ISO date/time, not a timezone-less date interpreted as UTC.
    if "T" not in timestamp:
        raise InvalidEvent("occurred_at must be an ISO timestamp with timezone")
    try:
        occurred = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidEvent("occurred_at must be an ISO timestamp with timezone") from None
    if occurred.tzinfo is None or occurred.utcoffset() is None:
        raise InvalidEvent("occurred_at requires an explicit timezone offset")
    try:
        local_date = occurred.astimezone(LOCAL_TZ).date()
        normalized_time = occurred.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (OverflowError, ValueError):
        raise InvalidEvent("occurred_at is outside supported date range") from None
    if local_date < date(1982, 1, 1):
        raise InvalidEvent("this modern study ledger supports dates from 1982-01-01")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"kind", "ref"}:
        raise InvalidEvent("evidence requires exactly kind and ref")
    if not isinstance(evidence["kind"], str) or evidence["kind"] not in EVIDENCE_KINDS:
        raise InvalidEvent("evidence must cite an explicit user statement or a direct user action")
    evidence = {"kind": evidence["kind"], "ref": text_field(evidence["ref"], "evidence.ref", 512)}
    source_refs = value.get("source_refs")
    if not isinstance(source_refs, list) or not 1 <= len(source_refs) <= 32:
        raise InvalidEvent("source_refs must contain 1 to 32 actual source references")
    sources = []
    for source in source_refs:
        if not isinstance(source, dict) or set(source) - SOURCE_KEYS:
            raise InvalidEvent("source_refs entries must use documented fields")
        item = {name: text_field(source.get(name), f"source_refs.{name}", 512)
                for name in ("source_id", "title", "locator")}
        if "uri" in source:
            item["uri"] = text_field(source["uri"], "source_refs.uri", 2048)
        if "content_sha256" in source:
            digest = source["content_sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise InvalidEvent("content_sha256 must be a SHA-256 hexadecimal digest")
            item["content_sha256"] = digest.lower()
        sources.append(item)
    # Reordering references or an equivalent timezone representation is not a
    # distinct event. Preserve every distinct locator, even with the same ID.
    sources = [json.loads(s) for s in sorted({canonical(s) for s in sources})]
    result = {
        "event_id": event_id, "occurred_at": normalized_time, "actor": "user",
        "action": action, "topic": text_field(value.get("topic"), "topic", 120),
        "evidence": evidence, "source_refs": sources,
    }
    for field in ("note", "unresolved"):
        if field in value:
            result[field] = text_field(value[field], field)
    return result, local_date.isoformat()


def valid_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def markdown_text(value):
    return re.sub(r"([\\`*_{}\[\]<>#|!])", r"\\\1", str(value))


def make_summary(day, revision, events, fingerprint, updated_at):
    topics = {}
    sources = {}
    for event in events:
        topic = topics.setdefault(event["topic"], {"topic": event["topic"], "event_count": 0,
                                                   "actions": Counter(), "source_ids": set()})
        topic["event_count"] += 1
        topic["actions"][event["action"]] += 1
        for source in event["source_refs"]:
            sources[canonical(source)] = source
            topic["source_ids"].add(source["source_id"])
    groups = []
    for topic in topics.values():
        topic["actions"] = dict(sorted(topic["actions"].items()))
        topic["source_ids"] = sorted(topic["source_ids"])
        groups.append(topic)
    sentence = f"记录到 {len(events)} 次明确学习/查阅活动，涉及 {len(groups)} 个主题；未评估掌握程度。"
    payload = {
        "schema_version": SCHEMA_VERSION, "date": day, "timezone": TIMEZONE_NAME,
        "revision": revision, "input_sha256": fingerprint, "updated_at": updated_at,
        "event_count": len(events), "summary": sentence, "topics": groups,
        "events": events, "sources": list(sources.values()),
        "memory_import": {"status": "not_implemented", "imported": False},
    }
    lines = [f"# {day} 学习记录（{TIMEZONE_NAME}，v{revision}）", "", sentence, ""]
    for group in groups[:8]:
        activity = "、".join(f"{ACTIONS[action]} {count} 次" for action, count in group["actions"].items())
        lines.append(f"- {markdown_text(group['topic'])}：{activity}。")
    if len(groups) > 8:
        lines.append(f"- 另有 {len(groups) - 8} 个主题，见同日 JSON 完整事实。")
    questions = list(dict.fromkeys(e["unresolved"] for e in events if e.get("unresolved")))
    if questions:
        lines.extend(["", "明确留下的问题："])
        lines.extend(f"- {markdown_text(q)}" for q in questions[:3])
        if len(questions) > 3:
            lines.append(f"- 另 {len(questions) - 3} 项见 JSON。")
    lines.extend(["", "出处（完整事件及逐项引用见同日 JSON）："])
    for source in payload["sources"][:8]:
        lines.append(f"- {markdown_text(source['source_id'])}：{markdown_text(source['title'])}，{markdown_text(source['locator'])}。")
    if len(payload["sources"]) > 8:
        lines.append(f"- 另 {len(payload['sources']) - 8} 条出处见 JSON。")
    lines.extend(["", "状态：本地摘要已生成；尚未导入外部记忆系统。", ""])
    return payload, "\n".join(lines)


def atomic_write(path, content):
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


class Ledger:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else DEFAULT_HOME
        self.db_path = self.home / "events.sqlite3"

    def _connect(self):
        self.home.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise RuntimeError("unsupported learning ledger schema version")
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
                    local_date TEXT NOT NULL, payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_day ON events(local_date, occurred_at, event_id);
                CREATE TABLE IF NOT EXISTS summaries (
                    local_date TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    input_sha256 TEXT NOT NULL, summary_json TEXT NOT NULL,
                    markdown TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS summary_versions (
                    local_date TEXT NOT NULL, revision INTEGER NOT NULL,
                    input_sha256 TEXT NOT NULL, summary_json TEXT NOT NULL,
                    markdown TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(local_date, revision)
                );
                PRAGMA user_version=1;
            """)
            return con
        except Exception:
            con.close()
            raise

    def record(self, value):
        event, day = normalized_event(value)
        payload = canonical(event)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT payload_json FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            if previous:
                if previous["payload_json"] != payload:
                    raise EventConflict("event_id already exists with different facts; no overwrite performed")
                inserted = False
            else:
                con.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                            (event["event_id"], event["occurred_at"], day, payload, now_utc()))
                inserted = True
            con.commit()
            return {"status": "recorded" if inserted else "duplicate", "event_id": event["event_id"],
                    "date": day, "inserted": inserted}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _events(con, day):
        rows = con.execute("SELECT payload_json FROM events WHERE local_date=? ORDER BY occurred_at, event_id", (day,)).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    @staticmethod
    def _fingerprint(events):
        return hashlib.sha256(canonical(events).encode("utf-8")).hexdigest()

    def status(self):
        if not self.db_path.exists():
            return {"status": "empty", "event_count": 0, "summary_count": 0, "pending_dates": [],
                    "timezone": TIMEZONE_NAME, "external_memory_imported": False}
        con = self._connect()
        try:
            con.execute("BEGIN")
            days = [r[0] for r in con.execute("SELECT DISTINCT local_date FROM events ORDER BY local_date")]
            pending = []
            for day in days:
                previous = con.execute("SELECT input_sha256 FROM summaries WHERE local_date=?", (day,)).fetchone()
                if not previous or previous[0] != self._fingerprint(self._events(con, day)):
                    pending.append(day)
            count = con.execute("SELECT count(*) FROM events").fetchone()[0]
            summaries = con.execute("SELECT count(*) FROM summaries").fetchone()[0]
            return {"status": "ready", "event_count": count, "summary_count": summaries,
                    "pending_dates": pending, "timezone": TIMEZONE_NAME, "external_memory_imported": False}
        finally:
            con.close()

    def _export_latest(self, day):
        # Serialize export with ledger writers, and read the *latest* committed
        # summary. An older distiller can never overwrite a newer revision.
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM summaries WHERE local_date=?", (day,)).fetchone()
            if not row:
                return False
            folder = self.home / "summaries"
            folder.mkdir(parents=True, exist_ok=True)
            changed_json = atomic_write(folder / f"{day}.json", row["summary_json"] + "\n")
            changed_md = atomic_write(folder / f"{day}.md", row["markdown"])
            con.commit()
            return changed_json or changed_md
        finally:
            con.close()

    def distill(self, day=None):
        if day is not None:
            valid_day(day)
        if not self.db_path.exists():
            return {"status": "no_events", "results": [], "external_memory_imported": False}
        # For a daily job, revisit all event dates, including late arrivals and
        # damaged/missing projection files. Unchanged dates do not gain versions.
        con = self._connect()
        try:
            days = [day] if day else [r[0] for r in con.execute("SELECT DISTINCT local_date FROM events ORDER BY local_date")]
        finally:
            con.close()
        results = []
        for current in days:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                events = self._events(con, current)
                if not events:
                    con.commit()
                    continue
                fingerprint = self._fingerprint(events)
                previous = con.execute("SELECT * FROM summaries WHERE local_date=?", (current,)).fetchone()
                changed = not previous or previous["input_sha256"] != fingerprint
                revision = (previous["revision"] + 1 if previous else 1) if changed else previous["revision"]
                if changed:
                    updated = now_utc()
                    summary, markdown = make_summary(current, revision, events, fingerprint, updated)
                    args = (current, revision, fingerprint, canonical(summary), markdown, updated)
                    con.execute("INSERT INTO summary_versions VALUES (?, ?, ?, ?, ?, ?)", args)
                    con.execute("INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?, ?, ?)", args)
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            repaired = self._export_latest(current)
            results.append({"date": current, "status": "updated" if changed else "unchanged",
                            "revision": revision, "event_count": len(events), "exports_written": repaired,
                            "files_generated": True, "external_memory_imported": False})
        return {"status": "complete" if results else "no_events", "results": results,
                "external_memory_imported": False}

    def read(self, day, as_markdown=False):
        valid_day(day)
        if not self.db_path.exists():
            return None
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM summaries WHERE local_date=?", (day,)).fetchone()
            if row is None:
                return None
            return row["markdown"] if as_markdown else json.loads(row["summary_json"])
        finally:
            con.close()
