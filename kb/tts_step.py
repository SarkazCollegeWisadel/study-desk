# -*- coding: utf-8 -*-
"""用 Step token plan 合成语音（StepAudio 2.5 TTS + 自定义复刻音色）。

用法:
    set STEP_API_KEY=xxx
    python tts_step.py "要念的文本" -o out.mp3
    python tts_step.py -f 脚本.txt -o out.mp3 -i "语气温柔，语速偏慢"
    python tts_step.py "（轻笑）你好呀" -o out.mp3 -v YOUR_VOICE_ID
    python tts_step.py --list-voices          # 列出账号下所有音色

要点:
    - 端点固定为 /step_plan/v1（token plan 专属），模型只能用 stepaudio-2.5-tts
    - 用 stepaudio-3-tts 会 404（plan 内不含）
    - 必须清空 HTTP_PROXY 等变量，否则被沙箱代理拦截
    - instruction 定整段基调；正文里用全角括号 () 写句内指令，括号内容不会被朗读
"""
import argparse
import json
import os
import sys
import urllib.request

BASE = "https://api.stepfun.com/step_plan/v1"
MODEL = "stepaudio-2.5-tts"

for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(v, None)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(path: str, payload: dict | None = None, key: str = "", timeout: int = 300):
    url = f"{BASE}{path}"
    if payload is None:
        r = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    else:
        r = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
    return _opener.open(r, timeout=timeout).read()


def main():
    ap = argparse.ArgumentParser(description="Step token plan 语音合成")
    ap.add_argument("text", nargs="?", help="要合成的文本（含 () 内联指令）")
    ap.add_argument("-f", "--file", help="从文件读文本（UTF-8）")
    ap.add_argument("-o", "--out", default="output.mp3", help="输出文件")
    ap.add_argument("-v", "--voice", default=os.environ.get("STEP_VOICE_ID", ""), help="音色 ID")
    ap.add_argument("-i", "--instruction", default="", help="全局基调（≤200 字符）")
    ap.add_argument("-k", "--key", default=os.environ.get("STEP_API_KEY", ""), help="API Key")
    ap.add_argument("--list-voices", action="store_true", help="列出音色后退出")
    args = ap.parse_args()

    if not args.key:
        sys.exit("缺少 API Key：设置环境变量 STEP_API_KEY 或用 -k 传入")

    if args.list_voices:
        data = json.loads(_req("/audio/voices", None, args.key, 60))
        for v in data.get("data", []):
            print(v["id"])
        return

    text = open(args.file, encoding="utf-8").read().strip() if args.file else args.text
    if not text:
        sys.exit("没有输入文本：给位置参数或用 -f 指定文件")

    payload = {
        "model": MODEL,
        "voice": args.voice,
        "input": text,
        "response_format": "mp3",
    }
    if args.instruction:
        payload["instruction"] = args.instruction

    data = _req("/audio/speech", payload, args.key)
    with open(args.out, "wb") as f:
        f.write(data)

    print(f"已合成: {args.out}")
    print(f"  字符数 {len(text)} | 音频 {len(data)} 字节 | 音色 {args.voice}")


if __name__ == "__main__":
    main()
