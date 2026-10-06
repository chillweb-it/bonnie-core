# bonnie-core

Railway worker for Bonnie. Slack Socket Mode remains available, while Agent
Project v0.3 adds a minimal single-agent Notion Tasks workflow using Claude as
the primary executor.

## v0.3 workflow

Every polling cycle selects at most one Tasks row matching all three rules:

- `Status = Open`
- `CEO Required = No`
- `Need clarification = No`

The worker changes the task to `Doing`, records a unique claim in `Assignee`,
and verifies that claim before calling Claude. On success it appends a concise
result to `Notes` and sets `Status = Done`. On failure it records a redacted
error in `Notes` and `Waiting on`, then sets `Status = Waiting`.

Only task status and text fields are updated. Existing `Company`, `Project`,
and other relation properties are never replaced.

Notion does not provide a conditional page update, so duplicate prevention in
v0.3 uses a unique verified claim plus a single `bonnie-core` Railway replica.
Do not scale this service above one replica until a shared lock is introduced
in a later version.

## Required Railway variables

Keep all tokens in Railway Variables; never commit them.

| Variable | Purpose |
| --- | --- |
| `SLACK_BOT_TOKEN` | Existing Slack bot token |
| `SLACK_APP_TOKEN` | Existing Slack Socket Mode token |
| `ANTHROPIC_API_KEY` | Claude executor credential |
| `MINIMAX_API_KEY` | Existing optional fallback credential |
| `NOTION_TOKEN` | Notion internal integration secret |
| `NOTION_TASKS_DATA_SOURCE_ID` | Tasks data source ID |

Optional variables:

| Variable | Default |
| --- | --- |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` |
| `MINIMAX_MODEL` | `MiniMax-M3` |
| `NOTION_VERSION` | `2026-03-11` |
| `NOTION_POLL_SECONDS` | `30` (minimum `5`) |

For this workspace, the Tasks data source ID is:

```text
d8077d5a-5ae0-43e5-bd11-b67fc398a090
```

The Notion integration must have read and update access to the `Tasks` database
and to related `Projects` and `Companies` pages if their names should be included
in task context. Share those pages with the integration in Notion before enabling
the worker.

## Local run

Use Python 3.11 or newer.

```bash
python -m pip install -r requirements.txt
python app.py
```

If the two Notion variables are missing, the Notion worker stays disabled and
the existing Slack service continues to start normally.

## Tests

```bash
python -m unittest -v
```

## Safe deployment check

1. Confirm Railway has exactly one `bonnie-core` replica.
2. Add the Notion variables without changing existing Slack or AI variables.
3. Create a harmless task with `Status = Open`, both approval checkboxes off,
   and a note asking Bonnie to return a short acknowledgement.
4. Confirm Railway logs show one claim and one completion without token values.
5. Confirm the same task becomes `Done` and has a concise Bonnie note.

The future multi-agent registry, routing, retry database, and Agent Runs model
are intentionally outside v0.3.
