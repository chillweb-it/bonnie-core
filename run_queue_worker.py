"""Conversation-first Claude execution queue for Bonnie v0.5."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from anthropic_executor import ExecutionResult
from notion_worker import (
    AgentRecord,
    FALLBACK_AGENT_ID,
    NotionClient,
    _checkbox,
    _log_event,
    _plain_text,
    _rich_text_value,
    _safe_error,
    _select_name,
    _utc_now,
)


logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 1800
MAX_NOTES_CHARS = 12000


@dataclass(frozen=True)
class QueuedClaudeRun:
    page_id: str
    run_id: str
    company: str
    domain: str
    work_brief: str
    notes: str
    ceo_required: bool
    need_clarification: bool
    max_attempts: int
    agent_relation_ids: tuple[str, ...]


class AgentRunQueueWorker:
    """Execute CLAUDE-routed Agent Runs created by Bonnie ChatGPT."""

    def __init__(
        self,
        client: NotionClient,
        executor: Any,
        *,
        poll_seconds: int = 10,
        max_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self.client = client
        self.executor = executor
        self.poll_seconds = max(5, poll_seconds)
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._processing_lock = threading.Lock()
        self._stop_event = threading.Event()

    def _find_queued_run(self) -> dict[str, Any] | None:
        result = self.client._query(
            self.client.agent_runs_data_source_id,
            {
                "filter": {
                    "and": [
                        {"property": "Status", "select": {"equals": "Queued"}},
                        {
                            "property": "Execution Route",
                            "select": {"equals": "CLAUDE"},
                        },
                    ]
                },
                "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
                "page_size": 1,
            },
        )
        pages = result.get("results", [])
        return pages[0] if pages else None

    @staticmethod
    def _parse_run(page: dict[str, Any], default_max_attempts: int) -> QueuedClaudeRun:
        props = page.get("properties", {})
        relation_ids = tuple(
            item.get("id", "")
            for item in props.get("Agent", {}).get("relation", [])
            if item.get("id")
        )
        configured_attempts = (props.get("Max Attempts", {}) or {}).get("number")
        return QueuedClaudeRun(
            page_id=page["id"],
            run_id=_plain_text(props.get("Run ID")) or page["id"][:12],
            company=_select_name(props.get("Company")) or "HQ",
            domain=_select_name(props.get("Domain")) or "GENERAL",
            work_brief=_plain_text(props.get("Work Brief")),
            notes=_plain_text(props.get("Notes")),
            ceo_required=_checkbox(props.get("CEO Required")),
            need_clarification=_checkbox(props.get("Need Clarification")),
            max_attempts=max(
                1,
                int(configured_attempts)
                if configured_attempts is not None
                else default_max_attempts,
            ),
            agent_relation_ids=relation_ids,
        )

    @staticmethod
    def _choose_agent(
        run: QueuedClaudeRun, agents: dict[str, AgentRecord]
    ) -> AgentRecord | None:
        if run.agent_relation_ids:
            for agent in agents.values():
                if agent.page_id in run.agent_relation_ids and agent.enabled:
                    return agent

        exact = sorted(
            (
                agent
                for agent in agents.values()
                if agent.enabled
                and agent.company == run.company
                and agent.domain == run.domain
            ),
            key=lambda item: item.agent_id,
        )
        if exact:
            return exact[0]

        fallback = agents.get(FALLBACK_AGENT_ID)
        if fallback and fallback.enabled:
            return fallback
        return None

    @staticmethod
    def _build_prompt(run: QueuedClaudeRun, agent: AgentRecord) -> str:
        return (
            "Complete this delegated AI work request for Bonnie. "
            "This execution route has no browser or connected-app access unless the "
            "brief itself contains the needed information. Do not claim external actions "
            "or web research were completed unless the provided context proves it. "
            "Return a useful deliverable that Bonnie can review and report to Evan.\n\n"
            f"Company: {run.company}\n"
            f"Domain: {run.domain}\n"
            f"Assigned agent: {agent.agent_id} ({agent.name})\n"
            f"Work brief:\n{run.work_brief}\n\n"
            f"Additional context / notes:\n{run.notes or '(none)'}"
        )

    def _update_run(self, page_id: str, properties: dict[str, Any]) -> None:
        self.client.update_page(page_id, properties)

    def _wait_run(self, run: QueuedClaudeRun, reason: str) -> None:
        self._update_run(
            run.page_id,
            {
                "Status": {"select": {"name": "Waiting"}},
                "Error": _rich_text_value(reason),
                "Report Status": {"select": {"name": "Ready"}},
                "Ended At": {"date": {"start": _utc_now()}},
            },
        )

    def _claim(self, run: QueuedClaudeRun, agent: AgentRecord) -> str | None:
        claim_id = f"runclaim:{uuid.uuid4().hex[:12]}"
        self._update_run(
            run.page_id,
            {
                "Status": {"select": {"name": "Running"}},
                "Started At": {"date": {"start": _utc_now()}},
                "Worker Claim": _rich_text_value(claim_id),
                "Agent": {"relation": [{"id": agent.page_id}]},
                "Report Status": {"select": {"name": "Pending"}},
                "Error": _rich_text_value(""),
            },
        )
        time.sleep(0.5)
        current = self.client.get_page(run.page_id).get("properties", {})
        if (
            _select_name(current.get("Status")) == "Running"
            and claim_id in _plain_text(current.get("Worker Claim"))
        ):
            return claim_id
        return None

    def _complete(
        self,
        run: QueuedClaudeRun,
        result: ExecutionResult,
        *,
        attempt: int,
    ) -> None:
        full_note = result.text[:MAX_NOTES_CHARS]
        properties: dict[str, Any] = {
            "Status": {"select": {"name": "Completed"}},
            "Ended At": {"date": {"start": _utc_now()}},
            "Attempt": {"number": attempt},
            "Result Summary": _rich_text_value(result.text[:MAX_RESULT_CHARS]),
            "Notes": _rich_text_value(full_note),
            "OpenAI Response/Run ID": _rich_text_value(result.response_id),
            "Report Status": {"select": {"name": "Ready"}},
            "Error": _rich_text_value(""),
        }
        if result.total_tokens is not None:
            properties["Token Usage"] = {"number": result.total_tokens}
        if result.estimated_cost is not None:
            properties["Estimated Cost"] = {"number": result.estimated_cost}
        self._update_run(run.page_id, properties)

    def _fail(self, run: QueuedClaudeRun, exc: Exception, *, attempt: int) -> None:
        self._update_run(
            run.page_id,
            {
                "Status": {"select": {"name": "Failed"}},
                "Ended At": {"date": {"start": _utc_now()}},
                "Attempt": {"number": attempt},
                "Error": _rich_text_value(_safe_error(exc)),
                "Report Status": {"select": {"name": "Ready"}},
            },
        )

    def process_one(self) -> bool:
        if not self._processing_lock.acquire(blocking=False):
            return False
        try:
            page = self._find_queued_run()
            if not page:
                return False

            run = self._parse_run(page, self.max_attempts)
            if run.ceo_required:
                self._wait_run(run, "CEO approval required before Claude execution.")
                _log_event("run_gated", run_id=run.run_id, gate="ceo_required")
                return True
            if run.need_clarification:
                self._wait_run(run, "Clarification required before Claude execution.")
                _log_event("run_gated", run_id=run.run_id, gate="clarification")
                return True
            if not run.work_brief.strip():
                self._wait_run(run, "Work Brief is empty.")
                _log_event("run_gated", run_id=run.run_id, gate="missing_brief")
                return True

            agents = self.client.load_agents()
            agent = self._choose_agent(run, agents)
            if agent is None:
                self._wait_run(run, "No enabled matching agent or Bonnie fallback is available.")
                _log_event("run_routing_failed", run_id=run.run_id)
                return True

            claim_id = self._claim(run, agent)
            if not claim_id:
                _log_event(
                    "run_claim_lost",
                    run_id=run.run_id,
                    agent_id=agent.agent_id,
                )
                return False

            _log_event(
                "run_claimed",
                run_id=run.run_id,
                agent_id=agent.agent_id,
                claim_id=claim_id,
            )

            prompt = self._build_prompt(run, agent)
            last_error: Exception | None = None
            result: ExecutionResult | None = None
            completed_attempt = 0

            for attempt in range(1, run.max_attempts + 1):
                self._update_run(
                    run.page_id,
                    {
                        "Attempt": {"number": attempt},
                        "Max Attempts": {"number": run.max_attempts},
                    },
                )
                try:
                    _log_event(
                        "run_attempt",
                        run_id=run.run_id,
                        agent_id=agent.agent_id,
                        attempt=attempt,
                    )
                    result = self.executor.execute(
                        model=agent.model,
                        instructions=agent.instructions,
                        prompt=prompt,
                    )
                    if not result.text.strip():
                        raise ValueError("Claude returned an empty result")
                    completed_attempt = attempt
                    break
                except Exception as exc:
                    last_error = exc
                    _log_event(
                        "run_attempt_failed",
                        run_id=run.run_id,
                        agent_id=agent.agent_id,
                        attempt=attempt,
                        error=_safe_error(exc),
                    )
                    if attempt < run.max_attempts:
                        time.sleep(self.retry_delay_seconds * attempt)

            if result is None:
                assert last_error is not None
                self._fail(run, last_error, attempt=run.max_attempts)
                return True

            self._complete(run, result, attempt=completed_attempt)
            _log_event(
                "run_completed",
                run_id=run.run_id,
                agent_id=agent.agent_id,
                attempt=completed_attempt,
            )
            return True
        except Exception:
            logger.exception("Claude Agent Run queue cycle failed")
            return True
        finally:
            self._processing_lock.release()

    def run_forever(self) -> None:
        _log_event(
            "agent_run_queue_started",
            poll_seconds=self.poll_seconds,
            max_attempts=self.max_attempts,
        )
        while not self._stop_event.is_set():
            try:
                processed = self.process_one()
            except Exception:
                processed = False
                logger.exception("Claude Agent Run queue polling failed")
            self._stop_event.wait(1 if processed else self.poll_seconds)
        _log_event("agent_run_queue_stopped")

    def stop(self) -> None:
        self._stop_event.set()
