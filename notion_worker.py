"""Minimal, single-agent Notion Tasks worker for Bonnie."""

from __future__ import annotations

import logging
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)

DEFAULT_NOTION_VERSION = "2026-03-11"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RELATIONS_PER_PROPERTY = 5
MAX_RESULT_CHARS = 1800
MAX_NOTES_CHARS = 12000


class NotionAPIError(RuntimeError):
    """A safe-to-log summary of a failed Notion API request."""


@dataclass(frozen=True)
class NotionTask:
    page_id: str
    title: str
    notes: str
    context: dict[str, str]


def _plain_text(property_value: dict[str, Any] | None) -> str:
    if not property_value:
        return ""

    property_type = property_value.get("type")
    if property_type == "title":
        items = property_value.get("title", [])
    elif property_type == "rich_text":
        items = property_value.get("rich_text", [])
    else:
        return ""

    return "".join(item.get("plain_text", "") for item in items).strip()


def _select_name(property_value: dict[str, Any] | None) -> str:
    if not property_value:
        return ""
    selected = property_value.get("select") or property_value.get("status")
    return (selected or {}).get("name", "")


def _rich_text_value(text: str) -> dict[str, list[dict[str, Any]]]:
    # Notion limits each text.content item to 2,000 characters.
    chunks = [text[index : index + 1800] for index in range(0, len(text), 1800)]
    return {
        "rich_text": [
            {"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]
        ]
    }


def _append_note(existing: str, note: str) -> str:
    combined = f"{existing.rstrip()}\n\n{note}" if existing.strip() else note
    return combined[-MAX_NOTES_CHARS:]


def _safe_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}".strip()
    message = re.sub(
        r"(?i)(bearer\s+|sk-|ntn_|secret_)[A-Za-z0-9._-]+",
        "[redacted]",
        message,
    )
    return message[:500]


class NotionClient:
    """Small HTTP client scoped to the Tasks data source."""

    def __init__(
        self,
        token: str,
        data_source_id: str,
        *,
        notion_version: str = DEFAULT_NOTION_VERSION,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.data_source_id = data_source_id
        self.timeout_seconds = timeout_seconds
        self.opener = opener
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": notion_version,
            "User-Agent": "bonnie-core/0.3",
        }

    def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request = Request(
            f"https://api.notion.com/v1{path}",
            data=(json.dumps(json_body).encode("utf-8") if json_body is not None else None),
            headers=self.headers,
            method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8"))
                code = error.get("code", "unknown_error")
                detail = error.get("message", "Notion request failed")
            except (UnicodeDecodeError, ValueError):
                code = "invalid_response"
                detail = "Notion returned a non-JSON error"
            raise NotionAPIError(
                f"{method} {path} failed ({exc.code}, {code}): {detail}"
            ) from exc
        except URLError as exc:
            raise NotionAPIError(f"{method} {path} connection failed: {exc.reason}") from exc

    @staticmethod
    def open_task_query() -> dict[str, Any]:
        return {
            "filter": {
                "and": [
                    {"property": "Status", "select": {"equals": "Open"}},
                    {
                        "property": "CEO Required",
                        "checkbox": {"equals": False},
                    },
                    {
                        "property": "Need clarification",
                        "checkbox": {"equals": False},
                    },
                ]
            },
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": 1,
        }

    def find_open_task(self) -> dict[str, Any] | None:
        result = self._request(
            "POST",
            f"/data_sources/{self.data_source_id}/query",
            json_body=self.open_task_query(),
        )
        pages = result.get("results", [])
        return pages[0] if pages else None

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/pages/{page_id}", json_body={"properties": properties}
        )

    def _relation_titles(
        self, properties: dict[str, Any], property_name: str
    ) -> str:
        relation = properties.get(property_name, {}).get("relation", [])
        titles: list[str] = []
        for item in relation[:MAX_RELATIONS_PER_PROPERTY]:
            related = self.get_page(item["id"])
            for value in related.get("properties", {}).values():
                title = _plain_text(value)
                if title and value.get("type") == "title":
                    titles.append(title)
                    break
        return ", ".join(titles)

    def parse_task(self, page: dict[str, Any]) -> NotionTask:
        properties = page.get("properties", {})
        context = {
            "Priority": _select_name(properties.get("Priority")),
            "Area": _select_name(properties.get("Area")),
            "Company": self._relation_titles(properties, "Company"),
            "Project": self._relation_titles(properties, "Project"),
            "Assignee": _plain_text(properties.get("Assignee")),
        }
        due_date = (properties.get("Due date", {}).get("date") or {}).get("start", "")
        if due_date:
            context["Due date"] = due_date

        return NotionTask(
            page_id=page["id"],
            title=_plain_text(properties.get("Title")) or "Untitled task",
            notes=_plain_text(properties.get("Notes")),
            context={key: value for key, value in context.items() if value},
        )

    def claim_task(self, task: NotionTask, claim_id: str) -> bool:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        progress = f"[Bonnie · {timestamp}] Processing started."
        self.update_page(
            task.page_id,
            {
                "Status": {"select": {"name": "Doing"}},
                "Assignee": _rich_text_value(f"Bonnie / bonnie-core [{claim_id}]"),
                "Notes": _rich_text_value(_append_note(task.notes, progress)),
            },
        )

        # Notion has no compare-and-swap update. Verifying a unique claim marker,
        # together with a single Railway replica, prevents normal duplicate runs.
        time.sleep(0.75)
        claimed = self.get_page(task.page_id).get("properties", {})
        return (
            _select_name(claimed.get("Status")) == "Doing"
            and claim_id in _plain_text(claimed.get("Assignee"))
        )

    def complete_task(self, task: NotionTask, result: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        started = f"[Bonnie · {timestamp}] Processing started."
        completed = f"[Bonnie · {timestamp}] Done: {result[:MAX_RESULT_CHARS]}"
        notes = _append_note(_append_note(task.notes, started), completed)
        self.update_page(
            task.page_id,
            {
                "Status": {"select": {"name": "Done"}},
                "Assignee": _rich_text_value("Bonnie / bonnie-core"),
                "Waiting on": _rich_text_value(""),
                "Notes": _rich_text_value(notes),
            },
        )

    def fail_task(self, task: NotionTask, exc: Exception) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        started = f"[Bonnie · {timestamp}] Processing started."
        error = _safe_error(exc)
        failed = f"[Bonnie · {timestamp}] Waiting: {error}"
        notes = _append_note(_append_note(task.notes, started), failed)
        self.update_page(
            task.page_id,
            {
                "Status": {"select": {"name": "Waiting"}},
                "Assignee": _rich_text_value("Bonnie / bonnie-core"),
                "Waiting on": _rich_text_value(error),
                "Notes": _rich_text_value(notes),
            },
        )


class NotionTaskWorker:
    """Poll and process at most one eligible task at a time."""

    def __init__(
        self,
        client: NotionClient,
        processor: Callable[[str], str],
        *,
        poll_seconds: int = 30,
    ) -> None:
        self.client = client
        self.processor = processor
        self.poll_seconds = max(5, poll_seconds)
        self._processing_lock = threading.Lock()
        self._stop_event = threading.Event()

    @staticmethod
    def build_prompt(task: NotionTask) -> str:
        context = "\n".join(f"- {key}: {value}" for key, value in task.context.items())
        return (
            "Complete this Notion task as Bonnie, a single executive-assistant agent.\n"
            "Return only a concise result or progress note suitable for writing back to "
            "Notion. Do not claim an external action was completed unless the task "
            "context proves it.\n\n"
            f"Task title: {task.title}\n"
            f"Notes: {task.notes or '(none)'}\n"
            f"Context:\n{context or '- (none)'}"
        )

    def process_one(self) -> bool:
        if not self._processing_lock.acquire(blocking=False):
            return False

        try:
            page = self.client.find_open_task()
            if not page:
                return False

            task = self.client.parse_task(page)
            claim_id = f"claim:{uuid.uuid4().hex[:12]}"
            if not self.client.claim_task(task, claim_id):
                logger.warning("Task claim lost page_id=%s", task.page_id)
                return False

            logger.info(
                "Task claimed page_id=%s title=%r claim_id=%s",
                task.page_id,
                task.title,
                claim_id,
            )
            try:
                result = self.processor(self.build_prompt(task)).strip()
                if not result:
                    raise ValueError("AI returned an empty task result")
                self.client.complete_task(task, result)
                logger.info("Task completed page_id=%s", task.page_id)
            except Exception as exc:
                logger.exception("Task processing failed page_id=%s", task.page_id)
                try:
                    self.client.fail_task(task, exc)
                except Exception:
                    logger.exception(
                        "Could not record task failure in Notion page_id=%s",
                        task.page_id,
                    )
            return True
        finally:
            self._processing_lock.release()

    def run_forever(self) -> None:
        logger.info("Notion worker started poll_seconds=%s", self.poll_seconds)
        while not self._stop_event.is_set():
            try:
                processed = self.process_one()
            except Exception:
                processed = False
                logger.exception("Notion polling cycle failed")
            self._stop_event.wait(1 if processed else self.poll_seconds)
        logger.info("Notion worker stopped")

    def stop(self) -> None:
        self._stop_event.set()
