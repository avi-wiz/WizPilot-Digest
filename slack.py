"""Format the digest as Slack Block Kit and post it.

DUPLICATE NOTICE: this is a copy of ../wizpilot-digest/app/slack.py, made so
poc/ is self-contained for Railway deployment (Railway deploys one folder;
poc_digest.py previously reached into the sibling wizpilot-digest/ folder via
a sys.path hack, which doesn't work once poc/ ships on its own). If you
change Slack Block Kit formatting in the real app, copy the change here too
— these two files will drift otherwise.
"""
from __future__ import annotations
import json
import urllib.request


def _pct(cur, prev):
    if prev in (None, 0) or cur is None:
        return None
    return round((cur - prev) / prev * 100)


def _delta_str(cur, prev, label):
    p = _pct(cur, prev)
    if p is None:
        return ""
    arrow = "▲" if p > 0 else ("▼" if p < 0 else "▬")
    return f"  {arrow} {abs(p)}% {label}"


def _deltas(cur, cmp, cmp_dod, key):
    return (_delta_str(cur, cmp.get(key), "vs last wk")
            + _delta_str(cur, cmp_dod.get(key), "vs yesterday"))


def build_blocks(m: dict, summary: dict) -> list[dict]:
    cmp = m.get("comparison") or {}
    cmp_dod = m.get("comparison_dod") or {}
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"☀️ What happened in WizPilot — {m['report_date']}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{summary['headline']}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary["narrative"]}},
        {"type": "divider"},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Active users*\n{m['active_users']}"
                f" ({m['new_users']} new){_deltas(m['active_users'], cmp, cmp_dod, 'active_users')}"},
            {"type": "mrkdwn", "text": f"*Active tenants*\n{m['active_tenants']}"
                f" ({m['new_tenants']} new){_deltas(m['active_tenants'], cmp, cmp_dod, 'active_tenants')}"},
            {"type": "mrkdwn", "text": f"*Conversations*\n{m['total_conversations']}"
                f"{_deltas(m['total_conversations'], cmp, cmp_dod, 'total_conversations')}"},
            {"type": "mrkdwn", "text": f"*Messages*\n{m['total_messages']}"
                f"{_deltas(m['total_messages'], cmp, cmp_dod, 'total_messages')}"},
            {"type": "mrkdwn", "text": f"*Avg msgs / convo*\n{m['avg_msgs_per_convo']}"},
            {"type": "mrkdwn", "text": f"*Peak hour*\n{m['peak_hour_ist']:02d}:00 IST"},
        ]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*🔥 Longest conversation:* {m['top_thread_msgs']} messages — "
                 f"*{m['top_thread_user_name']}* @ *{m['top_thread_tenant_name']}*"}},
    ]

    if m.get("tenant_users"):
        # Combined tenant/messages/users table — richer than the plain
        # "Top tenants" list below (currently only the Wiz Join extraction
        # path populates it; see poc_digest.py). "Power users" still renders
        # separately underneath — this doesn't replace it.
        lines = "\n".join(
            f"{i+1}. *{t['tenant']}* — {t['messages']} msgs: {t['users']}"
            for i, t in enumerate(m["tenant_users"])
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Top tenants*\n{lines}"}})
    elif m.get("top_tenants"):
        lines = "\n".join(
            f"{i+1}. *{t['name']}* — {t['messages']} msgs, {t['distinct_users']} users"
            for i, t in enumerate(m["top_tenants"])
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Top tenants*\n{lines}"}})
    if m.get("top_users"):
        lines = "\n".join(
            f"{i+1}. *{u['name']}* ({u['tenant_name']}) — {u['messages']} msgs"
            for i, u in enumerate(m["top_users"])
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Power users*\n{lines}"}})

    if summary.get("insights"):
        ins = "\n".join(f"• {i}" for i in summary["insights"])
        blocks += [{"type": "divider"},
                   {"type": "section", "text": {"type": "mrkdwn", "text": f"*💡 Insights*\n{ins}"}}]

    footer = "Prompt content isn't logged, so query topics aren't included."
    if summary.get("_llm_fallback"):
        footer += "  ⚠️ Narrative generated from template (LLM unavailable)."
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def post(token: str, channel: str, blocks: list[dict], text: str) -> dict:
    payload = json.dumps({"channel": channel, "blocks": blocks, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError(f"Slack post failed: {resp.get('error')}")
    return resp
