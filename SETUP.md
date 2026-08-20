# Redash-API POC — setup

Extraction goes through the Redash API instead of direct Postgres, so you don't
need the VPC connector or read-only DB users to see this working end to end.

## 1. Save three queries in Redash

Create each, run once, note the query ID from the URL (`/queries/<ID>`).

| Query | Source file | Data source |
|---|---|---|
| WizPilot – metrics | `../metrics.redash.sql` | LiteLLM |
| WizPilot – leaderboards | `../leaderboards.redash.sql` | LiteLLM |
| WizPilot – names | `../resolve_names.redash.sql` | **ultron replica** |

## 2. Convert the hardcoded params to Redash parameters

The `.redash.sql` files hardcode the date so you could run them by hand. For the
API to pass a date per-run, swap the literals in the `params` CTE for `{{ }}`:

```sql
WITH params AS (
  SELECT
    DATE '{{ target_date }}'                            AS target_date,
    '{{ tz }}'                                          AS tz,
    string_to_array(NULLIF('{{ exclude_tenants }}',''), ',') AS exclude_tenants
),
```

In the query editor each `{{ }}` gets a parameter widget — set `target_date`
type Date, `tz` and `exclude_tenants` type Text. The `string_to_array(NULLIF(...))`
wrapper turns an empty parameter into NULL rather than `ARRAY['']`, so
"exclude nothing" behaves.

For the names query, do the same with `{{ tenant_ids }}` / `{{ user_ids }}`:

```sql
string_to_array('{{ tenant_ids }}', ',')::uuid[] AS tenant_ids,
```

## 3. Get an API key

Redash profile → **API Key** (a user key, runs anything you can see). A per-query
key works too but you'd need three.

## 4. Run it

```bash
export REDASH_URL=https://redash.yourco.com
export REDASH_API_KEY=...
export Q_METRICS=1234 Q_LEADERBOARDS=1235 Q_NAMES=1236
export EXCLUDE_TENANTS=1f9fd5e7-d922-4767-971a-1c285256e5d4
export LLM_BASE_URL=... LLM_API_KEY=...
export SLACK_BOT_TOKEN=xoxb-... SLACK_CHANNEL=#your-private-test-channel

python3 poc_digest.py --date 2026-08-19 --dry-run   # print only
python3 poc_digest.py --date 2026-08-19             # post for real
```

Check the dry-run numbers against what Redash shows you by hand. They must match.

## Troubleshooting

- **HTTP 401/403** — bad API key, or the key's user can't see that query.
- **"did not return a job"** — wrong query ID, or the query has unsaved changes.
- **Query failed in Redash** — the parameter names in `{{ }}` don't match the
  keys the script sends (`target_date`, `tz`, `exclude_tenants`).
- **`not_in_channel`** — `/invite` the bot to the channel.
- **Baseline unavailable** — non-fatal; deltas are dropped and the digest still sends.
