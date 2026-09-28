"""Проверки безопасности вебхука и базового сценария без вызова сети."""

import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ["ALICE_SKILL_ID"] = "test-skill-id"
os.environ["ALICE_WEBHOOK_TOKEN"] = "test-only-token"
os.environ["OPENAI_API_KEY"] = "test-only-key"

from api import index  # noqa: E402


class WebhookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), index.handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def send(self, token, skill_id, command="Привет"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps({"session": {"skill_id": skill_id}, "request": {"command": command}})
        conn.request("POST", f"/api?token={token}", body, {"Content-Type": "application/json"})
        result = conn.getresponse()
        status = result.status
        data = json.loads(result.read())
        conn.close()
        return status, data

    def test_wrong_token_is_rejected(self):
        status, _ = self.send("wrong-token", "test-skill-id")
        self.assertEqual(status, 404)

    def test_wrong_skill_is_rejected(self):
        status, _ = self.send("test-only-token", "other-skill")
        self.assertEqual(status, 403)

    def test_valid_request_returns_alice_format(self):
        fake = {"id": "resp_example", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Здравствуйте!"}]}]}
        with patch.object(index, "_api_request", return_value=fake):
            status, data = self.send("test-only-token", "test-skill-id")
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], "1.0")
        self.assertEqual(data["response"]["text"], "Здравствуйте!")
        self.assertEqual(data["session_state"]["previous_response_id"], "resp_example")

    def test_search_is_explicit(self):
        self.assertTrue(index._needs_web_search("поищи в интернете новости"))
        self.assertFalse(index._needs_web_search("объясни фотосинтез"))


if __name__ == "__main__":
    unittest.main()

