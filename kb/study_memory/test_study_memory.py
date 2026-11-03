"""Synthetic events only. All writes are scoped to TemporaryDirectory."""

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from .core import EventConflict, InvalidEvent, Ledger, normalized_event
from .importer import import_records, sync, validate_source


def fixture(event_id="synthetic-001", occurred_at="2026-11-02T23:00:00+08:00", **changes):
    result = {
        "event_id": event_id, "occurred_at": occurred_at, "actor": "user",
        "action": "read", "topic": "合成问诊主题",
        "evidence": {"kind": "explicit_user_statement", "ref": "synthetic:user-confirmation:001"},
        "source_refs": [{"source_id": "synthetic-textbook", "title": "合成测试教材",
                         "locator": "第 2 章，第 3 页", "uri": "fixture://textbook/chapter-2",
                         "content_sha256": "a" * 64}],
    }
    result.update(changes)
    return result


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-study-memory-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "ledger"
        self.ledger = Ledger(self.home)

    def test_zero_events_do_not_create_storage_or_summary(self):
        self.assertEqual(self.ledger.status()["event_count"], 0)
        self.assertEqual(self.ledger.distill()["status"], "no_events")
        self.assertIsNone(self.ledger.read("2026-11-02"))
        self.assertFalse(self.home.exists())

    def test_singapore_utc_boundary(self):
        before = fixture("before", "2026-11-02T15:59:59Z")
        after = fixture("after", "2026-11-02T16:00:00Z")
        self.assertEqual(self.ledger.record(before)["date"], "2026-11-02")
        self.assertEqual(self.ledger.record(after)["date"], "2026-11-03")
        self.ledger.distill()
        self.assertEqual(self.ledger.read("2026-11-02")["event_count"], 1)
        self.assertEqual(self.ledger.read("2026-11-03")["event_count"], 1)

    def test_non_singapore_offset_normalizes(self):
        self.assertEqual(normalized_event(fixture(occurred_at="2026-11-02T08:00:00-08:00"))[1], "2026-11-03")

    def test_timezone_required(self):
        for timestamp in ("2026-11-02T12:00:00", "2026-11-02", "not-a-time"):
            with self.subTest(timestamp=timestamp), self.assertRaises(InvalidEvent):
                self.ledger.record(fixture(occurred_at=timestamp))
        self.assertFalse(self.home.exists())

    def test_duplicate_id_is_noop(self):
        first = self.ledger.record(fixture())
        second = self.ledger.record(fixture())
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(self.ledger.status()["event_count"], 1)

    def test_equivalent_timezone_does_not_conflict(self):
        self.ledger.record(fixture())
        result = self.ledger.record(fixture(occurred_at="2026-11-02T15:00:00Z"))
        self.assertEqual(result["status"], "duplicate")

    def test_conflicting_id_cannot_overwrite_facts(self):
        self.ledger.record(fixture())
        with self.assertRaises(EventConflict):
            self.ledger.record(fixture(topic="被修改的事实"))
        self.ledger.distill()
        self.assertEqual(self.ledger.read("2026-11-02")["events"][0]["topic"], "合成问诊主题")

    def test_agent_events_rejected(self):
        for action in ("read", "study", "answer", "retrieval_test", "audio_generated", "draft_generated"):
            with self.subTest(action=action), self.assertRaises(InvalidEvent):
                self.ledger.record(fixture(actor="agent", action=action))
        self.assertFalse(self.home.exists())

    def test_generation_and_inferred_learning_rejected(self):
        for change in ({"action": "audio_generated"}, {"action": "retrieval_test"},
                       {"action": "draft_generated"},
                       {"evidence": {"kind": "agent_inference", "ref": "synthetic:test"}}):
            with self.subTest(change=change), self.assertRaises(InvalidEvent):
                self.ledger.record(fixture(**change))

    def test_user_action_requires_evidence_reference(self):
        with self.assertRaises(InvalidEvent):
            self.ledger.record(fixture(evidence={"kind": "direct_user_action", "ref": ""}))
        result = self.ledger.record(fixture(evidence={"kind": "direct_user_action", "ref": "synthetic:explicit-read-button:1"}))
        self.assertTrue(result["inserted"])

    def test_source_reference_required_and_complete(self):
        for sources in ([], [{"source_id": "book"}], [{"source_id": "book", "title": "title", "locator": ""}]):
            with self.subTest(sources=sources), self.assertRaises(InvalidEvent):
                self.ledger.record(fixture(source_refs=sources))

    def test_all_source_details_survive_distillation(self):
        event = fixture(note="用户明确表示已阅读", unresolved="主诉与现病史如何区分？")
        self.ledger.record(event)
        self.ledger.distill()
        result = self.ledger.read("2026-11-02")
        self.assertEqual(result["sources"], event["source_refs"])
        self.assertEqual(result["events"][0]["evidence"], event["evidence"])
        self.assertEqual(result["events"][0]["unresolved"], event["unresolved"])
        self.assertEqual(result["events"][0]["note"], event["note"])

    def test_same_day_rerun_has_identical_content_and_one_version(self):
        self.ledger.record(fixture())
        self.ledger.distill()
        first = self.ledger.read("2026-11-02")
        exported = (self.home / "summaries" / "2026-11-02.md").read_bytes()
        second = self.ledger.distill()["results"][0]
        self.assertEqual(second["status"], "unchanged")
        self.assertFalse(second["exports_written"])
        self.assertEqual(first, self.ledger.read("2026-11-02"))
        self.assertEqual(exported, (self.home / "summaries" / "2026-11-02.md").read_bytes())
        with closing(sqlite3.connect(self.ledger.db_path)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM summary_versions").fetchone()[0], 1)

    def test_late_event_updates_original_day_with_preserved_revision(self):
        self.ledger.record(fixture())
        self.ledger.distill()
        self.ledger.record(fixture("late-002", "2026-11-02T12:00:00+08:00", action="answer"))
        self.assertEqual(self.ledger.status()["pending_dates"], ["2026-11-02"])
        self.ledger.distill()
        summary = self.ledger.read("2026-11-02")
        self.assertEqual(summary["revision"], 2)
        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["events"][0]["event_id"], "late-002")
        with closing(sqlite3.connect(self.ledger.db_path)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM summaries").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM summary_versions").fetchone()[0], 2)
            old = json.loads(con.execute("SELECT summary_json FROM summary_versions WHERE revision=1").fetchone()[0])
            self.assertEqual(old["event_count"], 1)
        self.assertEqual(self.ledger.status()["pending_dates"], [])

    def test_date_with_no_events_does_not_create_entry(self):
        self.ledger.record(fixture())
        self.assertEqual(self.ledger.distill("2026-11-01")["status"], "no_events")
        self.assertIsNone(self.ledger.read("2026-11-01"))
        self.assertFalse((self.home / "summaries").exists())

    def test_small_activity_produces_small_summary_not_mastery(self):
        self.ledger.record(fixture())
        self.ledger.distill()
        text = self.ledger.read("2026-11-02", as_markdown=True)
        self.assertLess(len(text), 350)
        self.assertIn("阅读 1 次", text)
        self.assertIn("未评估掌握程度", text)
        self.assertNotIn("已经掌握", text)
        self.assertFalse(self.ledger.read("2026-11-02")["memory_import"]["imported"])

    def test_many_topics_have_bounded_preview_and_full_facts(self):
        for number in range(20):
            self.ledger.record(fixture(f"synthetic-{number}", topic=f"合成主题 {number}"))
        self.ledger.distill()
        result = self.ledger.read("2026-11-02")
        self.assertEqual(len(result["events"]), 20)
        self.assertEqual(len(result["topics"]), 20)
        self.assertIn("另有 12 个主题", self.ledger.read("2026-11-02", as_markdown=True))

    def test_crash_after_commit_is_repairable_without_new_revision(self):
        self.ledger.record(fixture())
        with patch("study_memory.core.atomic_write", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.ledger.distill()
        self.assertEqual(self.ledger.read("2026-11-02")["revision"], 1)
        result = self.ledger.distill()["results"][0]
        self.assertEqual(result["revision"], 1)
        self.assertTrue(result["exports_written"])
        self.assertTrue((self.home / "summaries" / "2026-11-02.md").exists())

    def test_corrupt_projection_repaired_from_authoritative_ledger(self):
        self.ledger.record(fixture())
        self.ledger.distill()
        output = self.home / "summaries" / "2026-11-02.json"
        output.write_text("synthetic corrupted projection", encoding="utf-8")
        self.assertEqual(self.ledger.read("2026-11-02")["event_count"], 1)
        result = self.ledger.distill()["results"][0]
        self.assertTrue(result["exports_written"])
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["revision"], 1)

    def test_concurrent_duplicates_are_not_double_counted(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: Ledger(self.home).record(fixture()), range(8)))
        self.assertEqual(sum(r["inserted"] for r in results), 1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: Ledger(self.home).distill(), range(4)))
        self.assertEqual(self.ledger.read("2026-11-02")["revision"], 1)

    def test_reference_reordering_and_duplicates_normalized(self):
        event = fixture()
        other = copy.deepcopy(event["source_refs"][0])
        other["locator"] = "第 5 页"
        event["source_refs"].append(other)
        self.ledger.record(event)
        event["source_refs"] = [other, event["source_refs"][0], other]
        self.assertEqual(self.ledger.record(event)["status"], "duplicate")

    def test_invalid_field_types_are_validation_errors(self):
        changes = [{"action": []}, {"topic": None}, {"event_id": "../escape"},
                   {"evidence": {"kind": [], "ref": "test"}}, {"unexpected": True},
                   {"topic": "contains\x1bcontrol"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(InvalidEvent):
                self.ledger.record(fixture(**change))

    def test_invalid_date_cannot_escape_export_directory(self):
        for day in ("../escape", "2026-11-03/../../test", "2026-02-30"):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.ledger.distill(day)

    def test_cli_status_record_distill_read_roundtrip(self):
        root = Path(__file__).resolve().parent.parent
        prefix = [sys.executable, "-B", "-m", "study_memory", "--data-dir", str(self.home)]
        def run(*args, payload=None):
            return subprocess.run(prefix + list(args), cwd=root, input=payload,
                                  encoding="utf-8", capture_output=True, check=False)
        empty = run("status")
        self.assertEqual(empty.returncode, 0)
        self.assertEqual(json.loads(empty.stdout)["event_count"], 0)
        missing = run("read", "--date", "2026-11-02")
        self.assertEqual(missing.returncode, 3)
        recorded = run("record", payload=json.dumps(fixture(), ensure_ascii=False))
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(json.loads(recorded.stdout)["status"], "recorded")
        self.assertNotIn("合成问诊", recorded.stdout)
        distilled = run("distill")
        self.assertEqual(distilled.returncode, 0, distilled.stderr)
        result = run("read", "--date", "2026-11-02", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sources"], fixture()["source_refs"])
        rejected = run("record", payload=json.dumps(fixture(actor="agent")))
        self.assertEqual(rejected.returncode, 2)
        self.assertNotIn("合成问诊", rejected.stderr)


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-study-import-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "ledger"
        self.ledger = Ledger(self.home)

    @staticmethod
    def note(**changes):
        result = {"date": "2026-11-02", "head": "合成科目/合成主题",
                  "body": "问：合成问题？\n用时：未核　状态：已进入"}
        result.update(changes)
        return result

    def imported(self):
        return json.loads((self.home / "legacy_notes" / "index.json").read_text(encoding="utf-8"))

    def test_legacy_first_import_not_confirmed_learning(self):
        result = import_records(self.ledger, {"records": [self.note()]})
        self.assertTrue(result["first_import"])
        self.assertEqual(result["new_or_changed_notes"], 1)
        self.assertEqual(result["confirmed_learning_imported"], 0)
        self.assertEqual(self.ledger.status()["event_count"], 0)
        self.assertEqual(self.ledger.distill()["status"], "no_events")
        self.assertFalse(self.ledger.db_path.exists())
        self.assertFalse((self.home / "summaries").exists())
        note = self.imported()["notes"][0]
        self.assertEqual(note["import_class"], "historical_import")
        self.assertIn("既有学习记录记载", note["memory_statement"])

    def test_original_day_and_text_preserved_without_invented_time(self):
        original = self.note()
        import_records(self.ledger, {"records": [original]})
        note = self.imported()["notes"][0]
        path = self.home / "legacy_notes" / (note["source_record_sha256"] + ".json")
        full = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(full["body"], original["body"])
        self.assertEqual(full["head"], original["head"])
        self.assertEqual(full["date"], "2026-11-02")
        self.assertEqual(full["date_precision"], "day")
        self.assertIsNone(full["occurred_at"])
        self.assertIsNone(full["source_event_id"])
        self.assertEqual(full["actor"], "unknown")
        self.assertFalse(full["count_as_confirmed_learning"])
        self.assertEqual(full["source"]["locator"], "sha256:" + note["source_record_sha256"])

    def test_reimport_does_not_duplicate_or_regenerate_note_index(self):
        payload = {"records": [self.note()]}
        import_records(self.ledger, payload)
        path = self.home / "legacy_notes" / "index.json"
        original = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        result = import_records(self.ledger, payload)
        self.assertFalse(result["first_import"])
        self.assertEqual(result["new_or_changed_notes"], 0)
        self.assertEqual(result["already_seen_notes"], 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_new_late_and_modified_notes_increment_without_daily_summary(self):
        original = self.note()
        import_records(self.ledger, {"records": [original]})
        changed = self.note(body="问：合成问题？\n补充：新的原日志文字")
        late = self.note(date="2026-10-28", head="合成旧主题")
        result = import_records(self.ledger, {"records": [late, changed]})
        self.assertEqual(result["new_or_changed_notes"], 2)
        self.assertEqual(result["total_legacy_notes"], 3)
        index = self.imported()["notes"]
        self.assertEqual(sum(n["import_class"] == "historical_import" for n in index), 1)
        self.assertEqual(sum(n["import_class"] == "newly_observed_note" for n in index), 2)
        self.assertIn("2026-10-28", [n["date"] for n in index])
        self.assertFalse((self.home / "summaries").exists())

    def test_explicit_agent_generation_and_test_markers_excluded(self):
        records = [self.note(body=marker) for marker in (
            "actor: agent", "来源类型：codex", "action: audio_generated",
            "origin: synthetic", "[agent-test]", "自动生成记录：合成稿件")]
        result = import_records(self.ledger, {"records": records})
        self.assertEqual(result["explicitly_excluded_notes"], len(records))
        self.assertEqual(result["total_legacy_notes"], 0)
        self.assertFalse(self.ledger.db_path.exists())

    def test_incidental_test_word_not_falsely_excluded(self):
        result = import_records(self.ledger, {"records": [self.note(body="医学检查 test 的合成课程内容，讨论 AI 局限。") ]})
        self.assertEqual(result["explicitly_excluded_notes"], 0)
        self.assertEqual(result["total_legacy_notes"], 1)

    def test_no_topic_kept_as_pending_index_without_guess(self):
        result = import_records(self.ledger, {"records": [self.note(head="")]})
        self.assertEqual(result["pending_notes_in_response"], 1)
        note = self.imported()["notes"][0]
        self.assertEqual(note["status"], "pending_index")
        self.assertIsNone(note["memory_statement"])
        self.assertIn("missing_explicit_topic", note["pending_reasons"])

    def test_invalid_date_pending_not_default_today(self):
        import_records(self.ledger, {"records": [self.note(date="不知道")]})
        note = self.imported()["notes"][0]
        self.assertEqual(note["date"], "不知道")
        self.assertIn("invalid_source_date", note["pending_reasons"])

    def test_duplicate_record_same_response_only_once(self):
        result = import_records(self.ledger, {"records": [self.note(), self.note()]})
        self.assertEqual(result["total_legacy_notes"], 1)

    def test_api_window_limit_reported(self):
        result = import_records(self.ledger, {"records": [self.note()]}, source_limit=1)
        self.assertTrue(result["source_window_may_be_truncated"])

    def test_reject_external_endpoints_credentials_and_write_routes(self):
        urls = ["https://example.com/api/unit/log?n=5", "http://localhost:8765/api/unit/log?n=5",
                "http://user:secret@127.0.0.1:8765/api/unit/log?n=5",
                "http://127.0.0.1:8765/api/unit/save", "http://127.0.0.1:8765/api/llm/answer",
                "http://127.0.0.1:8765/api/unit/log?n=0", "http://127.0.0.1:8765/api/unit/log?n=5&token=secret"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(InvalidEvent):
                validate_source(url)

    def test_http_sync_reads_only_get_and_cli_safe_summary(self):
        seen = []
        payload = {"records": [self.note()]}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.command, self.path))
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}/api/unit/log?n=1000"
        prefix = [sys.executable, "-B", "-m", "study_memory", "--data-dir", str(self.home)]
        result = subprocess.run(prefix + ["sync", "--source", endpoint],
                                cwd=Path(__file__).resolve().parent.parent,
                                encoding="utf-8", capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_legacy_notes"], 1)
        self.assertNotIn("合成科目", result.stdout)
        self.assertEqual(seen, [("GET", "/api/unit/log?n=1000")])
        self.assertFalse(self.ledger.db_path.exists())

    def test_confirmed_browse_is_browse_not_study_or_mastery(self):
        self.ledger.record(fixture(action="browse"))
        self.ledger.distill()
        text = self.ledger.read("2026-11-02", as_markdown=True)
        self.assertIn("查阅 1 次", text)
        self.assertNotIn("学习 1 次", text)


if __name__ == "__main__":
    unittest.main()
