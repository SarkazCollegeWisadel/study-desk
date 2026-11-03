"""Synthetic contract tests; no network or medical corpus required."""
from pathlib import Path
import tempfile,unittest
from build_catalog import Catalog,classify,clean_url,parse_scope
from render_catalog import render

class CatalogTests(unittest.TestCase):
    def test_signed_url_is_stripped(self):
        self.assertEqual(clean_url('https://user:pass@example.test:443/course?token=secret&cpi=person#private'),'https://example.test/course')
        self.assertIsNone(clean_url('file:///private/config.json'))
    def test_outside_root_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);allowed=root/'allowed';allowed.mkdir();outside=root/'outside.txt';outside.write_text('test')
            with self.assertRaises(ValueError):Catalog(allowed,allowed,allowed).add(outside,'示例')
    def test_content_ids_deduplicate_and_mismatch_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a=root/'a.md';b=root/'b.md';a.write_text('identical');b.write_text('identical')
            c=Catalog(root,root,root);sid=c.add(a,'示例');self.assertEqual(sid,c.add(b,'示例'));self.assertEqual(len(c.sources),1);self.assertEqual(len(c.sources[sid]['locations']),2)
            c.add(a,'示例',expected='0'*64);self.assertFalse(c.sources[sid]['integrity_verified']);self.assertEqual(c.issues[0]['kind'],'hash_mismatch')
    def test_scope_keeps_line_positions_and_not_formal_label(self):
        rows=parse_scope('# 目录\n1\n问诊\n1.1 主诉\n','id')
        self.assertEqual((rows[0]['start_line'],rows[0]['end_line']),(2,3));self.assertEqual(rows[1]['title'],'主诉');self.assertIn('not_answer',rows[1]['coverage'])
        self.assertEqual(classify('执业医师考试大纲.docx'),'licensing_exam_reference_not_school_syllabus')
        self.assertEqual(classify('旧课表.xlsx'),'historical_timetable_not_current_progress')
    def test_renderer_escapes_titles_and_has_no_active_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);output=root/'index.html';source={'source_id':'id','sha256':'a'*64,'title':'<script>bad</script>','document_type':'reference','versions':[],'locations':[{'root':'corpus','path':'source.md'}]}
            render(output,{'sources':[source],'relations':[],'issues':[],'observed_at':'test'},{'courses':[],'diagnostics_review':{'topics':[]}}, {'corpus':root})
            text=output.read_text();self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text);self.assertNotIn('https://',text)

if __name__=='__main__':unittest.main()