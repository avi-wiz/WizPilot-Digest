"""WizPilot digest POC — Redash extraction instead of direct Postgres.

Closes the loop: Redash (extract) -> LLM (summarize) -> Slack (deliver).
Reuses the shipped app's Slack block builder so the output is identical to prod.

Extraction source: the "Wiz Join" Redash queries (metrics_wizjoin.redash.sql /
leaderboards_wizjoin.redash.sql), NOT LiteLLM_SpendLogs. These already carry
resolved tenant/user names (via query_1954 + query_1915) and a real
internal-user filter (session_user_name = 'Internal' / @wizcommerce. email),
so there is no separate name-resolution query or ultron-replica call here.

On Mondays only, also pulls the trailing 7 days of questions and has the LLM
cluster them for a "Recurring topics this week" section (see
find_recurring_topics.py for why this needs an LLM pass and can't be a plain
GROUP BY, and why it's weekly rather than daily). Set Q_QUESTIONS to enable;
leave it unset to skip this section entirely on every run.

Run:
  export REDASH_URL=https://redash.yourco.com
  export REDASH_API_KEY=...
  export Q_METRICS=1997 Q_LEADERBOARDS=1998   # the *_wizjoin query ids
  export Q_TENANT_USERS=2000                  # optional: tenant_users_wizjoin id (combined table)
  export Q_QUESTIONS=1999                     # optional: questions_sample_wizjoin id (weekly topics)
  export Q_TODAY_QUESTIONS=2001               # optional: questions_today_wizjoin id (pick-3 section)
  export EXCLUDE_TENANTS=none                 # or a tenant_id, matches the Redash dropdown

  # LLM — pick ONE of the two (see llm_client.py):
  export ANTHROPIC_API_KEY=sk-ant-...         # direct Anthropic; ANTHROPIC_MODEL defaults to Haiku 4.5
  #   or:
  export LLM_BASE_URL=https://api.x.ai/v1 LLM_API_KEY=xai-... LLM_MODEL=grok-4  # OpenAI-compatible

  export SLACK_BOT_TOKEN=xoxb-... SLACK_CHANNEL=#your-test-channel
  python3 poc_digest.py --dry-run          # print, don't post
  python3 poc_digest.py --date 2026-08-19  # post for real
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

# Resolve paths relative to THIS file so the script runs from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                                   # redash_client, slack (local copy)
from redash_client import Redash, RedashError  # noqa: E402
from find_recurring_topics import cluster as cluster_topics  # noqa: E402
import llm_client  # noqa: E402


def load_dotenv(path: str) -> int:
    """Minimal .env loader (stdlib only, no python-dotenv needed).

    Real environment variables WIN over .env, so you can override a single
    value inline:  EXCLUDE_TENANTS=none python3 poc/poc_digest.py --dry-run
    """
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            # Quoted value: take what's inside the quotes, ignore any trailing
            # comment. Unquoted: strip a trailing "# comment" only when preceded
            # by whitespace, so values like #slack-channel survive intact.
            mq = re.match(r'^([\'"])(.*?)\1', val)
            val = mq.group(2) if mq else re.split(r"\s+#", val, maxsplit=1)[0].strip()
            if key and key not in os.environ:                   # env wins
                os.environ[key] = val
                n += 1
    return n


def _env(name: str, required: bool = True) -> str:
    v = os.environ.get(name, "")
    if required and not v:
        sys.exit(f"Missing required env var: {name}")
    return v


def _qid(name: str) -> int:
    """Query id from env. Accepts a bare id or a pasted Redash URL."""
    raw = _env(name).strip()
    if raw.isdigit():
        return int(raw)
    m = re.search(r"/queries/(\d+)", raw)      # .../queries/1997/source
    if m:
        return int(m.group(1))
    sys.exit(f"{name}={raw!r} is not a query id. Use the number from "
             f"https://<redash>/queries/<ID>/source (e.g. 1997).")


def _one(rows: list[dict], what: str) -> dict:
    if not rows:
        raise RuntimeError(f"{what} returned no rows — refusing to build a misleading digest.")
    return rows[0]


def _name(row: dict, key: str, placeholder: str, what: str) -> str:
    """Resolve a name column, distinguishing the two ways it can be absent.

    A MISSING KEY means the deployed Redash query doesn't emit that column at
    all — i.e. it has drifted behind the .sql file in this repo. That is a bug
    and it used to fail silently: the `or "Unknown ..."` fallback turned a
    schema mismatch into plausible-looking Slack output ("Unknown user @
    Unknown tenant") that nobody could distinguish from real unresolvable data.
    Shout about it on stderr so the next drift is visible.

    A PRESENT-BUT-NULL value is the case the placeholder was actually written
    for — the id genuinely didn't resolve in query_1954/query_1915. Stays quiet.
    """
    if key not in row:
        print(f"  ! {key} missing from the {what} query result — the deployed Redash "
              f"query is out of date with the local .sql file; showing {placeholder!r}",
              file=sys.stderr)
        return placeholder
    return row[key] or placeholder


def extract(rd: Redash, target: str, exclude: str) -> dict:
    """Pull metrics + leaderboards via the Wiz Join Redash queries.

    No `tz` parameter: metrics_wizjoin.redash.sql/leaderboards_wizjoin.redash.sql
    do the UTC->IST shift in-SQL (asked_at is UTC, confirmed) rather than taking
    a timezone parameter — sending an undeclared param 400s in Redash the same
    way a missing one does, so don't add `tz` back here without adding it there.

    No separate names query: both Wiz Join queries already resolve tenant/user
    names via query_1954 + query_1915, so leaderboard rows carry `name` and
    metrics carries top_thread_tenant_name / top_thread_user_name directly.
    """
    p = {"target_date": target, "exclude_tenants": exclude}

    m = _one(rd.refresh(_qid("Q_METRICS"), p), "metrics")
    if not m.get("total_messages"):
        raise RuntimeError(f"No WizPilot activity for {target}.")

    # Leaderboards declares one extra parameter. Redash rejects parameters a
    # query doesn't declare, so top_n goes here rather than in the shared dict.
    top_n = int(os.environ.get("TOP_N", "3"))
    lb = rd.refresh(_qid("Q_LEADERBOARDS"), {**p, "top_n": top_n})
    tenants = [r for r in lb if r.get("kind") == "tenant"]
    users = [r for r in lb if r.get("kind") == "user"]

    # Optional: combined tenant/messages/users table (tenant_users_wizjoin.
    # redash.sql) — supersedes top_tenants+top_users in slack.py when present.
    # Skipped gracefully if Q_TENANT_USERS isn't configured, same pattern as
    # the weekly-topics query.
    tenant_users = None
    if os.environ.get("Q_TENANT_USERS"):
        try:
            tu_rows = rd.refresh(_qid("Q_TENANT_USERS"), {**p, "top_n": top_n})
            tenant_users = [{"tenant": r["tenant"], "messages": r["messages"], "users": r["users"]}
                            for r in tu_rows]
        except Exception as e:  # noqa: BLE001 - this section is a bonus, never fatal
            print(f"  ! tenant_users query failed ({e}); falling back to top_tenants/top_users", file=sys.stderr)

    # Baseline: same weekday last week (behaviour is weekly-seasonal).
    prev_week = (dt.date.fromisoformat(target) - dt.timedelta(days=7)).isoformat()
    try:
        base = _one(rd.refresh(_qid("Q_METRICS"), {**p, "target_date": prev_week}), "baseline")
    except Exception as e:  # noqa: BLE001 - baseline is nice-to-have, not fatal
        print(f"  ! weekly baseline unavailable ({e}); those deltas will be omitted", file=sys.stderr)
        base = {}

    # Day-on-day baseline: catches a real trend or a today-specific anomaly
    # that the weekly comparison can't, since it's controlling for weekday.
    prev_day = (dt.date.fromisoformat(target) - dt.timedelta(days=1)).isoformat()
    try:
        base_dod = _one(rd.refresh(_qid("Q_METRICS"), {**p, "target_date": prev_day}), "dod baseline")
    except Exception as e:  # noqa: BLE001 - baseline is nice-to-have, not fatal
        print(f"  ! day-on-day baseline unavailable ({e}); those deltas will be omitted", file=sys.stderr)
        base_dod = {}

    m["report_date"] = target
    m["top_thread_tenant_name"] = _name(m, "top_thread_tenant_name", "Unknown tenant", "metrics")
    m["top_thread_user_name"] = _name(m, "top_thread_user_name", "Unknown user", "metrics")
    m["top_tenants"] = [
        {"name": _name(t, "name", "Unknown tenant", "leaderboards"),
         "messages": t["messages"], "distinct_users": t["distinct_users"]}
        for t in tenants
    ]
    m["top_users"] = [
        {"name": _name(u, "name", "Unknown user", "leaderboards"),
         "tenant_name": _name(u, "tenant_id_ref", "Unknown tenant", "leaderboards"),
         "messages": u["messages"]}
        for u in users
    ]
    if tenant_users is not None:
        m["tenant_users"] = tenant_users
    cmp_keys = ("active_users", "active_tenants", "total_conversations", "total_messages")
    m["comparison"] = {k: base.get(k) for k in cmp_keys}
    m["comparison_dod"] = {k: base_dod.get(k) for k in cmp_keys}
    return m


def fetch_weekly_topics(rd: Redash, target: str, exclude: str) -> dict | None:
    """Recurring-topic clustering over the trailing 7 days ending `target`.

    Gated to Mondays only (weekday() == 0): a single day's questions are too
    thin a sample for "recurring" to mean anything (see
    questions_sample_wizjoin.redash.sql / find_recurring_topics.py — SQLite
    can only match exact text, so this needs an LLM pass over real volume,
    not a query). Running it daily would spend an LLM call on a sample that
    will usually cluster to nothing. Returns None on any day but Monday, or
    if Q_QUESTIONS isn't configured — this whole feature is optional.
    """
    if dt.date.fromisoformat(target).weekday() != 0:  # 0 = Monday
        return None
    qid_raw = os.environ.get("Q_QUESTIONS", "")
    if not qid_raw:
        print("  ! Q_QUESTIONS not set; skipping weekly recurring-topics section", file=sys.stderr)
        return None

    start = (dt.date.fromisoformat(target) - dt.timedelta(days=6)).isoformat()
    try:
        # Two plain Date parameters, not a Date Range widget — this Redash
        # build has no dual-range picker (confirmed live: the parameter type
        # dropdown offers only Text/Number/Date/Dropdown), so
        # questions_sample_wizjoin.redash.sql takes start_date/end_date.
        rows = rd.refresh(_qid("Q_QUESTIONS"),
                          {"start_date": start, "end_date": target, "exclude_tenants": exclude})
    except Exception as e:  # noqa: BLE001 - this section is a bonus, never fatal
        print(f"  ! weekly topics query failed ({e}); skipping section", file=sys.stderr)
        return None

    questions = [r["question"] for r in rows if r.get("question")]
    if not questions:
        return None
    try:
        result = cluster_topics(questions)
    except Exception as e:  # noqa: BLE001
        print(f"  ! topic clustering failed ({e}); skipping section", file=sys.stderr)
        return None
    return {"window": f"{start}..{target}", "sample_size": len(questions),
            "topics": result.get("topics") or []}


def weekly_topics_block(wt: dict) -> dict | None:
    """Slack block for the weekly recurring-topics section, or None if empty."""
    if not wt or not wt.get("topics"):
        return None
    lines = "\n".join(
        f"{i+1}. *{t['label']}* — asked {t['count']}x" for i, t in enumerate(wt["topics"][:5])
    )
    return {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🔁 Recurring topics this week* ({wt['window']}, "
                    f"{wt['sample_size']} questions sampled)\n{lines}"}}


_PICK3_SYSTEM = (
    "You are shown a sample of real user questions asked to WizPilot on one "
    "day, each with the asking tenant. Select the 3 you judge MOST COMPLEX or "
    "interesting to a product/GTM audience. Complexity means: asks for "
    "multi-step reasoning, spans multiple data dimensions (time + region + "
    "product, etc.), requests a comparison or anomaly detection, or is "
    "unusually specific/unusual for a support-style assistant — NOT merely a "
    "long sentence. A short question can be more interesting than a long "
    "one; judge substance, not length. Return each question's EXACT verbatim "
    "text, unedited — do not paraphrase, summarize, or truncate it. If fewer "
    "than 3 qualify as genuinely interesting, return fewer; do not pad with "
    "mundane ones just to reach 3. Return STRICT JSON only, no markdown: "
    '{"picks": [{"question": string (verbatim from the sample), '
    '"tenant": string, "why": string (<=100 chars, what makes it complex)}]}.'
)


def pick_interesting_questions(rows: list[dict]) -> list[dict]:
    """Ask the LLM to select the 3 most complex/interesting questions,
    verbatim, from today's sample. See _PICK3_SYSTEM for the actual selection
    criteria — this is a judgment call the LLM is suited for (what reads as
    substantive vs. routine), not a numeric score, and not something SQL can
    do since "interesting" isn't a column.
    """
    if not rows:
        return []
    sample = [{"question": r["question"], "tenant": r.get("tenant", "")} for r in rows]
    try:
        raw = llm_client.chat(_PICK3_SYSTEM, json.dumps(sample), max_tokens=800, temperature=0.2)
        picks = json.loads(llm_client.strip_code_fence(raw)).get("picks", [])
    except Exception as e:  # noqa: BLE001 - this section is a bonus, never fatal
        print(f"  ! pick-3 questions failed ({e}); skipping section", file=sys.stderr)
        return []

    # Verify each pick is actually verbatim from the sample — an LLM asked
    # for "exact text" will sometimes still paraphrase. Drop anything that
    # doesn't match a real row rather than show a fabricated/altered quote.
    real_questions = {r["question"] for r in rows}
    verified = [p for p in picks if p.get("question") in real_questions]
    dropped = len(picks) - len(verified)
    if dropped:
        print(f"  ! dropped {dropped} pick(s) that weren't verbatim from the sample", file=sys.stderr)
    return verified[:3]


def interesting_questions_block(picks: list[dict]) -> dict | None:
    """Slack block for the 'interesting questions today' section, or None."""
    if not picks:
        return None
    lines = "\n\n".join(
        f"{i+1}. _{p['question']}_\n   — *{p.get('tenant','')}* · {p.get('why','')}"
        for i, p in enumerate(picks)
    )
    return {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🧠 Interesting questions today*\n{lines}"}}


def summarize(metrics: dict) -> dict:
    """Ask the LLM for prose. Never lets it compute a number; falls back on failure.

    Provider-agnostic via llm_client.chat() — set either ANTHROPIC_API_KEY
    (direct Anthropic) or LLM_BASE_URL+LLM_API_KEY (OpenAI-compatible: xAI
    Grok, a LiteLLM proxy, OpenAI itself).
    """
    system = (
        "You are a product analyst writing a daily usage digest for WizPilot. You are given "
        "PRE-COMPUTED metrics for one day, with TWO baselines: `comparison` (same weekday "
        "last week — controls for weekly seasonality) and `comparison_dod` (yesterday — "
        "catches a same-week trend or a today-specific spike/drop). Treat them as distinct "
        "signals; don't conflate them, and don't repeat the same swing twice if both "
        "baselines happen to agree. Do not invent, recompute or alter any number. "
        "Return STRICT JSON only: "
        '{"headline": string (<=90 chars), "narrative": string (2-3 sentences), '
        '"insights": array of 2-3 short strings}. '
        "If a comparison field is null, say the baseline is unavailable rather than guessing."
    )
    try:
        raw = llm_client.chat(system, json.dumps(metrics, default=str), max_tokens=500, temperature=0.3)
        data = json.loads(llm_client.strip_code_fence(raw))
        assert {"headline", "narrative", "insights"} <= data.keys()
        return data
    except Exception as e:  # noqa: BLE001 - digest must survive a bad LLM turn
        return _fallback(metrics, str(e))


def _fallback(m: dict, error: str) -> dict:
    return {
        "headline": f"WizPilot: {m.get('active_users','?')} active users, "
                    f"{m.get('total_conversations','?')} conversations",
        "narrative": (f"{m.get('active_users','?')} users across {m.get('active_tenants','?')} "
                      f"tenants ran {m.get('total_messages','?')} messages in "
                      f"{m.get('total_conversations','?')} conversations."),
        "insights": [f"{m.get('new_users','?')} new users, {m.get('new_tenants','?')} new tenants."],
        "_llm_fallback": True, "_llm_error": error,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: yesterday IST)")
    ap.add_argument("--dry-run", action="store_true", help="print payload, do not post")
    ap.add_argument("--env-file", default=os.path.join(_HERE, ".env"),
                    help="path to .env (default: poc/.env)")
    args = ap.parse_args()

    loaded = load_dotenv(args.env_file)
    if loaded:
        print(f"→ loaded {loaded} vars from {args.env_file}")
    print(f"→ LLM provider: {llm_client.active_provider()}")

    import slack  # local copy — see poc/slack.py header for why it's duplicated

    # dt.date.today() uses the SYSTEM clock — UTC in a Railway container, not
    # IST. That only silently disagrees with "yesterday in IST" during a
    # ~5.5hr window (roughly UTC 18:30-23:59, i.e. IST midnight-05:30) —
    # verified by sweeping every hour. The scheduled 2:30 UTC / 08:00 IST run
    # sits outside that window, but computing explicitly in IST removes the
    # landmine for any backfill/manual run at a different hour.
    ist_today = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    target = args.date or (ist_today - dt.timedelta(days=1)).isoformat()
    # "none" (not "") is the exclude_tenants Dropdown's real empty option in
    # the Wiz Join queries — Redash 400s on a blank value for that parameter.
    exclude = os.environ.get("EXCLUDE_TENANTS", "none") or "none"

    rd = Redash(_env("REDASH_URL"), _env("REDASH_API_KEY"))
    print(f"→ Redash {rd.base} | date={target} | exclude={exclude}")

    print("→ extracting…")
    m = extract(rd, target, exclude)
    print(f"  {m['total_messages']} msgs / {m['total_conversations']} convos / "
          f"{m['active_users']} users / {m['active_tenants']} tenants")

    print("→ summarizing…")
    s = summarize(m)
    if s.get("_llm_fallback"):
        print(f"  ! LLM unavailable, using template: {s.get('_llm_error','')[:120]}")
    else:
        print(f"  \"{s['headline']}\"")

    blocks = slack.build_blocks(m, s)
    text = f"What happened in WizPilot — {m['report_date']}: {s['headline']}"

    print("→ checking for weekly recurring topics (Mondays only)…")
    wt = fetch_weekly_topics(rd, target, exclude)
    wt_block = weekly_topics_block(wt)
    if wt_block:
        # Insert before the trailing footer context block so it reads as part
        # of the digest body, not an afterthought stapled to the very end.
        blocks = blocks[:-1] + [{"type": "divider"}, wt_block] + blocks[-1:]
        print(f"  found {len(wt['topics'])} recurring topic(s) over {wt['sample_size']} questions")
    elif wt is not None and not wt.get("topics"):
        print("  no topic repeated ≥3x this week")

    if os.environ.get("Q_TODAY_QUESTIONS"):
        print("→ picking today's 3 most interesting questions…")
        try:
            q_rows = rd.refresh(_qid("Q_TODAY_QUESTIONS"), {"target_date": target, "exclude_tenants": exclude})
        except Exception as e:  # noqa: BLE001 - this section is a bonus, never fatal
            print(f"  ! today's-questions query failed ({e}); skipping section", file=sys.stderr)
            q_rows = []
        picks = pick_interesting_questions(q_rows)
        iq_block = interesting_questions_block(picks)
        if iq_block:
            blocks = blocks[:-1] + [{"type": "divider"}, iq_block] + blocks[-1:]
            print(f"  picked {len(picks)} question(s) from {len(q_rows)} sampled")
        else:
            print(f"  nothing selected as interesting from {len(q_rows)} sampled")

    if args.dry_run:
        print("\n--- DRY RUN, not posting ---")
        print(json.dumps({"text": text, "blocks": blocks}, indent=2, default=str))
        return 0

    print("→ posting to Slack…")
    slack.post(_env("SLACK_BOT_TOKEN"), _env("SLACK_CHANNEL"), blocks, text)
    print(f"✓ Posted digest for {m['report_date']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RedashError, RuntimeError) as e:
        print(f"\n✗ {e}", file=sys.stderr)
        raise SystemExit(1)
