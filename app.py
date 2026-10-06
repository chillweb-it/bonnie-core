import logging
import os

from openai import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])
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


def ask_bonnie(text: str) -> str:
    """Generate a final user-facing reply with MiniMax."""
    response = minimax.chat.completions.create(
        model="MiniMax-M3",
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


@app.event("message")
def handle_message(event, say):
    """Answer human-authored direct messages with MiniMax."""
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
        logger.exception("MiniMax authentication failed")
        say("MiniMax API 認證失敗，請檢查 Railway 入面嘅 API key。")
    except RateLimitError:
        logger.exception("MiniMax rate limit reached")
        say("MiniMax 暫時太繁忙或已達使用限額，請稍後再試。")
    except APIConnectionError:
        logger.exception("Could not connect to MiniMax")
        say("暫時連接唔到 MiniMax，請稍後再試。")
    except (APIError, ValueError):
        logger.exception("MiniMax request failed")
        say("Bonnie 暫時未能完成回覆，請稍後再試。")


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
