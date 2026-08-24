"""THROWAWAY: dump user questions from Redash, classify intent, theme the
"Reporting" ones.

Reuses the POC's existing plumbing rather than reinventing it:
  redash_client.Redash  -> Q_QUESTIONS (questions_sample_wizjoin.redash.sql,
                           params start_date / end_date / exclude_tenants;
                           already excludes internal + @wizcommerce. users)
  llm_client.chat       -> whichever provider .env selects (Anthropic wins
                           when ANTHROPIC_API_KEY is set)

Two LLM passes, deliberately:
  1. CLASSIFY  — batched, tags each question's intent. "Reporting" isn't a
     column and isn't keyword-detectable ("how many orders last month" is
     reporting; "how do I add a user" isn't), so this needs a read.
  2. THEME     — clusters ONLY the Reporting questions into <=10 themes and
     quotes real questions back verbatim.

Run:
  python3 poc/reporting_themes.py --days 4
  python3 poc/reporting_themes.py --days 4 --out /tmp/x.json --raw-only
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from redash_client import Redash, RedashError  # noqa: E402
from poc_digest import load_dotenv, _env, _qid  # noqa: E402  (reuse, don't re-copy)
import llm_client  # noqa: E402

CLASSIFY_SYSTEM = (
    "You classify real end-user questions asked to WizPilot, a B2B wholesale "
    "commerce assistant. For each question, assign exactly ONE intent:\n"
    "  Reporting     - asks for data, numbers, metrics, lists, rankings, "
    "trends, comparisons, or an aggregate pulled from business records "
    "(sales, orders, customers, inventory, payments, reps, products).\n"
    "  HowTo         - asks how to perform an action or use a feature.\n"
    "  Troubleshoot  - reports something broken, wrong, or unexpected.\n"
    "  Catalog       - asks about a specific product/SKU's attributes, price, "
    "or availability WITHOUT any aggregation.\n"
    "  Other         - greetings, tests, gibberish, or anything else.\n"
    "Judge intent, not phrasing. 'Show me top customers' is Reporting even "
    "without the word report. Return STRICT JSON only, no markdown: "
    '{"labels": [{"i": <int, the question\'s id from the input>, '
    '"intent": "Reporting"|"HowTo"|"Troubleshoot"|"Catalog"|"Other"}]}. '
    "Return one entry for EVERY input id, in the same order."
)

THEME_SYSTEM = (
    "You are given real user questions to WizPilot that have all been "
    "pre-classified as REPORTING questions (requests for data/metrics). "
    "Cluster them by the UNDERLYING THING BEING ASKED FOR, not by wording — "
    "'top 10 customers by revenue' and 'who are my biggest buyers' are the "
    "SAME theme. Return the 10 most common themes, ordered by how many "
    "questions fall into each, descending. If there are genuinely fewer than "
    "10 distinct themes, return fewer rather than splitting hairs.\n"
    "For each theme give: a short label (<=6 words), the count of questions "
    "in it, a one-line description of what users want, and up to 5 EXAMPLE "
    "questions quoted VERBATIM from the input — never paraphrased. "
    "Return STRICT JSON only, no markdown: "
    '{"themes": [{"label": string, "count": int, "description": string, '
    '"examples": [string]}]}'
)

BATCH = 60  # questions per classify call — keeps each request small enough
            # that a single malformed reply costs one batch, not the whole run.


def fetch(rd: Redash, days: int, exclude: str) -> list[dict]:
    """Pull the trailing `days` of questions (IST dates, as the SQL shifts)."""
    end = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    start = end - dt.timedelta(days=days - 1)
    print(f"→ Redash Q_QUESTIONS {start}..{end} (exclude={exclude})")
    rows = rd.refresh(_qid("Q_QUESTIONS"),
                      {"start_date": start.isoformat(), "end_date": end.isoformat(),
                       "exclude_tenants": exclude})
    return [r for r in rows if (r.get("question") or "").strip()]


def classify(rows: list[dict]) -> list[dict]:
    """Tag each row with an intent, in batches. Rows whose batch fails are
    left intent=None rather than silently dropped or mislabelled."""
    for r in rows:
        r["intent"] = None
    for off in range(0, len(rows), BATCH):
        chunk = rows[off:off + BATCH]
        payload = [{"i": off + n, "q": r["question"]} for n, r in enumerate(chunk)]
        try:
            raw = llm_client.chat(CLASSIFY_SYSTEM, json.dumps(payload, ensure_ascii=False),
                                  max_tokens=4000, temperature=0)
            labels = json.loads(llm_client.strip_code_fence(raw))["labels"]
            by_i = {int(l["i"]): l.get("intent") for l in labels}
            for n, r in enumerate(chunk):
                r["intent"] = by_i.get(off + n)
        except Exception as e:  # noqa: BLE001 - one bad batch shouldn't kill the run
            print(f"  ! classify batch {off}-{off+len(chunk)} failed ({e})", file=sys.stderr)
        done = min(off + BATCH, len(rows))
        print(f"  classified {done}/{len(rows)}")
    return rows


def theme(reporting: list[dict]) -> list[dict]:
    """Cluster the Reporting questions into <=10 themes, then verify every
    quoted example is verbatim — an LLM told 'exact text' still paraphrases."""
    if not reporting:
        return []
    qs = [r["question"] for r in reporting]
    raw = llm_client.chat(THEME_SYSTEM, json.dumps(qs, ensure_ascii=False),
                          max_tokens=4000, temperature=0.2)
    themes = json.loads(llm_client.strip_code_fence(raw)).get("themes", [])
    real = set(qs)
    for t in themes:
        kept = [e for e in (t.get("examples") or []) if e in real]
        if len(kept) != len(t.get("examples") or []):
            print(f"  ! dropped non-verbatim example(s) from theme {t.get('label')!r}",
                  file=sys.stderr)
        t["examples"] = kept
    return themes[:10]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4, help="trailing days, IST (default 4)")
    ap.add_argument("--out", default=os.path.join(_HERE, "reporting_themes.json"))
    ap.add_argument("--raw-only", action="store_true", help="dump questions, skip both LLM passes")
    ap.add_argument("--env-file", default=os.path.join(_HERE, ".env"))
    args = ap.parse_args()

    load_dotenv(args.env_file)
    rd = Redash(_env("REDASH_URL"), _env("REDASH_API_KEY"))
    exclude = os.environ.get("EXCLUDE_TENANTS", "none") or "none"

    rows = fetch(rd, args.days, exclude)
    print(f"  {len(rows)} questions pulled")
    if not rows:
        print("✗ no questions in range", file=sys.stderr)
        return 1

    out = {"days": args.days, "total_questions": len(rows),
           "questions": [{"question": r["question"], "tenant": r.get("tenant"),
                          "asked_at": r.get("asked_at")} for r in rows]}

    if not args.raw_only:
        print(f"→ LLM: {llm_client.active_provider()}")
        print("→ classifying intent…")
        classify(rows)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["intent"] or "UNCLASSIFIED"] = counts.get(r["intent"] or "UNCLASSIFIED", 0) + 1
        print("  intent mix: " + ", ".join(f"{k}={v}" for k, v in
                                           sorted(counts.items(), key=lambda kv: -kv[1])))
        reporting = [r for r in rows if r["intent"] == "Reporting"]
        print(f"→ theming {len(reporting)} Reporting question(s)…")
        themes = theme(reporting)
        out.update(intent_counts=counts, reporting_count=len(reporting), themes=themes)
        for r, q in zip(rows, out["questions"]):
            q["intent"] = r["intent"]

        print(f"\n=== TOP {len(themes)} REPORTING THEMES "
              f"({len(reporting)} of {len(rows)} questions) ===")
        for n, t in enumerate(themes, 1):
            print(f"\n{n}. {t['label']} — {t.get('count')} question(s)")
            print(f"   {t.get('description','')}")
            for e in t.get("examples", []):
                print(f"     · {e}")

    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\n✓ wrote {args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RedashError, RuntimeError) as e:
        print(f"\n✗ {e}", file=sys.stderr)
        raise SystemExit(1)
