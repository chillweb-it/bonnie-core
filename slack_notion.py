"""Explicit Slack commands for creating tasks in Bonnie's Notion queue."""

from __future__ import annotations

import re
from dataclasses import dataclass


TASK_COMMAND = re.compile(
    r"^(?:task|create\s+task|任務|建立任務|新增任務|幫我(?:建立|新增|開)\s*task)\s*[:：]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
PRIORITY_LINE = re.compile(
    r"^\s*(?:priority|優先次序|優先級)\s*[:：]\s*(P[0-3])\s*$",
    re.IGNORECASE,
)
PRIORITY_SUFFIX = re.compile(r"\s+\[(P[0-3])\]\s*$", re.IGNORECASE)

SLACK_TASK_HELP = """要建立 Notion 任務，請用以下格式：
`Task: 任務標題`

可以加詳細資料同優先級：
```
Task: 準備下星期董事會 agenda
整理未完成決定、風險同需要 CEO 批准嘅項目。
Priority: P1
```
Bonnie 會將任務加入 Notion queue；普通訊息仍然當作對話處理。"""


@dataclass(frozen=True)
class SlackTaskCommand:
    title: str
    notes: str
    priority: str = "P2"


def is_task_help(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return normalized in {
        "task help",
        "task: help",
        "任務幫助",
        "任務：幫助",
        "點樣建立任務",
    }


def parse_slack_task_command(text: str) -> SlackTaskCommand | None:
    """Parse only deliberate task commands; ordinary conversation returns None."""
    match = TASK_COMMAND.match(text.strip())
    if not match:
        return None

    body = match.group(1).strip()
    if not body:
        return None

    lines = [line.strip() for line in body.splitlines()]
    priority = "P2"
    content_lines: list[str] = []
    for line in lines:
        priority_match = PRIORITY_LINE.match(line)
        if priority_match:
            priority = priority_match.group(1).upper()
        else:
            content_lines.append(line)

    while content_lines and not content_lines[-1]:
        content_lines.pop()

    if not content_lines:
        return None

    suffix_match = PRIORITY_SUFFIX.search(content_lines[0])
    if suffix_match:
        priority = suffix_match.group(1).upper()
        content_lines[0] = PRIORITY_SUFFIX.sub("", content_lines[0]).strip()

    title = content_lines[0][:200].strip()
    if not title:
        return None

    notes = "\n".join(content_lines).strip()
    return SlackTaskCommand(title=title, notes=notes, priority=priority)
