# bonnie-core

Railway service for Bonnie. v0.4 adds a Notion-backed agent registry, router,
durable Agent Runs ledger, idempotent task claims, bounded retries, and Claude
execution while preserving the v0.2 Slack replies and v0.3.1 Slack-to-Notion
task bridge.

## v0.4 architecture

```text
Notion Tasks (oldest Open task, one at a time)
  -> CEO / clarification gates
  -> load enabled state and instructions from Notion Agents
  -> route to CD Research specialist or Bonnie fallback
  -> verified task claim (single Railway replica)
  -> create one idempotent Agent Run (Queued)
  -> Anthropic Claude execution (Running, bounded retries and timeout)
  -> write result to Task + mark Agent Run Completed
     or write a redacted reason + mark Waiting/Failed
```

The worker never replaces Company, Project, Source, or other user-owned
relations. It only updates the operational status/text fields needed for the
workflow and appends concise progress/result notes.

## Routing and safety rules

- `CEO Required = Yes`: no claim and no execution; Task becomes/remains
  `Waiting` with a CEO approval note.
- `Need clarification = Yes`: no claim and no execution; Task becomes/remains
  `Waiting` with a clarification note.
- `Company = CD` and `Domain = RSCH` (or `Research`): route to
  `AGT-CD-RSCH-ANL-v1` when enabled.
- All other tasks route to `AGT-HQ-BONNIE-ORCH-v1`.
- A disabled or missing specialist falls back to the enabled Bonnie agent.
- If neither the specialist nor fallback is valid and enabled, the Task waits
  with an explicit routing error and no Agent Run is created.

Agent instructions, model, enabled state, and QA requirement are loaded from
the Agents database for every claimed task. Both v0.4 seed agents currently use
`QA Result = Not Required` because their registry rows have `Requires QA` off.

## Claims, run IDs, and idempotency

The worker processes one Task per cycle and uses an in-process mutex plus a
unique claim marker written to `Assignee`. It reads the page back before
execution, so a competing writer invalidates the losing claim. Keep Railway at
exactly one replica for this MVP because Notion does not offer compare-and-swap
page updates.

Run IDs use `RUN-{COMPANY}-{DOMAIN}-{YYYYMMDD}-{NNN}`. Before claiming, the
worker queries Agent Runs for an idempotency key derived from the Notion Task ID
and the Task's pre-claim `last_edited_time`. An existing active or completed run
suppresses another claim/run. Reopening or materially editing a Task changes its
Notion version and therefore intentionally permits a new run.

Lifecycle:

```text
Queued -> Running -> Completed
                  -> Failed (Task -> Waiting)
```

Every attempt updates `Attempt`; `Max Attempts` records the configured bound.
Claude message ID and token usage are stored when returned by the API. The
existing Agent Runs property is still named `OpenAI Response/Run ID` for schema
compatibility, but v0.4 writes the Anthropic message ID there. Cost is left
empty unless the executor receives a reliable cost value.

## Slack behavior retained

Every ordinary Bonnie direct message is sent to Anthropic Claude. There is no
OpenAI or MiniMax fallback. Explicit task commands still create a safe Notion
queue item:
Explicit task commands still create a safe Notion queue item:

```text
Task: Prepare next week's board agenda
Summarize open decisions, risks, and items that need CEO approval.
Priority: P1
```

Supported prefixes include `Task:`, `Create task:`, `任務：`, `建立任務：`, and
`幫我建立 Task：`. Only DMs are accepted. Slack retry IDs are remembered in
memory for 24 hours to prevent normal duplicate task creation.

## Railway variables

Keep secrets in Railway Variables and never commit them.

| Variable | Required for | Default |
| --- | --- | --- |
| `SLACK_BOT_TOKEN` | Existing Slack service | — |
| `SLACK_APP_TOKEN` | Existing Slack Socket Mode | — |
| `ANTHROPIC_API_KEY` | Slack replies and v0.4 agent execution | — |
| `ANTHROPIC_MODEL` | Slack reply model | `claude-sonnet-5-5` |
| `ANTHROPIC_AGENT_MODEL` | Fallback when an Agent model is blank or non-Claude | `ANTHROPIC_MODEL` |
| `NOTION_TOKEN` | Tasks, Agents, and Agent Runs | — |
| `NOTION_TASKS_DATA_SOURCE_ID` | Task queue | `d8077d5a-5ae0-43e5-bd11-b67fc398a090` |
| `NOTION_AGENTS_DATA_SOURCE_ID` | Agent registry | `a4bd3853-3941-47fe-856e-fd9e9f4fef1b` |
| `NOTION_AGENT_RUNS_DATA_SOURCE_ID` | Run ledger | `a65473ac-a52c-407b-9117-270e3519908a` |
| `NOTION_VERSION` | Notion API | `2026-03-11` |
| `NOTION_POLL_SECONDS` | Idle poll interval | `30` (minimum `5`) |
| `NOTION_TIMEOUT_SECONDS` | Each Notion request | `30` |
| `AGENT_MAX_ATTEMPTS` | Total Claude attempts | `3` (minimum `1`) |
| `AGENT_TIMEOUT_SECONDS` | Each Claude request | `90` |
| `AGENT_RETRY_DELAY_SECONDS` | Linear retry backoff base | `2` |
| `AGENT_MAX_TOKENS` | Maximum output tokens per task | `2000` |

The Notion integration needs read/update access to Tasks, read access to Agents,
insert/update access to Agent Runs, and read access to related Company and
Project pages so their titles can be included in prompts.

## Local verification

Use Python 3.11 or newer.

```bash
python -m pip install -r requirements.txt
python -m unittest -v
python app.py
```

The v0.4 worker stays disabled if any required v0.4 Notion ID or
`ANTHROPIC_API_KEY` is missing; the Slack handlers still load and use Claude.
Railway logs use compact JSON events containing `task_id`, `run_id`, `agent_id`,
and `attempt` where applicable. Secrets are never included.

## Deployment and rollback

1. Keep `bonnie-core` at one replica.
2. Add the v0.4 environment variables without removing existing Slack/AI
   variables.
3. Deploy and confirm `notion_worker_started` in logs.
4. Run the harmless CD/RSCH test task and verify exactly one Completed Agent Run.
5. Wait for another poll and confirm no second run is created.

Rollback is code-only: redeploy the previous Railway deployment or revert the
v0.4 commit. Do not delete the Agents or Agent Runs databases. Existing Agent
Runs remain an audit log, and v0.3 task/Slack data stays intact. If a rapid
worker-only stop is needed, remove `NOTION_AGENT_RUNS_DATA_SOURCE_ID` and
redeploy; this disables the Notion worker while leaving Slack available.
