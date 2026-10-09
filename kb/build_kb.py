#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地检索知识库（Local RAG KB）—— 基于 Ollama 本地嵌入模型
================================================================
能力：
  1. 文档自动读取与结构化分块（md 按标题层级 / txt 按段落 / csv 按行组）
  2. 向量化存储（Ollama 嵌入 -> numpy 矩阵，L2 归一化）
  3. 混合语义检索（余弦相似度 + BM25 关键词，jieba 分词）
  4. 自然语言提问 -> 返回相关片段 + 来源标注（文件 / 标题路径 / 行号 / 相似度）
  5. 增量更新（manifest 记录 mtime+size+sha1，只重嵌变更文件，不重建整个索引）

用法：
  python build_kb.py index                 # 增量建库 / 更新
  python build_kb.py index --full          # 全量重建
  python build_kb.py index --strict        # 用 sha1 精确校验（默认 mtime+size 快路径）
  python build_kb.py search "问题" -k 8    # 纯检索，返回片段 + 来源
  python build_kb.py ask "问题" -k 6       # 检索 + 本地 LLM 生成带引用答案
  python build_kb.py stats                 # 索引概况
  python build_kb.py serve --port 8765     # 启动 Web 查询界面
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
KB_DIR = HERE
STORE_DIR = KB_DIR / "store"
CONFIG_PATH = KB_DIR / "config.json"

HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
IMG_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
MD_NOISE_RE = re.compile(r"^\s*(\[.*?\]\(.*?\)|!\[.*?\]\(.*?\)|<[^>]+>)\s*$")

DEFAULT_CONFIG = {
    "root": str(KB_DIR.parent),
    "include_ext": [".md", ".txt", ".csv"],
    "include_pdf": True,
    "skip_pdf_if_md_exists": True,
    "pdf_include_globs": ["*/PPT课件版/01-便携PDF/*"],
    # 排除格式模板与"规则类"文件：画像/提示词/技能说明是规则不是知识，
    # 进了检索池会被当成答案来源（见 05_给Cortex_Ollama的方法建议 2.3）。
    "exclude_globs": [
        "*/学习记录/模板_*",
        "之前学习的画像和知识/00_README*",
        "之前学习的画像和知识/01_*",
        "之前学习的画像和知识/04_*",
        "之前学习的画像和知识/05_*",
        "之前学习的画像和知识/06_*",
        "之前学习的画像和知识/AGENTS.md",
        "之前学习的画像和知识/skills/*",
        "AGENTS.md",
        "00-Paper2Galgame学习资料-质量验收标准.md",
    ],
    "exclude_dirs": ["kb", ".workbuddy-ai", ".git", "node_modules", "__pycache__"],
    "embed_model": "bge-m3",
    "ollama_host": "http://127.0.0.1:11434",
    "llm_model": "qwen3:14b",
    "llm_api_base": "",
    "llm_api_key": "",
    "chunk": {"max_chars": 800, "min_chars": 150, "overlap": 120,
              "csv_rows": 12, "merge_max_chars": 1200},
    "retrieval": {
        "top_k": 8,
        "alpha": 0.62,
        "max_per_file": 3,
        # 保底召回：保证 top-k 里至少出现 N 块来自指定路径（默认旧基石），
        # 否则模型只讲新课、接不回旧课。
        "reserve": [{"path": "原始档案", "min": 1, "min_score_ratio": 0.68}],
    },
}


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------- 工具

def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def norm_ws(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\u00a0", " ").replace("\ufeff", "")
    return s


def nfc(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", s)


_JIEBA = None


def tokenize(text: str) -> list[str]:
    """中英混合分词，用于 BM25。"""
    global _JIEBA
    if _JIEBA is None:
        import jieba
        jieba.setLogLevel(60)
        _JIEBA = jieba
    out = []
    for tok in _JIEBA.cut_for_search(text):
        tok = tok.strip().lower()
        if not tok:
            continue
        if len(tok) == 1 and not tok.isalnum():
            continue
        if tok.isascii() and len(tok) == 1 and not tok.isdigit():
            continue
        out.append(tok)
    return out


# ---------------------------------------------------------------- 文档读取

def read_text_file(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
        try:
            return norm_ws(path.read_text(encoding=enc))
        except (UnicodeDecodeError, UnicodeError):
            continue
    return norm_ws(path.read_text(encoding="utf-8", errors="replace"))


def read_pdf_file(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    parts = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        t = norm_ws(t).strip()
        if t:
            parts.append(f"\n<!-- p.{i + 1} -->\n{t}")
    return "\n".join(parts)


def discover_files(cfg: dict) -> list[Path]:
    import fnmatch

    root = Path(cfg["root"]).resolve()
    exts = {e.lower() for e in cfg["include_ext"]}
    if cfg.get("include_pdf"):
        exts.add(".pdf")
    pdf_globs = cfg.get("pdf_include_globs") or []
    excl_globs = cfg.get("exclude_globs") or []
    excl = set(cfg["exclude_dirs"])
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excl and not d.startswith(".")]
        dp = Path(dirpath)
        for fn in filenames:
            p = dp / fn
            if p.suffix.lower() not in exts:
                continue
            rel = p.relative_to(root).as_posix()
            if excl_globs and any(fnmatch.fnmatch(rel, g) for g in excl_globs):
                continue
            if p.suffix.lower() == ".pdf":
                if cfg.get("skip_pdf_if_md_exists") and (dp / (p.stem + ".md")).exists():
                    continue
                if pdf_globs and not any(fnmatch.fnmatch(rel, g) for g in pdf_globs):
                    continue
            found.append(p)
    return sorted(found)


# ---------------------------------------------------------------- 分块

def parent_of(heading: str) -> str:
    parts = [p.strip() for p in heading.split(">")]
    return " > ".join(parts[:-1]) if len(parts) > 1 else ""


def common_prefix(a: str, b: str) -> str:
    pa = [p.strip() for p in a.split(">")]
    pb = [p.strip() for p in b.split(">")]
    out = []
    for x, y in zip(pa, pb):
        if x == y:
            out.append(x)
        else:
            break
    return " > ".join(out)


def merge_short_chunks(chunks: list[dict], min_chars: int, merge_max: int) -> list[dict]:
    """把同一父章节下的碎片块合并，减少 <min_chars 的噪声块。行号范围随之扩展。"""
    out: list[dict] = []
    for c in chunks:
        if out:
            prev = out[-1]
            if (
                prev["file"] == c["file"]
                and parent_of(prev["heading"]) == parent_of(c["heading"])
                and (len(prev["text"]) < min_chars or len(c["text"]) < min_chars)
                and len(prev["text"]) + len(c["text"]) + 2 <= merge_max
            ):
                prev["text"] = prev["text"] + "\n\n" + c["text"]
                prev["end"] = max(prev["end"], c["end"])
                cp = common_prefix(prev["heading"], c["heading"])
                if cp:
                    prev["heading"] = cp
                continue
        out.append(dict(c))
    return out


def split_md_sections(text: str) -> list[dict]:
    """按 markdown 标题层级切 section，返回 [{path:[...], start:int, end:int, lines:[...]}]"""
    lines = text.split("\n")
    sections: list[dict] = []
    stack: list[str] = []
    cur = {"path": [], "start": 1, "lines": []}
    for i, ln in enumerate(lines, 1):
        m = HEAD_RE.match(ln)
        if m:
            if cur["lines"] and any(x.strip() for x in cur["lines"]):
                cur["end"] = i - 1
                sections.append(cur)
            elif not cur["lines"] and cur["path"]:
                cur["end"] = i - 1
                sections.append(cur)
            lvl = len(m.group(1))
            title = m.group(2).strip()
            stack = stack[: lvl - 1] + [title]
            cur = {"path": list(stack), "start": i, "lines": [], "header": ln}
        else:
            cur["lines"].append(ln)
    if cur["lines"] and any(x.strip() for x in cur["lines"]):
        cur["end"] = len(lines)
        sections.append(cur)
    return sections


def pack_paragraphs(lines: list[str], max_chars: int, overlap: int) -> list[str]:
    """把 section 内文本按段落打包成 <= max_chars 的块，带 overlap。"""
    paras: list[str] = []
    buf: list[str] = []
    for ln in lines:
        if ln.strip() == "":
            if buf:
                paras.append("\n".join(buf))
                buf = []
            paras.append("")
        else:
            buf.append(ln.rstrip())
    if buf:
        paras.append("\n".join(buf))

    chunks: list[str] = []
    cur = ""
    for para in paras:
        if para == "":
            if cur:
                cur += "\n"
            continue
        if not cur:
            cur = para
        elif len(cur) + len(para) + 1 <= max_chars:
            cur += "\n" + para
        else:
            chunks.append(cur.strip())
            tail = cur[-overlap:] if overlap > 0 else ""
            cur = (tail + "\n" + para) if tail else para
    if cur.strip():
        chunks.append(cur.strip())

    # 超长单段（如无换行的长文/表格）硬切
    out: list[str] = []
    for c in chunks:
        if len(c) <= max_chars * 1.6:
            out.append(c)
            continue
        step = max_chars - overlap if max_chars > overlap else max_chars
        for i in range(0, len(c), step):
            piece = c[i : i + max_chars].strip()
            if piece:
                out.append(piece)
    return out


def chunk_markdown(rel: str, text: str, cfg: dict) -> list[dict]:
    ck = cfg["chunk"]
    out: list[dict] = []
    for sec in split_md_sections(text):
        body_lines = list(sec["lines"])
        if sec.get("header"):
            body_lines = [sec["header"]] + body_lines
        # 去掉纯图片行（jpg 路径对检索无信息量，只占字符）
        body_lines = [ln for ln in body_lines if not IMG_ONLY_RE.match(ln)]
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        heading = " > ".join(sec["path"]) if sec["path"] else Path(rel).stem
        for piece in pack_paragraphs(body_lines, ck["max_chars"], ck["overlap"]):
            out.append(
                {
                    "file": rel,
                    "heading": heading,
                    "start": sec["start"],
                    "end": sec["end"],
                    "text": piece,
                    "kind": "md",
                }
            )
    return merge_short_chunks(out, ck["min_chars"], ck.get("merge_max_chars", 1200))


def pack_lines_with_pos(lines: list[str], max_chars: int, overlap: int
                        ) -> list[tuple[int, int, str]]:
    """按行打包，返回 (起始行号, 结束行号, 文本)。行号从 1 起，用于来源定位。"""
    out: list[tuple[int, int, str]] = []
    buf: list[str] = []
    buf_len = 0
    start = 1
    for i, ln in enumerate(lines, 1):
        if not ln.strip():
            if buf:
                buf.append("")
            continue
        if buf_len + len(ln) + 1 > max_chars and buf:
            out.append((start, i - 1, "\n".join(buf).strip()))
            keep: list[str] = []
            acc = 0
            for prev in reversed(buf):
                if acc + len(prev) > overlap:
                    break
                keep.insert(0, prev)
                acc += len(prev) + 1
            buf = keep
            buf_len = acc
            start = max(1, i - len(keep))
        buf.append(ln)
        buf_len += len(ln) + 1
    if buf and any(x.strip() for x in buf):
        out.append((start, len(lines), "\n".join(buf).strip()))
    return out


def chunk_plain(rel: str, text: str, cfg: dict) -> list[dict]:
    ck = cfg["chunk"]
    lines = text.split("\n")
    out = []
    for s, e, piece in pack_lines_with_pos(lines, ck["max_chars"], ck["overlap"]):
        out.append(
            {
                "file": rel,
                "heading": Path(rel).stem,
                "start": s,
                "end": e,
                "text": piece,
                "kind": "txt",
            }
        )
    return merge_short_chunks(out, ck["min_chars"], ck.get("merge_max_chars", 1200))


def chunk_csv(rel: str, text: str, cfg: dict) -> list[dict]:
    """csv：首行作表头，按 N 行一组打包，表头随块携带。"""
    rows = [r for r in text.split("\n") if r.strip()]
    if not rows:
        return []
    header = rows[0]
    body = rows[1:]
    n = max(1, int(cfg["chunk"].get("csv_rows", 12)))
    out = []
    for i in range(0, len(body), n):
        group = body[i : i + n]
        out.append(
            {
                "file": rel,
                "heading": f"{Path(rel).stem} 第{i + 1}-{i + len(group)}行",
                "start": i + 2,
                "end": i + 1 + len(group),
                "text": header + "\n" + "\n".join(group),
                "kind": "csv",
            }
        )
    return out


def chunk_file(path: Path, cfg: dict) -> list[dict]:
    root = Path(cfg["root"]).resolve()
    rel = path.resolve().relative_to(root).as_posix()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = read_pdf_file(path)
        if not text.strip():
            return []
        out = chunk_plain(rel, text, cfg)
        stem = Path(rel).stem
        for c in out:
            c["kind"] = "pdf"
            # read_pdf_file 每页插入 <!-- p.N -->，据此给出页码范围
            pages = re.findall(r"<!--\s*p\.(\d+)\s*-->", c["text"])
            c["heading"] = f"{stem} (p.{pages[0]}-{pages[-1]})" if pages else stem
        return out
    text = read_text_file(path)
    if not text.strip():
        return []
    if suffix == ".md":
        return chunk_markdown(rel, text, cfg)
    if suffix == ".csv":
        return chunk_csv(rel, text, cfg)
    return chunk_plain(rel, text, cfg)


# ---------------------------------------------------------------- Ollama 客户端

class Ollama:
    """直连 Ollama 的客户端。

    注意：必须绕开环境变量里的 HTTP_PROXY —— 本机代理会把每个嵌入请求转发一遍，
    几千次请求后代理过载、连接被 reset（ConnectionResetError / 连接堆积到队列上限）。
    用 http.client 直接建连，既不走代理，又能复用连接（避免 TIME_WAIT 堆积）。
    """

    def __init__(self, host: str, model: str):
        self.host = host.rstrip("/")
        self.model = model
        u = urllib.parse.urlsplit(self.host)
        self._host = u.hostname or "127.0.0.1"
        self._port = u.port or 11434
        self._conn = None
        self.cpu = False     # 由 ensure_ollama 按 config.json 的 embed_cpu 打开

    def _close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: int = 900, retries: int = 3) -> dict:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                if self._conn is None:
                    self._conn = http.client.HTTPConnection(
                        self._host, self._port, timeout=timeout
                    )
                body = json.dumps(payload).encode("utf-8") if payload is not None else None
                headers = {"Content-Type": "application/json"} if body else {}
                self._conn.request(method, path, body=body, headers=headers)
                resp = self._conn.getresponse()
                data = resp.read()
                if resp.status != 200:
                    raise RuntimeError(
                        f"HTTP {resp.status}: {data[:200].decode('utf-8', 'replace')}"
                    )
                return json.loads(data.decode("utf-8"))
            except Exception as e:
                last = e
                self._close()
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Ollama 请求失败 {path}: {last}")

    def health(self) -> list[str]:
        data = self._request("GET", "/api/tags", timeout=10)
        return [m["name"] for m in data.get("models", [])]

    def embed(self, texts: list[str], batch: int = 64) -> list[list[float]]:
        vecs: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = texts[i : i + batch]
            req = {"model": self.model, "input": chunk}
            if self.cpu:                     # 2026-09-23：嵌入只用 CPU，不占显卡
                req["options"] = {"num_gpu": 0}
            r = self._request("POST", "/api/embed", req)
            vecs.extend(r["embeddings"])
        return vecs

    def chat(self, messages: list[dict], temperature: float = 0.3,
             num_ctx: int = 16384, think: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": think,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        r = self._request("POST", "/api/chat", payload)
        return (r.get("message") or {}).get("content", "").strip()


class CloudLLM:
    """OpenAI 兼容接口（可选，配置 llm_api_base + llm_api_key 后启用）。"""

    def __init__(self, base: str, key: str, model: str):
        self.base = base.rstrip("/")
        self.key = key
        self.model = model

    def chat(self, messages: list[dict], temperature: float = 0.3, **_) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()


def make_llm(cfg: dict):
    if cfg.get("llm_api_base") and cfg.get("llm_api_key"):
        return CloudLLM(cfg["llm_api_base"], cfg["llm_api_key"], cfg["llm_model"])
    return Ollama(cfg["ollama_host"], cfg["llm_model"])


def ensure_ollama(cfg: dict) -> Ollama:
    """返回可用的 Ollama 客户端；服务没起就尝试自动拉起。"""
    o = Ollama(cfg["ollama_host"], cfg["embed_model"])
    o.cpu = bool(cfg.get("embed_cpu"))
    try:
        o.health()
        return o
    except Exception:
        pass

    exe = shutil.which("ollama")
    if not exe:
        for cand in (
            os.path.expanduser(r"~\AppData\Local\Programs\Ollama\ollama.exe"),
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"),
        ):
            if os.path.exists(cand):
                exe = cand
                break
    if exe:
        print("[Ollama] 服务未运行，正在拉起…", flush=True)
        try:
            subprocess.Popen(
                [exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=0x00000008,  # DETACHED_PROCESS
            )
        except Exception as e:
            print(f"[Ollama] 拉起失败：{e}")
        for _ in range(40):
            time.sleep(1)
            try:
                o.health()
                print("[Ollama] 就绪", flush=True)
                return o
            except Exception:
                continue
    raise SystemExit(
        "Ollama 未运行。请先启动 Ollama（双击 Ollama 托盘程序，或执行 ollama serve）后重试。"
    )


# ---------------------------------------------------------------- 云端嵌入（2026-09-23 新增）
#
# 目的：嵌入不占本机显卡。硅基流动的 BAAI/bge-m3 与本地 Ollama bge-m3 是同一模型、同为 1024 维，
# 旧向量可以直接复用，不触发全量重建（meta 里的 embed_model 仍记为 "bge-m3"）。
# 密钥放 kb/providers.json 的 providers.siliconflow.key；留空 = 不启用，完全走原来的 Ollama。
# 云端任何一次失败（断网 / 额度 / 限流）都会自动退回本地 Ollama，不会让检索挂掉。

class CloudEmbed:
    MAX_BATCH = 32          # 硅基流动 bge-m3 单次最多 32 条

    def __init__(self, base: str, key: str, model: str, max_chars: int = 6000):
        self.base = base.rstrip("/")
        self.key = key
        self.model = model
        self.max_chars = max_chars
        # 国内直连，显式不走系统代理（和 Ollama 客户端同样的理由）
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, texts: list[str]) -> list[list[float]]:
        req = urllib.request.Request(
            f"{self.base}/embeddings",
            data=json.dumps({"model": self.model, "input": texts,
                             "encoding_format": "float"}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"},
        )
        with self._opener.open(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
        items = sorted(data["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in items]

    def _post_retry(self, texts: list[str]) -> list[list[float]]:
        limit = self.max_chars
        last: Exception | None = None
        for attempt in range(5):
            try:
                return self._post([t[:limit] for t in texts])
            except urllib.error.HTTPError as e:
                last = e
                body = e.read()[:300].decode("utf-8", "replace")
                if e.code in (400, 413) and limit > 400:
                    limit //= 2            # 超长：截短再试
                    continue
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 * (attempt + 1))   # 限流 / 服务端抖动
                    continue
                raise RuntimeError(f"云端嵌入 HTTP {e.code}: {body}")
            except Exception as e:          # 断网 / 连不上：只再试一次就退本地，别让检索干等
                last = e
                if attempt >= 1:
                    break
                time.sleep(1.0)
        raise RuntimeError(f"云端嵌入失败：{last}")

    def health(self) -> list[str]:
        self._post(["ping"])
        return [self.model]

    def embed(self, texts: list[str], batch: int = 64) -> list[list[float]]:
        step = min(batch, self.MAX_BATCH)
        out: list[list[float]] = []
        for i in range(0, len(texts), step):
            out.extend(self._post_retry(texts[i : i + step]))
        return out


class FallbackEmbed:
    """先云端，失败退本地 Ollama。label 供日志 / 状态页显示当前实际用的是谁。"""

    def __init__(self, cloud: CloudEmbed, cfg: dict):
        self.cloud = cloud
        self.cfg = cfg
        self._local: Ollama | None = None
        self.label = f"云端 {cloud.model}"

    def _get_local(self) -> Ollama:
        if self._local is None:
            self._local = ensure_ollama(self.cfg)
        return self._local

    def embed(self, texts: list[str], batch: int = 64) -> list[list[float]]:
        try:
            v = self.cloud.embed(texts, batch)
            self.label = f"云端 {self.cloud.model}"
            return v
        except Exception as e:
            print(f"[嵌入] 云端不可用（{e}），退回本地 Ollama", flush=True)
            self.label = "本地 Ollama（云端退避）"
            return self._get_local().embed(texts, batch)


def load_cloud_embed(cfg: dict) -> CloudEmbed | None:
    p = KB_DIR / "providers.json"
    try:
        prov = json.loads(p.read_text(encoding="utf-8"))["providers"]
    except Exception:
        return None
    sf = prov.get("siliconflow") or {}
    key = (sf.get("key") or "").strip()
    if not key or sf.get("enabled") is False:
        return None
    return CloudEmbed(sf.get("base") or "https://api.siliconflow.cn/v1", key,
                      sf.get("embed_model") or "BAAI/bge-m3")


def ensure_embedder(cfg: dict):
    """检索和建索引统一从这里拿嵌入器：配了云端就云端优先，否则原样走 Ollama。"""
    if cfg.get("embed_model") == "bge-m3":      # 只有同模型才能和旧向量混用
        cloud = load_cloud_embed(cfg)
        if cloud is not None:
            print(f"[嵌入] 使用云端 {cloud.model}（失败自动退回本地）", flush=True)
            return FallbackEmbed(cloud, cfg)
    return ensure_ollama(cfg)


# ---------------------------------------------------------------- 存储层

class Store:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.chunks: list[dict] = []
        self.vecs = None
        self.manifest: dict = {}
        self.meta: dict = {}

    # --- 路径
    @property
    def vec_path(self) -> Path:
        return STORE_DIR / "vectors.npy"

    @property
    def chunk_path(self) -> Path:
        return STORE_DIR / "chunks.jsonl"

    @property
    def manifest_path(self) -> Path:
        return STORE_DIR / "manifest.json"

    @property
    def meta_path(self) -> Path:
        return STORE_DIR / "meta.json"

    def exists(self) -> bool:
        return self.vec_path.exists() and self.chunk_path.exists()

    def load(self) -> "Store":
        import numpy as np

        STORE_DIR.mkdir(parents=True, exist_ok=True)
        if self.chunk_path.exists():
            self.chunks = []
            with self.chunk_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.chunks.append(json.loads(line))
        if self.vec_path.exists():
            self.vecs = np.load(self.vec_path)
        else:
            self.vecs = np.zeros((0, 0), dtype="float32")
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if self.meta_path.exists():
            self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        return self

    def save(self) -> None:
        import numpy as np

        STORE_DIR.mkdir(parents=True, exist_ok=True)
        with self.chunk_path.open("w", encoding="utf-8", newline="\n") as f:
            for c in self.chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        if self.vecs is None:
            self.vecs = np.zeros((0, 0), dtype="float32")
        np.save(self.vec_path, self.vecs.astype("float32"))
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def l2norm(mat):
    import numpy as np

    if mat.size == 0:
        return mat
    n = np.linalg.norm(mat, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (mat / n).astype("float32")


# ---------------------------------------------------------------- 建库 / 增量更新

def file_sig(path: Path, strict: bool) -> dict:
    st = path.stat()
    sig = {"mtime": int(st.st_mtime), "size": st.st_size}
    if strict:
        sig["sha1"] = sha1_text(read_text_file(path) if path.suffix.lower() != ".pdf" else str(st.st_mtime))
    return sig


def cmd_index(cfg: dict, full: bool = False, strict: bool = False,
              limit: int | None = None) -> None:
    import numpy as np

    t0 = time.time()
    root = Path(cfg["root"]).resolve()
    files = discover_files(cfg)
    if limit:
        files = files[:limit]
    print(f"[扫描] {root}")
    print(f"[扫描] 命中 {len(files)} 个文档  (ext={cfg['include_ext']}, pdf={cfg['include_pdf']})")

    store = Store(cfg).load()
    model = cfg["embed_model"]

    # 嵌入模型变更 -> 必须全量重建（向量空间不同，不可混用）
    prev_model = (store.meta or {}).get("embed_model")
    if prev_model and prev_model != model and not full:
        print(f"[警告] 嵌入模型由 {prev_model} 变为 {model}，向量空间不一致，自动全量重建")
        full = True

    # 2026-09-23：内容级向量缓存。文件 mtime 变了但分块文本没变（整理目录、同步、复制都会改 mtime），
    # 就直接复用旧向量，不重新嵌入。只在同一嵌入模型下启用；全量重建不用缓存。
    vec_cache: dict[str, int] = {}
    cache_vecs = None
    if not full and store.vecs.size and len(store.chunks) == store.vecs.shape[0]:
        cache_vecs = store.vecs
        for i, c in enumerate(store.chunks):
            vec_cache.setdefault(sha1_text((c["heading"] + "\n" + c["text"])[:6000]), i)

    if full:
        store.chunks = []
        store.vecs = np.zeros((0, 0), dtype="float32")
        store.manifest = {}

    old_manifest = dict(store.manifest)
    rels = []
    for p in files:
        try:
            rels.append((p, p.resolve().relative_to(root).as_posix()))
        except ValueError:
            continue
    current = {rel for _, rel in rels}

    # 1) 判定变更
    changed: list[tuple[Path, str]] = []
    for p, rel in rels:
        sig = file_sig(p, strict)
        old = old_manifest.get(rel)
        if old and all(old.get(k) == sig.get(k) for k in sig):
            continue
        changed.append((p, rel))
    # --limit 是调试模式：只处理前 N 个文件，不判定删除（否则会误清索引）
    removed = set() if limit else {rel for rel in old_manifest if rel not in current}

    if not changed and not removed:
        print(f"[增量] 无变化，索引最新。共 {len(store.chunks)} 块。")
        return

    print(f"[增量] 新增/变更 {len(changed)} 个文件，删除 {len(removed)} 个文件")

    # 2) 保留未受影响文件的块
    stale = {rel for _, rel in changed} | removed
    keep_idx = [i for i, c in enumerate(store.chunks) if c["file"] not in stale]
    keep_chunks = [store.chunks[i] for i in keep_idx]
    keep_vecs = store.vecs[keep_idx] if store.vecs.size else np.zeros((0, 0), dtype="float32")
    print(f"[增量] 复用已有 {len(keep_chunks)} 块（未重新嵌入）")

    # 3) 重新分块 + 嵌入
    new_chunks: list[dict] = []
    per_file_chunks: dict[str, list[dict]] = {}
    for p, rel in changed:
        try:
            cs = chunk_file(p, cfg)
        except Exception as e:
            print(f"  ! 跳过 {rel}: {e}")
            continue
        for c in cs:
            c["id"] = f"c{len(new_chunks):06d}"
        per_file_chunks[rel] = cs
        new_chunks.extend(cs)

    payloads = [(c["heading"] + "\n" + c["text"])[:6000] for c in new_chunks]
    hit_rows: dict[int, int] = {}
    if vec_cache:
        for j, t in enumerate(payloads):
            r = vec_cache.get(sha1_text(t))
            if r is not None:
                hit_rows[j] = r
    miss = [j for j in range(len(new_chunks)) if j not in hit_rows]
    print(f"[分块] 共 {len(new_chunks)} 块：内容未变直接复用 {len(hit_rows)} 块，真正待嵌入 {len(miss)} 块",
          flush=True)
    vecs_list: list[list[float]] = []
    if new_chunks:
        miss_vecs: list[list[float]] = []
        if miss:
            ollama = ensure_embedder(cfg)
            BATCH = int(cfg.get("embed_batch", 64))
            t_emb = time.time()
            for i in range(0, len(miss), BATCH):
                payload = [payloads[j] for j in miss[i : i + BATCH]]
                miss_vecs.extend(ollama.embed(payload, batch=BATCH))
                done = min(i + BATCH, len(miss))
                if done % (BATCH * 2) == 0 or done == len(miss):
                    el = time.time() - t_emb
                    rate = done / el if el > 0 else 0
                    eta = (len(miss) - done) / rate if rate > 0 else 0
                    print(f"  · 嵌入 {done}/{len(miss)}  ({rate:.1f} 块/秒, 剩余 {eta/60:.1f} 分钟)",
                          flush=True)
        it = iter(miss_vecs)
        for j in range(len(new_chunks)):
            if j in hit_rows:
                vecs_list.append(cache_vecs[hit_rows[j]].tolist())
            else:
                vecs_list.append(next(it))

    new_vecs = l2norm(np.asarray(vecs_list, dtype="float32")) if vecs_list else np.zeros((0, 0), dtype="float32")

    # 4) 合并
    if keep_vecs.size and new_vecs.size:
        all_vecs = np.vstack([keep_vecs, new_vecs])
    elif keep_vecs.size:
        all_vecs = keep_vecs
    else:
        all_vecs = new_vecs
    all_chunks = keep_chunks + new_chunks

    # 重编号 id，保证与行序一致
    for i, c in enumerate(all_chunks):
        c["id"] = f"c{i:06d}"

    store.chunks = all_chunks
    store.vecs = all_vecs
    store.meta = {
        "embed_model": model,
        "dim": int(all_vecs.shape[1]) if all_vecs.size else 0,
        "chunks": len(all_chunks),
        "files": len(current),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 5) 更新 manifest（含未变文件的旧签名）
    manifest = {}
    for p, rel in rels:
        if rel in per_file_chunks:
            manifest[rel] = {**file_sig(p, strict), "chunks": len(per_file_chunks[rel])}
        elif rel in old_manifest:
            manifest[rel] = old_manifest[rel]
        else:
            manifest[rel] = {**file_sig(p, strict), "chunks": 0}
    store.manifest = manifest
    store.save()

    # 6) 清空 BM25 缓存（数据变了），随后预热，避免首次查询卡顿
    bm_cache = STORE_DIR / "bm25.json"
    if bm_cache.exists():
        bm_cache.unlink()
    try:
        rt = Retriever(cfg)
        t1 = time.time()
        rt._load_bm25()
        print(f"[BM25] 关键词索引就绪（{len(rt.chunks)} 块，{time.time()-t1:.1f}s）")
    except Exception as e:
        print(f"[BM25] 预热跳过：{e}")

    dt = time.time() - t0
    print(f"[完成] 索引 {len(all_chunks)} 块 / {len(current)} 文件，用时 {dt:.1f}s")
    print(f"[存储] {STORE_DIR}")


# ---------------------------------------------------------------- 检索

class Retriever:
    def __init__(self, cfg: dict):
        import numpy as np

        self.cfg = cfg
        self.store = Store(cfg).load()
        if not self.store.chunks:
            raise SystemExit("索引为空，先运行：python build_kb.py index")
        self.vecs = self.store.vecs
        self.chunks = self.store.chunks
        self._bm25 = None
        self.ollama = ensure_embedder(cfg)

    # --- BM25
    def _load_bm25(self):
        if self._bm25 is not None:
            return self._bm25
        cache = STORE_DIR / "bm25.json"
        if cache.exists():
            try:
                d = json.loads(cache.read_text(encoding="utf-8"))
                if d.get("n") == len(self.chunks):
                    self._bm25 = d
                    return d
            except Exception:
                pass
        print("[BM25] 首次构建关键词索引…", file=sys.stderr)
        df: Counter = Counter()
        docs_tf: list[Counter] = []
        lens: list[int] = []
        for c in self.chunks:
            toks = tokenize(c["heading"] + " " + c["text"])
            tf = Counter(toks)
            docs_tf.append(tf)
            lens.append(len(toks))
            for t in tf:
                df[t] += 1
        d = {
            "n": len(self.chunks),
            "avgdl": (sum(lens) / len(lens)) if lens else 1.0,
            "df": dict(df),
            "tf": [{k: v for k, v in t.items()} for t in docs_tf],
            "len": lens,
        }
        cache.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        self._bm25 = d
        return d

    def _bm25_scores(self, query: str):
        import numpy as np

        d = self._load_bm25()
        n = d["n"]
        if n == 0:
            return np.zeros(0, dtype="float32")
        q_toks = tokenize(query)
        if not q_toks:
            return np.zeros(n, dtype="float32")
        k1, b = 1.5, 0.75
        avgdl = d["avgdl"] or 1.0
        scores = np.zeros(n, dtype="float32")
        df = d["df"]
        for t in set(q_toks):
            f = df.get(t, 0)
            if f == 0 or f >= n:
                continue
            idf = math.log(1 + (n - f + 0.5) / (f + 0.5))
            for i, tf in enumerate(d["tf"]):
                c = tf.get(t)
                if not c:
                    continue
                dl = d["len"][i] or 1
                scores[i] += idf * (c * (k1 + 1)) / (c + k1 * (1 - b + b * dl / avgdl))
        return scores

    # --- 检索
    def search(self, query: str, k: int | None = None, alpha: float | None = None,
               path_filter: str | None = None, max_per_file: int | None = None,
               reserve: list | None = None) -> list[dict]:
        import numpy as np

        rcfg = self.cfg["retrieval"]
        k = k or rcfg["top_k"]
        alpha = rcfg["alpha"] if alpha is None else alpha
        max_per_file = rcfg.get("max_per_file", 3) if max_per_file is None else max_per_file
        rsv = rcfg.get("reserve") or [] if reserve is None else reserve

        qv = np.asarray(self.ollama.embed([query])[0], dtype="float32")
        qn = np.linalg.norm(qv)
        if qn > 0:
            qv = qv / qn
        cos = self.vecs @ qv if self.vecs.size else np.zeros(len(self.chunks), dtype="float32")

        bm = self._bm25_scores(query)
        bm_max = float(bm.max()) if bm.size else 0.0
        bm_n = bm / bm_max if bm_max > 0 else bm

        score = alpha * cos + (1 - alpha) * bm_n
        order = np.argsort(-score)

        picked: list[int] = []
        picked_set: set[int] = set()
        per_file: Counter = Counter()
        for idx in order:
            i = int(idx)
            c = self.chunks[i]
            if path_filter and path_filter not in c["file"]:
                continue
            if per_file[c["file"]] >= max_per_file:
                continue
            per_file[c["file"]] += 1
            picked.append(i)
            picked_set.add(i)
            if len(picked) >= k:
                break

        # 保底召回：确保指定路径在结果里有名额，挤掉末尾的非该路径块。
        # min_score_ratio 是门槛：保底块分数须 ≥ 榜首分数 × 该比例，否则宁可不塞，
        # 避免为了"接旧课"把无关的旧基石块硬塞进上下文。
        top_score = float(score[picked[0]]) if picked else 0.0
        for item in rsv:
            path, need = item.get("path"), int(item.get("min", 1))
            if not path:
                continue
            ratio = float(item.get("min_score_ratio", 0.0))
            cutoff = top_score * ratio if ratio > 0 else 0.0
            have = sum(1 for i in picked if path in self.chunks[i]["file"])
            need -= have
            if need <= 0:
                continue
            for idx in order:
                i = int(idx)
                if need <= 0:
                    break
                if i in picked_set or path not in self.chunks[i]["file"]:
                    continue
                if path_filter and path_filter not in self.chunks[i]["file"]:
                    continue
                if cutoff > 0 and float(score[i]) < cutoff:
                    break  # order 是降序，再往后只会更低
                for j in range(len(picked) - 1, -1, -1):
                    if path not in self.chunks[picked[j]]["file"]:
                        picked_set.discard(picked[j])
                        picked[j] = i
                        picked_set.add(i)
                        need -= 1
                        break
                else:
                    picked.append(i)
                    picked_set.add(i)
                    need -= 1

        picked.sort(key=lambda i: -float(score[i]))

        results = []
        for i in picked:
            c = self.chunks[i]
            results.append(
                {
                    "id": c["id"],
                    "score": float(score[i]),
                    "cos": float(cos[i]),
                    "bm25": float(bm_n[i]),
                    "file": c["file"],
                    "heading": c["heading"],
                    "start": c["start"],
                    "end": c["end"],
                    "text": c["text"],
                }
            )
        return results


def fmt_sources(results: list[dict]) -> str:
    lines = []
    for n, r in enumerate(results, 1):
        lines.append(
            f"[{n}] {r['file']}  §{r['heading']}  (L{r['start']}-{r['end']})  "
            f"score={r['score']:.3f}  cos={r['cos']:.3f}  bm25={r['bm25']:.3f}"
        )
    return "\n".join(lines)


def cmd_search(cfg: dict, query: str, k: int, alpha: float | None,
               path_filter: str | None, show_text: bool = True) -> None:
    r = Retriever(cfg)
    t0 = time.time()
    res = r.search(query, k=k, alpha=alpha, path_filter=path_filter)
    dt = (time.time() - t0) * 1000
    print(f"\n查询：{query}")
    print(f"模型：{cfg['embed_model']}  |  命中 {len(res)} 块  |  {dt:.0f} ms")
    print("=" * 78)
    for n, it in enumerate(res, 1):
        print(f"\n[{n}] {it['file']}  §{it['heading']}  (L{it['start']}-{it['end']})")
        print(f"    相似度 {it['score']:.3f}   (向量 {it['cos']:.3f} / 关键词 {it['bm25']:.3f})")
        if show_text:
            body = it["text"]
            if len(body) > 900:
                body = body[:900] + " …"
            print("    " + body.replace("\n", "\n    "))
    print()


SYS_PROMPT = """你是 Yan 的医学学习助教。Yan 是长沙医学院口腔医学大三学生，预推理型学习者。

回答规则：
1. 只依据【资料片段】回答，不得引入片段之外的记忆充当证据。
2. 每个结论后面用 [编号] 标注来源，编号对应片段编号；一句话多来源就写 [1][3]。
3. 资料未覆盖的内容，直接写"资料未覆盖"，不要编，不要用自信语气冒充证据。
4. 标签体系：可推导的写【推】；共性可推、细节需记的写【半】；纯记忆（数字、剂量、命名、正常值）写【桶】。
5. 语言中文，风格直接、密度高、不铺垫；不讲课，超过 8 行的连续解释就是讲课。
6. 结尾给一行"来源"列表，格式：编号 → 文件 §标题 (行号)。"""


def build_context(results: list[dict], max_chars: int = 7000) -> str:
    parts, used = [], 0
    for n, r in enumerate(results, 1):
        block = f"[{n}] 来源：{r['file']} §{r['heading']} (L{r['start']}-{r['end']})\n{r['text']}"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n---\n\n".join(parts)


def cmd_ask(cfg: dict, query: str, k: int, alpha: float | None,
            path_filter: str | None, show_sources: bool = True) -> None:
    r = Retriever(cfg)
    res = r.search(query, k=k, alpha=alpha, path_filter=path_filter)
    if not res:
        print("无检索结果。")
        return
    ctx = build_context(res)
    llm = make_llm(cfg)
    messages = [
        {"role": "system", "content": SYS_PROMPT},
        {"role": "user", "content": f"【资料片段】\n\n{ctx}\n\n【问题】\n{query}"},
    ]
    print(f"\n问题：{query}")
    print(f"召回 {len(res)} 块，模型 {cfg['llm_model']} …\n" + "=" * 78)
    t0 = time.time()
    try:
        ans = llm.chat(messages)
    except Exception as e:
        print(f"[LLM 调用失败] {e}\n回退为纯检索结果：\n")
        cmd_search(cfg, query, k, alpha, path_filter)
        return
    print(ans)
    print("\n" + "-" * 78)
    print(f"耗时 {time.time()-t0:.1f}s")
    if show_sources:
        print("来源标注：")
        print(fmt_sources(res))


# ---------------------------------------------------------------- Web UI

def cmd_serve(cfg: dict, port: int, open_browser: bool = False) -> None:
    import http.server
    import socketserver
    import threading
    import webbrowser

    # 今日单元 / 模型层（可选）：任一加载失败都不影响检索
    try:
        import unit_api
        unit_err = ""
    except Exception as e:          # noqa: BLE001
        unit_api, unit_err = None, str(e)
    try:
        import llm_api
        llm_err = ""
    except Exception as e:          # noqa: BLE001
        llm_api, llm_err = None, str(e)
    try:
        import kb_api
        kb_err = ""
    except Exception as e:          # noqa: BLE001
        kb_api, kb_err = None, str(e)

    print("正在加载索引、检查 Ollama 服务 ...", flush=True)
    r = Retriever(cfg)
    if llm_api is not None:
        # 把检索函数交给模型层，让它能做"检索 + 外部模型生成"
        llm_api.bind(lambda q, k=8, pf=None: r.search(q, k=k, path_filter=pf))
    html_path = KB_DIR / "web" / "index.html"
    html = html_path.read_text(encoding="utf-8") if html_path.exists() else "<h1>缺少 web/index.html</h1>"
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _mod(self, mod, err, method, body):
            """/api/unit/* 与 /api/llm/* 的统一转发"""
            if mod is None:
                self._send(503, json.dumps({"error": err}, ensure_ascii=False).encode())
                return
            u = urllib.parse.urlparse(self.path)
            try:
                if method == "GET":
                    code, payload = mod.handle_get(u.path, urllib.parse.parse_qs(u.query))
                else:
                    code, payload = mod.handle_post(u.path, body or {})
            except Exception as e:      # noqa: BLE001
                code, payload = 500, {"error": str(e)}
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def _unit(self, method, body):
            self._mod(unit_api, f"unit_api 未加载：{unit_err}", method, body)

        def _llm(self, method, body):
            self._mod(llm_api, f"llm_api 未加载：{llm_err}", method, body)

        def _kb(self, method, body):
            self._mod(kb_api, f"kb_api 未加载：{kb_err}", method, body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/api/unit/"):
                self._unit("GET", None)
            elif self.path.startswith("/api/llm/"):
                self._llm("GET", None)
            elif self.path.startswith("/api/kb/"):
                self._kb("GET", None)
            elif self.path == "/api/stats":
                meta = r.store.meta
                files = Counter(c["file"].split("/")[0] for c in r.chunks)
                body = json.dumps(
                    {"meta": meta, "top_dirs": files.most_common(20)},
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(200, body)
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
            except Exception:
                self._send(400, b'{"error":"bad json"}')
                return
            if self.path.startswith("/api/unit/"):
                self._unit("POST", req)
                return
            if self.path.startswith("/api/llm/"):
                self._llm("POST", req)
                return
            if self.path.startswith("/api/kb/"):
                self._kb("POST", req)
                return
            q = (req.get("query") or "").strip()
            if not q:
                self._send(400, b'{"error":"empty query"}')
                return
            k = int(req.get("k") or cfg["retrieval"]["top_k"])
            pf = req.get("path_filter") or None
            mode = req.get("mode") or "search"
            with lock:
                try:
                    res = r.search(q, k=k, path_filter=pf)
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode())
                    return
            payload = {"results": res, "query": q}
            if mode == "ask":
                try:
                    llm = make_llm(cfg)
                    msgs = [
                        {"role": "system", "content": SYS_PROMPT},
                        {
                            "role": "user",
                            "content": f"【资料片段】\n\n{build_context(res)}\n\n【问题】\n{q}",
                        },
                    ]
                    payload["answer"] = llm.chat(msgs)
                except Exception as e:
                    payload["answer"] = f"[生成失败] {e}"
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://127.0.0.1:{port}"
        print()
        print("=" * 52)
        print(f"  本地知识库已就绪  ->  {url}")
        print("=" * 52)
        print(f"  索引    : {len(r.chunks)} 块 / {r.store.meta.get('files', 0)} 文件")
        print(f"  嵌入    : {cfg['embed_model']}   ({r.store.meta.get('dim', 0)} 维)")
        print(f"  生成    : {cfg['llm_model']}")
        print()
        print("  停止服务：关闭本窗口，或按 Ctrl+C")
        print("=" * 52)
        print(flush=True)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception as e:
                print(f"（自动打开浏览器失败：{e}，请手动访问 {url}）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")


# ---------------------------------------------------------------- stats

def cmd_stats(cfg: dict) -> None:
    s = Store(cfg).load()
    meta = s.meta
    print("=" * 60)
    print("知识库概况")
    print("=" * 60)
    if not meta:
        print("尚未建库。运行：python build_kb.py index")
        return
    print(f"根目录      : {cfg['root']}")
    print(f"嵌入模型    : {meta.get('embed_model')}  ({meta.get('dim')} 维)")
    print(f"文档数      : {meta.get('files')}")
    print(f"块数        : {meta.get('chunks')}")
    print(f"构建时间    : {meta.get('built_at')}")
    size = sum(f.stat().st_size for f in STORE_DIR.glob("*") if f.is_file())
    print(f"索引体积    : {size/1024/1024:.1f} MB")
    print("-" * 60)
    c = Counter(ch["file"].split("/")[0] for ch in s.chunks)
    print(f"{'目录':<28}{'块数':>8}")
    for k, v in c.most_common(30):
        print(f"{k:<28}{v:>8}")
    print("=" * 60)


# ---------------------------------------------------------------- main

RESTART_FLAG = STORE_DIR / ".restart_request"


def _listener_pid(port: int) -> int | None:
    """找正在监听 127.0.0.1:<port> 的进程 pid（Windows 用 netstat -ano）。"""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 text=True, errors="replace").stdout
            for ln in out.splitlines():
                parts = ln.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and (
                        parts[3].upper() == "LISTENING" or parts[2] in ("0.0.0.0:0", "[::]:0")):
                    return int(parts[4])
    except Exception:
        pass
    return None


def _maybe_restart_server() -> None:
    """2026-09-23：让「知识库页 → 重建索引」顺带重启服务（改了 .py 之后不用手动关窗口）。

    只有同时满足两条才动手：
      1 kb/store/.restart_request 存在（人或 agent 主动放的；用一次就删）
      2 本进程是 kb_api 从服务里拉起的重建子进程（kb_api 会设 PYTHONUNBUFFERED=1 + NO_PROXY=*）
    做法：结束父进程（旧服务）→ 在新控制台窗口里起一个新服务（关窗口即停，和 .bat 一样）。
    """
    if not RESTART_FLAG.exists():
        return
    if os.environ.get("PYTHONUNBUFFERED") != "1" or os.environ.get("NO_PROXY") != "*":
        print("[重启] 有重启请求，但不是从服务里触发的重建，跳过（双击 .bat 即可）", flush=True)
        return
    try:
        RESTART_FLAG.unlink()
    except Exception:
        pass
    # 注意：venv 的 Scripts\python.exe 是个启动器，会再拉起真正的解释器，
    # 所以 os.getppid() 是启动器而不是服务本体。改为按端口找正在监听 8765 的进程。
    pid = _listener_pid(8765)
    print(f"[重启] 收到重启请求：结束旧服务（pid {pid}），在新窗口启动新服务", flush=True)
    env = dict(os.environ)
    for k in ("NO_PROXY", "PYTHONUNBUFFERED"):
        env.pop(k, None)
    if pid:
        try:
            if os.name == "nt":
                # 不能加 /T：本进程就是服务的子孙进程，/T 会把自己也连根杀掉，新服务就起不来了
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
    for _ in range(20):                 # 等端口真的放出来
        time.sleep(0.5)
        if not _listener_pid(8765):
            break
    # CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB：新服务不挂在旧进程树下
    cmd = [sys.executable, str(KB_DIR / "build_kb.py"), "serve", "--port", "8765"]
    tries = [0x00000010 | 0x01000000, 0x00000010] if os.name == "nt" else [0]
    for flags in tries:
        try:
            subprocess.Popen(cmd, cwd=str(KB_DIR), env=env, creationflags=flags, close_fds=True)
            break
        except OSError:
            continue        # 所在作业对象不允许脱离时，退回只开新控制台
    # 注意：stdin/stdout/stderr 都不传——传了任何一个，Windows 会把其余两个也接到
    # 本进程继承来的管道上（旧服务已死，管道断了），新服务一 print 就崩。


def main():
    ap = argparse.ArgumentParser(
        prog="build_kb", description="本地检索知识库（Ollama 嵌入 + 混合检索 + 增量更新）"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="建库 / 增量更新")
    p.add_argument("--full", action="store_true", help="全量重建")
    p.add_argument("--strict", action="store_true", help="用 sha1 精确校验变更")
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 个文件（调试）")
    p.add_argument("--root", default=None, help="覆盖根目录")
    p.add_argument("--model", default=None, help="覆盖嵌入模型")

    for name, helptext in (("search", "语义检索"), ("ask", "检索 + LLM 生成答案")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("query")
        q.add_argument("-k", type=int, default=None)
        q.add_argument("--alpha", type=float, default=None, help="向量权重 0~1（默认 0.62）")
        q.add_argument("--path", default=None, help="按路径子串过滤，如 诊断学")
        q.add_argument("--no-text", action="store_true", help="只列来源不显示正文")

    s = sub.add_parser("serve", help="启动 Web 界面")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="就绪后自动打开浏览器")

    sub.add_parser("stats", help="索引概况")

    args = ap.parse_args()
    cfg = load_config()
    if getattr(args, "root", None):
        cfg["root"] = args.root
    if getattr(args, "model", None):
        cfg["embed_model"] = args.model
    if not CONFIG_PATH.exists():
        save_config(cfg)

    if args.cmd == "index":
        cmd_index(cfg, full=args.full, strict=args.strict, limit=args.limit)
        _maybe_restart_server()
    elif args.cmd == "search":
        cmd_search(cfg, args.query, args.k or cfg["retrieval"]["top_k"], args.alpha,
                   args.path, show_text=not args.no_text)
    elif args.cmd == "ask":
        cmd_ask(cfg, args.query, args.k or cfg["retrieval"]["top_k"], args.alpha, args.path)
    elif args.cmd == "serve":
        cmd_serve(cfg, args.port, args.open)
    elif args.cmd == "stats":
        cmd_stats(cfg)


if __name__ == "__main__":
    main()
