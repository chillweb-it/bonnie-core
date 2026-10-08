# bonnie-core

Railway control-plane service for **Bonnie**, Evan's conversation-first AI Chief of Staff.

## v0.5 architecture

The primary entry point is now **ChatGPT Bonnie**, not Notion.

```text
Evan
  -> ChatGPT Bonnie
      -> CHAT      : Bonnie answers directly
      -> RECORD    : Bonnie stores durable notes/decisions in Notion
      -> CLAUDE    : Bonnie creates a CLAUDE Agent Run in Notion
                     -> Railway bonnie-core claims it
                     -> Anthropic Claude executes
                     -> result is written back to Agent Runs
                     -> Bonnie reviews/reports to Evan
      -> WORK      : Bonnie starts ChatGPT Work when browser/computer/
                     connected-app capability is needed
      -> HUMAN     : Bonnie creates/updates a Notion Task and communicates
                     with staff through Slack Bonnie HQ
      -> CEO GATE  : Bonnie stops and asks Evan for the decision
```

### Important boundary

Railway does **not** launch ChatGPT Work. Work is started from ChatGPT when Bonnie
decides a task needs cloud browser/computer/connected-app capability.

Railway is the 24/7 unattended control plane for Claude execution, Slack,
Notion persistence, retries, locks, and run-state tracking.

## Current responsibilities

### ChatGPT Bonnie
- primary user interface for Evan
- understand intent and company context
- decide execution route
- write durable business context to Notion
- create CLAUDE Agent Runs when unattended AI execution is appropriate
- launch ChatGPT Work when browser/computer capability is required
- create human Tasks and send/coordinate staff work through Slack
- review results and report only the important outcome to Evan

### Railway `bonnie-core`
- always-on Slack Socket Mode service
- consume queued `Execution Route = CLAUDE` Agent Runs
- load agent instructions from Notion Agents
- call Anthropic Claude
- retry bounded failures
- write result/error/token/run metadata back to Agent Runs
- keep run claim/status audit trail
- optional legacy Notion Task worker (disabled by default)

### Notion
- durable Bonnie memory / company records
- Agents registry
- Agent Runs audit ledger
- Human Tasks / project tracking
- **not** the normal CEO command interface

### Slack Bonnie HQ
- colleague communication channel
- staff can ask Bonnie questions
- explicit `Task:` commands remain supported for compatibility
- human work is tracked in Notion

## Agent Run queue

Bonnie creates an Agent Runs row with at least:

- `Run ID`
- `Execution Route = CLAUDE`
- `Status = Queued`
- `Work Brief`
- `Company`
- `Domain`
- optional `Agent`
- `Source = ChatGPT`
- `Requested By = Evan`
- `Report Status = Pending`
- `CEO Required = No`
- `Need Clarification = No`

Railway then:

```text
Queued
  -> gate check
  -> choose exact Company/Domain agent or Bonnie fallback
  -> verified worker claim
  -> Running
  -> Claude
  -> Completed / Failed / Waiting
  -> Report Status = Ready
```

The result remains in Notion until Bonnie reads/reviews it and reports it to Evan.
`Report Status` can then be changed to `Reported`.

## Execution routes

- `CLAUDE`: unattended reasoning/research/coding work that does not need a
  browser or authenticated connected app.
- `WORK`: ChatGPT Work; created directly from ChatGPT Bonnie, not by Railway.
- `HUMAN`: colleague work; Notion Task + Slack coordination.
- `RECORD`: durable note/decision/context only.
- `CEO GATE`: no execution until Evan decides.

## Safety

- `CEO Required = Yes`: Claude execution is blocked and the run becomes Waiting.
- `Need Clarification = Yes`: execution is blocked and the run becomes Waiting.
- a queued CLAUDE run is claimed before the model call.
- only one queue item is handled at a time per Railway replica.
- bounded retries are used; failures are recorded in Notion.
- secrets are kept only in Railway Variables.
- user content is not destructively replaced as part of execution routing.

## Railway variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `SLACK_BOT_TOKEN` | Slack bot | — |
| `SLACK_APP_TOKEN` | Slack Socket Mode | — |
| `ANTHROPIC_API_KEY` | Claude | — |
| `ANTHROPIC_MODEL` | staff-facing Slack Claude model | `claude-sonnet-5-5` |
| `ANTHROPIC_AGENT_MODEL` | fallback Claude agent model | `ANTHROPIC_MODEL` |
| `NOTION_TOKEN` | Notion API | — |
| `NOTION_TASKS_DATA_SOURCE_ID` | Human/legacy Tasks | existing Tasks DB |
| `NOTION_AGENTS_DATA_SOURCE_ID` | Agents registry | existing Agents DB |
| `NOTION_AGENT_RUNS_DATA_SOURCE_ID` | Agent Runs ledger/queue | existing Agent Runs DB |
| `ENABLE_AGENT_RUN_QUEUE` | consume CLAUDE Agent Runs | `true` |
| `AGENT_RUN_POLL_SECONDS` | queue poll interval | `10` |
| `ENABLE_NOTION_TASK_WORKER` | legacy Notion-as-trigger mode | `false` |
| `AGENT_MAX_ATTEMPTS` | bounded Claude attempts | `3` |
| `AGENT_TIMEOUT_SECONDS` | model request timeout | `90` |
| `AGENT_RETRY_DELAY_SECONDS` | retry backoff base | `2` |
| `AGENT_MAX_TOKENS` | max output per Claude run | `2000` |

## Local verification

```bash
python -m pip install -r requirements.txt
python -m unittest -v
python app.py
```

Expected startup logs for v0.5 include:

```text
agent_run_queue_started
Legacy Notion Task worker disabled
```

## Rollback

Redeploy a prior Railway deployment or revert the v0.5 commits. Keep the Notion
Agents and Agent Runs databases: they are durable audit history. To pause only
the Claude queue, set `ENABLE_AGENT_RUN_QUEUE=false` and redeploy/restart.


## Shared Knowledge v0.6

Notion `Bonnie Knowledge Registry` -> approved Notion source pages -> atomic sync
-> PostgreSQL/pgvector -> Slack Claude and Agent Run Claude prompts.
`ENABLE_KNOWLEDGE=false` leaves the v0.5 execution path unchanged.

The approved Active registry row declares Knowledge ID, source URL (blank = row
body), version, company, domain, audience, MUST/SHOULD policy and effective date.
One Active row per Knowledge ID is allowed. Create/edit a Draft separately, then
set the prior row Superseded and activate the approved replacement. In-place
source edits produce immutable content revisions even if version is unchanged.
All active IDs and metadata are replaced atomically after complete ingestion.
Superseded, archived, unapproved and future-effective records are excluded.
Unsupported Notion blocks fail ingestion rather than silently omitting content.
Notion pages and their child blocks must be shared with the existing Notion API
integration. Never assume connector access implies backend token access.

Multilingual CPU embeddings use FastEmbed's
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions).
No new embedding provider credential is needed. First startup downloads model
weights; reserve startup time and memory. Semantic and pg_trgm keyword scores
are combined, after company/audience/domain filtering. Initial exhaustive vector
search is appropriate for a small corpus; add HNSW after measuring larger loads.
Changing encoder/dimensions requires a reviewed migration and complete reindex.
MUST documents are loaded in full independently of top-k; oversized mandatory
context fails instead of silently truncating rules. Unready/stale knowledge
blocks model execution. Already claimed Agent Runs become Waiting on retrieval
failure and must be requeued after fixing sync; unready startup pauses polling.

Unlisted Slack users can only retrieve staff-readable Shared knowledge. Add
verified Slack user IDs to KNOWLEDGE_SLACK_SCOPES with company/audience grants.
Company aliases CD/CW/THT in runs resolve to Cloud Decoct/ChillWeb/THT Lions.
Agents default to staff-readable knowledge; verified agent IDs may be granted
ceo audience with KNOWLEDGE_AGENT_SCOPES. A run's company constrains retrieval;
its knowledge grants do not grant external
tools. No automatic CEO privileges are inferred from a message's text.

Authenticated external adapters:
- POST `/v1/knowledge/search`: query, optional company/domain, limit 1..20.
- POST `/mcp`: stateless JSON-RPC initialize, tools/list, tools/call, ping.
- GET `/v1/system/runtime`: CEO-scoped read of current configured model names,
  encoder and deployed commit (never API keys).
- GET `/health`: index readiness only, no knowledge or credentials.

KNOWLEDGE_API_KEYS is a secret JSON map of independent random bearer token to
{principal,companies,audience}. A request cannot change its token's scope.
Do not share the database password as an API credential. ChatGPT does NOT read
Railway automatically: install/connect the authenticated MCP/API adapter in its
available tool environment before claiming both channels share this index.
API writes are deliberately absent: Notion remains the authoring/approval UI.
The minimal MCP adapter returns JSON responses; streaming/subscriptions/OAuth
are not implemented. A client requiring OAuth needs a gateway integration.

Runtime initialization applies additive idempotent SQL and runs a real retrieval
probe. Logs expose only counts and error types. Retrieval audit stores principal,
query hash and document IDs; logs expire after 90 days. Immutable source text and
revisions persist in PostgreSQL. The database is a Railway-hosted PostgreSQL
container with persistent volume; configure and restore-test volume backups
before treating it as a fully managed disaster recovery service.

Rollback: disable ENABLE_KNOWLEDGE and redeploy the prior application commit.
Keep registry, database and source history. Do not delete the database to roll
back the app. Source sync is polled every 120 seconds; no 10-second freshness SLA.
