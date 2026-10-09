#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
今日单元后端（unit_api）—— 给 kb/web 前端的「今日单元」模块供数据与落盘
================================================================
设计约束（来自 AGENTS.md，不要绕过）：
  - 学习单元 = 20 分钟、一条链、一个抛问；不是"讲一章"。
  - 三色标签只有【推】【半】【桶】。
  - 单元记录追加进 学习记录/学习日志.md（只追加，不改历史段）。
  - 桶卡片写进 <科目>/03_桶_卡片.csv（Q,A,Tag,Hook,Source）。
  - 缺口扫描结果在 学习记录/缺口_<科目>.md，扫过的把 ☐ 勾成 ☑。

对外只有三个入口：
  handle_get(path, query)  -> (code, dict)
  handle_post(path, body)  -> (code, dict)
  ROOT_INFO()              -> dict（调试用）

所有写操作都限制在 root 之内；改动既有文件（缺口打勾）前自动备份一次。
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import shutil
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# ---------------------------------------------------------------- 路径

def _root() -> Path:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        r = cfg.get("root")
        if r:
            return Path(r).resolve()
    except Exception:
        pass
    return HERE.parent.resolve()


ROOT = _root()
LOG_PATH = ROOT / "学习记录" / "学习日志.md"
LEGACY_LOG = ROOT / "之前学习的画像和知识" / "学习记录" / "学习日志.md"
GAP_DIR = ROOT / "学习记录"

SKIP_DIRS = {
    "kb", "nblm", "学习记录", "之前学习的画像和知识", ".workbuddy-ai",
    ".git", "node_modules", "__pycache__", "_工具", "音色试听",
}
SKIP_PREFIX = ("_废弃", "_备份", "播客_", "音色试听", ".")

LOG_HEADER = """# 学习日志（只追加）

> 格式见 `模板_单元记录.md`。由 kb 前端「今日单元」自动追加，也可手写。
> 更早的记录在 `之前学习的画像和知识/学习记录/学习日志.md`。
"""

CARD_HEADER = ["Q", "A", "Tag", "Hook", "Source"]

REC_RE = re.compile(r"^## 单元记录\s+(\d{4}-\d{2}-\d{2})\s*(.*)$")
NUMQ_RE = re.compile(r"^\s*\d+[\.、]\s*(.+[？?])\s*$")
TICK_EMPTY = "☐"
TICK_DONE = "☑"


def _safe(p: Path) -> Path:
    p = p.resolve()
    if ROOT not in p.parents and p != ROOT:
        raise ValueError(f"拒绝写到 root 之外：{p}")
    return p


def _read(p: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, FileNotFoundError):
            if enc == "gbk":
                return ""
            continue
    return ""


def subjects() -> list[str]:
    out = []
    if not ROOT.exists():
        return out
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir():
            continue
        n = d.name
        if n in SKIP_DIRS or n.startswith(SKIP_PREFIX):
            continue
        out.append(n)
    return out


# ---------------------------------------------------------------- 日志解析

def _parse_log(text: str, src: str) -> list[dict]:
    recs, cur = [], None
    for line in text.splitlines():
        m = REC_RE.match(line)
        if m:
            if cur:
                recs.append(cur)
            head = m.group(2).strip()
            subj, _, chap = head.partition("/")
            cur = {"date": m.group(1), "subject": subj.strip(),
                   "chapter": chap.strip(), "head": head,
                   "lines": [], "src": src}
            continue
        if cur is not None:
            if line.startswith("## "):
                recs.append(cur)
                cur = None
            else:
                cur["lines"].append(line)
    if cur:
        recs.append(cur)
    for r in recs:
        body = "\n".join(r.pop("lines")).strip()
        r["body"] = body
        r["question"] = _field(body, "问")
        r["next_q"] = _field(body, "下一单元候选问")
        r["minutes"] = re.split(r"[\u3000\s]*状态", _field(body, "用时"))[0].strip()
        r["state"] = ""
        mm = re.search(r"状态[:：]\s*(\S+)", body)
        if mm:
            r["state"] = mm.group(1)
    return recs


def _field(body: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}[:：]\s*(.*)$", body, re.M)
    return m.group(1).strip() if m else ""


def all_records() -> list[dict]:
    recs = []
    if LEGACY_LOG.exists():
        recs += _parse_log(_read(LEGACY_LOG), "legacy")
    if LOG_PATH.exists():
        recs += _parse_log(_read(LOG_PATH), "main")
    recs.sort(key=lambda r: r["date"])
    return recs


def progress() -> dict:
    recs = all_records()
    days = sorted({r["date"] for r in recs}, reverse=True)
    today = date.today()
    # 连续天数：从今天或昨天起往回数，中间断一天就停
    streak = 0
    if days:
        cursor = today
        if days[0] != today.isoformat():
            if days[0] == (today - timedelta(days=1)).isoformat():
                cursor = today - timedelta(days=1)
            else:
                cursor = None
        dset = set(days)
        while cursor and cursor.isoformat() in dset:
            streak += 1
            cursor -= timedelta(days=1)
    week = []
    for i in range(13, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        week.append({"date": d, "n": sum(1 for r in recs if r["date"] == d)})
    by_subject: dict[str, int] = {}
    for r in recs:
        if r["subject"]:
            by_subject[r["subject"]] = by_subject.get(r["subject"], 0) + 1
    entered = [r for r in recs if "进入" in r.get("state", "") and "没" not in r.get("state", "")]
    return {
        "total": len(recs),
        "streak": streak,
        "today": sum(1 for r in recs if r["date"] == today.isoformat()),
        "days": len(days),
        "week": week,
        "by_subject": sorted(by_subject.items(), key=lambda kv: -kv[1]),
        "entered_rate": round(len(entered) / len(recs), 2) if recs else 0.0,
        "last": recs[-1] if recs else None,
    }


# ---------------------------------------------------------------- 抛问候选

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s).strip("「」\"'。？?")


def _gap_files() -> list[Path]:
    if not GAP_DIR.exists():
        return []
    return sorted(p for p in GAP_DIR.glob("缺口_*.md") if p.is_file())


def _parse_gap(p: Path) -> list[dict]:
    """解析缺口文件里的表格行，返回未扫（☐）的条目。"""
    subject = p.stem.replace("缺口_", "")
    rows, section = [], ""
    for i, line in enumerate(_read(p).splitlines()):
        if line.startswith("#"):
            section = line.lstrip("# ").strip()
            continue
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        if TICK_EMPTY not in cells[-1]:
            continue
        if not re.match(r"^\d+$", cells[0]):
            continue
        item = cells[1]
        if not item or set(item) <= set("-: "):
            continue
        label = ""
        for c in cells:
            m = re.search(r"【(推|半|桶)】", c)
            if m:
                label = m.group(1)
                break
        rows.append({
            "subject": subject, "item": item, "label": label,
            "section": section, "file": str(p.relative_to(ROOT)),
            "line": i + 1, "belong": cells[2] if len(cells) > 3 else "",
        })
    return rows


def gap_coverage() -> list[dict]:
    """每个缺口文件的 ☑/总数，前端画覆盖条。"""
    out = []
    for p in _gap_files():
        done = todo = 0
        for line in _read(p).splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3 or not re.match(r"^\d+$", cells[0]):
                continue
            if not cells[1] or set(cells[1]) <= set("-: "):
                continue            # 模板里的空行不算条目
            if TICK_DONE in cells[-1]:
                done += 1
            elif TICK_EMPTY in cells[-1]:
                todo += 1
        if done + todo:
            out.append({"subject": p.stem.replace("缺口_", ""),
                        "done": done, "total": done + todo,
                        "file": str(p.relative_to(ROOT))})
    return out


def _gap_question(g: dict) -> str:
    it = g["item"]
    sec = g.get("section", "")
    if "名解" in sec or "措辞" in sec or "术语" in sec:
        return f"不看书，把「{it}」的教材定义整句复述出来；再逐字比对原句，圈出差在哪个词。"
    if g["label"] == "推":
        return f"把「{it}」整条推一遍——它从哪条承重墙长出来？哪一步最容易断？"
    if g["label"] == "桶":
        return f"「{it}」是纯记的。先花两分钟找一个 hook，找不到就当场做卡认命进桶。"
    if g["label"] == "半":
        return f"「{it}」里哪部分能推、哪部分必须进桶？把分界线划出来。"
    return f"「{it}」：先自己推一遍，再回教材核对差在哪。"


def _skeleton_questions() -> list[dict]:
    """从各科目的索引骨架 / 主链里捞现成的问句。"""
    out = []
    for subj in subjects():
        base = ROOT / subj
        for p in base.rglob("*.md"):
            name = p.name
            if not (("索引" in name and "骨架" in name) or "主链" in name):
                continue
            if any(part.startswith(SKIP_PREFIX) for part in p.parts):
                continue
            for i, line in enumerate(_read(p).splitlines()):
                m = NUMQ_RE.match(line)
                if not m:
                    continue
                q = m.group(1).strip()
                if len(q) < 8:
                    continue
                out.append({"subject": subj, "question": q, "kind": "chain",
                            "source": str(p.relative_to(ROOT)), "line": i + 1})
    return out


def candidates(subject: str = "", limit: int = 40, chapter: int | None = None) -> list[dict]:
    recs = all_records()
    asked = {_norm(r["question"]) for r in recs if r["question"]}
    head: list[dict] = []
    pool: list[dict] = []

    # 1) 上次单元留下的候选问 —— 最高优先级，接得上昨天
    for r in reversed(recs):
        if r["next_q"] and _norm(r["next_q"]) not in asked:
            head.append({
                "subject": r["subject"], "question": r["next_q"], "kind": "next",
                "why": f"{r['date']} 那次单元自己留下的",
                "source": "学习记录/学习日志.md", "gap": None,
            })
            break

    # 2) 缺口清单里没扫过的条目
    gaps = []
    for p in _gap_files():
        gaps += _parse_gap(p)
    random.shuffle(gaps)
    for g in gaps:
        q = _gap_question(g)
        if _norm(q) in asked:
            continue
        sec = g["section"]
        if chapter is not None:
            chs = _gap_chapters(g.get("belong", ""))
            if chapter not in chs:
                continue
        pool.append({
            "subject": g["subject"], "question": q,
            "kind": "gap-" + (g["label"] or ("名解" if ("名解" in sec or "术语" in sec) else "其他")),
            "why": f"缺口清单未扫 · {sec or '待扫'}" + (f" · {g['belong']}" if g["belong"] else ""),
            "source": g["file"], "item": g["item"],
            "gap": {"file": g["file"], "item": g["item"], "line": g["line"]},
        })

    # 3) 索引骨架里的主链问句
    sk = _skeleton_questions()
    random.shuffle(sk)
    for s in sk:
        if _norm(s["question"]) in asked:
            continue
        pool.append({
            "subject": s["subject"], "question": s["question"], "kind": "chain",
            "why": "索引骨架主链", "source": s["source"], "gap": None,
        })

    if subject:
        head = [c for c in head if c["subject"] == subject]
        pool = [c for c in pool if c["subject"] == subject]
        out = head + pool
    else:
        # 按科目轮转，"换一个"不会连着十条都是同一门课
        buckets: dict[str, list[dict]] = {}
        for c in pool:
            buckets.setdefault(c["subject"], []).append(c)
        order = sorted(buckets, key=lambda s: -len(buckets[s]))
        out = list(head)
        while any(buckets[s] for s in order):
            for s in order:
                if buckets[s]:
                    out.append(buckets[s].pop(0))
    for i, c in enumerate(out):
        c["id"] = i
    return out[:limit]


# ---------------------------------------------------------------- 章节树

SLICE_RE = re.compile(r"^(\d+)-(第[一二三四五六七八九十百]+章)-(.+?)-P(\d+)-(\d+)\.md$")
UNIT_RE = re.compile(r"^U(\d+)-(第[一二三四五六七八九十百]+章)-(.+?)-(\d+)-P(\d+)-(\d+)\.md$")
CN_NUM = {c: i for i, c in enumerate("一二三四五六七八九", 1)}


def _cn2int(s: str) -> int:
    """第二十一章 → 21"""
    s = s.replace("第", "").replace("章", "")
    if not s:
        return 0
    if "十" not in s:
        return CN_NUM.get(s, 0)
    a, _, b = s.partition("十")
    return (CN_NUM.get(a, 1) if a else 1) * 10 + (CN_NUM.get(b, 0) if b else 0)


def _gap_chapters(item_belong: str) -> list[int]:
    """归属列 '13§2' / '3/4 + 12§3' → [13] / [3,4,12]"""
    out = []
    for m in re.finditer(r"(\d+)\s*§", item_belong or ""):
        out.append(int(m.group(1)))
    if not out:
        for m in re.finditer(r"\b(\d{1,2})\b", item_belong or ""):
            out.append(int(m.group(1)))
    return out


def _subject_dirs(subject: str) -> list[Path]:
    base = ROOT / subject
    return [base] if base.exists() else []


def build_tree(subject: str = "") -> list[dict]:
    """科目 → 章 → 学习单元 / 缺口条目。全部从既有文件名和表格里长出来。"""
    subs = [subject] if subject else subjects()
    gaps_by_subject: dict[str, list[dict]] = {}
    for p in _gap_files():
        s = p.stem.replace("缺口_", "")
        gaps_by_subject.setdefault(s, []).extend(_parse_gap(p))

    out = []
    for s in subs:
        base = ROOT / s
        if not base.exists():
            continue
        chapters: dict[int, dict] = {}

        def ch(no: int, name: str, title: str) -> dict:
            if no not in chapters:
                chapters[no] = {"no": no, "name": name, "title": title,
                                "units": [], "gaps": [], "pages": [9999, 0]}
            return chapters[no]

        for f in base.rglob("*.md"):
            if any(part.startswith(SKIP_PREFIX) for part in f.parts):
                continue
            m = SLICE_RE.match(f.name)
            if m:
                no = _cn2int(m.group(2))
                c = ch(no, m.group(2), m.group(3))
                c["slice"] = str(f.relative_to(ROOT))
                c["pages"] = [min(c["pages"][0], int(m.group(4))),
                              max(c["pages"][1], int(m.group(5)))]
                continue
            m = UNIT_RE.match(f.name)
            if m:
                no = _cn2int(m.group(2))
                c = ch(no, m.group(2), m.group(3))
                c["units"].append({"id": "U" + m.group(1), "seq": int(m.group(4)),
                                   "p0": int(m.group(5)), "p1": int(m.group(6)),
                                   "file": str(f.relative_to(ROOT))})
                c["pages"] = [min(c["pages"][0], int(m.group(5))),
                              max(c["pages"][1], int(m.group(6)))]

        for g in gaps_by_subject.get(s, []):
            for no in _gap_chapters(g.get("belong", "")):
                if no in chapters:
                    chapters[no]["gaps"].append(g)
                    break
            else:
                ch(0, "", "未归章").setdefault("gaps", []).append(g)

        chs = []
        for no in sorted(chapters):
            c = chapters[no]
            c["units"].sort(key=lambda u: u["seq"])
            if c["pages"][0] == 9999:
                c["pages"] = None
            c["gap_total"] = len(c["gaps"])
            c["label"] = (c["name"] + " " + c["title"]).strip() if c["name"] else (c["title"] or "未归章")
            c["gaps"] = [{"item": g["item"], "label": g["label"], "section": g["section"],
                          "file": g["file"], "line": g["line"], "belong": g.get("belong", "")}
                         for g in c["gaps"]]
            chs.append(c)

        topics = []
        if s == "口腔组织病理学":
            for f in sorted(ROOT.glob("播客_*_脚本.md")):
                topics.append({"label": f.stem.replace("播客_", "").replace("_脚本", ""),
                               "file": str(f.relative_to(ROOT))})
        out.append({"subject": s, "chapters": chs, "topics": topics,
                    "n_units": sum(len(c["units"]) for c in chs),
                    "n_gaps": sum(c["gap_total"] for c in chs)})
    return out


def scope_hint(file_rel: str, max_chars: int = 3500) -> str:
    """读一个单元/章节切片的正文开头，喂给模型生成抛问。"""
    if not file_rel:
        return ""
    p = (ROOT / file_rel).resolve()
    if ROOT not in p.parents or not p.exists():
        return ""
    txt = _read(p)
    txt = re.sub(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$", "", txt, flags=re.M)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt[:max_chars]


# ---------------------------------------------------------------- 落盘

def _find_card_csv(subject: str) -> Path:
    """桶卡落点 = AGENTS.md 规定的 <科目>/02_其他内容/03_桶_卡片.csv。

    2026-09-23 改：原来按 rglob 找第一个表头相符的文件，口组病会命中
    02_其他内容/04-系统化学习包/03_桶_卡片.csv（NotebookLM 导出包，是产物不是主表）。
    现在固定写规范路径，不存在就新建；不再去子目录里猜。
    """
    return ROOT / subject / "02_其他内容" / "03_桶_卡片.csv"


def _card_row(header: list[str], subject: str, c: dict, source: str) -> list[str]:
    """按目标文件的实际表头填列：5 列表头照旧；13 列扩展表头按列名对上，
    认识的列（科目）顺手填，其余留空（包括 下次复习——那是复习端的事）。"""
    val = {
        "Q": (c.get("q") or "").strip(),
        "A": (c.get("a") or "").strip(),
        "Tag": (c.get("tag") or "").strip() or f"{subject}/桶",
        "Hook": (c.get("hook") or "").strip(),
        "Source": source,
        "科目": subject,
    }
    return [val.get(h, "") for h in header]


def _append_cards(subject: str, cards: list[dict], source: str) -> dict:
    cards = [c for c in cards if (c.get("q") or "").strip() and (c.get("a") or "").strip()]
    if not cards:
        return {"path": "", "n": 0}
    p = _safe(_find_card_csv(subject))
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = _read(p) if p.exists() else ""
    header = []
    if existing.strip():
        first = next(csv.reader(io.StringIO(existing.splitlines()[0])), [])
        header = [h.strip().lstrip("\ufeff") for h in first]
    if header and header[:2] != ["Q", "A"]:
        # 不认识的表头（例如 卡号,章,A面,B面,核对）：不硬塞，报出来让人看
        raise ValueError(f"{p.relative_to(ROOT)} 的表头不是 Q,A,… 格式：{header[:5]}，未写入")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_ALL)
    if not header:
        header = list(CARD_HEADER)
        w.writerow(header)
    for c in cards:
        w.writerow(_card_row(header, subject, c, source))
    text = buf.getvalue()
    if existing and not existing.endswith("\n"):
        text = "\n" + text
    # 新文件带 BOM（和库里其他桶卡一致，Excel 直接打开不乱码）；旧文件纯追加
    enc = "utf-8" if existing.strip() else "utf-8-sig"
    with p.open("a", encoding=enc, newline="") as f:
        f.write(text)
    return {"path": str(p.relative_to(ROOT)), "n": len(cards)}


def _tick_gap(gap: dict) -> dict:
    if not gap or not gap.get("file") or not gap.get("item"):
        return {"ok": False, "why": "无缺口引用"}
    p = _safe(ROOT / gap["file"])
    if not p.exists():
        return {"ok": False, "why": "缺口文件不存在"}
    lines = _read(p).splitlines(keepends=True)
    item = gap["item"].strip()
    hit = -1
    ln = int(gap.get("line") or 0) - 1
    if 0 <= ln < len(lines) and item in lines[ln] and TICK_EMPTY in lines[ln]:
        hit = ln
    else:
        for i, line in enumerate(lines):
            if line.lstrip().startswith("|") and item in line and TICK_EMPTY in line:
                hit = i
                break
    if hit < 0:
        return {"ok": False, "why": "没找到未勾的对应行"}
    bak = p.with_suffix(p.suffix + f".bak-{date.today():%Y%m%d}")
    if not bak.exists():
        shutil.copy2(p, bak)
    lines[hit] = lines[hit].replace(TICK_EMPTY, TICK_DONE, 1)
    p.write_text("".join(lines), encoding="utf-8")
    return {"ok": True, "file": gap["file"], "line": hit + 1}


def render_record(d: dict) -> str:
    head = d["subject"] + (("/" + d["chapter"]) if d.get("chapter") else "")
    L = [f"## 单元记录 {d['date']} {head}".rstrip()]
    L.append(f"问：{d['question']}")
    L.append(f"链：{d.get('chain', '').strip() or '（未填）'}")
    fal = (d.get("falsify") or "").strip()
    broke = d.get("broke")
    brk = (d.get("break_point") or "").strip()
    if fal or brk:
        tail = "断" if broke else "未断"
        L.append(f"证伪：{fal or '（未填）'} → {tail}；断点：{brk or '—'}")
    L.append("落点：")
    pts = [p for p in (d.get("points") or []) if (p.get("text") or "").strip()]
    if pts:
        for p in pts:
            lab = p.get("label") or ""
            note = (p.get("note") or "").strip()
            line = f"- {p['text'].strip()}"
            if lab:
                line += f"【{lab}】"
            if note:
                line += f"（{note}）"
            L.append(line)
    else:
        L.append("- （未落大纲条目）")
    cards = [c for c in (d.get("cards") or []) if (c.get("q") or "").strip()]
    L.append("桶卡：")
    L.append("Q | A | Tag | Hook")
    if cards:
        for c in cards:
            L.append(" | ".join([c.get("q", "").strip(), c.get("a", "").strip(),
                                 c.get("tag", "").strip(), c.get("hook", "").strip()]))
    else:
        L.append("（本单元无新桶卡）")
    mins = d.get("minutes")
    mins = f"{mins} 分钟" if mins else "未核"
    state = "已进入" if d.get("entered") else "没进入"
    L.append(f"用时：{mins}　状态：{state}")
    L.append(f"下一单元候选问：{(d.get('next_q') or '').strip() or '—'}")
    return "\n".join(L) + "\n"


def save_unit(d: dict) -> dict:
    subject = (d.get("subject") or "").strip()
    question = (d.get("question") or "").strip()
    if not subject:
        raise ValueError("缺少科目")
    if not question:
        raise ValueError("缺少抛问")
    d = dict(d)
    d["subject"] = subject
    d["question"] = question
    d.setdefault("date", date.today().isoformat())

    p = _safe(LOG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists() or not _read(p).strip():
        p.write_text(LOG_HEADER, encoding="utf-8")
    block = render_record(d)
    with p.open("a", encoding="utf-8") as f:
        f.write("\n" + block)

    src = f"单元记录 {d['date']} {subject}" + (("/" + d["chapter"]) if d.get("chapter") else "")
    cards = _append_cards(subject, d.get("cards") or [], src)
    tick = _tick_gap(d.get("gap") or {})
    return {
        "ok": True, "log": str(p.relative_to(ROOT)), "block": block,
        "cards": cards, "gap": tick, "progress": progress(),
        "coverage": gap_coverage(),
    }


# ---------------------------------------------------------------- 路由

def ROOT_INFO() -> dict:
    return {"root": str(ROOT), "log": str(LOG_PATH), "log_exists": LOG_PATH.exists(),
            "gap_files": [str(p.name) for p in _gap_files()], "subjects": subjects()}


def _drill():
    """练习类接口（名解回译等）放在 drill_api.py，按需加载；加载失败不影响今日单元。"""
    import drill_api
    return drill_api


def handle_get(path: str, query: dict) -> tuple[int, dict]:
    if path.startswith("/api/unit/drill/"):
        return _drill().handle_get(path, query)
    if path == "/api/unit/state":
        subj = (query.get("subject") or [""])[0]
        chap = (query.get("chapter") or [""])[0]
        chap_i = int(chap) if str(chap).isdigit() else None
        recs = all_records()
        return 200, {
            "subjects": subjects(),
            "candidates": candidates(subj, chapter=chap_i),
            "progress": progress(),
            "coverage": gap_coverage(),
            "recent": [{"date": r["date"], "head": r["head"], "question": r["question"],
                        "state": r.get("state", ""), "minutes": r.get("minutes", "")}
                       for r in recs[-8:]][::-1],
            "root": str(ROOT),
        }
    if path == "/api/unit/log":
        n = int((query.get("n") or ["5"])[0])
        recs = all_records()[-n:][::-1]
        return 200, {"records": [{"date": r["date"], "head": r["head"],
                                  "body": r["body"]} for r in recs]}
    if path == "/api/unit/tree":
        return 200, {"tree": build_tree((query.get("subject") or [""])[0])}
    if path == "/api/unit/hint":
        f = (query.get("file") or [""])[0]
        return 200, {"file": f, "text": scope_hint(f)}
    if path == "/api/unit/debug":
        return 200, ROOT_INFO()
    return 404, {"error": "unknown unit endpoint"}


def handle_post(path: str, body: dict) -> tuple[int, dict]:
    if path.startswith("/api/unit/drill/"):
        return _drill().handle_post(path, body)
    if path == "/api/unit/save":
        try:
            return 200, save_unit(body)
        except Exception as e:
            return 400, {"error": str(e)}
    if path == "/api/unit/preview":
        try:
            b = dict(body)
            b.setdefault("date", date.today().isoformat())
            return 200, {"block": render_record(b)}
        except Exception as e:
            return 400, {"error": str(e)}
    return 404, {"error": "unknown unit endpoint"}


if __name__ == "__main__":
    print(json.dumps(ROOT_INFO(), ensure_ascii=False, indent=2))
    print("--- 候选 ---")
    for c in candidates()[:8]:
        print(f"[{c['kind']}] {c['subject']} :: {c['question']}   ({c['why']})")
    print("--- 进度 ---")
    pr = progress()
    print(json.dumps({k: v for k, v in pr.items() if k != "week"}, ensure_ascii=False, indent=2))
