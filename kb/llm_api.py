#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型层（llm_api）—— 让本地知识库能接外部模型，按"角色"分工而不是按"哪家好"
================================================================
角色（roles）：一个角色 = 一种活，绑一个模型。换模型只改 providers.json。

  quick   小活、要快   ：把章节/单元变成今天的抛问、给桶卡补 hook、判三色标签
  answer  检索问答     ：拿着召回片段写带 [n] 引用的答案
  probe   证伪 / 出题  ：读他的推链找反例；按大纲逐条出题扫缺口
  local   离线兜底     ：断网或不想烧配额时用本机 Ollama

配置：kb/providers.json（不进检索索引，config.json 的 exclude_dirs 已排除 kb/）
  {
    "providers": {
      "<id>": {"kind":"openai|gemini|ollama", "base":"...", "key":"...",
               "model":"...", "label":"显示名", "note":""}
    },
    "roles": {"quick":"<id>", "answer":"<id>", "probe":"<id>", "local":"<id>"}
  }

对外：
  handle_get(path, query) / handle_post(path, body)
  bind(search_fn)   —— build_kb.py 在 serve 时把检索函数塞进来
"""
from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = HERE / "providers.json"

_search_fn = None          # 由 build_kb.cmd_serve 注入
_last_error = ""


def bind(search_fn=None):
    global _search_fn
    _search_fn = search_fn
    try:
        _probe_bg()          # 服务启动时后台探一次代理，不阻塞
    except Exception:       # noqa: BLE001
        pass


# ---------------------------------------------------------------- 配置

DEFAULTS = {
    "providers": {
        "ollama": {"kind": "ollama", "base": "http://127.0.0.1:11434",
                   "model": "qwen3:14b", "label": "本机 Ollama", "note": "离线可用，慢"},
    },
    "roles": {"quick": "ollama", "answer": "ollama", "probe": "ollama", "local": "ollama"},
}


def load_conf() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONF.exists():
        try:
            user = json.loads(CONF.read_text(encoding="utf-8"))
            cfg["providers"].update(user.get("providers") or {})
            cfg["roles"].update(user.get("roles") or {})
        except Exception as e:      # noqa: BLE001
            global _last_error
            _last_error = f"providers.json 读不了：{e}"
    return cfg


def save_conf(cfg: dict) -> None:
    """只覆盖 providers / roles，文件里的 _说明 之类的键原样留着。"""
    out = {}
    if CONF.exists():
        try:
            out = json.loads(CONF.read_text(encoding="utf-8"))
        except Exception:       # noqa: BLE001
            out = {}
    out["providers"] = cfg.get("providers", {})
    out["roles"] = cfg.get("roles", {})
    CONF.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def _mask(k: str) -> str:
    if not k:
        return ""
    return (k[:6] + "…" + k[-4:]) if len(k) > 14 else "…"


# ---------------------------------------------------------------- HTTP

# ---- 代理自动探测（2026-09-23）------------------------------------------------
# Gemini 国内直连不通。起服务时在后台试本机常见代理口（Clash 7890 / Clash Verge 7897 / v2rayN 10809），
# 能打通 generativelanguage.googleapis.com 的 CONNECT 隧道就记下来。
# 用法：providers.json 里 proxy 填 "auto"；或填 "env" 但系统没设代理变量时也会退到探测结果。
PROXY_PORTS = (7890, 7897, 10809)
PROBE_HOST = "generativelanguage.googleapis.com"
_probe = {"state": "未探测", "proxy": "", "tried": [], "at": 0.0}


def detect_proxy(timeout: float = 3.0) -> dict:
    import socket
    tried = []
    for port in PROXY_PORTS:
        t = time.time()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                pass
        except OSError:
            tried.append({"port": port, "ok": False, "why": "端口没开"})
            continue
        try:
            c = http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout)
            c.set_tunnel(PROBE_HOST, 443)
            c.request("GET", "/", headers={"Host": PROBE_HOST})
            c.getresponse().read(64)
            c.close()
            ms = int((time.time() - t) * 1000)
            tried.append({"port": port, "ok": True, "ms": ms})
            _probe.update(state="可用", proxy=f"http://127.0.0.1:{port}", tried=tried, at=time.time())
            return dict(_probe)
        except Exception as e:      # noqa: BLE001
            tried.append({"port": port, "ok": False, "why": f"端口开着但连不到 Google：{str(e)[:60]}"})
    _probe.update(state="没找到可用代理", proxy="", tried=tried, at=time.time())
    return dict(_probe)


def _probe_bg():
    import threading
    _probe["state"] = "探测中"
    threading.Thread(target=lambda: detect_proxy(), daemon=True).start()


def _proxy_for(spec: str) -> tuple[str, int] | None:
    """spec: "" 不走代理 / "env" 读系统 HTTPS_PROXY（没设就用探测结果）/ "auto" 用探测结果 /
    "http://127.0.0.1:7890" 指定"""
    if not spec:
        return None
    if spec in ("env", "auto"):
        env = "" if spec == "auto" else (
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
        spec = env or _probe.get("proxy") or ""
        if not spec:
            return None
    pu = urllib.parse.urlparse(spec if "://" in spec else "http://" + spec)
    if not pu.hostname:
        return None
    return pu.hostname, pu.port or 8080


def _req(method: str, url: str, headers: dict, payload: dict | None, timeout: int = 120,
         proxy: str = ""):
    u = urllib.parse.urlparse(url)
    https = u.scheme == "https"
    conn_cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    px = _proxy_for(proxy)
    if px:
        conn = conn_cls(px[0], px[1], timeout=timeout)
        conn.set_tunnel(u.hostname, u.port or (443 if https else 80))
    else:
        conn = conn_cls(u.netloc, timeout=timeout)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    path = u.path + (("?" + u.query) if u.query else "")
    h = {"Content-Type": "application/json", **headers}
    try:
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        raw = r.read().decode("utf-8", "replace")
        if r.status >= 400:
            raise RuntimeError(f"HTTP {r.status}: {raw[:300]}")
        return json.loads(raw)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 适配器

def _chat_openai(p: dict, msgs: list[dict], temperature: float, max_tokens: int) -> str:
    base = (p.get("base") or "").rstrip("/")
    url = base + "/chat/completions"
    payload = {"model": p["model"], "messages": msgs,
               "temperature": temperature, "max_tokens": max_tokens}
    d = _req("POST", url, {"Authorization": "Bearer " + (p.get("key") or "")}, payload,
             timeout=int(p.get("timeout") or 90), proxy=p.get("proxy") or "")
    m = d["choices"][0]["message"]
    txt = (m.get("content") or "").strip()
    if not txt:
        # 有些"思考型"模型会把预算全花在 reasoning 上，content 为空
        txt = (m.get("reasoning_content") or m.get("reasoning") or "").strip()
        if txt:
            txt = "[模型只输出了思考过程，建议调大 max_tokens 或换模型]\n" + txt[:800]
    return txt


def _chat_gemini(p: dict, msgs: list[dict], temperature: float, max_tokens: int) -> str:
    base = (p.get("base") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    url = f"{base}/models/{p['model']}:generateContent"
    sys_txt = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    contents = [{"role": ("model" if m["role"] == "assistant" else "user"),
                 "parts": [{"text": m["content"]}]}
                for m in msgs if m["role"] != "system"]
    payload = {"contents": contents,
               "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if sys_txt:
        payload["systemInstruction"] = {"parts": [{"text": sys_txt}]}
    d = _req("POST", url, {"x-goog-api-key": p.get("key") or ""}, payload,
             timeout=int(p.get("timeout") or 90), proxy=p.get("proxy") or "")
    cands = d.get("candidates") or []
    if not cands:
        raise RuntimeError(f"没有候选返回：{json.dumps(d, ensure_ascii=False)[:200]}")
    parts = cands[0].get("content", {}).get("parts", [])
    return "".join(x.get("text", "") for x in parts).strip()


def _chat_ollama(p: dict, msgs: list[dict], temperature: float, max_tokens: int) -> str:
    base = (p.get("base") or "http://127.0.0.1:11434").rstrip("/")
    payload = {"model": p["model"], "messages": msgs, "stream": False,
               "options": {"temperature": temperature, "num_predict": max_tokens}}
    d = _req("POST", base + "/api/chat", {}, payload,
             timeout=int(p.get("timeout") or 180), proxy="")
    return (d.get("message", {}).get("content") or "").strip()


KINDS = {"openai": _chat_openai, "gemini": _chat_gemini, "ollama": _chat_ollama}


def _chain(cfg: dict, role_or_id: str) -> list[str]:
    v = cfg["roles"].get(role_or_id)
    if v is None:
        return [role_or_id]
    return list(v) if isinstance(v, list) else [v]


def chat(role_or_id: str, msgs: list[dict], temperature: float = 0.3,
         max_tokens: int = 1200, timeout: int | None = None) -> tuple[str, dict]:
    """按角色取模型；配了备选就依次退。返回 (文本, 元信息)。"""
    cfg = load_conf()
    tried = []
    for pid in _chain(cfg, role_or_id):
        p = cfg["providers"].get(pid)
        if not p:
            tried.append(f"{pid}：没这个模型")
            continue
        fn = KINDS.get(p.get("kind"))
        if not fn:
            tried.append(f"{pid}：不认识的接口类型 {p.get('kind')}")
            continue
        if timeout:
            p = dict(p, timeout=timeout)
        t = time.time()
        try:
            txt = fn(p, msgs, temperature, max_tokens)
        except Exception as e:          # noqa: BLE001
            tried.append(f"{p.get('label') or pid}：{e}")
            continue
        meta = {"provider": pid, "label": p.get("label") or pid,
                "model": p.get("model"), "ms": int((time.time() - t) * 1000)}
        if len(tried):
            meta["fellback"] = tried
        return txt, meta
    raise RuntimeError("；".join(tried) or "没有可用的模型")


# ---------------------------------------------------------------- 提示词

SYS_ANSWER = """你是 Yan 的医学陪学助教。硬规则，违反等于没做：
1 只依据【资料片段】回答；片段外的记忆不能充当证据。每个结论后标 [编号]。
2 片段没覆盖就写"资料未覆盖"，不要补。
3 三色标签只有【推】【半】【桶】：能从机制推出的标【推】，纯命名/纯数字/纯位置标【桶】，之间标【半】。
4 名词解释一律给片段里的教材原句并标"原句"，禁止改写。
5 中文，密度高，不铺垫，不复述问题。"""

SYS_PROBE = """你是 Yan 的证伪对手，不是老师。他是预推理型：结论他自己会推，他需要的是有人来撞他的链。
做法：
1 先指出这条链里**最弱的一环**是哪一步，为什么弱。
2 给一个**具体的反例**（真实存在的病/现象/数据），能撞断就撞断。
3 如果链其实成立，就说成立，并指出它成立的前提条件是什么——不要为了显得有用而硬找茬。
4 他的链已按句编号（【1】【2】…）。最后一行固定格式：
  断点：第N句｜断在哪条承重墙 / 哪个概念混淆
  断不了就写：断点：未断｜成立的前提条件
中文，不超过 250 字，不铺垫，不安慰。"""

SYS_QUICK = """你给 Yan 写学习单元的抛问。他是预推理型，注意力靠好奇心尖峰启动，没触发就只有 20 分钟。
一个好抛问的标准：
- 是一个**为什么/怎么会**式的问句，不是"简述 X"。
- 答案能从机制推出来，推的过程正好覆盖这段教材的核心。
- 读完让人想马上试着推一遍，而不是想去翻书抄。
只输出问句本身，一句话，不加编号、不加引号、不解释。"""

SYS_QUIZ = """你按大纲给 Yan 出题，目的不是考他懂不懂，是**扫描他预推理的覆盖缺口**。
所以：专挑边角——重点他自己推得到，缺口在非重点、在纯命名、在措辞。
出 5 题，每题一行，格式：`n | 题面 | 【推/半/桶】`
不给答案。中文。"""


# ---------------------------------------------------------------- 业务

def _ctx(results: list[dict], max_chars: int = 7000) -> str:
    out, n = [], 0
    for i, r in enumerate(results, 1):
        blk = f"[{i}] {r['file']} §{r.get('heading','')}\n{r['text']}"
        if n + len(blk) > max_chars:
            break
        out.append(blk)
        n += len(blk)
    return "\n\n".join(out)


def do_ask(q: str, k: int = 6, path_filter: str = "", role: str = "answer") -> dict:
    if _search_fn is None:
        raise RuntimeError("检索未就绪")
    res = _search_fn(q, k, path_filter or None)
    msgs = [{"role": "system", "content": SYS_ANSWER},
            {"role": "user", "content": f"【资料片段】\n\n{_ctx(res)}\n\n【问题】\n{q}"}]
    txt, meta = chat(role, msgs, 0.25, 1400)
    return {"answer": txt, "results": res, "meta": meta}


def do_probe(question: str, chain: str, subject: str = "") -> dict:
    ctx = ""
    if _search_fn is not None and chain:
        try:
            res = _search_fn(chain[:120] or question, 5, subject or None)
            ctx = "\n\n【可参考的教材片段】\n" + _ctx(res, 3000)
        except Exception:
            ctx = ""
    chain_lines = _split_sents(chain)
    numbered = "\n".join(f"【{i}】{s}" for i, s in enumerate(chain_lines, 1)) or chain
    user = f"【他今天的抛问】\n{question}\n\n【他推的链】\n{numbered}{ctx}"
    txt, meta = chat("probe", [{"role": "system", "content": SYS_PROBE},
                               {"role": "user", "content": user}], 0.5, 1600)
    return {"text": txt, "meta": meta, **_verdict(txt, chain_lines)}


# 逆转裁判用的结构化结果（2026-09-23）：前端按 testimony 逐句打字，
# break_index 指向他链里被撞断的那一句（从 0 数），盖红章用；broken=False 时给正面反馈。
_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def _split_sents(t: str) -> list[str]:
    return [m.group(0).strip() for m in _SENT_RE.finditer(t or "") if m.group(0).strip(" 。；;")]


def _verdict(txt: str, chain_lines: list[str]) -> dict:
    body, verdict_line = txt, ""
    for ln in reversed((txt or "").splitlines()):
        if ln.strip().startswith("断点"):
            verdict_line = ln.strip()
            body = txt[: txt.rfind(ln)].strip()
            break
    broken, idx, reason = None, None, ""
    if verdict_line:
        rest = re.sub(r"^断点[:：]\s*", "", verdict_line)
        head, _, reason = rest.partition("｜")
        if not reason:
            head, _, reason = rest.partition("|")
        if "未断" in head:
            broken = False
        else:
            broken = True
            m = re.search(r"第\s*(\d+)\s*句", head)
            if m and 1 <= int(m.group(1)) <= len(chain_lines):
                idx = int(m.group(1)) - 1
        reason = (reason or head).strip()
    return {"testimony": _split_sents(body), "chain_lines": chain_lines,
            "broken": broken, "break_index": idx, "break_reason": reason,
            "verdict_line": verdict_line}


def do_spark(scope_text: str, subject: str = "", hint: str = "") -> dict:
    """从一段教材/章节标题生成今天的抛问。"""
    user = f"科目：{subject}\n范围：{scope_text}"
    if hint:
        user += f"\n\n【这段教材的内容节选】\n{hint[:4000]}"
    txt, meta = chat("quick", [{"role": "system", "content": SYS_QUICK},
                               {"role": "user", "content": user}], 0.9, 300)
    txt = txt.strip().strip("「」\"'").split("\n")[0]
    return {"question": txt, "meta": meta}


def do_quiz(scope_text: str, subject: str = "", hint: str = "") -> dict:
    user = f"科目：{subject}\n范围：{scope_text}"
    if hint:
        user += f"\n\n【大纲/教材节选】\n{hint[:5000]}"
    # 出题是生成活不是推理活：交给 answer 角色，reasoner 会把预算全烧在思考上
    txt, meta = chat("answer", [{"role": "system", "content": SYS_QUIZ},
                                {"role": "user", "content": user}], 0.6, 1600)
    return {"text": txt, "meta": meta}


# ---------------------------------------------------------------- 路由

def _public(cfg: dict) -> dict:
    ps = []
    for pid, p in cfg["providers"].items():
        ps.append({"id": pid, "kind": p.get("kind"), "model": p.get("model"),
                   "label": p.get("label") or pid, "note": p.get("note", ""),
                   "has_key": bool(p.get("key")) or p.get("kind") == "ollama",
                   "key_hint": _mask(p.get("key") or ""),
                   "proxy": p.get("proxy") or "", "timeout": p.get("timeout") or 90})
    return {"providers": ps, "roles": cfg["roles"],
            "roles_desc": {"quick": "抛问 / 小活（要快）", "answer": "检索问答（带引用）",
                           "probe": "证伪 / 出题（要狠）", "local": "离线兜底"},
            "error": _last_error}


def handle_get(path: str, query: dict) -> tuple[int, dict]:
    if path == "/api/llm/proxy":
        if (query.get("refresh") or [""])[0]:
            detect_proxy()
        return 200, dict(_probe)
    if path == "/api/llm/providers":
        d = _public(load_conf())
        d["proxy_probe"] = dict(_probe)
        d["env_proxy"] = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
                          or os.environ.get("https_proxy") or os.environ.get("http_proxy") or "")
        return 200, d
    return 404, {"error": "unknown llm endpoint"}


def handle_post(path: str, body: dict) -> tuple[int, dict]:
    try:
        if path == "/api/llm/test":
            pid = body.get("id") or body.get("role") or "answer"
            txt, meta = chat(pid, [{"role": "user", "content": "只回四个字：连通正常"}], 0, 64,
                             timeout=int(body.get("timeout") or 20))
            return 200, {"ok": True, "text": txt[:160], "meta": meta}
        if path == "/api/llm/roles":
            cfg = load_conf()
            for r, pid in (body.get("roles") or {}).items():
                if r in cfg["roles"] and pid in cfg["providers"]:
                    old = cfg["roles"][r]
                    rest = [x for x in (old if isinstance(old, list) else [old])
                            if x != pid and x in cfg["providers"]]
                    cfg["roles"][r] = ([pid] + rest) if rest else pid
            save_conf(cfg)
            return 200, _public(load_conf())
        if path == "/api/llm/ask":
            return 200, do_ask(body.get("query", ""), int(body.get("k") or 6),
                               body.get("path_filter") or "", body.get("role") or "answer")
        if path == "/api/llm/probe":
            return 200, do_probe(body.get("question", ""), body.get("chain", ""),
                                 body.get("subject", ""))
        if path == "/api/llm/spark":
            return 200, do_spark(body.get("scope", ""), body.get("subject", ""),
                                 body.get("hint", ""))
        if path == "/api/llm/scan/start":          # 完整版缺口扫描（2026-09-23，见 scan_api.py）
            import scan_api
            return 200, scan_api.start(chat, _search_fn, body.get("subject", ""),
                                       int(body.get("n") or 10))
        if path == "/api/llm/scan/grade":
            import scan_api
            return 200, scan_api.grade(chat, body.get("round_id", ""), body.get("answers") or [],
                                       dry=bool(body.get("dry")))
        if path == "/api/llm/quiz":
            return 200, do_quiz(body.get("scope", ""), body.get("subject", ""),
                                body.get("hint", ""))
    except Exception as e:      # noqa: BLE001
        return 200, {"error": str(e)}
    return 404, {"error": "unknown llm endpoint"}


if __name__ == "__main__":
    cfg = load_conf()
    print(json.dumps(_public(cfg), ensure_ascii=False, indent=2))
    for role in ("quick", "answer", "probe"):
        try:
            txt, meta = chat(role, [{"role": "user", "content": "只回四个字：连通正常"}], 0, 64)
            print(f"[{role:6}] {meta['label']:22} {meta['ms']:>5}ms  {txt[:40]}")
        except Exception as e:      # noqa: BLE001
            print(f"[{role:6}] 失败：{e}")
