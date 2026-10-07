"""Notion-backed v0.4 registry, router, run ledger, and task worker."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from anthropic_executor import ExecutionResult


logger = logging.getLogger(__name__)

DEFAULT_NOTION_VERSION = "2026-03-11"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RELATIONS_PER_PROPERTY = 5
MAX_RESULT_CHARS = 1800
MAX_NOTES_CHARS = 12000
MAX_TITLE_CHARS = 200
FALLBACK_AGENT_ID = "AGT-HQ-BONNIE-ORCH-v1"
CD_RESEARCH_AGENT_ID = "AGT-CD-RSCH-ANL-v1"


class NotionAPIError(RuntimeError):
    """A safe-to-log summary of a failed Notion API request."""


@dataclass(frozen=True)
class NotionTask:
    page_id: str
    page_url: str
    version: str
    title: str
    notes: str
    status: str
    company: str
    domain: str
    ceo_required: bool
    need_clarification: bool
    context: dict[str, str]


@dataclass(frozen=True)
class AgentRecord:
    page_id: str
    agent_id: str
    name: str
    company: str
    domain: str
    enabled: bool
    model: str
    instructions: str
    requires_qa: bool


@dataclass(frozen=True)
class AgentRun:
    page_id: str
    run_id: str
    idempotency_key: str
    status: str


class AgentExecutor(Protocol):
    def execute(self, *, model: str, instructions: str, prompt: str) -> ExecutionResult:
        ...


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


def _checkbox(property_value: dict[str, Any] | None) -> bool:
    return bool((property_value or {}).get("checkbox", False))


def _rich_text_value(text: str) -> dict[str, list[dict[str, Any]]]:
    chunks = [text[index : index + 1800] for index in range(0, len(text), 1800)]
    return {
        "rich_text": [
            {"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]
        ]
    }


def _title_value(text: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "title": [
            {
                "type": "text",
                "text": {"content": text.strip()[:MAX_TITLE_CHARS]},
            }
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


def _normalise_company(value: str) -> str:
    upper = value.strip().upper()
    if upper == "CD" or "祥雲" in value or "CLOUD DECOCT" in upper:
        return "CD"
    if upper in {"HQ", "CW", "THT"}:
        return upper
    return upper or "HQ"


def _normalise_domain(value: str) -> str:
    upper = value.strip().upper()
    aliases = {"RESEARCH": "RSCH", "RSCH": "RSCH", "ORCHESTRATION": "ORCH"}
    return aliases.get(upper, upper or "GENERAL")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))


class NotionClient:
    """Small Notion REST client scoped to Tasks, Agents, and Agent Runs."""

    def __init__(
        self,
        token: str,
        data_source_id: str,
        *,
        agents_data_source_id: str = "",
        agent_runs_data_source_id: str = "",
        notion_version: str = DEFAULT_NOTION_VERSION,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.data_source_id = data_source_id
        self.agents_data_source_id = agents_data_source_id
        self.agent_runs_data_source_id = agent_runs_data_source_id
        self.timeout_seconds = timeout_seconds
        self.opener = opener
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": notion_version,
            "User-Agent": "bonnie-core/0.4",
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

    def _query(self, data_source_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", f"/data_sources/{data_source_id}/query", json_body=body
        )

    @staticmethod
    def open_task_query() -> dict[str, Any]:
        return {
            "filter": {"property": "Status", "select": {"equals": "Open"}},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": 1,
        }

    def find_open_task(self) -> dict[str, Any] | None:
        result = self._query(self.data_source_id, self.open_task_query())
        pages = result.get("results", [])
        return pages[0] if pages else None

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/pages/{page_id}", json_body={"properties": properties}
        )

    def create_task(
        self,
        title: str,
        notes: str,
        *,
        priority: str = "P2",
    ) -> dict[str, Any]:
        """Preserve the v0.3.1 Slack-to-Notion task bridge."""
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Task title is required")
        if priority not in {"P0", "P1", "P2", "P3"}:
            raise ValueError("Priority must be P0, P1, P2, or P3")
        return self._request(
            "POST",
            "/pages",
            json_body={
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": self.data_source_id,
                },
                "properties": {
                    "Title": _title_value(clean_title),
                    "Notes": _rich_text_value(notes.strip() or clean_title),
                    "Status": {"select": {"name": "Open"}},
                    "Priority": {"select": {"name": priority}},
                    "CEO Required": {"checkbox": False},
                    "Need clarification": {"checkbox": False},
                    "Assignee": _rich_text_value("Bonnie / bonnie-core"),
                },
            },
        )

    def _relation_titles(self, properties: dict[str, Any], property_name: str) -> str:
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
        company_title = self._relation_titles(properties, "Company")
        domain = _select_name(properties.get("Domain"))
        if not domain and _select_name(properties.get("Area")).upper() == "RESEARCH":
            domain = "RSCH"
        context = {
            "Priority": _select_name(properties.get("Priority")),
            "Area": _select_name(properties.get("Area")),
            "Company": company_title,
            "Domain": domain,
            "Project": self._relation_titles(properties, "Project"),
            "Assignee": _plain_text(properties.get("Assignee")),
        }
        due_date = (properties.get("Due date", {}).get("date") or {}).get("start", "")
        if due_date:
            context["Due date"] = due_date
        return NotionTask(
            page_id=page["id"],
            page_url=page.get("url", ""),
            version=page.get("last_edited_time", ""),
            title=_plain_text(properties.get("Title")) or "Untitled task",
            notes=_plain_text(properties.get("Notes")),
            status=_select_name(properties.get("Status")),
            company=_normalise_company(company_title),
            domain=_normalise_domain(domain),
            ceo_required=_checkbox(properties.get("CEO Required")),
            need_clarification=_checkbox(properties.get("Need clarification")),
            context={key: value for key, value in context.items() if value},
        )

    def load_agents(self) -> dict[str, AgentRecord]:
        if not self.agents_data_source_id:
            return {}
        pages: list[dict[str, Any]] = []
        cursor = ""
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            result = self._query(self.agents_data_source_id, body)
            pages.extend(result.get("results", []))
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor") or ""
            if not cursor:
                break
        agents: dict[str, AgentRecord] = {}
        for page in pages:
            props = page.get("properties", {})
            agent_id = _plain_text(props.get("Agent ID"))
            if not agent_id:
                continue
            agents[agent_id] = AgentRecord(
                page_id=page["id"],
                agent_id=agent_id,
                name=_plain_text(props.get("Name")) or agent_id,
                company=_normalise_company(_select_name(props.get("Company"))),
                domain=_normalise_domain(_select_name(props.get("Domain"))),
                enabled=_checkbox(props.get("Enabled")),
                model=_plain_text(props.get("Model")),
                instructions=_plain_text(props.get("Instructions")),
                requires_qa=_checkbox(props.get("Requires QA")),
            )
        return agents

    @staticmethod
    def idempotency_key(task: NotionTask) -> str:
        version = task.version or "unversioned"
        digest = hashlib.sha256(f"{task.page_id}:{version}".encode()).hexdigest()[:24]
        return f"task:{task.page_id}:v:{digest}"

    def find_run_by_idempotency_key(self, key: str) -> AgentRun | None:
        result = self._query(
            self.agent_runs_data_source_id,
            {
                "filter": {
                    "property": "Idempotency Key",
                    "rich_text": {"equals": key},
                },
                "page_size": 1,
            },
        )
        pages = result.get("results", [])
        if not pages:
            return None
        page = pages[0]
        props = page.get("properties", {})
        return AgentRun(
            page_id=page["id"],
            run_id=_plain_text(props.get("Run ID")),
            idempotency_key=_plain_text(props.get("Idempotency Key")),
            status=_select_name(props.get("Status")),
        )

    def next_run_id(self, company: str, domain: str) -> str:
        prefix = f"RUN-{company}-{domain}-{datetime.now(timezone.utc):%Y%m%d}-"
        result = self._query(
            self.agent_runs_data_source_id,
            {
                "filter": {"property": "Run ID", "title": {"starts_with": prefix}},
                "page_size": 100,
            },
        )
        highest = 0
        for page in result.get("results", []):
            run_id = _plain_text(page.get("properties", {}).get("Run ID"))
            match = re.fullmatch(re.escape(prefix) + r"(\d{3,})", run_id)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"{prefix}{highest + 1:03d}"

    def claim_task(self, task: NotionTask, claim_id: str) -> bool:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        progress = f"[Bonnie v0.4 · {timestamp}] Processing started ({claim_id})."
        self.update_page(
            task.page_id,
            {
                "Status": {"select": {"name": "Doing"}},
                "Assignee": _rich_text_value(f"Bonnie / bonnie-core [{claim_id}]"),
                "Notes": _rich_text_value(_append_note(task.notes, progress)),
            },
        )
        time.sleep(0.75)
        claimed = self.get_page(task.page_id).get("properties", {})
        return (
            _select_name(claimed.get("Status")) == "Doing"
            and claim_id in _plain_text(claimed.get("Assignee"))
        )

    def create_run(
        self,
        task: NotionTask,
        agent: AgentRecord,
        run_id: str,
        idempotency_key: str,
        max_attempts: int,
    ) -> AgentRun:
        page = self._request(
            "POST",
            "/pages",
            json_body={
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": self.agent_runs_data_source_id,
                },
                "properties": {
                    "Run ID": _title_value(run_id),
                    "Task": {"relation": [{"id": task.page_id}]},
                    "Agent": {"relation": [{"id": agent.page_id}]},
                    "Company": {"select": {"name": task.company}},
                    "Domain": {"select": {"name": task.domain}},
                    "Status": {"select": {"name": "Queued"}},
                    "Idempotency Key": _rich_text_value(idempotency_key),
                    "Attempt": {"number": 0},
                    "Max Attempts": {"number": max_attempts},
                    "QA Result": {
                        "select": {"name": "Pending" if agent.requires_qa else "Not Required"}
                    },
                    "Notes": _rich_text_value(
                        f"Queued by bonnie-core v0.4 for {agent.agent_id}."
                    ),
                },
            },
        )
        return AgentRun(page["id"], run_id, idempotency_key, "Queued")

    def mark_run_running(self, run: AgentRun, attempt: int) -> None:
        self.update_page(
            run.page_id,
            {
                "Status": {"select": {"name": "Running"}},
                "Attempt": {"number": attempt},
                "Started At": {"date": {"start": _utc_now()}},
                "Error": _rich_text_value(""),
            },
        )

    def complete_run(self, run: AgentRun, result: ExecutionResult) -> None:
        properties: dict[str, Any] = {
            "Status": {"select": {"name": "Completed"}},
            "Ended At": {"date": {"start": _utc_now()}},
            "Result Summary": _rich_text_value(result.text[:MAX_RESULT_CHARS]),
            "OpenAI Response/Run ID": _rich_text_value(result.response_id),
            "Error": _rich_text_value(""),
        }
        if result.total_tokens is not None:
            properties["Token Usage"] = {"number": result.total_tokens}
        if result.estimated_cost is not None:
            properties["Estimated Cost"] = {"number": result.estimated_cost}
        self.update_page(run.page_id, properties)

    def fail_run(self, run: AgentRun, exc: Exception) -> None:
        self.update_page(
            run.page_id,
            {
                "Status": {"select": {"name": "Failed"}},
                "Ended At": {"date": {"start": _utc_now()}},
                "Error": _rich_text_value(_safe_error(exc)),
            },
        )

    def complete_task(
        self, task: NotionTask, result: str, *, run_id: str = "", agent_id: str = ""
    ) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        label = " · ".join(value for value in (run_id, agent_id) if value)
        completed = (
            f"[Bonnie v0.4 · {timestamp}] Done"
            f"{f' ({label})' if label else ''}: {result[:MAX_RESULT_CHARS]}"
        )
        self.update_page(
            task.page_id,
            {
                "Status": {"select": {"name": "Done"}},
                "Assignee": _rich_text_value(agent_id or "Bonnie / bonnie-core"),
                "Waiting on": _rich_text_value(""),
                "Notes": _rich_text_value(_append_note(task.notes, completed)),
            },
        )

    def wait_task(self, task: NotionTask, reason: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        note = f"[Bonnie v0.4 · {timestamp}] Waiting: {reason}"
        self.update_page(
            task.page_id,
            {
                "Status": {"select": {"name": "Waiting"}},
                "Waiting on": _rich_text_value(reason),
                "Notes": _rich_text_value(_append_note(task.notes, note)),
            },
        )

    def fail_task(self, task: NotionTask, exc: Exception) -> None:
        self.wait_task(task, _safe_error(exc))


class NotionTaskWorker:
    """Route and execute at most one Notion task per polling cycle."""

    def __init__(
        self,
        client: NotionClient,
        executor: AgentExecutor | Callable[[str], str],
        *,
        poll_seconds: int = 30,
        max_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self.client = client
        self.executor = executor
        self.poll_seconds = max(5, poll_seconds)
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0, retry_delay_seconds)
        self._processing_lock = threading.Lock()
        self._stop_event = threading.Event()

    @staticmethod
    def route(task: NotionTask, agents: dict[str, AgentRecord]) -> AgentRecord | None:
        preferred_id = (
            CD_RESEARCH_AGENT_ID
            if task.company == "CD" and task.domain == "RSCH"
            else FALLBACK_AGENT_ID
        )
        preferred = agents.get(preferred_id)
        if preferred and preferred.enabled:
            return preferred
        fallback = agents.get(FALLBACK_AGENT_ID)
        if fallback and fallback.enabled:
            return fallback
        return None

    @staticmethod
    def build_prompt(task: NotionTask, agent: AgentRecord | None = None) -> str:
        context = "\n".join(f"- {key}: {value}" for key, value in task.context.items())
        agent_name = agent.name if agent else "Bonnie"
        return (
            f"Complete this Notion task as {agent_name}.\n"
            "Return only a concise, useful result or progress note suitable for writing "
            "back to Notion. Preserve factual uncertainty and do not claim an external "
            "action was completed unless the supplied context proves it.\n\n"
            f"Task title: {task.title}\n"
            f"Notes: {task.notes or '(none)'}\n"
            f"Context:\n{context or '- (none)'}"
        )

    def _execute(self, agent: AgentRecord, prompt: str) -> ExecutionResult:
        if hasattr(self.executor, "execute"):
            return self.executor.execute(
                model=agent.model,
                instructions=agent.instructions,
                prompt=prompt,
            )
        result = self.executor(prompt)
        return ExecutionResult(text=str(result).strip())

    def process_one(self) -> bool:
        if not self._processing_lock.acquire(blocking=False):
            return False
        try:
            page = self.client.find_open_task()
            if not page:
                return False
            task = self.client.parse_task(page)
            if task.ceo_required:
                self.client.wait_task(task, "CEO approval required; automatic execution is blocked.")
                _log_event("task_gated", task_id=task.page_id, gate="ceo_required")
                return True
            if task.need_clarification:
                self.client.wait_task(
                    task, "Clarification required before automatic execution can start."
                )
                _log_event("task_gated", task_id=task.page_id, gate="clarification")
                return True

            agents = self.client.load_agents()
            agent = self.route(task, agents)
            if agent is None:
                self.client.wait_task(
                    task,
                    "No valid enabled agent is available (specialist and Bonnie fallback unavailable).",
                )
                _log_event("routing_failed", task_id=task.page_id)
                return True

            key = self.client.idempotency_key(task)
            existing = self.client.find_run_by_idempotency_key(key)
            if existing:
                reason = f"Existing Agent Run {existing.run_id} is {existing.status}; duplicate suppressed."
                self.client.wait_task(task, reason)
                _log_event(
                    "duplicate_suppressed",
                    task_id=task.page_id,
                    run_id=existing.run_id,
                    agent_id=agent.agent_id,
                )
                return True

            claim_id = f"claim:{uuid.uuid4().hex[:12]}"
            if not self.client.claim_task(task, claim_id):
                _log_event("claim_lost", task_id=task.page_id, agent_id=agent.agent_id)
                return False

            run_id = self.client.next_run_id(task.company, task.domain)
            run = self.client.create_run(task, agent, run_id, key, self.max_attempts)
            _log_event(
                "task_claimed",
                task_id=task.page_id,
                run_id=run.run_id,
                agent_id=agent.agent_id,
                attempt=0,
            )

            prompt = self.build_prompt(task, agent)
            last_error: Exception | None = None
            result: ExecutionResult | None = None
            completed_attempt = 0
            for attempt in range(1, self.max_attempts + 1):
                try:
                    self.client.mark_run_running(run, attempt)
                    _log_event(
                        "run_attempt",
                        task_id=task.page_id,
                        run_id=run.run_id,
                        agent_id=agent.agent_id,
                        attempt=attempt,
                    )
                    result = self._execute(agent, prompt)
                    if not result.text.strip():
                        raise ValueError("AI returned an empty task result")
                    completed_attempt = attempt
                    break
                except Exception as exc:
                    last_error = exc
                    _log_event(
                        "run_attempt_failed",
                        task_id=task.page_id,
                        run_id=run.run_id,
                        agent_id=agent.agent_id,
                        attempt=attempt,
                        error=_safe_error(exc),
                    )
                    if attempt < self.max_attempts:
                        time.sleep(self.retry_delay_seconds * attempt)

            if result is None:
                assert last_error is not None
                try:
                    self.client.fail_run(run, last_error)
                finally:
                    self.client.fail_task(task, last_error)
                return True

            # Persist the successful response once. A Notion write failure must
            # never trigger another billable model execution for the same run.
            self.client.complete_run(run, result)
            self.client.complete_task(
                task,
                result.text,
                run_id=run.run_id,
                agent_id=agent.agent_id,
            )
            _log_event(
                "run_completed",
                task_id=task.page_id,
                run_id=run.run_id,
                agent_id=agent.agent_id,
                attempt=completed_attempt,
            )
            return True
        except Exception as exc:
            logger.exception("Notion v0.4 polling cycle failed")
            try:
                if "task" in locals():
                    self.client.fail_task(task, exc)
            except Exception:
                logger.exception("Could not record task failure in Notion")
            return True
        finally:
            self._processing_lock.release()

    def run_forever(self) -> None:
        _log_event(
            "notion_worker_started",
            poll_seconds=self.poll_seconds,
            max_attempts=self.max_attempts,
        )
        while not self._stop_event.is_set():
            try:
                processed = self.process_one()
            except Exception:
                processed = False
                logger.exception("Notion polling cycle failed")
            self._stop_event.wait(1 if processed else self.poll_seconds)
        _log_event("notion_worker_stopped")

    def stop(self) -> None:
        self._stop_event.set()
