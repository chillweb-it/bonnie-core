import unittest
from types import SimpleNamespace
from slack_bridge import BOT_ID, own_dm_history, relay, bridge_reply

EVA = "U07GH6ZN8RW"
EVAN = "U07AGT63JGY"


class Client:
    def __init__(self):
        self.sent = []
        self.identity = BOT_ID
        self.fail = False
        self.history = []

    def auth_test(self):
        return {"user_id": self.identity}

    def chat_postMessage(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail:
            raise TimeoutError()
        return {"ok": True, "channel": "D_target", "ts": "10.1"}

    def conversations_history(self, **kwargs):
        return {"messages": self.history}


class Receipts:
    def __init__(self):
        self.states = {}

    def claim(self, key, recipient):
        if key in self.states:
            return self.states[key]
        self.states[key] = "pending"
        return "new"

    def finish(self, key, channel, ts):
        self.states[key] = "sent"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.receipts = Receipts()

    def send(self, caller=EVA, recipient=EVAN):
        return relay(self.client, self.receipts, caller, "D_source", "9.1",
                     {"recipient": recipient, "message": "今日一齊食 Lunch？時間地點未定。"})

    def test_invitation_is_attributed_and_pending(self):
        reply = self.send()
        self.assertIn("已轉達", reply)
        self.assertIn("尚未確認", reply)
        self.assertEqual(self.client.sent[0]["channel"], EVAN)
        self.assertIn("Eva Lau 請我轉達", self.client.sent[0]["text"])
        self.assertFalse(self.client.sent[0]["mrkdwn"])

    def test_evan_can_relay_reply_to_eva(self):
        self.send(EVAN, EVA)
        self.assertEqual(self.client.sent[0]["channel"], EVA)

    def test_retries_do_not_duplicate(self):
        self.send()
        self.assertIn("唔會重複", self.send())
        self.assertEqual(len(self.client.sent), 1)

    def test_uncertain_delivery_never_resends_or_claims_success(self):
        self.client.fail = True
        self.assertIn("未能確認", self.send())
        self.client.fail = False
        self.assertIn("唔會自動重發", self.send())
        self.assertEqual(len(self.client.sent), 1)

    def test_wrong_sender_and_unverified_people_cannot_send(self):
        self.client.identity = EVAN
        self.assertIn("身份未能核實", self.send())
        self.client.identity = BOT_ID
        for caller, target in [("U_other", EVAN), (EVA, "U_other"), (EVA, EVA)]:
            self.assertIn("未轉達", self.send(caller, target))
        self.assertEqual(self.client.sent, [])

    def test_history_excludes_other_users_and_edited_messages(self):
        self.client.history = [{"user": EVA, "text": "今日"},
            {"user": "U_other", "text": "private"},
            {"user": BOT_ID, "text": "幾時？"},
            {"user": EVA, "text": "幫我約 Evan 食 Lunch"}]
        history = own_dm_history(self.client, "D_source", "9.1", EVA)
        self.assertEqual([m["content"] for m in history], ["幫我約 Evan 食 Lunch", "幾時？", "今日"])
        self.assertEqual(own_dm_history(self.client, "C_public", "9.1", EVA), [])

    def test_model_tool_returns_actual_receipt_not_presend_text(self):
        response = SimpleNamespace(content=[SimpleNamespace(type="text", text="約好咗"),
            SimpleNamespace(type="tool_use", name="relay_message", input={"recipient": EVAN, "message": "食 Lunch？"})])
        claude = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))
        reply = bridge_reply(claude, "model", "system", [{"role": "user", "content": "幫我約 Evan"}],
                             self.client, self.receipts, EVA, "D_source", "9.1")
        self.assertNotIn("約好", reply)
        self.assertIn("尚未確認", reply)

    def test_multiple_tool_calls_send_nothing(self):
        call = SimpleNamespace(type="tool_use", name="relay_message", input={"recipient": EVAN, "message": "Lunch?"})
        claude = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(content=[call, call])))
        reply = bridge_reply(claude, "model", "system", [], self.client, self.receipts, EVA, "D_source", "9.1")
        self.assertIn("未轉達", reply)
        self.assertEqual(self.client.sent, [])


if __name__ == "__main__":
    unittest.main()
