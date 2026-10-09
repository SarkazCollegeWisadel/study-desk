#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识库管理（kb_api）—— 在前端加科目、导资料、改收录规则、重建索引
================================================================
设计前提：**不动 config.json 的 root**。
索引里每个块的 id 是"相对 root 的路径"，多根目录会撞车，也会让 94MB 的现有索引失效。
所以"加一个知识库" = 在 root 下面新建一个科目文件夹，把资料放进去，再增量索引。
外部资料是**复制**进来的，原文件不动。

对外：handle_get / handle_post
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STORE_META = HERE / "store" / "meta.json"

MAX_IMPORT_FILES = 400
MAX_IMPORT_BYTES = 800 * 1024 * 1024
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_job = {"running": False, "lines": [], "started": 0, "code": None, "cmd": ""}
_lock = threading.Lock()


def _cfg() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_cfg(c: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")


def ROOT() -> Path:
    return Path(_cfg()["root"]).resolve()


def _inside(p: Path) -> Path:
    p = p.resolve()
    r = ROOT()
    if r not in p.parents and p != r:
        raise ValueError(f"拒绝操作 root 之外的路径：{p}")
    return p


def _safe_name(n: str) -> str:
    n = (n or "").strip()
    if ".." in n:
        raise ValueError("名字里不能有 ..")
    n = BAD_NAME.sub("", n).strip(". ")
    if not n:
        raise ValueError("名字不能为空")
    if len(n) > 60:
        raise ValueError("名字太长")
    return n


# ---------------------------------------------------------------- 概况

SKIP_DIRS = {"kb", "nblm", "学习记录", "之前学习的画像和知识", ".workbuddy-ai",
             ".git", "node_modules", "__pycache__", "_工具", "音色试听"}
SKIP_PREFIX = ("_废弃", "_备份", "播客_", "音色试听", ".")


def status() -> dict:
    c = _cfg()
    root = ROOT()
    meta = {}
    if STORE_META.exists():
        try:
            meta = json.loads(STORE_META.read_text(encoding="utf-8"))
        except Exception:       # noqa: BLE001
            meta = {}

    subs = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith(SKIP_PREFIX):
            continue
        n_md = n_pdf = n_csv = 0
        size = 0
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            if any(x.startswith(SKIP_PREFIX) for x in p.relative_to(root).parts[:-1]):
                continue
            sfx = p.suffix.lower()
            if sfx == ".md":
                n_md += 1
            elif sfx == ".pdf":
                n_pdf += 1
            elif sfx == ".csv":
                n_csv += 1
            else:
                continue
            try:
                size += p.stat().st_size
            except OSError:
                pass
        gap = (root / "学习记录" / f"缺口_{d.name}.md").exists()
        cards = bool(list(d.rglob("03_桶_卡片.csv")) or list(d.rglob("03-桶卡片.csv")))
        slices = bool(list(d.glob("*/01-章节切片")))
        units = bool(list(d.glob("*/02-分步学习单元")))
        subs.append({"name": d.name, "md": n_md, "pdf": n_pdf, "csv": n_csv,
                     "mb": round(size / 1048576, 1), "gap": gap, "cards": cards,
                     "slices": slices, "units": units})

    return {
        "root": str(root),
        "meta": meta,
        "subjects": subs,
        "config": {k: c.get(k) for k in
                   ("include_ext", "include_pdf", "skip_pdf_if_md_exists",
                    "pdf_include_globs", "exclude_globs", "exclude_dirs",
                    "embed_model", "llm_model")},
        "job": job_state(),
    }


# ---------------------------------------------------------------- 新建科目

SUBJ_README = """# {name}｜当前可用学习入口

> 由学习台新建于 {today}。往下面几个位置放东西，前端就能认出来：
>
> - `扫描版Markdown/01-章节切片/NN-第X章-章名-P001-020.md` → 选课器里的"章"
> - `扫描版Markdown/02-分步学习单元/UNNN-第X章-章名-01-P001-008.md` → 章下面的"单元"（约 8 页一块）
> - `学习记录/缺口_{name}.md` → 缺口清单（模板见 `学习记录/模板_缺口扫描.md`）
> - `03_桶_卡片.csv` → 桶卡，表头 `Q,A,Tag,Hook,Source`

## 推荐起点

（把教材/课件放进来之后写在这里）

## 当前缺口

（还没确认教师大纲/考试口径）
"""

GAP_TMPL = """# 缺口扫描 · {name}

> 模板见 `学习记录/模板_缺口扫描.md`。**只追加，不改历史段。**
> 错因三分类：`推链断`（回承重墙）/ `桶没记`（加卡）/ `措辞偏`（原句卡）。
> 出题原则：按大纲逐条出题，专挑边角——重点自己能推得到，缺口在非重点。

---

## 待扫清单

| # | 条目 | 归属 | 标签 | 扫过 |
|---|---|---|---|---|
| 1 |  |  | 【推】 | ☐ |
"""


def new_subject(name: str, with_gap: bool = True) -> dict:
    name = _safe_name(name)
    root = ROOT()
    d = _inside(root / name)
    created = []
    if d.exists():
        raise ValueError(f"「{name}」已经存在了")
    (d / "扫描版Markdown" / "01-章节切片").mkdir(parents=True, exist_ok=True)
    (d / "扫描版Markdown" / "02-分步学习单元").mkdir(parents=True, exist_ok=True)
    created.append(name + "/扫描版Markdown/{01-章节切片,02-分步学习单元}/")

    rd = d / "00-目录与学习顺序.md"
    rd.write_text(SUBJ_README.format(name=name, today=date.today().isoformat()),
                  encoding="utf-8")
    created.append(str(rd.relative_to(root)))

    cards = d / "03_桶_卡片.csv"
    if not cards.exists():
        cards.write_text('"Q","A","Tag","Hook","Source"\n', encoding="utf-8")
        created.append(str(cards.relative_to(root)))

    if with_gap:
        g = _inside(root / "学习记录" / f"缺口_{name}.md")
        g.parent.mkdir(parents=True, exist_ok=True)
        if not g.exists():
            g.write_text(GAP_TMPL.format(name=name), encoding="utf-8")
            created.append(str(g.relative_to(root)))

    return {"ok": True, "subject": name, "created": created}


# ---------------------------------------------------------------- 导入资料

ALLOW_EXT = {".md", ".txt", ".csv", ".pdf", ".json"}


def import_into(subject: str, src: str, sub: str = "", move: bool = False) -> dict:
    """把本机某个文件/文件夹里的资料**复制**进某个科目。原文件不动。"""
    subject = _safe_name(subject)
    root = ROOT()
    dst_dir = _inside(root / subject / (_safe_name(sub) if sub else ""))
    s = Path(os.path.expandvars(os.path.expanduser(src.strip().strip('"')))).resolve()
    if not s.exists():
        raise ValueError(f"找不到：{s}")
    if (root == s or root in s.parents) and dst_dir in (s, *s.parents):
        raise ValueError("源和目标是同一处")

    files: list[Path] = []
    if s.is_file():
        files = [s]
    else:
        for p in sorted(s.rglob("*")):
            if p.is_file() and p.suffix.lower() in ALLOW_EXT:
                files.append(p)
    files = [f for f in files if f.suffix.lower() in ALLOW_EXT]
    if not files:
        raise ValueError(f"没有可导入的文件（只收 {', '.join(sorted(ALLOW_EXT))}）")
    if len(files) > MAX_IMPORT_FILES:
        raise ValueError(f"一次最多 {MAX_IMPORT_FILES} 个文件，这次有 {len(files)} 个")
    total = sum(f.stat().st_size for f in files)
    if total > MAX_IMPORT_BYTES:
        raise ValueError(f"一共 {total/1048576:.0f}MB，超过 {MAX_IMPORT_BYTES//1048576}MB 上限")

    dst_dir.mkdir(parents=True, exist_ok=True)
    done, skipped = [], []
    base = s if s.is_dir() else s.parent
    for f in files:
        rel = f.relative_to(base)
        out = _inside(dst_dir / rel)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            skipped.append(str(out.relative_to(root)))
            continue
        shutil.copy2(f, out)             # 只复制，绝不删源文件
        done.append(str(out.relative_to(root)))
    return {"ok": True, "copied": len(done), "skipped": len(skipped),
            "files": done[:40], "skipped_files": skipped[:20],
            "dst": str(dst_dir.relative_to(root)), "bytes": total}


# ---------------------------------------------------------------- 收录规则

def set_config(patch: dict) -> dict:
    c = _cfg()
    allow = {"include_pdf", "skip_pdf_if_md_exists", "pdf_include_globs",
             "exclude_globs", "include_ext"}
    for k, v in (patch or {}).items():
        if k not in allow:
            continue
        if k in ("include_pdf", "skip_pdf_if_md_exists"):
            c[k] = bool(v)
        elif isinstance(v, list):
            c[k] = [str(x) for x in v if str(x).strip()]
    _save_cfg(c)
    return {"ok": True, "config": {k: c.get(k) for k in allow}}


# ---------------------------------------------------------------- 重建索引

def job_state() -> dict:
    with _lock:
        return {"running": _job["running"], "code": _job["code"],
                "cmd": _job["cmd"], "started": _job["started"],
                "elapsed": int(time.time() - _job["started"]) if _job["started"] else 0,
                "lines": _job["lines"][-40:]}


def reindex(full: bool = False) -> dict:
    with _lock:
        if _job["running"]:
            return {"ok": False, "error": "已经在跑了"}
        _job.update(running=True, lines=[], started=time.time(), code=None)
        # -u：子进程不缓冲，重建日志实时回吐到知识库页（2026-09-23）
        cmd = [sys.executable, "-u", str(HERE / "build_kb.py"), "index"] + (["--full"] if full else [])
        _job["cmd"] = " ".join(cmd[-2:])

    def run():
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            # 建库要直连本机 Ollama，别让系统代理把 localhost 请求转出去
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env.pop(k, None)
            env["NO_PROXY"] = "*"
            pr = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=env,
                                  text=True, encoding="utf-8", errors="replace", bufsize=1)
            for line in pr.stdout:            # type: ignore[union-attr]
                line = line.rstrip()
                if not line:
                    continue
                with _lock:
                    _job["lines"].append(line)
                    if len(_job["lines"]) > 400:
                        del _job["lines"][:200]
            pr.wait()
            with _lock:
                _job["code"] = pr.returncode
        except Exception as e:      # noqa: BLE001
            with _lock:
                _job["lines"].append(f"[失败] {e}")
                _job["code"] = -1
        finally:
            with _lock:
                _job["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "job": job_state()}


# ---------------------------------------------------------------- 路由

def handle_get(path: str, query: dict) -> tuple[int, dict]:
    if path == "/api/kb/status":
        return 200, status()
    if path == "/api/kb/job":
        return 200, job_state()
    return 404, {"error": "unknown kb endpoint"}


def handle_post(path: str, body: dict) -> tuple[int, dict]:
    try:
        if path == "/api/kb/subject":
            return 200, new_subject(body.get("name", ""), bool(body.get("gap", True)))
        if path == "/api/kb/import":
            return 200, import_into(body.get("subject", ""), body.get("src", ""),
                                    body.get("sub", ""))
        if path == "/api/kb/config":
            return 200, set_config(body.get("patch") or {})
        if path == "/api/kb/reindex":
            return 200, reindex(bool(body.get("full")))
    except Exception as e:      # noqa: BLE001
        return 200, {"error": str(e)}
    return 404, {"error": "unknown kb endpoint"}


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=2)[:2000])
