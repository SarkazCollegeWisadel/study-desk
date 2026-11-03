"""Explicit local CLI; no scheduler, network, or automatic event inference."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .core import EventConflict, InvalidEvent, Ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local evidence-based study memory")
    parser.add_argument("--data-dir", type=Path, help="local storage override; never use a public tracked directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="counts and pending dates only; no study contents")
    sync_parser = commands.add_parser("sync", help="read legacy unit-log notes; never count them as confirmed learning")
    sync_parser.add_argument("--source", default="http://127.0.0.1:8765/api/unit/log?n=1000")
    record = commands.add_parser("record", help="record one explicitly evidenced user event")
    record.add_argument("--file", default="-", help="UTF-8 JSON file, or - for stdin")
    distill = commands.add_parser("distill", help="idempotently summarize all recorded days, including late arrivals")
    distill.add_argument("--date", help="optional single Singapore date YYYY-MM-DD")
    read = commands.add_parser("read", help="read an existing daily summary from the ledger")
    read.add_argument("--date", required=True)
    read.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args(argv)
    # Python on Windows otherwise inherits legacy console encoding.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ledger = Ledger(args.data_dir)
    try:
        if args.command == "status":
            result = ledger.status()
        elif args.command == "sync":
            from .importer import sync
            result = sync(ledger, args.source)
        elif args.command == "record":
            if args.file == "-":
                raw = sys.stdin.buffer.read(1_048_577) if hasattr(sys.stdin, "buffer") else sys.stdin.read(1_048_577)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8-sig")
            else:
                with Path(args.file).open("r", encoding="utf-8-sig") as handle:
                    raw = handle.read(1_048_577)
            if len(raw) > 1_048_576:
                raise InvalidEvent("event JSON exceeds the 1 MiB limit")
            result = ledger.record(json.loads(raw))
        elif args.command == "distill":
            result = ledger.distill(args.date)
        else:
            result = ledger.read(args.date, as_markdown=args.format == "markdown")
            if result is None:
                result = {"status": "not_distilled", "date": args.date, "external_memory_imported": False}
                print(json.dumps(result, ensure_ascii=False))
                return 3
            if args.format == "markdown":
                print(result, end="")
                return 0
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidEvent, EventConflict) as exc:
        # Validation messages are fixed field descriptions, never raw payloads.
        print(json.dumps({"status": "error", "kind": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(json.dumps({"status": "error", "kind": type(exc).__name__,
                          "message": "Local input/storage operation failed; no input contents or credentials logged."}), file=sys.stderr)
        return 2
