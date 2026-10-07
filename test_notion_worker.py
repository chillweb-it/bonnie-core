import unittest

from notion_worker import NotionClient, NotionTask, NotionTaskWorker, _safe_error


class FakeClient:
    def __init__(self, task, *, claim=True):
        self.task = task
        self.claim = claim
        self.completed = None
        self.failed = None

    def find_open_task(self):
        return {"id": self.task.page_id} if self.task else None

    def parse_task(self, page):
        return self.task

    def claim_task(self, task, claim_id):
        return self.claim

    def complete_task(self, task, result):
        self.completed = (task, result)

    def fail_task(self, task, exc):
        self.failed = (task, exc)


class NotionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.task = NotionTask(
            page_id="page-123",
            title="Prepare weekly update",
            notes="Cover delivery risks.",
            context={"Priority": "P0", "Project": "Agent Project — Bonnie OS"},
        )

    def test_query_only_returns_eligible_open_tasks(self):
        query = NotionClient.open_task_query()
        self.assertEqual(query["page_size"], 1)
        filters = query["filter"]["and"]
        self.assertIn(
            {"property": "Status", "select": {"equals": "Open"}}, filters
        )
        self.assertIn(
            {"property": "CEO Required", "checkbox": {"equals": False}},
            filters,
        )
        self.assertIn(
            {"property": "Need clarification", "checkbox": {"equals": False}},
            filters,
        )

    def test_processes_one_claimed_task_and_completes_it(self):
        client = FakeClient(self.task)
        worker = NotionTaskWorker(client, lambda prompt: "Weekly update prepared.")

        self.assertTrue(worker.process_one())
        self.assertEqual(client.completed[1], "Weekly update prepared.")
        self.assertIsNone(client.failed)

    def test_failed_ai_call_moves_task_to_failure_path(self):
        client = FakeClient(self.task)

        def fail(_prompt):
            raise RuntimeError("provider unavailable")

        worker = NotionTaskWorker(client, fail)
        self.assertTrue(worker.process_one())
        self.assertIsNone(client.completed)
        self.assertIsInstance(client.failed[1], RuntimeError)

    def test_lost_claim_is_not_processed(self):
        client = FakeClient(self.task, claim=False)
        calls = []
        worker = NotionTaskWorker(client, lambda prompt: calls.append(prompt))

        self.assertFalse(worker.process_one())
        self.assertEqual(calls, [])

    def test_prompt_contains_title_notes_and_context(self):
        prompt = NotionTaskWorker.build_prompt(self.task)
        self.assertIn(self.task.title, prompt)
        self.assertIn(self.task.notes, prompt)
        self.assertIn("Priority: P0", prompt)
        self.assertIn("Project: Agent Project — Bonnie OS", prompt)

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
            "Prepare board agenda",
            "Summarize decisions and risks.",
            priority="P1",
        )

        self.assertEqual(page["id"], "new-page")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/pages")
        body = captured["body"]
        self.assertEqual(body["parent"]["data_source_id"], "data-source-id")
        properties = body["properties"]
        self.assertEqual(properties["Status"]["select"]["name"], "Open")
        self.assertEqual(properties["Priority"]["select"]["name"], "P1")
        self.assertFalse(properties["CEO Required"]["checkbox"])
        self.assertFalse(properties["Need clarification"]["checkbox"])
        self.assertNotIn("Company", properties)
        self.assertNotIn("Project", properties)

    def test_create_task_rejects_invalid_priority(self):
        client = NotionClient("not-a-real-token", "data-source-id")
        with self.assertRaises(ValueError):
            client.create_task("Prepare update", "Notes", priority="urgent")


if __name__ == "__main__":
    unittest.main()
