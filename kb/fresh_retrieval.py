"""CPU-only, citation-preserving retrieval. No model, network, or source writes.

Build a separate FTS5 index with `index`; queries open it read-only. The legacy
embedding store is intentionally independent (different embedding spaces must not mix).
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

DEFAULT_KB = Path(__file__).resolve().parent
SCHEMA = 1
# Deployments may set "subjects"; empty means every top-level directory is eligible.
VERSION_MARKERS = ()  # (filename marker, version note) pairs from config.json
ARCHIVE_MARKER = ''  # optional archive subdirectory name counted in index metadata
STOP = set('什么 如何 为什么 哪些 哪个 下列 关于 正确 错误 不是 不属于 属于 选择 选项 题目 请问 简述 说明 分析 以及 可以 进行 需要 根据 教材 考试 教学 大纲 考纲 要求 了解 熟悉 掌握 的 了 是 和 与 或 在 有 为 对 中 将 其 应'.split())
SUBJECTS = ()  # configured per deployment via config.json "subjects"


def load_hooks(cfg):
    """Read the optional deployment hooks; absent or malformed values leave defaults."""
    global SUBJECTS, VERSION_MARKERS, ARCHIVE_MARKER
    SUBJECTS = configured_subjects(cfg)
    markers = cfg.get('version_markers', [])
    if not isinstance(markers, list) or any(not isinstance(pair, (list, tuple)) or len(pair) != 2
                                           or not all(isinstance(x, str) for x in pair) for pair in markers):
        raise ValueError('version_markers must be [marker, note] string pairs')
    VERSION_MARKERS = tuple(tuple(pair) for pair in markers)
    ARCHIVE_MARKER = cfg.get('archive_marker', '')
    if not isinstance(ARCHIVE_MARKER, str):
        raise ValueError('archive_marker must be a string')
    return cfg


def configured_subjects(cfg):
    values = cfg.get('subjects', [])
    if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values):
        raise ValueError('subjects must be a list of nonempty strings')
    return tuple(dict.fromkeys(x.strip() for x in values))


def load_engine(kb):
    spec = importlib.util.spec_from_file_location('_kb_source_reader', Path(kb) / 'build_kb.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def terms(text):
    import jieba
    jieba.setLogLevel(60)
    return [s.lower() for s in jieba.cut_for_search(text) if re.fullmatch(r'[\w]+', s) and s not in STOP and (len(s) > 1 or s.isdigit())]


def grams(text):
    return [part[i:i + 2] for part in re.findall(r'[\u3400-\u9fff]+', text) for i in range(len(part) - 1)]


def metadata(file, preamble):
    name = Path(file).stem
    if '非正式' in name and '章节目录' in name:
        kind = '章节目录（非正式大纲）'
    elif '教学目标' in name:
        kind = '教师教学目标（非整门正式大纲）'
    elif '执' in name and '考' in name:
        kind = '执医考纲/复习材料（非本校大纲）'
    elif '考试大纲' in name:
        kind = '课程考试大纲'
    elif '教学大纲' in name:
        kind = '课程教学大纲'
    elif '课程简介' in name:
        kind = '课程简介'
    elif '课表' in name:
        kind = '历史课表（非本学期进度）'
    elif 'PPT课件版' in file:
        kind = '教师课件'
    elif '复习' in name or '病例对照研究' in name:
        kind = '课程练习/复习材料'
    elif '质检' in name or '覆盖核对' in name or '两书对照' in name:
        kind = '资料质检/覆盖报告（非教材正文）'
    elif '目录' in name or '索引' in name:
        kind = '学习索引（非教材原句）'
    elif '原始档案' in file:
        kind = '旧基石资料'
    else:
        kind = '教材/学习资料'
    version = []
    for marker, note in VERSION_MARKERS:
        if marker and marker in file:
            version.append(note)
    if '2025-2026-2' in file:
        version.append('旧学期课表')
    if 'RapidOCR' in preamble:
        version.append('OCR转录，未逐字校订；须核对原页')
    # Keep the original source link for tracing a Markdown transcription to its PDF.
    links = re.findall(r'原文件：.*?\]\(<([^>]+)>\)', preamble)
    if not links:
        links = re.findall(r"(?m)^source_pdf:\s*['\"]?(.+?\.pdf)['\"]?\s*$", preamble)
    if not links:
        links = re.findall(r'源[：:]\s*`([^`]+\.pdf)`', preamble)
    return {'source_kind': kind, 'version_note': '；'.join(version),
            'original_source': links[0] if links else ''}


def pdf_pages(line):
    """Read physical PDF page markers, never infer printed book page numbers."""
    match = re.search(r'PDF\s*(?:第\s*)?(?:p\.?\s*)?(\d+)\s*(?:[–—-]\s*p?\s*(\d+))?', line, re.I)
    if not match:
        match = re.search(r'<!--\s*p\.(\d+)\s*-->', line)
    if not match:
        return []
    start = int(match[1]); end = int(match[2] or start) if match.lastindex > 1 else start
    return [str(i) for i in range(start, min(end, start + 1000) + 1)]


def substantive_text(text):
    """Keep actual source prose/questions while dropping document scaffolding."""
    return '\n'.join(line for line in text.splitlines() if line.strip() and not (
        re.match(r'^\s*(?:#{1,6}\s|<!--|<a\s|---\s*$)', line)
        or re.match(r'^\s*>\s*(?:PDF\b|OCR\b|第[一二三四五六七八九十百零〇\d]+篇|源[：:]|页码关系[：:]|逐页\s*OCR|这是检索底稿|主标签[：:]|做完题再看)', line, re.I)
        or re.match(r'^\s*\*\*PDF\s', line, re.I)
        or re.match(r'^\s*(?:title|source_pdf|source_pages|conversion|page_anchor):', line)))


def markdown_chunks(file, text, size=900, overlap=120):
    """Exact line ranges; a PDF-page heading starts a new chunk and remains inherited."""
    lines = text.splitlines()
    stack, pages, buf, count = [], [], [], 0
    result = []
    def emit():
        if not buf:
            return
        body = '\n'.join(s for _, s in buf)
        if not substantive_text(body).strip():
            return
        result.append({'file': file, 'heading': ' > '.join(h for _, h in stack) or Path(file).stem,
                       'start': buf[0][0], 'end': buf[-1][0], 'text': body,
                       'kind': 'md', 'pages': list(pages)})
    for n, line in enumerate(lines, 1):
        h = re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
        marker = pdf_pages(line) if re.match(r'^\s*(?:>|<!--|\*\*PDF|#{1,6}\s.*PDF)', line, re.I) else []
        if marker and marker != pages:
            emit(); buf, count = [], 0
            pages = marker
        if h:
            emit(); buf, count = [], 0
            level = len(h[1])
            # OCR may mark a new Part as H2 beneath the previous chapter's H1.
            # A visible part boundary is explicit evidence to discard that stale ancestry.
            if re.match(r'^第[一二三四五六七八九十百零〇\d]+篇', h[2]):
                stack = []; level = 1
            stack = [(depth, title) for depth, title in stack if depth < level]
            stack.append((level, h[2]))
        if re.match(r'^\s*(?:!\[.*?\]\(.*\)|<a\s+id=.*|<!--.*-->)\s*$', line):
            continue
        if count + len(line) > size and buf:
            emit()
            tail, total = [], 0
            for entry in reversed(buf):
                if total + len(entry[1]) > overlap:
                    break
                tail.insert(0, entry); total += len(entry[1]) + 1
            buf, count = tail, total
        # An unusually long source line is kept intact so citations stay truthful.
        buf.append((n, line)); count += len(line) + 1
    emit()
    return result


def resolve_original(root, source_file, meta):
    """Resolve only explicit local PDF references; do not guess from similar names."""
    source = root / source_file
    references = [(source.parent, meta.get('original_source', ''))]
    for parent in source.parents:
        if not parent.is_relative_to(root):
            break
        raw = parent / '01_原料_OCR.md'
        if raw.is_file() and raw != source:
            preamble = raw.read_text(encoding='utf-8-sig')[:4000]
            references.append((parent, metadata(source_file, preamble)['original_source']))
    for base, reference in references:
        if not reference:
            continue
        ref = Path(reference)
        choices = [ref] if ref.is_absolute() else [parent / ref for parent in (base, *base.parents) if parent.is_relative_to(root)]
        for candidate in choices:
            resolved = candidate.resolve()
            if resolved.is_relative_to(root) and resolved.is_file() and resolved.suffix.lower() == '.pdf':
                return resolved
    return None


def eligible(file):
    p = Path(file)
    if SUBJECTS and p.parts[0] not in SUBJECTS:
        return False
    if any(x.startswith(('_备份', '_废弃', '.')) for x in p.parts):
        return False
    if any(x in p.parts for x in ('01_播客', '_raw', 'images', '原始文件', 'skills')):
        return False
    return not any(x in p.name for x in ('AGENTS', 'README', '阅读说明', '使用说明', '工作笔记', '交接', '模板'))


def build(kb=DEFAULT_KB, db=None):
    kb = Path(kb)
    config_path = kb / 'config.json'
    if not config_path.is_file():
        raise ValueError('Initialize config.json with an explicit data root before indexing')
    selected = json.loads(config_path.read_text(encoding='utf-8-sig'))
    if not isinstance(selected.get('root'), str) or not selected['root'].strip():
        raise ValueError('config.json must select a data root')
    engine = load_engine(kb); cfg = load_hooks(engine.load_config())
    root = Path(cfg['root']).resolve(); db = Path(db or kb / 'store' / 'fresh_fts.sqlite3')
    files = [p for p in engine.discover_files(cfg) if eligible(p.relative_to(root).as_posix())]
    manifest = {}
    for p in files:
        st = p.stat()
        manifest[p.relative_to(root).as_posix()] = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
                                                  'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if db.exists():
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                old = json.loads(conn.execute('SELECT value FROM meta').fetchone()[0])
            if old.get('manifest') == manifest and old.get('code_hash') == code_hash:
                return {**{k: v for k, v in old.items() if k != 'manifest'}, 'unchanged': True}
        except (sqlite3.Error, ValueError, TypeError):
            pass
    db.parent.mkdir(parents=True, exist_ok=True)
    temp = db.with_name(db.name + '.building-' + str(os.getpid()))
    if temp.exists():
        raise RuntimeError('Build temporary file already exists')
    conn = sqlite3.connect(temp)
    try:
        conn.executescript('''CREATE TABLE chunks(id INTEGER PRIMARY KEY, file TEXT, payload TEXT);
            CREATE VIRTUAL TABLE words USING fts5(title, body, tokenize='unicode61');
            CREATE VIRTUAL TABLE bigrams USING fts5(title, body, tokenize='unicode61');
            CREATE TABLE meta(value TEXT);''')
        n, per_file, seen, original_hashes = 0, {}, set(), {}
        for p in files:
            rel = p.relative_to(root).as_posix()
            if p.suffix.lower() == '.md':
                text = p.read_text(encoding='utf-8-sig')
                chunks = markdown_chunks(rel, text)
            else:
                chunks = engine.chunk_file(p, cfg)
                text = '\n'.join(c['text'] for c in chunks[:2])
            meta = metadata(rel, text[:4000])
            original = resolve_original(root, rel, meta)
            if original:
                if original not in original_hashes:
                    original_hashes[original] = hashlib.sha256(original.read_bytes()).hexdigest()
                meta.update(original_source=original.relative_to(root).as_posix(),
                            original_source_sha256=original_hashes[original])
            count = 0
            for c in chunks:
                digest = hashlib.sha256(re.sub(r'\s+', '', c['text']).encode()).hexdigest()
                # Deduplicate within subject, retaining independent subjects and references.
                key = (rel.split('/')[0], digest)
                if key in seen:
                    continue
                seen.add(key); n += 1; count += 1
                pages = c.get('pages') or re.findall(r'<!--\s*p\.(\d+)\s*-->', c['text'])
                c.update(meta); c.update(id='fresh-' + str(n), pages=pages,
                                         content_hash=digest, source_sha256=manifest[rel]['sha256'])
                title = Path(rel).stem + ' ' + c['heading']
                conn.execute('INSERT INTO chunks VALUES(?,?,?)', (n, rel, json.dumps(c, ensure_ascii=False)))
                conn.execute('INSERT INTO words(rowid,title,body) VALUES(?,?,?)',
                             (n, ' '.join(terms(title)), ' '.join(terms(c['text']))))
                conn.execute('INSERT INTO bigrams(rowid,title,body) VALUES(?,?,?)',
                             (n, ' '.join(grams(title)), ' '.join(grams(c['text']))))
            per_file[rel] = count
        meta = {'schema': SCHEMA, 'root': str(root), 'built_at': datetime.now(ZoneInfo('Asia/Singapore')).isoformat(),
                'files': sum(v > 0 for v in per_file.values()), 'chunks': n, 'dim': 0,
                'engine': 'fts5_word_bigram_rrf', 'embed_model': 'none (CPU lexical)',
                'code_hash': code_hash, 'manifest': manifest,
                'new_archive_files': sum(bool(ARCHIVE_MARKER) and ARCHIVE_MARKER in f and per_file[f] > 0 for f in per_file),
                'file_chunks': per_file}
        conn.execute('INSERT INTO meta VALUES(?)', (json.dumps(meta, ensure_ascii=False),))
        conn.execute("INSERT INTO words(words) VALUES('optimize')")
        conn.execute("INSERT INTO bigrams(bigrams) VALUES('optimize')")
        conn.commit()
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Index integrity check failed')
        conn.close()
        # Refuse a mixed snapshot if a source changed while it was being indexed.
        for file, sig in manifest.items():
            st = (root / file).stat()
            if st.st_mtime_ns != sig['mtime_ns'] or st.st_size != sig['size']:
                raise RuntimeError('Source changed during indexing: ' + file)
        os.replace(temp, db)
        return {k: v for k, v in meta.items() if k not in ('manifest', 'file_chunks')}
    finally:
        conn.close()
        if temp.exists():
            temp.unlink()


class FreshRetriever:
    def __init__(self, cfg=None, kb=DEFAULT_KB, db=None):
        self.kb = Path(kb); self.db = Path(db or self.kb / 'store' / 'fresh_fts.sqlite3')
        self.cfg = cfg if cfg is not None else load_engine(self.kb).load_config()
        self.subjects = configured_subjects(self.cfg)
        self._cache_stamp, self.meta = None, {}
        self._refresh()

    def connect(self):
        return sqlite3.connect(self.db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)

    def _refresh(self):
        st = self.db.stat(); stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._cache_stamp:
            with closing(self.connect()) as conn:
                self.meta = json.loads(conn.execute('SELECT value FROM meta').fetchone()[0])
            if self.meta.get('schema') != SCHEMA or Path(self.meta['root']).resolve() != Path(self.cfg['root']).resolve():
                raise ValueError('Index schema/root mismatch')
            self._cache_stamp = stamp
        return self.meta

    @property
    def chunks(self):
        with closing(self.connect()) as conn:
            return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM chunks')]

    @property
    def store(self):
        self._refresh()
        return SimpleNamespace(meta={k: v for k, v in self.meta.items() if k not in ('manifest', 'file_chunks')})

    def search(self, query, k=None, alpha=None, path_filter=None, max_per_file=None, reserve=None):
        self._refresh(); query = str(query).strip()[:1400]
        k = max(1, min(int(k or 8), 30)); cap = int(max_per_file or 2)
        if not query:
            return []
        explicit = [s for s in self.subjects if s in query]
        if not path_filter and len(explicit) == 1:
            path_filter = explicit[0]
        # Subject selects the corpus; omit it from ranking to avoid matching every page.
        content_query = query
        for s in explicit:
            content_query = content_query.replace(s, ' ')
        qterms = list(dict.fromkeys(terms(content_query)))[:48]
        qgrams = list(dict.fromkeys(g for t in qterms for g in grams(t)))[:80]
        # Pure scope requests still need to find their named documents.
        if not qterms:
            qterms = terms(query)
        requested_kind = ('课程考试大纲' if re.search(r'考纲|考试大纲|考试题型|试卷结构', query)
                          else '课程教学大纲' if '教学大纲' in query
                          else '教师教学目标（非整门正式大纲）' if '教学目标' in query else None)
        scores, candidates = {}, {}
        where, params = (' AND c.file LIKE ? ESCAPE "\\"', ['%' + path_filter.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%']) if path_filter else ('', [])
        with closing(self.connect()) as conn:
            for table, tokens, weight in [('words', qterms, 1.0), ('bigrams', qgrams, .65)]:
                if not tokens:
                    continue
                expression = ' OR '.join('"' + t.replace('"', '""') + '"' for t in tokens)
                rows = conn.execute(f'''SELECT {table}.rowid,c.payload,bm25({table},3.0,1.0)
                    FROM {table} JOIN chunks c ON c.id={table}.rowid
                    WHERE {table} MATCH ? {where} ORDER BY bm25({table},3.0,1.0) LIMIT 120''',
                    [expression] + params).fetchall()
                for rank, (idx, payload, _) in enumerate(rows, 1):
                    candidates[idx] = json.loads(payload)
                    scores[idx] = scores.get(idx, 0) + weight / (60 + rank)
                if requested_kind:
                    # Search the requested source tier before candidate truncation;
                    # a short outline must not be crowded out by hundreds of textbook hits.
                    priority = conn.execute(f'''SELECT {table}.rowid,c.payload
                        FROM {table} JOIN chunks c ON c.id={table}.rowid
                        WHERE {table} MATCH ? {where} AND json_extract(c.payload,'$.source_kind')=?
                        ORDER BY bm25({table},3.0,1.0) LIMIT 40''',
                        [expression] + params + [requested_kind]).fetchall()
                    for rank, (idx, payload) in enumerate(priority, 1):
                        candidates[idx] = json.loads(payload)
                        scores[idx] = scores.get(idx, 0) + weight / (60 + rank)
        syllabus = bool(re.search(r'大纲|考纲|教学目标|掌握|熟悉|了解|考试范围|考试题型', query))
        for idx, c in candidates.items():
            body = (c['heading'] + '\n' + c['text']).lower()
            matches = sum(t in body for t in qterms)
            coverage = matches / max(1, len(qterms))
            scores[idx] *= .35 + .65 * coverage
            prose = substantive_text(c['text']).lower()
            # Matching only a filename/heading is weaker evidence than matching
            # the actual answer passage (for every subject, not a topic keyword fix).
            prose_coverage = sum(t in prose for t in qterms) / max(1, len(qterms))
            scores[idx] *= .5 + .5 * prose_coverage
            if syllabus and '大纲' in c['source_kind'] and '非' not in c['source_kind']:
                scores[idx] *= 1.3
            elif not syllabus and ('章节目录' in c['source_kind'] or '学习索引' in c['source_kind'] or '资料质检' in c['source_kind']):
                scores[idx] *= .15
            if not substantive_text(c['text']).strip():
                scores[idx] = 0
            if requested_kind:
                scores[idx] *= 3 if c['source_kind'] == requested_kind else .5
            # Reject unrelated bigram fragments: exact query term evidence is required.
            if not matches:
                scores[idx] = 0
        result, per_file, seen = [], Counter(), set()
        root = Path(self.cfg['root']).resolve()
        for idx in sorted(scores, key=scores.get, reverse=True):
            c = candidates[idx]
            if scores[idx] <= 0 or (result and scores[idx] < result[0]['score'] * .22):
                continue
            if per_file[c['file']] >= cap or c['content_hash'] in seen:
                continue
            target = (root / c['file']).resolve()
            if not target.is_relative_to(root):
                continue
            try:
                st = target.stat(); sig = self.meta['manifest'][c['file']]
                if st.st_mtime_ns != sig['mtime_ns'] or st.st_size != sig['size']:
                    continue  # Changed/deleted sources cannot be presented as current citations.
            except (OSError, KeyError):
                continue
            c.update(score=scores[idx], cos=None, bm25=None)
            result.append(c); per_file[c['file']] += 1; seen.add(c['content_hash'])
            if len(result) >= k:
                break
        # Preserve a relevant old-foundation reserve without injecting off-topic material.
        if result and not path_filter and not any('原始档案' in c['file'] for c in result):
            for idx in sorted(scores, key=scores.get, reverse=True):
                c = candidates[idx]
                if '原始档案' in c['file'] and scores[idx] >= result[0]['score'] * .68:
                    target = root / c['file']; sig = self.meta['manifest'][c['file']]
                    if target.exists() and target.stat().st_mtime_ns == sig['mtime_ns'] and target.stat().st_size == sig['size']:
                        c.update(score=scores[idx], cos=None, bm25=None)
                        if len(result) == k:
                            result[-1] = c
                        else:
                            result.append(c)
                    break
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['index', 'search', 'stats'])
    parser.add_argument('query', nargs='?', default='')
    parser.add_argument('--kb', type=Path, default=DEFAULT_KB)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--path', dest='path_filter')
    parser.add_argument('-k', type=int, default=8)
    args = parser.parse_args()
    started = time.perf_counter()
    if args.command == 'index':
        data = build(args.kb, args.db)
    else:
        r = FreshRetriever(kb=args.kb, db=args.db)
        data = r.store.meta if args.command == 'stats' else {'results': r.search(args.query, k=args.k, path_filter=args.path_filter), 'meta': r.store.meta}
    data['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
