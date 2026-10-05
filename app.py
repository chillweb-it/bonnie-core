import os

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.event("message")
def handle_message(event, say):
    """Acknowledge human-authored direct messages."""
    if event.get("bot_id") or event.get("subtype"):
        return

    if event.get("channel_type") != "im":
        return

    text = (event.get("text") or "").strip()
    if text.casefold() == "hello bonnie":
        say("Hello Evan, Bonnie is online.")
    else:
        say("Bonnie is online. I received your message.")


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
