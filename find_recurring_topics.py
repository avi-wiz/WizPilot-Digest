"""Find recurring question TOPICS across a date range — one-off analysis, NOT
part of the daily digest.

Why this isn't a SQL query: Wiz Join runs on SQLite (see leaderboards_wizjoin
notes), which can only match question text EXACTLY. Real questions are
paraphrased differently almost every time ("show me the last 10 orders" vs
"show last 10 orders please" are the same intent, zero exact-string overlap),
so a GROUP BY on the literal text would report nearly everything as a one-off.
This script instead pulls the raw questions and has the LLM cluster them by
intent, which is the only way to see genuine recurrence.

Not deterministic: rerunning may group borderline cases slightly differently.
Treat the output as a qualitative read of "what people keep asking", not a
number to put in a dashboard.

Run:
  export REDASH_URL=... REDASH_API_KEY=...
  export Q_QUESTIONS=<questions_sample_wizjoin query id>
  export ANTHROPIC_API_KEY=...          # or LLM_BASE_URL+LLM_API_KEY — see llm_client.py
  python3 find_recurring_topics.py --start 2026-08-13 --end 2026-08-19
"""
from __future__ import annotations
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from redash_client import Redash, RedashError  # noqa: E402
import llm_client  # noqa: E402
# NOTE: poc_digest imports `cluster` from this module (for the weekly digest
# section), so importing poc_digest back at module level here would be
# circular. The main()-only CLI path below needs load_dotenv/_env/_qid from
# poc_digest, so that import is deferred into main() instead — this module
# has no top-level dependency on poc_digest.


SYSTEM = (
    "You analyze a sample of real user questions asked to WizPilot, an AI "
    "assistant embedded in WizOrder (an order-management product). Group "
    "them by underlying INTENT, not exact wording — paraphrases of the same "
    "request (e.g. \"show me the last 10 orders\" and \"show last 10 orders "
    "please\") belong in the same group. Ignore one-off or highly specific "
    "questions; only report a group if at least 3 distinct questions in the "
    "sample clearly share the same intent. Do not invent questions that "
    "aren't in the sample. Return STRICT JSON only, no markdown: "
    '{"topics": [{"label": string (<=60 chars, describes the intent), '
    '"count": integer, "example_questions": array of up to 3 verbatim '
    'strings from the sample}]}, sorted by count descending. If nothing '
    "recurs at least 3 times, return {\"topics\": []}."
)


def cluster(questions: list[str]) -> dict:
    """Raises llm_client.LLMError on failure — callers (poc_digest.py's
    fetch_weekly_topics, and main() below) already catch this, so it stays a
    plain exception rather than sys.exit()ing the whole process."""
    raw = llm_client.chat(SYSTEM, json.dumps(questions), max_tokens=1500, temperature=0.2)
    return json.loads(llm_client.strip_code_fence(raw))


def main() -> int:
    from poc_digest import load_dotenv, _env, _qid  # deferred: see note above

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--exclude-tenants", default=os.environ.get("EXCLUDE_TENANTS", "none"))
    ap.add_argument("--env-file", default=os.path.join(_HERE, ".env"))
    args = ap.parse_args()

    loaded = load_dotenv(args.env_file)
    if loaded:
        print(f"→ loaded {loaded} vars from {args.env_file}")

    rd = Redash(_env("REDASH_URL"), _env("REDASH_API_KEY"))
    print(f"→ pulling questions {args.start}..{args.end} (exclude={args.exclude_tenants})")
    # Two plain Date parameters, not a Date Range widget — see
    # questions_sample_wizjoin.redash.sql for why (this Redash build has no
    # dual-range picker).
    rows = rd.refresh(_qid("Q_QUESTIONS"), {
        "start_date": args.start, "end_date": args.end,
        "exclude_tenants": args.exclude_tenants,
    })
    questions = [r["question"] for r in rows if r.get("question")]
    print(f"  {len(questions)} questions in range")
    if not questions:
        print("Nothing to cluster.")
        return 0

    print("→ clustering with the LLM…")
    result = cluster(questions)
    topics = result.get("topics", [])
    if not topics:
        print("No recurring topics found (nothing repeated ≥3 times).")
        return 0

    print(f"\n{len(topics)} recurring topic(s):\n")
    for t in topics:
        print(f"  [{t['count']}x] {t['label']}")
        for ex in t.get("example_questions", []):
            print(f"        - {ex}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RedashError, llm_client.LLMError) as e:
        print(f"\n✗ {e}", file=sys.stderr)
        raise SystemExit(1)
