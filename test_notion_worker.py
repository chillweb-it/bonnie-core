import unittest

from notion_worker import (
    AgentRecord,
    AgentRun,
    NotionClient,
    NotionTask,
    NotionTaskWorker,
    _safe_error,
)
from anthropic_executor import ExecutionResult


def agent(agent_id, *, company="HQ", domain="ORCH", enabled=True):
    return AgentRecord(
        page_id=f"page-{agent_id}",
        agent_id=agent_id,
        name=agent_id,
        company=company,
        domain=domain,
        enabled=enabled,
        model="gpt-5.6",
        instructions="Be concise.",
        requires_qa=False,
    )


class FakeClient:
    def __init__(self, task, *, claim=True, existing_run=None, agents=None):
        self.task = task
        self.claim = claim
        self.existing_run = existing_run
        self.agents = agents or {
            "AGT-HQ-BONNIE-ORCH-v1": agent("AGT-HQ-BONNIE-ORCH-v1"),
            "AGT-CD-RSCH-ANL-v1": agent(
                "AGT-CD-RSCH-ANL-v1", company="CD", domain="RSCH"
            ),
        }
        self.events = []

    def find_open_task(self):
        return {"id": self.task.page_id} if self.task else None

    def parse_task(self, page):
        return self.task

    def wait_task(self, task, reason):
        self.events.append(("task_waiting", reason))

    def load_agents(self):
        return self.agents

    def idempotency_key(self, task):
        return "idem-1"

    def find_run_by_idempotency_key(self, key):
        return self.existing_run

    def claim_task(self, task, claim_id):
        self.events.append(("claim", claim_id))
        return self.claim

    def next_run_id(self, company, domain):
        return f"RUN-{company}-{domain}-20261007-001"

    def create_run(self, task, selected_agent, run_id, key, max_attempts):
        self.events.append(("queued", run_id, selected_agent.agent_id, max_attempts))
        return AgentRun("run-page", run_id, key, "Queued")

    def mark_run_running(self, run, attempt):
        self.events.append(("running", attempt))

    def complete_run(self, run, result):
        self.events.append(("run_completed", result.text, result.response_id))

    def complete_task(self, task, result, *, run_id="", agent_id=""):
        self.events.append(("task_completed", result, run_id, agent_id))

    def fail_run(self, run, exc):
        self.events.append(("run_failed", str(exc)))

    def fail_task(self, task, exc):
        self.events.append(("task_failed", str(exc)))


class FakeExecutor:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0

    def execute(self, *, model, instructions, prompt):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("provider timed out")
        return ExecutionResult("Research complete.", "resp_123", 42)


class NotionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.task = NotionTask(
            page_id="page-123",
            page_url="https://notion.so/page-123",
            version="2026-10-07T00:00:00.000Z",
            title="Prepare weekly update",
            notes="Cover delivery risks.",
            status="Open",
            company="CD",
            domain="RSCH",
            ceo_required=False,
            need_clarification=False,
            context={"Priority": "P0", "Project": "Agent Project — Bonnie OS"},
        )

    def test_query_includes_gated_open_tasks_for_explicit_handling(self):
        query = NotionClient.open_task_query()
        self.assertEqual(query["page_size"], 1)
        self.assertEqual(
            query["filter"], {"property": "Status", "select": {"equals": "Open"}}
        )

    def test_cd_research_routes_to_specialist(self):
        client = FakeClient(self.task)
        worker = NotionTaskWorker(client, FakeExecutor(), retry_delay_seconds=0)
        self.assertTrue(worker.process_one())
        queued = next(event for event in client.events if event[0] == "queued")
        self.assertEqual(queued[2], "AGT-CD-RSCH-ANL-v1")
        self.assertIn(("running", 1), client.events)
        self.assertTrue(any(event[0] == "run_completed" for event in client.events))
        self.assertTrue(any(event[0] == "task_completed" for event in client.events))

    def test_disabled_specialist_falls_back_to_bonnie(self):
        agents = {
            "AGT-HQ-BONNIE-ORCH-v1": agent("AGT-HQ-BONNIE-ORCH-v1"),
            "AGT-CD-RSCH-ANL-v1": agent(
                "AGT-CD-RSCH-ANL-v1", company="CD", domain="RSCH", enabled=False
            ),
        }
        client = FakeClient(self.task, agents=agents)
        NotionTaskWorker(client, FakeExecutor(), retry_delay_seconds=0).process_one()
        queued = next(event for event in client.events if event[0] == "queued")
        self.assertEqual(queued[2], "AGT-HQ-BONNIE-ORCH-v1")

    def test_no_enabled_agent_waits_without_claim(self):
        client = FakeClient(
            self.task,
            agents={"AGT-HQ-BONNIE-ORCH-v1": agent("AGT-HQ-BONNIE-ORCH-v1", enabled=False)},
        )
        NotionTaskWorker(client, FakeExecutor()).process_one()
        self.assertEqual(client.events[0][0], "task_waiting")
        self.assertFalse(any(event[0] == "claim" for event in client.events))

    def test_ceo_gate_waits_without_creating_run(self):
        gated = NotionTask(**{**self.task.__dict__, "ceo_required": True})
        client = FakeClient(gated)
        NotionTaskWorker(client, FakeExecutor()).process_one()
        self.assertIn("CEO approval", client.events[0][1])
        self.assertFalse(any(event[0] == "queued" for event in client.events))

    def test_clarification_gate_waits_without_creating_run(self):
        gated = NotionTask(**{**self.task.__dict__, "need_clarification": True})
        client = FakeClient(gated)
        NotionTaskWorker(client, FakeExecutor()).process_one()
        self.assertIn("Clarification", client.events[0][1])

    def test_existing_idempotency_key_suppresses_duplicate(self):
        existing = AgentRun("run-page", "RUN-CD-RSCH-20261007-001", "idem-1", "Completed")
        client = FakeClient(self.task, existing_run=existing)
        NotionTaskWorker(client, FakeExecutor()).process_one()
        self.assertIn("duplicate suppressed", client.events[0][1])
        self.assertFalse(any(event[0] == "claim" for event in client.events))

    def test_retries_are_bounded_and_lifecycle_completes(self):
        client = FakeClient(self.task)
        executor = FakeExecutor(failures=2)
        worker = NotionTaskWorker(
            client, executor, max_attempts=3, retry_delay_seconds=0
        )
        worker.process_one()
        self.assertEqual(executor.calls, 3)
        self.assertEqual(
            [event[1] for event in client.events if event[0] == "running"],
            [1, 2, 3],
        )
        self.assertTrue(any(event[0] == "run_completed" for event in client.events))

    def test_final_failure_updates_run_and_task(self):
        client = FakeClient(self.task)
        worker = NotionTaskWorker(
            client, FakeExecutor(failures=3), max_attempts=3, retry_delay_seconds=0
        )
        worker.process_one()
        self.assertTrue(any(event[0] == "run_failed" for event in client.events))
        self.assertTrue(any(event[0] == "task_failed" for event in client.events))

    def test_prompt_contains_title_notes_context_and_agent(self):
        selected = agent("AGT-CD-RSCH-ANL-v1", company="CD", domain="RSCH")
        prompt = NotionTaskWorker.build_prompt(self.task, selected)
        self.assertIn(self.task.title, prompt)
        self.assertIn(self.task.notes, prompt)
        self.assertIn("Priority: P0", prompt)
        self.assertIn(selected.name, prompt)

    def test_error_text_redacts_tokens(self):
        error = _safe_error(RuntimeError("Bearer secret_super-secret-value"))
        self.assertNotIn("super-secret-value", error)
        self.assertIn("[redacted]", error)

    def test_completion_never_updates_relation_properties(self):
        client = NotionClient("not-a-real-token", "data-source-id")
        captured = {}

        def capture(page_id, properties):
            captured.update(properties)
            return {}

        client.update_page = capture
        client.complete_task(self.task, "Finished safely.")
        self.assertNotIn("Company", captured)
        self.assertNotIn("Project", captured)
        self.assertEqual(captured["Status"]["select"]["name"], "Done")

    def test_create_task_uses_safe_queue_defaults(self):
        client = NotionClient("not-a-real-token", "data-source-id")
        captured = {}

        def capture(method, path, *, json_body=None):
            captured.update({"method": method, "path": path, "body": json_body})
            return {"id": "new-page", "url": "https://notion.so/new-page"}

        client._request = capture
        page = client.create_task(
            "Prepare board agenda", "Summarize decisions and risks.", priority="P1"
        )
        self.assertEqual(page["id"], "new-page")
        properties = captured["body"]["properties"]
        self.assertEqual(properties["Status"]["select"]["name"], "Open")
        self.assertFalse(properties["CEO Required"]["checkbox"])
        self.assertFalse(properties["Need clarification"]["checkbox"])
        self.assertNotIn("Company", properties)


if __name__ == "__main__":
    unittest.main()
