"""名解回译判分（2026-09-23 新增）

画像弱项①：名词解释「懂，但措辞和教材标准句不一致」。
这里做的是纯文本比对，不用模型：他默写一遍 → 逐字对教材原句 → 标出漏了什么、多了什么，
数字 / 否定词 / 英文术语单独拎出来（这三样错一个就是丢分点）。

题库来源（都是现成文件，不新建数据）：
  · 各科桶卡 CSV 里 Tag 含「名解」的行：Q = 术语，A = 教材原句
  · 缺口文件「名解」梯队里的术语：若能对上题库，练过就把那一行勾成 ☑（扫过）

副作用（只追加）：
  · 学习记录/名解回译记录.md —— 每练一次追加一行
  · 学习记录/缺口_<科目>.md —— 对应术语行 ☐→☑

路由（挂在 /api/unit/drill/ 下，由 unit_api 转发）：
  GET  /api/unit/drill/mingjie?subject=     题目列表（不含原句）
  POST /api/unit/drill/mingjie/check        {id, text, save=true} → 判分 + 差异 + 原句
  GET  /api/unit/drill/cards?subject=&n=5   桶卡抽背：到期的先出，没背过的其次
  POST /api/unit/drill/cards/rate           {id, result: 记得|模糊|忘了} → 下次复习日期

桶卡抽背不改任何卡片文件：复习状态单独追加在 学习记录/桶卡复习记录.csv
（日期,科目,卡片id,Q,结果,间隔天数,下次复习），取每张卡最后一行当当前状态。
间隔（初版，Leitner 式）：忘了→1 天；模糊→上次间隔减半（至少 1 天）；记得→上次间隔×2（首次 3 天，封顶 60 天）。
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import io
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import unit_api as U

RECORD_PATH = U.ROOT / "学习记录" / "名解回译记录.md"
RECORD_HEADER = """# 名解回译记录（只追加）

> 由学习台「名解回译」自动追加。召回 = 原句里有多少字被写出来；关键漏 = 数字 / 否定词 / 英文术语。
> 判定：召回 ≥ 85% 且无关键漏 → 过；否则 → 措辞偏（按 gap-scan-quiz 的错因分类）。

| 时间 | 科目 | 术语 | 召回 | 多写 | 关键漏 | 判定 |
|---|---|---|---|---|---|---|
"""

PASS_RECALL = 0.85          # 初版阈值，用几天再调
NEG_CHARS = "不无非未没否"
PAREN_ASCII = re.compile(r"[（(][A-Za-z0-9 ,，;；.\-'’/]+[)）]")
PUNCT = re.compile(r"[\s　，。、；：？！“”‘’（）()\[\]【】《》〈〉,.;:?!\"'`·—…/]")
NUM_RE = re.compile(r"\d+(?:\.\d+)?(?:\s*[~～\-–]\s*\d+(?:\.\d+)?)?\s*(?:%|μm|um|mm|cm|nm|℃|°C|岁|周|天|层|期|型)?")
ENG_RE = re.compile(r"[A-Za-z][A-Za-z \-']{2,}")


# ---------------------------------------------------------------- 题库

def _card_files() -> list[Path]:
    out = []
    for p in U.ROOT.rglob("*桶*卡片*.csv"):
        rel = p.relative_to(U.ROOT).parts
        if any(part.startswith(U.SKIP_PREFIX) for part in rel):
            continue
        if rel[0] in U.SKIP_DIRS:
            continue
        out.append(p)
    return sorted(out)


def _id(subject: str, term: str) -> str:
    return hashlib.sha1(f"{subject}|{term}".encode("utf-8")).hexdigest()[:10]


def bank(subject: str = "") -> list[dict]:
    """Tag 含「名解」、A 面非空的卡。同一科目同一术语只留第一张。"""
    seen, items = set(), []
    for p in _card_files():
        subj = p.relative_to(U.ROOT).parts[0]
        if subject and subj != subject:
            continue
        try:
            rows = list(csv.DictReader(io.StringIO(U._read(p))))
        except Exception:
            continue
        for r in rows:
            tag = (r.get("Tag") or "")
            term = (r.get("Q") or "").strip()
            orig = (r.get("A") or "").strip()
            if "名解" not in tag or not term or not orig:
                continue
            orig = re.sub(r"^原句[:：]\s*", "", orig)
            k = (subj, term)
            if k in seen:
                continue
            seen.add(k)
            items.append({"id": _id(subj, term), "subject": subj, "term": term,
                          "original": orig, "source": (r.get("Source") or "").strip(),
                          "file": str(p.relative_to(U.ROOT))})
    return items


def _gap_refs() -> dict:
    """术语 → 缺口文件里「名解」梯队那一行（未勾的）。"""
    refs = {}
    for g in (row for p in U._gap_files() for row in U._parse_gap(p)):
        sec = g.get("section", "")
        if "名解" in sec or "措辞" in sec or "术语" in sec:
            term = re.sub(r"[（(].*?[)）]", "", g["item"]).strip()
            refs[(g["subject"], term)] = g
    return refs


def _history() -> dict:
    """术语 → [判定...]，从回译记录里读，给列表页显示「练过几次 / 上次结果」。"""
    h: dict[tuple, list] = {}
    if not RECORD_PATH.exists():
        return h
    for line in U._read(RECORD_PATH).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 7 and re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            h.setdefault((cells[1], cells[2]), []).append(
                {"at": cells[0], "recall": cells[3], "verdict": cells[6]})
    return h


def listing(subject: str = "") -> dict:
    refs, hist = _gap_refs(), _history()
    items = []
    for it in bank(subject):
        k = (it["subject"], it["term"])
        hs = hist.get(k, [])
        items.append({
            "id": it["id"], "subject": it["subject"], "term": it["term"],
            "source": it["source"], "gap_todo": k in refs,   # 缺口表里还没勾的术语
            "tries": len(hs), "last": hs[-1] if hs else None,
        })
    # 没练过的在前，其次上次没过的，最后过了的
    rank = {None: 0, "措辞偏": 1, "过": 2}
    items.sort(key=lambda x: (rank.get((x["last"] or {}).get("verdict")), x["tries"]))
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------- 比对

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = PAREN_ASCII.sub("", s)          # 括号里的英文原名单独查，不计入逐字召回
    return PUNCT.sub("", s).lower()


def _keys(s: str) -> dict:
    s = unicodedata.normalize("NFKC", s)
    nums = [re.sub(r"\s+", "", m.group(0)) for m in NUM_RE.finditer(s) if m.group(0).strip()]
    eng = [m.group(0).strip().lower() for m in ENG_RE.finditer(s)]
    negs = [c for c in s if c in NEG_CHARS]
    return {"nums": nums, "eng": eng, "negs": negs}


def compare(original: str, answer: str) -> dict:
    a, b = _norm(original), _norm(answer)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    segs, same = [], 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            segs.append({"t": "eq", "s": a[i1:i2]})
            same += i2 - i1
        else:
            if i2 > i1:
                segs.append({"t": "miss", "s": a[i1:i2]})     # 原句有、他没写
            if j2 > j1:
                segs.append({"t": "extra", "s": b[j1:j2]})    # 他写了、原句没有
    recall = same / len(a) if a else 0.0
    extra = (len(b) - same) / len(b) if b else 0.0

    ko, ka = _keys(original), _keys(answer)
    na = unicodedata.normalize("NFKC", answer).lower()
    key_miss = []
    for n in ko["nums"]:
        core = re.sub(r"[^\d.~～\-–]", "", n)
        if core and core not in re.sub(r"\s+", "", na):
            key_miss.append({"kind": "数字", "s": n})
    for e in ko["eng"]:
        if e not in na:
            key_miss.append({"kind": "英文", "s": e})
    neg_o = sum(1 for c in ko["negs"])
    neg_a = sum(1 for c in ka["negs"])
    if neg_o != neg_a:
        key_miss.append({"kind": "否定词", "s": f"原句 {neg_o} 个 / 作答 {neg_a} 个"})

    # 英文原名不算硬性丢分（中文定义才是名解主体），只提示；数字和否定词算
    hard = [k for k in key_miss if k["kind"] != "英文"]
    verdict = "过" if recall >= PASS_RECALL and not hard else "措辞偏"
    return {"recall": round(recall, 3), "extra": round(extra, 3), "segments": segs,
            "key_miss": key_miss, "verdict": verdict}


# ---------------------------------------------------------------- 落盘

def _append_record(subject: str, term: str, r: dict) -> str:
    p = U._safe(RECORD_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists() or not U._read(p).strip():
        p.write_text(RECORD_HEADER, encoding="utf-8")
    km = "；".join(f"{k['kind']}:{k['s']}" for k in r["key_miss"]) or "—"
    km = km.replace("|", "/")
    line = (f"| {datetime.now().strftime('%Y-%m-%d %H:%M')} | {subject} | {term} | "
            f"{r['recall']:.0%} | {r['extra']:.0%} | {km} | {r['verdict']} |\n")
    existing = U._read(p)
    with p.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(line)
    return str(p.relative_to(U.ROOT))


def check(body: dict) -> dict:
    iid = (body.get("id") or "").strip()
    text = (body.get("text") or "").strip()
    if not iid:
        raise ValueError("缺少题目 id")
    if not text:
        raise ValueError("先写一遍再比对")
    it = next((x for x in bank() if x["id"] == iid), None)
    if it is None:
        raise ValueError("题目不存在（桶卡文件可能改过，刷新列表）")
    r = compare(it["original"], text)
    out = {**r, "term": it["term"], "subject": it["subject"],
           "original": it["original"], "source": it["source"]}
    if body.get("save", True):
        out["record"] = _append_record(it["subject"], it["term"], r)
        g = _gap_refs().get((it["subject"], it["term"]))
        out["gap"] = U._tick_gap(g) if g else {"ok": False, "why": "缺口表里没有这个术语"}
    return out


# ---------------------------------------------------------------- 桶卡抽背

REVIEW_PATH = U.ROOT / "学习记录" / "桶卡复习记录.csv"
REVIEW_HEADER = ["日期", "科目", "卡片id", "Q", "结果", "间隔天数", "下次复习"]


def all_cards(subject: str = "") -> list[dict]:
    """所有桶卡，统一成 {id, subject, q, a, tag, file, unverified}。
    认两种表头：Q,A,…（5 列 / 13 列）与 卡号,章,A面,B面,核对（组织学）。"""
    out, seen = [], set()
    for p in _card_files():
        subj = p.relative_to(U.ROOT).parts[0]
        if subject and subj != subject:
            continue
        try:
            rows = list(csv.DictReader(io.StringIO(U._read(p))))
        except Exception:
            continue
        rel = str(p.relative_to(U.ROOT))
        for r in rows:
            if "Q" in r:
                q, a, tag = r.get("Q"), r.get("A"), r.get("Tag") or ""
                note = ""
            else:
                q, a, tag = r.get("A面"), r.get("B面"), f"{subj}/桶"
                note = r.get("核对") or ""
            q, a = (q or "").strip(), (a or "").strip()
            if not q or not a or "不凭OCR" in a:      # 耳鼻喉那种"待办说明"不是卡
                continue
            cid = hashlib.sha1(f"{rel}|{q}".encode("utf-8")).hexdigest()[:10]
            if cid in seen:
                continue
            seen.add(cid)
            out.append({"id": cid, "subject": subj, "q": q, "a": a, "tag": tag,
                        "file": rel, "unverified": "未核" in note or "未核" in a})
    return out


def _review_state() -> dict:
    st = {}
    if not REVIEW_PATH.exists():
        return st
    for r in csv.DictReader(io.StringIO(U._read(REVIEW_PATH))):
        if r.get("卡片id"):
            st[r["卡片id"]] = r          # 后面的行覆盖前面的 = 最新状态
    return st


def draw(subject: str = "", n: int = 5) -> dict:
    today = date.today().isoformat()
    st = _review_state()
    cards = all_cards(subject)
    due, fresh, later = [], [], []
    for c in cards:
        s = st.get(c["id"])
        if not s:
            fresh.append(c)
        elif (s.get("下次复习") or "") <= today:
            due.append({**c, "last": s})
        else:
            later.append(c)
    due.sort(key=lambda c: c["last"].get("下次复习") or "")
    rng = __import__("random").Random(today)          # 同一天抽同一批，刷新不换卡
    rng.shuffle(fresh)
    pick = (due + fresh)[: max(1, n)]
    return {"cards": [{k: v for k, v in c.items() if k != "last"} for c in pick],
            "due": len(due), "fresh": len(fresh), "scheduled": len(later), "total": len(cards)}


def rate(body: dict) -> dict:
    cid = (body.get("id") or "").strip()
    res = (body.get("result") or "").strip()
    if res not in ("记得", "模糊", "忘了"):
        raise ValueError("result 只能是 记得 / 模糊 / 忘了")
    c = next((x for x in all_cards() if x["id"] == cid), None)
    if c is None:
        raise ValueError("卡片不存在（卡片文件可能改过，重新抽）")
    last = _review_state().get(cid)
    prev = int(last["间隔天数"]) if last and str(last.get("间隔天数", "")).isdigit() else 0
    if res == "忘了":
        gap = 1
    elif res == "模糊":
        gap = max(1, prev // 2) if prev else 1
    else:
        gap = min(60, prev * 2) if prev else 3
    nxt = (date.today() + timedelta(days=gap)).isoformat()
    p = U._safe(REVIEW_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or not U._read(p).strip()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if new:
        w.writerow(REVIEW_HEADER)
    w.writerow([date.today().isoformat(), c["subject"], cid, c["q"], res, gap, nxt])
    existing = "" if new else U._read(p)
    with p.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(buf.getvalue())
    return {"id": cid, "result": res, "interval": gap, "next": nxt,
            "record": str(p.relative_to(U.ROOT))}


# ---------------------------------------------------------------- 路由

def handle_get(path: str, query: dict) -> tuple[int, dict]:
    if path == "/api/unit/drill/mingjie":
        return 200, listing((query.get("subject") or [""])[0])
    if path == "/api/unit/drill/cards":
        n = (query.get("n") or ["5"])[0]
        return 200, draw((query.get("subject") or [""])[0], int(n) if n.isdigit() else 5)
    return 404, {"error": "unknown drill endpoint"}


def handle_post(path: str, body: dict) -> tuple[int, dict]:
    try:
        if path == "/api/unit/drill/mingjie/check":
            return 200, check(body)
        if path == "/api/unit/drill/cards/rate":
            return 200, rate(body)
    except ValueError as e:
        return 400, {"error": str(e)}
    return 404, {"error": "unknown drill endpoint"}
