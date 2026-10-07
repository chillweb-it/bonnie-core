import logging
import os
import threading
import time

from anthropic import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    Anthropic,
    RateLimitError,
)
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from anthropic_executor import AnthropicAgentExecutor
from notion_worker import NotionAPIError, NotionClient, NotionTaskWorker
from slack_notion import SLACK_TASK_HELP, is_task_help, parse_slack_task_command


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = Anthropic(
    api_key=os.environ["ANTHROPIC_API_KEY"],
    timeout=45.0,
)

SYSTEM_PROMPT = """You are Bonnie, Evan's capable executive assistant.
Reply in the same language as the user. When the user writes in Cantonese or
Traditional Chinese, use natural Hong Kong Cantonese. Be concise, practical,
and clear. Never claim that you completed an action or accessed information
unless the user supplied it in the conversation. In Slack DMs, users can create
a Notion queue item with an explicit `Task: ...` or `任務：...` command. If a user
asks you to access or change Notion without using that command, explain the
command briefly instead of saying that no Notion integration exists."""

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
_task_event_lock = threading.Lock()
_task_event_times: dict[str, float] = {}
_TASK_EVENT_TTL_SECONDS = 24 * 60 * 60


def build_notion_client() -> NotionClient | None:
    token = os.getenv("NOTION_TOKEN")
    tasks_data_source_id = os.getenv("NOTION_TASKS_DATA_SOURCE_ID")
    agents_data_source_id = os.getenv("NOTION_AGENTS_DATA_SOURCE_ID")
    runs_data_source_id = os.getenv("NOTION_AGENT_RUNS_DATA_SOURCE_ID")
    if not all(
        (token, tasks_data_source_id, agents_data_source_id, runs_data_source_id)
    ):
        return None
    return NotionClient(
        token=token,
        data_source_id=tasks_data_source_id,
        agents_data_source_id=agents_data_source_id,
        agent_runs_data_source_id=runs_data_source_id,
        notion_version=os.getenv("NOTION_VERSION", "2026-03-11"),
        timeout_seconds=int(os.getenv("NOTION_TIMEOUT_SECONDS", "30")),
    )


notion_client = build_notion_client()


def remember_task_event(event_id: str) -> bool:
    """Return False for a recent Slack retry so it cannot create a duplicate."""
    if not event_id:
        return True

    now = time.monotonic()
    with _task_event_lock:
        expired = [
            key
            for key, seen_at in _task_event_times.items()
            if now - seen_at > _TASK_EVENT_TTL_SECONDS
        ]
        for key in expired:
            _task_event_times.pop(key, None)
        if event_id in _task_event_times:
            return False
        _task_event_times[event_id] = now
    return True


def forget_task_event(event_id: str) -> None:
    if not event_id:
        return
    with _task_event_lock:
        _task_event_times.pop(event_id, None)


def ask_claude(text: str) -> str:
    """Generate a reply with Claude, the primary model."""
    response = claude.messages.create(
        model=ANTHROPIC_MODEL,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
        max_tokens=1200,
    )

    reply = "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not reply:
        raise ValueError("Claude returned an empty response")
    return reply


def ask_bonnie(text: str) -> str:
    """Route every Bonnie Slack message to Claude."""
    return ask_claude(text)


def start_notion_worker() -> NotionTaskWorker | None:
    """Start v0.4 only when its Notion and Anthropic settings are present."""
    if notion_client is None:
        logger.info(
            "Notion worker disabled; token plus Tasks, Agents, and Agent Runs "
            "data source IDs are required"
        )
        return None
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        logger.info("Notion worker disabled; ANTHROPIC_API_KEY is required for v0.4")
        return None

    executor = AnthropicAgentExecutor(
        anthropic_api_key,
        timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "90")),
        default_model=os.getenv("ANTHROPIC_AGENT_MODEL", ANTHROPIC_MODEL),
        max_tokens=int(os.getenv("AGENT_MAX_TOKENS", "2000")),
    )

    worker = NotionTaskWorker(
        notion_client,
        executor,
        poll_seconds=int(os.getenv("NOTION_POLL_SECONDS", "30")),
        max_attempts=int(os.getenv("AGENT_MAX_ATTEMPTS", "3")),
        retry_delay_seconds=float(os.getenv("AGENT_RETRY_DELAY_SECONDS", "2")),
    )
    thread = threading.Thread(
        target=worker.run_forever,
        name="notion-task-worker",
        daemon=True,
    )
    thread.start()
    return worker


@app.event("message")
def handle_message(event, say):
    """Answer human-authored direct messages with Claude only."""
    if event.get("bot_id") or event.get("subtype"):
        return

    if event.get("channel_type") != "im":
        return

    text = (event.get("text") or "").strip()
    if not text:
        return

    if is_task_help(text):
        say(SLACK_TASK_HELP)
        return

    task_command = parse_slack_task_command(text)
    logger.info(
        "Slack DM classified kind=%s length=%s",
        "task" if task_command else "chat",
        len(text),
    )
    if task_command:
        if notion_client is None:
            logger.error("Slack task command received but Notion is not configured")
            say("Notion 任務連接暫時未設定完成，請檢查 Railway 嘅 Notion variables。")
            return

        event_id = event.get("client_msg_id") or event.get("ts") or ""
        if not remember_task_event(event_id):
            logger.info("Ignored duplicate Slack task event event_id=%s", event_id)
            return

        try:
            page = notion_client.create_task(
                task_command.title,
                task_command.notes,
                priority=task_command.priority,
            )
            page_id = page.get("id", "unknown")
            page_url = page.get("url", "")
            logger.info(
                "Slack task created page_id=%s priority=%s",
                page_id,
                task_command.priority,
            )
            link = f"\n<{page_url}|喺 Notion 開啟>" if page_url else ""
            say(
                f"已建立 Notion 任務：*{task_command.title}* "
                f"（{task_command.priority}）。Bonnie 會自動處理。{link}"
            )
        except (NotionAPIError, ValueError):
            forget_task_event(event_id)
            logger.exception("Could not create Notion task from Slack")
            say("未能建立 Notion 任務。現有資料冇被改動，請稍後再試。")
        return

    try:
        say(ask_bonnie(text))
    except AuthenticationError:
        logger.exception("Claude authentication failed")
        say("Claude 暫時未能完成認證，請檢查 Railway 入面嘅 Anthropic API key。")
    except RateLimitError:
        logger.exception("Claude rate limit reached")
        say("Claude 暫時太繁忙或已達使用限額，請稍後再試。")
    except APIConnectionError:
        logger.exception("Could not connect to Claude")
        say("暫時連接唔到 Claude，請稍後再試。")
    except (APIError, ValueError):
        logger.exception("Claude request failed")
        say("Bonnie 暫時未能完成回覆，請稍後再試。")


if __name__ == "__main__":
    start_notion_worker()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
