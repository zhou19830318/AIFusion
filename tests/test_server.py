import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_server import server
from local_server.history_store import HistoryStore, default_data_dir


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data if data is not None else {"choices": [{"message": {"content": "ok"}}]}

    def json(self):
        return self._data


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.app = server.create_app()
        self.client = self.app.test_client()

    def test_health_does_not_expose_keys(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertNotIn("api_key", json.dumps(response.json))

    def test_history_defaults_to_project_directory(self):
        old = os.environ.pop("AIFUSION_DATA_DIR", None)
        try:
            self.assertEqual(default_data_dir(), Path(server.__file__).resolve().parent.parent / ".aifusion")
        finally:
            if old is not None:
                os.environ["AIFUSION_DATA_DIR"] = old

    def test_chat_rejects_invalid_payload(self):
        response = self.client.post("/api/chat", json={"messages": []})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "invalid_request")

    def test_chat_rejects_oversized_model_context_before_provider_call(self):
        huge = "x" * (server._MAX_MESSAGE_CHARS + 1)
        response = self.client.post("/api/chat", json={"messages": [{"role": "user", "content": huge}]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "invalid_request")

    def test_default_prompt_contains_sketch_text_signature(self):
        self.assertIn("SketchTexts.createInput(text, position)", server.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("adsk.core.Point3D", server.DEFAULT_SYSTEM_PROMPT)

    def test_chat_requires_key(self):
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)):
            response = self.client.post("/api/chat", json={"messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "missing_api_key")

    def test_provider_retry_then_success(self):
        calls = []

        def post(*args, **kwargs):
            calls.append(1)
            return FakeResponse(503 if len(calls) < 3 else 200)

        with patch.object(server.requests, "post", side_effect=post), patch.object(server.time, "sleep"):
            status, data = server._post_json_with_retry("https://example.test/chat", json_body={}, headers={})
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 3)
        self.assertIn("choices", data)

    def test_config_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = server._CONFIG_PATH
            try:
                server._CONFIG_PATH = Path(directory) / "config.json"
                server.save_config({"provider": "deepseek"})
                self.assertEqual(json.loads(server._CONFIG_PATH.read_text())["provider"], "deepseek")
                self.assertFalse(list(Path(directory).glob("*.tmp")))
            finally:
                server._CONFIG_PATH = old_path

    def test_masked_api_key_cannot_overwrite_real_key_when_switching_model(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = server._CONFIG_PATH
            try:
                server._CONFIG_PATH = Path(directory) / "config.json"
                cfg = dict(server.DEFAULT_CONFIG)
                cfg.update({"provider": "openai", "model": "gpt-5.6-sol", "openai_api_key": "sk-real-secret"})
                server.save_config(cfg)
                app = server.create_app()
                client = app.test_client()
                displayed = client.get("/api/config").json
                self.assertEqual(displayed["openai_api_key"], "***cret")
                response = client.post("/api/config", json={
                    "provider": "openai",
                    "model": "gpt-5.6-terra",
                    "openai_api_key": displayed["openai_api_key"],
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(server.load_config()["openai_api_key"], "sk-real-secret")
                self.assertEqual(server.load_config()["model"], "gpt-5.6-terra")
            finally:
                server._CONFIG_PATH = old_path

    def test_history_store_rehydrates_snapshot_and_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            project = store.create_project("Demo")
            session = store.create_session(project["id"], "Main")
            store.append_event(session["id"], "user_message", {"text": "box"}, {"conversation": [{"role": "user", "content": "box"}]})
            store.append_event(session["id"], "tool_result", {"tool": "execute", "ok": False}, {"conversation": [{"role": "user", "content": "box"}]})
            restored = store.get_session(session["id"])
            self.assertEqual(len(restored["events"]), 2)
            self.assertEqual(restored["state"]["conversation"][0]["content"], "box")

    def test_history_api_redacts_data_url(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "HistoryStore", return_value=HistoryStore(Path(directory) / "h.sqlite3")):
            app = server.create_app()
            client = app.test_client()
            project = client.post("/api/projects", json={"name": "Private"}).json["project"]
            session = client.post(f"/api/projects/{project['id']}/sessions", json={}).json["session"]
            response = client.post(f"/api/sessions/{session['id']}/events", json={
                "event_type": "user_message",
                "payload": {"api_key": "secret"},
                "state": {"conversation": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 400}}]}]}
            })
            self.assertEqual(response.status_code, 200)
            restored = client.get(f"/api/sessions/{session['id']}").json["session"]
            text = json.dumps(restored, ensure_ascii=False)
            self.assertNotIn("data:image/png", text)
            self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main()
