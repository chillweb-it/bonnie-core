import logging
import os
import threading
import time

from anthropic import Anthropic, APIError as AnthropicAPIError
from openai import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from notion_worker import NotionAPIError, NotionClient, NotionTaskWorker
from slack_notion import SLACK_TASK_HELP, is_task_help, parse_slack_task_command


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = Anthropic(
    api_key=os.environ["ANTHROPIC_API_KEY"],
    timeout=45.0,
)
minimax = OpenAI(
    api_key=os.environ["MINIMAX_API_KEY"],
    base_url="https://api.minimax.io/v1",
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
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3")
_task_event_lock = threading.Lock()
_task_event_times: dict[str, float] = {}
_TASK_EVENT_TTL_SECONDS = 24 * 60 * 60


def build_notion_client() -> NotionClient | None:
    token = os.getenv("NOTION_TOKEN")
    data_source_id = os.getenv("NOTION_TASKS_DATA_SOURCE_ID")
    if not token or not data_source_id:
        return None
    return NotionClient(
        token=token,
        data_source_id=data_source_id,
        notion_version=os.getenv("NOTION_VERSION", "2026-03-11"),
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


def ask_minimax(text: str) -> str:
    """Generate a reply with MiniMax, the fallback model."""
    response = minimax.chat.completions.create(
        model=MINIMAX_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        max_completion_tokens=1200,
        temperature=0.7,
        extra_body={"thinking": {"type": "disabled"}},
    )

    reply = response.choices[0].message.content
    if not reply or not reply.strip():
        raise ValueError("MiniMax returned an empty response")
    return reply.strip()


def ask_bonnie(text: str) -> str:
    """Use Claude first and fall back to MiniMax on provider failure."""
    try:
        return ask_claude(text)
    except (AnthropicAPIError, ValueError):
        logger.warning("Claude request failed; falling back to MiniMax", exc_info=True)
        return ask_minimax(text)


def start_notion_worker() -> NotionTaskWorker | None:
    """Start v0.3 only when both Notion settings are present."""
    if notion_client is None:
        logger.info(
            "Notion worker disabled; NOTION_TOKEN and "
            "NOTION_TASKS_DATA_SOURCE_ID are required"
        )
        return None

    worker = NotionTaskWorker(
        notion_client,
        ask_bonnie,
        poll_seconds=int(os.getenv("NOTION_POLL_SECONDS", "30")),
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
    """Answer human-authored direct messages with Claude and MiniMax fallback."""
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
        logger.exception("MiniMax fallback authentication failed")
        say("AI 服務暫時未能完成認證，請檢查 Railway 入面嘅 API keys。")
    except RateLimitError:
        logger.exception("MiniMax fallback rate limit reached")
        say("AI 服務暫時太繁忙或已達使用限額，請稍後再試。")
    except APIConnectionError:
        logger.exception("Could not connect to MiniMax fallback")
        say("暫時連接唔到 AI 服務，請稍後再試。")
    except (APIError, ValueError):
        logger.exception("Claude and MiniMax requests failed")
        say("Bonnie 暫時未能完成回覆，請稍後再試。")


if __name__ == "__main__":
    start_notion_worker()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
