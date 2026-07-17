# n8n — `self-docs weekly sync` workflow

Exported workflow: [`docs-sync.json`](./docs-sync.json). This is the automation from
IMPLEMENTATION_PLAN.md §T6 — it owns *scheduling* the weekly re-crawl; the ingestion container
itself stays cron-free and only reacts to `POST /sync`.

## What it does

1. **Weekly Schedule** — fires Sunday 03:00 (server timezone, see `settings.timezone` in the
   JSON — defaults to `Etc/UTC`; change it to your local timezone after import).
2. **Config** — a `Set` node holding the workflow's tunables: `ingestionBaseUrl`
   (`http://ingestion:8080`, container-DNS reachable — n8n and the ingestion service must be on
   the same Docker network), `pollIntervalSeconds` (default `60`), `timeoutMinutes` (default `60`).
   Edit these values directly in this node after import if you need different behavior.
3. **Trigger Sync** — `POST {{ingestionBaseUrl}}/sync` with `Authorization: Bearer <SYNC_TOKEN>`
   supplied by the **`self-docs-sync-token`** credential (Header Auth). No body is sent, so all
   configured sources are synced (per the ingestion API contract in `ingestion/app/main.py`).
4. **Sync Trigger Result** (branch on HTTP status code):
   - `409` → **No-op End** — another sync is already running. This is treated as a successful
     no-op per the T6 contract: **no alert is sent**.
   - `200`/`202` → proceed to the poll loop.
   - anything else (`401` unauthorized, `400` unknown source, network/timeout error) → tagged
     `trigger_error` and routed straight to the notification node.
5. **Poll loop** — `Init Poll` records the poll start time, then `Poll Wait` → `Get Status`
   (`GET /status`, unauthenticated per the ingestion contract) → `Evaluate Poll` (computes
   elapsed time and done/timeout flags) → `Poll Decision`:
   - `/status` returns `running: false` → done → **Evaluate Results**.
   - still `running: true` and elapsed ≥ `timeoutMinutes` → tagged `timeout` → notification.
   - still running and under the timeout → loops back to `Poll Wait` (waits
     `pollIntervalSeconds` again).
6. **Evaluate Results** — reads the `/status` response shape
   (`{running: false, <sourceName>: {pages_fetched, pages_skipped, pages_failed, pages_removed,
   chunks_indexed, last_status, last_synced, error}, ...}`), sums totals across all sources, and
   collects any source whose `last_status` is `failed` or `partial`.
7. **Any Failures?** branches to:
   - **Tag Sync Failure** — per-source name + `last_status` + `error` detail, plus run totals.
   - **Tag Success** — a summary of pages fetched/skipped/removed and chunks indexed across all
     sources.
8. **Prepare Notification** (single shared `Code` node, fed by all four failure/success/timeout/
   trigger-error branches) builds `{ text: "..." }`, then **Send Notification** (one shared HTTP
   Request node) POSTs it to your webhook.

## Importing

```bash
# via UI: Workflows → Import from File → select docs/n8n/docs-sync.json
# via CLI (self-hosted, container has the file mounted or copied in):
n8n import:workflow --input=docs-sync.json
```

The workflow starts **inactive** on import — review the `Config` node's values, create both
credentials below, then activate it.

## Credentials to create

The exported JSON references credentials **by name/id placeholder only** — it never contains a
token or webhook URL. You must create these in n8n's **Credentials** store before activating:

### 1. `self-docs-sync-token` (type: **Header Auth**)

Used by the **Trigger Sync** node to authenticate `POST /sync`.

- Name: `Authorization`
- Value: `Bearer <your SYNC_TOKEN>` (the same value as the `SYNC_TOKEN` env var the ingestion
  container was started with — see the repo root `.env`)

Then open **Trigger Sync** → Authentication → Generic Credential Type → HTTP Header Auth →
select this credential (the exported JSON has a placeholder credential id; n8n will prompt you
to re-select it on first open if the id doesn't match).

### 2. Notification webhook — **environment variable, not an n8n credential**

The failure/success notification POSTs `{ "text": "..." }` to a Discord/Slack-incoming-webhook
or ntfy-style endpoint. For these receivers **the secret is the full URL itself** (the
token/topic lives in the URL path), not a header or query parameter.

n8n's built-in generic auth credential types (**Header Auth**, **Query Auth**, **Custom Auth**)
can only inject a header, a query-string parameter, or `body`/`qs` fields into a request — none
of them can substitute the request's **URL**. There is consequently no stock n8n credential type
that can hold a full secret URL for a generic `HTTP Request` node (this was verified against the
installed node/credential source, not assumed). The supported, idiomatic mechanism for this kind
of secret in self-hosted n8n is an **environment variable** on the n8n process — it is just as
absent from the exported workflow JSON and from git as a credential value would be.

Set on the n8n container/service (e.g. in your n8n `docker-compose.yml` service or `.env`,
**not** committed to git):

```
NOTIFY_WEBHOOK_URL=https://discord.com/api/webhooks/XXXXX/YYYYY   # or a Slack incoming webhook,
                                                                    # or an ntfy topic URL, etc.
```

The **Send Notification** node reads it via `{{ $env.NOTIFY_WEBHOOK_URL }}` in its URL field.
If your n8n instance restricts environment-variable access in expressions
(`N8N_BLOCK_ENV_ACCESS_IN_NODE=true`), unset that restriction for this instance or move the value
into n8n's separate **Variables** feature (Enterprise) and adjust the expression to
`{{ $vars.NOTIFY_WEBHOOK_URL }}` instead.

If you specifically want the webhook credential to live in n8n's Credentials store (e.g. for a
particular platform), swap the generic **Send Notification** node for that platform's dedicated
n8n node (e.g. `n8n-nodes-base.discord`, Slack's "Send message" node) — those dedicated nodes
have first-class credential types (`discordWebhookApi`, Slack OAuth, etc.) that read the secret
server-side and never expose it to expressions or the workflow JSON at all. This is a
platform-specific trade-off the T6 task explicitly avoided in favor of one generic node that
works against any `{text: "..."}`-shaped webhook receiver.

## Adjusting schedule / poll interval / timeout

All three are edited in one place — the **Config** node right after the trigger:

| Field | Default | Meaning |
|---|---|---|
| `ingestionBaseUrl` | `http://ingestion:8080` | Container-DNS base URL of the ingestion service. Only change if you rename the compose service or move n8n off that network. |
| `pollIntervalSeconds` | `60` | How often `Get Status` is polled while a sync is running. |
| `timeoutMinutes` | `60` | Total time to keep polling before treating a still-running sync as a failure (per the runbook's documented expected-sync-durations). |

The weekly cadence itself (Sunday 03:00) is on the **Weekly Schedule** trigger node's own
parameters (`rule.interval`), not in `Config` — open that node to change the day/hour, or the
workflow `settings.timezone` field to change which timezone `03:00` is evaluated in.

## Notes on the `/status` response shape

`GET /status` returns `{"running": bool, "<source-name>": {...outcome...}, ...}` — the per-source
outcome objects are **top-level keys**, not nested under a `"sources"` key. The `Evaluate Poll`
and `Evaluate Results` Code nodes rely on this exact shape (matching `ingestion/app/main.py`);
if that contract ever changes, both Code nodes need updating together.

## Verification performed for this export (T6)

- `python3 -m json.tool` / `json.load` — valid JSON.
- `grep` for token/URL-like strings across the exported file — none found; only credential
  name/id placeholders and env-var *names* are present, never values.
- A real, disposable `n8nio/n8n:latest` Docker container (SQLite backend, no network exposure)
  was used to run `n8n import:workflow --input=docs-sync.json` and `n8n list:workflow` — the
  import succeeded (workflow row written, all node types/credential types resolved against the
  actual installed `n8n-nodes-base` package) with **no schema errors**. This confirms every node
  `type`/`typeVersion` and both credential type names (`httpHeaderAuth`) used in the exported
  JSON are real and installed in a current n8n build.
- A full **live manual execution** (schedule trigger fired, poll loop against a real ingestion
  container, failure branch fired against a bogus source) was **not** performed here — there is
  no live n8n/ingestion stack wired together in this task's worktree. That exercise is the T10
  acceptance criterion ("a manual execution against the live stack completes and the failure
  branch fires when pointed at a bogus source") and should be run there.
