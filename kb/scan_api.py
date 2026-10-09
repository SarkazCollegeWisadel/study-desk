"""缺口扫描·完整版（2026-09-23 新增，替代网页上那个 5 题不给答案的弱版 /api/llm/quiz）

照 之前学习的画像和知识/skills/gap-scan-quiz/SKILL.md 的协议走：
  · 一轮 10 题；从缺口表里**还没勾的条目**均匀抽（按梯队轮流取，不挑重点），名解至少 2 题
  · 出题前先检索教材片段，参考答案只能来自片段（数字错一个就白扫一轮）
  · 先只发题面，他答完再判；错因三分类：推链断 / 桶没记 / 措辞偏
  · 名解题额外跑一遍逐字比对（drill_api），措辞偏必须给原句

副作用（全部只追加，全部过 unit_api._safe）：
  · 缺口_<科目>.md：被扫到的条目 ☐→☑；文件末尾追加「## 扫描 <日期> <科目> 第n轮」一段
  · <科目>/02_其他内容/03_桶_卡片.csv：桶没记 / 措辞偏 生成的卡
  · 进行中的轮次存 kb/store/scan_rounds.json（服务重启也不丢）

路由（由 llm_api 转发）：
  POST /api/llm/scan/start  {subject, n=10}          → {round_id, questions:[{n,q,type,label}]}
  POST /api/llm/scan/grade  {round_id, answers:[..], dry?} → {results, coverage, gap_file, cards}
                            dry=true 只判不写
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from datetime import date
from pathlib import Path

import unit_api as U

HERE = Path(__file__).resolve().parent
ROUNDS = HERE / "store" / "scan_rounds.json"

SYS_MAKE = """你给 Yan 出缺口扫描题。目的不是考他懂不懂，是扫描他预推理没覆盖到的角落。
每个条目我都给了【教材片段】。硬规则：
1 参考答案只能来自片段；片段没覆盖就在 ref 里写"资料未覆盖"，不许凭记忆补。
2 名解题：题面就是"名词解释：<术语>"，ref 必须是片段里的教材原句，逐字抄，禁止改写。
3 数字、分型、顺序类条目，出成填空或单选，ref 写出原文数字。
4 推类条目出"为什么/怎么会"的简答，ref 写推理链的关键环节。
只输出一个 JSON 数组，不要任何解释，每个元素：
{"n":题号,"q":"题面","type":"单选|多选|填空|简答|名解","ref":"参考答案","src":"片段编号或出处"}"""

SYS_GRADE = """你是 Yan 的缺口扫描判卷人。逐题判，不夸奖不安慰，错就是错。
错因只有三类：
- 推链断：机制没推通 → fix 写缺的是哪一环 / 哪条承重墙
- 桶没记：纯数字/命名/位置没记住 → card 给一张卡
- 措辞偏：懂，但和教材原句不一致 → fix 写原句，card 给一张"原句卡"（A 面是原句）
答对 cause 写"—"。只依据 ref 判，ref 写"资料未覆盖"的题判"无法判"。
只输出 JSON 数组，每个元素：
{"n":题号,"ok":true/false,"cause":"推链断|桶没记|措辞偏|—|无法判","fix":"补救，一句话","card":{"q":"","a":"","hook":""} 或 null}"""


# ---------------------------------------------------------------- 轮次存取

def _load() -> dict:
    try:
        return json.loads(ROUNDS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    # 只留最近 20 轮
    keep = dict(sorted(d.items(), key=lambda kv: kv[1].get("t", 0))[-20:])
    p = U._safe(ROUNDS)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 抽题

def _is_mingjie(sec: str) -> bool:
    return any(k in sec for k in ("名解", "措辞", "术语"))


def sample(subject: str, n: int = 10, seed: int | None = None) -> list[dict]:
    """按梯队轮流抽未勾条目；名解梯队保底 2 题。"""
    p = U.GAP_DIR / f"缺口_{subject}.md"
    if not p.exists():
        raise ValueError(f"没有 学习记录/缺口_{subject}.md，先按 gap-scan-quiz 建首轮骨架")
    rows = U._parse_gap(p)
    if not rows:
        raise ValueError(f"缺口_{subject}.md 里的条目已经全部扫过了")
    rng = random.Random(seed)
    by_sec: dict[str, list] = {}
    for r in rows:
        by_sec.setdefault(r["section"], []).append(r)
    for v in by_sec.values():
        rng.shuffle(v)
    picked: list[dict] = []
    mj = [s for s in by_sec if _is_mingjie(s)]
    for s in mj:
        while by_sec[s] and sum(_is_mingjie(x["section"]) for x in picked) < 2:
            picked.append(by_sec[s].pop())
    secs = list(by_sec)
    while len(picked) < n and any(by_sec.values()):
        for s in secs:
            if by_sec[s] and len(picked) < n:
                picked.append(by_sec[s].pop())
    return picked


# ---------------------------------------------------------------- JSON 提取

def _json_array(txt: str) -> list:
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", txt, flags=re.S)
    if not m:
        raise RuntimeError(f"模型没按 JSON 输出：{txt[:200]}")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- 出题 / 判卷

def start(chat, search, subject: str, n: int = 10) -> dict:
    items = sample(subject, n)
    blocks = []
    for i, it in enumerate(items, 1):
        ctx = ""
        if search is not None:
            try:
                res = search(it["item"], 3, subject)
                ctx = "\n".join(f"  ({i}.{j}) {r['file']} §{r.get('heading','')}\n  {r['text'][:600]}"
                                for j, r in enumerate(res, 1))
            except Exception as e:      # noqa: BLE001
                ctx = f"  （检索失败：{e}）"
        kind = "名解" if _is_mingjie(it["section"]) else (it.get("label") or "")
        blocks.append(f"### 条目 {i}｜{it['item']}｜归属 {it.get('belong','')}｜{kind}\n"
                      f"【教材片段】\n{ctx or '  （无）'}")
    user = f"科目：{subject}\n共 {len(items)} 个条目，每个出 1 题，题号与条目号一致。\n\n" + "\n\n".join(blocks)
    txt, meta = chat("answer", [{"role": "system", "content": SYS_MAKE},
                                {"role": "user", "content": user}], 0.4, 3500)
    qs = _json_array(txt)
    qmap = {int(q.get("n", k + 1)): q for k, q in enumerate(qs)}
    rid = uuid.uuid4().hex[:10]
    rnd = {"t": time.time(), "subject": subject, "meta": meta, "items": []}
    for i, it in enumerate(items, 1):
        q = qmap.get(i) or {}
        rnd["items"].append({
            "n": i, "gap": it, "q": q.get("q") or f"「{it['item']}」", "type": q.get("type") or "",
            "ref": q.get("ref") or "资料未覆盖", "src": q.get("src") or "",
            "label": it.get("label") or ("名解" if _is_mingjie(it["section"]) else ""),
        })
    d = _load()
    d[rid] = rnd
    _save(d)
    return {"round_id": rid, "subject": subject, "meta": meta,
            "questions": [{"n": x["n"], "q": x["q"], "type": x["type"], "label": x["label"]}
                          for x in rnd["items"]]}


def _drill_bank() -> dict:
    try:
        import drill_api
        return {(b["subject"], b["term"]): b["original"] for b in drill_api.bank()}
    except Exception:
        return {}


def grade(chat, rid: str, answers: list, dry: bool = False) -> dict:
    """dry=True：只判卷返回结果，不勾缺口、不写卡、不追加扫描记录、不关闭这一轮（调试用）。"""
    d = _load()
    rnd = d.get(rid)
    if not rnd:
        raise ValueError("找不到这一轮（可能超过 20 轮被清掉了），重新开一轮")
    if rnd.get("done"):
        raise ValueError("这一轮已经判过了")
    subject = rnd["subject"]
    items = rnd["items"]
    ans = {int(a.get("n")): (a.get("text") or "").strip() for a in answers if a.get("n") is not None}

    # 名解题：先跑确定性的逐字比对，结果一起交给判卷模型
    bank = _drill_bank()
    diffs = {}
    try:
        import drill_api
        for x in items:
            if x["label"] == "名解" and ans.get(x["n"]):
                term = re.sub(r"^名词解释[:：]\s*", "", x["q"]).strip()
                orig = bank.get((subject, term)) or x["ref"]
                if orig and orig != "资料未覆盖":
                    diffs[x["n"]] = drill_api.compare(orig, ans[x["n"]])
                    diffs[x["n"]]["original"] = orig
    except Exception:
        pass

    lines = []
    for x in items:
        extra = ""
        if x["n"] in diffs:
            dd = diffs[x["n"]]
            extra = (f"\n逐字比对：召回 {dd['recall']:.0%}，关键漏 "
                     f"{'；'.join(k['kind']+':'+k['s'] for k in dd['key_miss']) or '无'}")
        lines.append(f"### 第{x['n']}题（{x['type']}｜{x['label']}）\n题面：{x['q']}\n"
                     f"ref：{x['ref']}\n作答：{ans.get(x['n']) or '（空）'}{extra}")
    txt, meta = chat("probe", [{"role": "system", "content": SYS_GRADE},
                               {"role": "user", "content": "\n\n".join(lines)}], 0.2, 4000)
    gmap = {int(g.get("n", 0)): g for g in _json_array(txt)}

    results, cards, table = [], [], []
    for x in items:
        g = gmap.get(x["n"]) or {}
        ok = bool(g.get("ok"))
        cause = g.get("cause") or ("—" if ok else "无法判")
        if not ans.get(x["n"]):
            ok, cause = False, g.get("cause") or "推链断"
        fix = g.get("fix") or ""
        if cause == "措辞偏" and x["n"] in diffs and "原句" not in fix:
            fix = f"原句：{diffs[x['n']]['original']}"
        card = g.get("card") if not ok else None
        if card and (card.get("q") or "").strip() and (card.get("a") or "").strip():
            tag = f"{subject}/{'名解' if cause == '措辞偏' else '桶'}"
            cards.append({"q": card["q"], "a": card["a"], "hook": card.get("hook", ""), "tag": tag})
        results.append({"n": x["n"], "q": x["q"], "answer": ans.get(x["n"], ""), "ok": ok,
                        "cause": cause, "fix": fix, "ref": x["ref"], "src": x["src"],
                        "diff": diffs.get(x["n"])})
        cell = lambda s: (s or "").replace("|", "/").replace("\n", " ")
        table.append(f"{x['n']} | {cell(x['gap']['item'])} | {x['label'] or '—'} | "
                     f"{'对' if ok else '错'} | {cause} | {cell(fix)[:120] or '—'}")

    if dry:
        return {"results": results, "cards_preview": cards, "dry": True, "meta": meta}

    # 1) 勾掉扫过的条目
    ticked = [U._tick_gap(x["gap"]) for x in items]
    # 2) 桶卡
    card_res = U._append_cards(subject, cards, f"缺口扫描 {date.today().isoformat()}") if cards else {"n": 0}
    # 3) 扫描记录段
    gp = U._safe(U.GAP_DIR / f"缺口_{subject}.md")
    text = U._read(gp)
    nth = len(re.findall(r"^## 扫描 \d{4}-\d{2}-\d{2}", text, flags=re.M)) + 1
    cov = next((c for c in U.gap_coverage() if c["subject"] == subject), {"done": 0, "total": 0})
    pct = f"{cov['done'] / cov['total']:.0%}" if cov["total"] else "—"
    broken = [f"{x['gap']['item']} → {r['fix']}" for x, r in zip(items, results) if r["cause"] == "推链断"]
    block = [f"## 扫描 {date.today().isoformat()} {subject} 第{nth}轮",
             "题号 | 大纲条目 | 标签 | 对/错 | 错因 | 补救"] + table + [
             f"本轮覆盖条目：{len(items)}/{cov['total']}；累计覆盖：{pct}",
             f"推链断汇总：{'；'.join(broken) or '无'}",
             "新增桶卡："] + ([f"{c['q']} | {c['a']} | {c['tag']} | {c['hook']}" for c in cards] or ["（无）"])
    with gp.open("a", encoding="utf-8") as f:
        f.write(("" if text.endswith("\n") else "\n") + "\n" + "\n".join(block) + "\n")

    rnd["done"] = True
    rnd["results"] = results
    d[rid] = rnd
    _save(d)
    return {"results": results, "coverage": cov, "gap_file": str(gp.relative_to(U.ROOT)),
            "round": nth, "cards": card_res, "ticked": sum(1 for t in ticked if t.get("ok")),
            "meta": meta}
