"""WizPilot daily FAILURE AUDIT — separate job from poc_digest.py.

Answers one question: "what did users ask yesterday that WizPilot didn't
actually answer?" Three failure shapes, per the ask:
  1. blank response
  2. explicit "can't do that" / out-of-scope
  3. answered without data (no widget rendered, or an empty result set)

Pipeline: Redash (ad-hoc SQL) -> deterministic triage -> LLM only for the
ambiguous remainder -> Slack.

Deliberately SEPARATE from the digest, and shares nothing at runtime:
  - own entrypoint (this file), own Railway service, own cron
  - own Slack app/token (AUDIT_SLACK_BOT_TOKEN) and channel
  - read-only: same Redash queries, no writes anywhere
It DOES reuse redash_client / llm_client / audit_client by import, so there is
one HTTP client and one LLM wrapper in this repo rather than two that drift.

Run:
  python3 audit_digest.py --dry-run              # yesterday IST, print only
  python3 audit_digest.py --date 2026-08-30
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from redash_client import Redash, RedashError   # noqa: E402
from poc_digest import load_dotenv, _env, _qid  # noqa: E402
from audit_client import triage                 # noqa: E402
import llm_client                               # noqa: E402

# Human-facing labels + the order sections appear in Slack. Ordered by how
# actionable the failure is, not by volume.
BUCKETS = [
    ("BLANK",     "🔴 No response at all"),
    ("REFUSAL",   "🚫 Said it can't help"),
    ("ERROR",     "⚠️ Hit an error"),
    ("NO_DATA",   "🕳️ Ran but found nothing"),
    ("CLARIFY",   "❓ Asked a question back"),
    ("NO_WIDGET", "📄 Answered without data"),
]

SQL = """
SELECT
  datetime(a.asked_at, '+5 hours', '+30 minutes')        AS asked_at_ist,
  COALESCE(c.tenant_name, a.tenant_id)                   AS tenant,
  COALESCE(a.session_user_name, b.full_name, a.user_id)  AS user_name,
  a.question,
  COALESCE(a.response, '')                               AS response
FROM query_1953 a
LEFT JOIN query_1954 b ON b.user_id = a.user_id
LEFT JOIN query_1915 c ON c.tenant_id = a.tenant_id
WHERE date(datetime(a.asked_at, '+5 hours', '+30 minutes')) = '{day}'
  AND COALESCE(a.session_user_name, '') <> 'Internal'
  AND (b.email IS NULL OR b.email NOT LIKE '%@wizcommerce.%')
  AND instr(',' || '{exclude}' || ',', ',' || a.tenant_id || ',') = 0
  AND a.question IS NOT NULL AND TRIM(a.question) <> ''
ORDER BY a.asked_at
"""

CLASSIFY_SYSTEM = (
    "You audit an AI assistant for a B2B wholesale commerce platform. Each item "
    "is a user question and the assistant's reply. The reply may contain the "
    "literal marker [widget], which means a data table/chart WAS rendered "
    "(its contents are not shown to you). Replies are truncated at 1500 chars.\n"
    "For each item decide whether the user actually got what they asked for:\n"
    "  OK        - the reply answers the question (a how-to answer, a "
    "confirmation of an action, or data the user asked for). Chit-chat and "
    "pleasantries are OK.\n"
    "  NO_WIDGET - the user asked for DATA (numbers, a list, a report, a "
    "comparison) but the reply is prose with no data and no [widget].\n"
    "  REFUSAL   - the assistant declined or said it was out of scope.\n"
    "  NO_DATA   - the assistant ran the request but found nothing.\n"
    "Judge the QUESTION's intent: a question that never wanted data (\"how do I "
    "reset my password\") answered in prose is OK, not NO_WIDGET. "
    "Return STRICT JSON only, no markdown: "
    '{"labels":[{"i":<int, the item id>,"verdict":"OK"|"NO_WIDGET"|"REFUSAL"|"NO_DATA"}]}. '
    "One entry for EVERY id."
)

SUMMARY_SYSTEM = (
    "You are a product analyst reviewing failures of WizPilot, an AI assistant "
    "for wholesale commerce. You are given user questions that the assistant "
    "did NOT answer well, already grouped by failure type. Identify the "
    "recurring THEMES in what users wanted but didn't get — cluster by the "
    "underlying capability gap, not by wording. Be concrete and specific about "
    "capability, e.g. 'multi-step margin/profitability analysis' rather than "
    "'complex queries'. Do not invent numbers. Return STRICT JSON only: "
    '{"headline": string (<=90 chars, the single biggest gap), '
    '"themes": [{"label": string (<=6 words), "count": int, "example": string '
    '(verbatim from the input)}], "recommendation": string (<=180 chars, the '
    'one capability worth building or fixing first)}. At most 5 themes.'
)

BATCH = 40


def _json_obj(raw: str) -> dict:
    """Parse the first JSON object out of an LLM reply.

    Haiku sometimes appends prose after the JSON ("Extra data: line 2" from
    json.loads), so slicing to the outermost {...} is more robust than trusting
    the whole string. strip_code_fence handles the ```json case first.
    """
    t = llm_client.strip_code_fence(raw).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j <= i:
            raise
        return json.loads(t[i:j + 1])


def run_adhoc(rd: Redash, ds: int, sql: str) -> list[dict]:
    body = {"query": sql, "data_source_id": ds, "max_age": 0}
    req = urllib.request.Request(rd.base + "/api/query_results",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Key {rd.key}")
    req.add_header("Content-Type", "application/json")
    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
    if "job" in r:
        jid = r["job"]["id"]
        for _ in range(150):
            j = rd._req("GET", f"/api/jobs/{jid}")["job"]
            if j["status"] == 3:
                r = rd._req("GET", f"/api/query_results/{j['query_result_id']}")
                break
            if j["status"] == 4:
                raise RedashError(f"audit query failed: {j.get('error')}")
            time.sleep(2)
        else:
            raise RedashError("audit query timed out")
    return r["query_result"]["data"]["rows"]


def classify_ambiguous(rows: list[dict]) -> None:
    """Ask the LLM to adjudicate only the AMBIGUOUS rows, in batches.

    Mutates each row's 'bucket' in place. A failed batch leaves the rows as
    AMBIGUOUS rather than guessing — they're reported under their own heading
    so a silent LLM outage can't quietly shrink the audit.
    """
    amb = [r for r in rows if r["bucket"] == "AMBIGUOUS"]
    if not amb:
        return
    print(f"  → {len(amb)} ambiguous turn(s) to the LLM")
    for off in range(0, len(amb), BATCH):
        chunk = amb[off:off + BATCH]
        payload = [{"i": off + n, "question": r["question"],
                    "reply": (r["response"] or "")[:900]}
                   for n, r in enumerate(chunk)]
        try:
            raw = llm_client.chat(CLASSIFY_SYSTEM, json.dumps(payload, ensure_ascii=False),
                                  max_tokens=3000, temperature=0)
            by_i = {int(l["i"]): l.get("verdict")
                    for l in _json_obj(raw)["labels"]}
            for n, r in enumerate(chunk):
                v = by_i.get(off + n)
                if v in ("OK", "NO_WIDGET", "REFUSAL", "NO_DATA"):
                    r["bucket"] = v
                    r["reason"] = "LLM adjudicated"
        except Exception as e:  # noqa: BLE001 - one bad batch must not kill the audit
            print(f"  ! classify batch {off} failed ({e}); leaving as AMBIGUOUS", file=sys.stderr)


def summarize(failures: list[dict]) -> dict | None:
    """LLM themes over the failed questions. None on failure — the audit still
    sends its counts and examples without this section."""
    if not failures:
        return None
    payload = [{"bucket": r["bucket"], "question": r["question"]} for r in failures]
    try:
        raw = llm_client.chat(SUMMARY_SYSTEM, json.dumps(payload, ensure_ascii=False),
                              max_tokens=1200, temperature=0.2)
        d = _json_obj(raw)
        assert "headline" in d and "themes" in d
        return d
    except Exception as e:  # noqa: BLE001
        print(f"  ! theme summary failed ({e}); sending without it", file=sys.stderr)
        return None


def build_blocks(day: str, total: int, rows: list[dict], summary: dict | None) -> list[dict]:
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_bucket[r["bucket"]].append(r)
    n_fail = sum(len(by_bucket[b]) for b, _ in BUCKETS) + len(by_bucket.get("AMBIGUOUS", []))
    pct = (100.0 * n_fail / total) if total else 0.0

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"WizPilot gaps — {day}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*{n_fail}* of *{total}* questions ({pct:.0f}%) didn't get a real answer."}},
    ]
    if summary and summary.get("headline"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"_{summary['headline']}_"}})

    counts = " · ".join(f"{label.split(' ',1)[0]} {len(by_bucket[b])}"
                        for b, label in BUCKETS if by_bucket[b])
    if counts:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": counts}]})
    blocks.append({"type": "divider"})

    for bucket, label in BUCKETS:
        items = by_bucket.get(bucket) or []
        if not items:
            continue
        lines = []
        for r in items[:5]:
            q = r["question"].replace("\n", " ").strip()
            q = q[:160] + "…" if len(q) > 160 else q
            lines.append(f"• _{q}_\n   ↳ {r['tenant']} · {r['user_name']}")
        more = f"\n_…and {len(items)-5} more_" if len(items) > 5 else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*{label}* ({len(items)})\n" + "\n".join(lines) + more}})

    amb = by_bucket.get("AMBIGUOUS") or []
    if amb:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*🤷 Unclassified* ({len(amb)}) — the LLM pass didn't "
                               "run or failed; these need a manual look."}})

    if summary and summary.get("themes"):
        lines = [f"{i}. *{t['label']}* — {t.get('count','?')}"
                 for i, t in enumerate(summary["themes"][:5], 1)]
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "*Recurring gaps*\n" + "\n".join(lines)}})
    if summary and summary.get("recommendation"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*Worth fixing first:* {summary['recommendation']}"}})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": "Excludes internal users and demo tenants. "
                           "Responses are truncated at 1500 chars upstream."}]})
    return blocks


def post(token: str, channel: str, blocks: list[dict], text: str) -> None:
    body = json.dumps({"channel": channel, "blocks": blocks, "text": text,
                       "unfurl_links": False}).encode()
    req = urllib.request.Request("https://slack.com/api/chat.postMessage",
                                 data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if not resp.get("ok"):
        raise RuntimeError(f"Slack rejected the post: {resp.get('error')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD IST (default: yesterday)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env-file", default=os.path.join(_HERE, ".env"))
    args = ap.parse_args()

    load_dotenv(args.env_file)
    ist_today = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    day = args.date or (ist_today - dt.timedelta(days=1)).isoformat()

    exclude = os.environ.get("EXCLUDE_TENANTS", "none") or "none"
    if exclude.strip().lower() == "none":
        exclude = ""

    rd = Redash(_env("REDASH_URL"), _env("REDASH_API_KEY"))
    ds = rd.get_query(_qid("Q_METRICS"))["data_source_id"]

    print(f"→ auditing {day} (LLM: {llm_client.active_provider()})")
    rows = run_adhoc(rd, ds, SQL.format(day=day, exclude=exclude))
    total = len(rows)
    print(f"  {total} question(s)")
    if not total:
        print("  nothing to audit")
        return 0

    for r in rows:
        r["bucket"], r["reason"] = triage(r["question"], r["response"])
    print("  triage: " + ", ".join(f"{k}={v}" for k, v in
                                   Counter(r["bucket"] for r in rows).most_common()))

    classify_ambiguous(rows)
    failures = [r for r in rows if r["bucket"] not in ("OK",)]
    print("  final:  " + ", ".join(f"{k}={v}" for k, v in
                                   Counter(r["bucket"] for r in rows).most_common()))

    if not failures:
        print("  ✓ no failures — nothing to report")
        if args.dry_run:
            return 0

    summary = summarize(failures)
    blocks = build_blocks(day, total, rows, summary)
    text = f"WizPilot gaps — {day}: {len(failures)} of {total} questions unanswered"

    if args.dry_run:
        print("\n--- DRY RUN ---")
        print(json.dumps({"text": text, "blocks": blocks}, indent=2, ensure_ascii=False))
        return 0

    # Own token/channel: never falls back to the digest's, so a misconfigured
    # audit stays silent rather than posting into the digest channel.
    post(_env("AUDIT_SLACK_BOT_TOKEN"), _env("AUDIT_SLACK_CHANNEL"), blocks, text)
    print(f"✓ posted audit for {day}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RedashError, RuntimeError) as e:
        print(f"\n✗ {e}", file=sys.stderr)
        raise SystemExit(1)
