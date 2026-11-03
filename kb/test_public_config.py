"""Synthetic public configuration regressions; no model calls or real corpus."""
from pathlib import Path
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import fresh_retrieval as fresh

HERE = Path(__file__).resolve().parent


def module_from(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kb = self.root / "kb"
        self.kb.mkdir()
        self.corpus = self.root / "data"
        self.corpus.mkdir()
        shutil.copyfile(HERE / "build_kb.py", self.kb / "build_kb.py")
        self.cfg = {"root": str(self.corpus), "subjects": ["示例甲", "示例乙"],
                    "include_pdf": False}
        self.save()

    def save(self):
        (self.kb / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def make_index(self):
        for subject in self.cfg["subjects"]:
            folder = self.corpus / subject
            folder.mkdir(exist_ok=True)
            (folder / "课文.md").write_text("# 示例课文\n\n## PDF p.7\n" +
                "这里是合成的检索资料，介绍基础结构与变化过程，供程序测试引用页码和来源。" * 12,
                encoding="utf-8")
        return fresh.build(self.kb)

    def test_cpu_build_requires_explicit_config(self):
        (self.kb / "config.json").unlink()
        with self.assertRaisesRegex(ValueError, "explicit data root"):
            fresh.build(self.kb)
        self.assertFalse((self.kb / "store").exists())

    def test_cpu_index_and_source_page_without_model(self):
        with patch("subprocess.Popen", side_effect=AssertionError("must not launch a service")), \
             patch("socket.socket.connect", side_effect=AssertionError("must not call a model")):
            result = self.make_index()
            retriever = fresh.FreshRetriever(dict(self.cfg), kb=self.kb)
            hits = retriever.search("基础结构", k=4)
        self.assertEqual(result["files"], 2)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["pages"], ["7"])
        self.assertEqual(len(hits[0]["source_sha256"]), 64)

    def test_explicit_cfg_is_loaded_and_instances_do_not_share_subjects(self):
        self.make_index()
        a = fresh.FreshRetriever(dict(self.cfg), kb=self.kb)
        other = dict(self.cfg, subjects=[])
        b = fresh.FreshRetriever(other, kb=self.kb)
        fresh.load_hooks({"subjects": ["无关科目"]})
        self.assertEqual(a.subjects, ("示例甲", "示例乙"))
        self.assertEqual(b.subjects, ())
        hits = a.search("示例甲 基础结构", k=4)
        self.assertTrue(hits)
        self.assertTrue(all(x["file"].startswith("示例甲/") for x in hits))

    def test_hooks_reset_and_validate(self):
        fresh.load_hooks({"subjects": ["示例甲"], "version_markers": [["v1", "old"]],
                          "archive_marker": "archive"})
        fresh.load_hooks({})
        self.assertEqual((fresh.SUBJECTS, fresh.VERSION_MARKERS, fresh.ARCHIVE_MARKER), ((), (), ""))
        with self.assertRaises(ValueError):
            fresh.load_hooks({"subjects": "wrong type"})

    def test_unit_topic_globs_are_really_loaded(self):
        self.cfg["topic_globs"] = {"示例甲": "topics/*.md"}
        self.save()
        shutil.copyfile(HERE / "unit_api.py", self.kb / "unit_api.py")
        module = module_from(self.kb / "unit_api.py", "unit_fixture")
        self.assertEqual(module.TOPIC_GLOBS, self.cfg["topic_globs"])
        self.assertEqual(module.ROOT, self.corpus)

    def test_unit_topic_glob_cannot_escape_root(self):
        self.cfg["topic_globs"] = {"示例甲": "../*.md"}
        self.save()
        shutil.copyfile(HERE / "unit_api.py", self.kb / "unit_api.py")
        with self.assertRaises(ValueError):
            module_from(self.kb / "unit_api.py", "unit_invalid")


if __name__ == "__main__":
    unittest.main()
