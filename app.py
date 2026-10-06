import logging
import os
import threading

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

from notion_worker import NotionClient, NotionTaskWorker


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
unless the user supplied it in the conversation."""

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3")


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
    token = os.getenv("NOTION_TOKEN")
    data_source_id = os.getenv("NOTION_TASKS_DATA_SOURCE_ID")
    if not token or not data_source_id:
        logger.info(
            "Notion worker disabled; NOTION_TOKEN and "
            "NOTION_TASKS_DATA_SOURCE_ID are required"
        )
        return None

    worker = NotionTaskWorker(
        NotionClient(
            token=token,
            data_source_id=data_source_id,
            notion_version=os.getenv("NOTION_VERSION", "2026-03-11"),
        ),
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
