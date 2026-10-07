import unittest

from slack_notion import is_task_help, parse_slack_task_command


class SlackNotionTests(unittest.TestCase):
    def test_plain_chat_is_not_a_task(self):
        self.assertIsNone(parse_slack_task_command("Can you read my Notion?"))

    def test_simple_english_task(self):
        command = parse_slack_task_command("Task: Prepare the weekly update")
        self.assertEqual(command.title, "Prepare the weekly update")
        self.assertEqual(command.notes, "Prepare the weekly update")
        self.assertEqual(command.priority, "P2")

    def test_cantonese_task_with_notes_and_priority(self):
        command = parse_slack_task_command(
            "幫我建立 Task：準備董事會 agenda\n"
            "整理未完成決定同風險。\n"
            "Priority: P1"
        )
        self.assertEqual(command.title, "準備董事會 agenda")
        self.assertIn("整理未完成決定同風險", command.notes)
        self.assertNotIn("Priority", command.notes)
        self.assertEqual(command.priority, "P1")

    def test_priority_suffix(self):
        command = parse_slack_task_command("任務：Prepare risk list [P0]")
        self.assertEqual(command.title, "Prepare risk list")
        self.assertEqual(command.priority, "P0")

    def test_help_command(self):
        self.assertTrue(is_task_help("Task help"))
        self.assertTrue(is_task_help("點樣建立任務"))


if __name__ == "__main__":
    unittest.main()
