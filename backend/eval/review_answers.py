"""Prepare source-bound JSON review packets and validate/summarize annotations.

  python eval/review_answers.py prepare result.json --out review.json
  python eval/review_answers.py summarize review.json --report result.json --out summary.json

Edit verdict/reviewer/reviewer_kind/notes/supports in the packet between steps.
No model, network or database calls are made. Existing files are never overwritten.
"""
import argparse
import json
from pathlib import Path

from answer_review import prepare_packet, summarize_packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("input", type=Path)
    prepare.add_argument("--case-id", action="append", help="select a case (repeatable); default all cases")
    prepare.add_argument("--out", type=Path, required=True)
    summarize = commands.add_parser("summarize")
    summarize.add_argument("input", type=Path)
    summarize.add_argument("--report", type=Path, required=True)
    summarize.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        if args.command == "prepare":
            output = prepare_packet(data, args.case_id)
        else:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            output = summarize_packet(data, report)
        # Exclusive creation also protects the input report and annotation file.
        with args.out.open("x", encoding="utf-8") as f:
            f.write(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"review error: {error}\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
