"""Ask the UZEX agent from the terminal.

Examples:
    .venv/bin/python -m app.cli "2025-yilda nechta bitim tuzilgan?"
    .venv/bin/python -m app.cli --repl
    .venv/bin/python -m app.cli --json "Сколько сделок было в 2025 году?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent import Agent
from .config import DB_PATH, GEMINI_API_KEY, GEMINI_MODEL
from .llm import GeminiClient


def _print_answer(answer, as_json: bool) -> None:
    if as_json:
        print(json.dumps(
            {
                "text": answer.text,
                "source": answer.source,
                "sql": answer.sql,
                "kind": answer.kind,
                "language": answer.language,
                "row_count": answer.row_count,
                "elapsed_ms": answer.elapsed_ms,
                "plan": answer.plan,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return
    print(answer.text)
    if answer.source:
        print("\n" + answer.source)
    print(f"\n[{answer.elapsed_ms} ms, {answer.kind}, {answer.language}]")


async def _run(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        print("Build it with: python app/build_db.py --data-dir uzex_spot_data --out data/uzex.sqlite", file=sys.stderr)
        return 2
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is missing. Put it in .env (see .env.example).", file=sys.stderr)
        return 2

    llm = GeminiClient(GEMINI_API_KEY, model=GEMINI_MODEL)
    agent = Agent(Path(args.db), llm)
    try:
        if args.repl:
            print("UZEX agent REPL. Empty line or Ctrl-D exits.")
            while True:
                try:
                    question = input("\nSavol / Вопрос> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not question:
                    break
                answer = await agent.ask(question)
                _print_answer(answer, args.json)
        else:
            answer = await agent.ask(args.question)
            _print_answer(answer, args.json)
            if answer.kind == "error":
                return 1
        return 0
    finally:
        await llm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the UZEX grounded agent a question.")
    parser.add_argument("question", nargs="?", help="question in Uzbek or Russian")
    parser.add_argument("--db", default=str(DB_PATH), help="path to uzex.sqlite")
    parser.add_argument("--repl", action="store_true", help="interactive mode")
    parser.add_argument("--json", action="store_true", help="print the full answer as JSON")
    args = parser.parse_args()
    if not args.repl and not args.question:
        parser.error("provide a question or use --repl")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
