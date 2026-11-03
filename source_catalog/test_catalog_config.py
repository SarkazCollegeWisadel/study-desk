"""Synthetic configuration and provenance regressions."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
import build_catalog as catalog


class CatalogConfigTests(unittest.TestCase):
    def test_subject_selection_is_required(self):
        with patch.object(sys, "argv", ["catalog", "--corpus-root", ".", "--archive-root", ".",
                                       "--raw-root", ".", "--output", "."]):
            with self.assertRaises(SystemExit) as error:
                catalog.main()
        self.assertEqual(error.exception.code, 2)

    def test_no_invented_acquisition_day_and_topic_subjects_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus, archive, raw = [root / x for x in ("corpus", "archive", "raw")]
            for path in (corpus, archive, raw):
                path.mkdir()
            file = corpus / "示例乙" / "source.md"
            file.parent.mkdir()
            file.write_text("合成内容", encoding="utf-8")
            sha = hashlib.sha256(file.read_bytes()).hexdigest()
            entries = [{"subject": "示例乙", "destination": str(file), "sha256": sha}]
            for name, value in [("学科文件夹同步核验.json", entries), ("来源与文件核验.json", []),
                                ("原始课程资料核验.json", []), ("Markdown转换核验.json", [])]:
                (archive / name).write_text(json.dumps(value), encoding="utf-8")
            seen = []
            def search(api, query):
                seen.append(query)
                return [{"file": "示例乙/source.md", "source_sha256": sha,
                         "heading": "合成", "start": 1, "end": 1}]
            topics = [{"title": "主题", "query": "没有科名前缀的查询", "subject": "示例乙"}]
            with patch.object(catalog, "SUBJECTS", ("示例甲", "示例乙")), \
                 patch.object(catalog, "TOPICS", topics), patch.object(catalog, "ACQUIRED", ""), \
                 patch.object(catalog, "search", search):
                data, outlines = catalog.build(corpus, archive, raw, "unused")
            self.assertEqual(len(outlines["courses"]), 2)
            self.assertEqual(seen, [topics[0]["query"]])
            source = data["sources"][0]
            self.assertIsNone(source["captured_on"])
            self.assertEqual(source["captured_time_precision"], "unknown")
            self.assertEqual(source["subjects"], ["示例乙"])


if __name__ == "__main__":
    unittest.main()
