# WizPilot Daily Failure Audit

A second, independent job in this repo. It answers: *what did users ask
yesterday that WizPilot didn't actually answer?*

It shares **no runtime state** with the digest — separate entrypoint, separate
Railway service, separate cron, separate Slack app/token/channel. It imports
`redash_client` / `llm_client` / `audit_client` so there's one HTTP client and
one LLM wrapper in the repo, but nothing the digest does can break it and
nothing it does can break the digest. Both are read-only.

## Failure buckets

Rules run first (deterministic), then the LLM adjudicates only what's left over.

| Bucket | Meaning | Decided by |
|---|---|---|
| `BLANK` | No response stored at all | rule (exact) |
| `REFUSAL` | "That's outside what I can help with" | rule, then LLM |
| `ERROR` | Surfaced an error/failure to the user | rule |
| `NO_DATA` | Ran, came back empty | rule, then LLM |
| `CLARIFY` | Answered with a question instead of an answer | rule |
| `NO_WIDGET` | Asked for data, got prose with no `[widget]` | LLM |
| `OK` | Not reported | both |

`[widget]` is a literal marker in the stored response text, so "did the user
actually get data" is checked exactly, not guessed. A blank response is a fact
and is never sent to the LLM to decide.

## Run it locally

```bash
python3 audit_digest.py --dry-run                # yesterday IST, print only
python3 audit_digest.py --date 2026-08-28 --dry-run
python3 audit_digest.py --date 2026-08-28        # actually post
```

## Slack app (one-time)

1. https://api.slack.com/apps → **Create New App** → From scratch.
2. **OAuth & Permissions** → Bot Token Scopes → add `chat:write`.
3. **Install to Workspace**, copy the `xoxb-…` token.
4. In your private channel: `/invite @YourAuditBot` (a bot cannot post to a
   private channel it isn't a member of, even with `chat:write`).

## Railway service (one-time)

Same repo, second service, so the digest's service is never touched:

- **New Service** → GitHub repo `avi-wiz/WizPilot-Digest` → same project.
- **Settings → Deploy → Start Command:** `python audit_digest.py`
  (the Dockerfile's `ENTRYPOINT` runs the digest; Railpack + an explicit start
  command is what keeps the two apart.)
- **Settings → Cron Schedule:** `30 2 * * *` (= 08:00 IST).
- **Settings → Restart Policy:** Never — it's a cron job, not a server.
- **Variables:** copy from the digest service, then add:
  - `AUDIT_SLACK_BOT_TOKEN` — the new app's `xoxb-…`
  - `AUDIT_SLACK_CHANNEL` — e.g. `#wizpilot-audit`

The audit reads `AUDIT_SLACK_*` only and never falls back to `SLACK_BOT_TOKEN`,
so a misconfigured audit stays silent rather than posting into the digest
channel.

## Model

Set `ANTHROPIC_MODEL=claude-sonnet-5`. The insight quality difference over
Haiku is large — Sonnet reasons about *why* a gap exists and separates a real
capability gap from a genuinely out-of-scope request. Roughly 2x the input
cost ($2/$10 per 1M vs $1/$5), on a handful of calls a day.

Two things this model family changes, both handled in `llm_client.py`:

- **`temperature` is rejected** (400 `temperature is deprecated for this
  model`). It's now sent only to models known to accept it.
- **Thinking is on by default**, so the reply's first content block is a
  `thinking` block, not text — the client takes the first *text* block rather
  than `content[0]`, and `max_tokens` is set high enough that thinking doesn't
  consume the whole budget before any answer is written.

## Notes

- Responses are truncated at 1500 chars upstream, so the LLM judges a partial
  reply. Rules key off the opening of the response, which is where refusal and
  empty-result wording appears.
- If the LLM pass fails, those turns are reported under "Unclassified" rather
  than silently dropped.
- A day with zero failures posts nothing.
