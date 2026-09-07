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
        # Neutralize both config and environment key sources so the request
        # deterministically fails with missing_api_key even if the developer
        # machine exports provider keys (e.g. DEEPSEEK_API_KEY).
        empty_env = {p["config_key"].upper(): "" for p in server.PROVIDERS.values()}
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
                patch.dict(os.environ, empty_env):
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

    # ── Fix 1: HistoryStore must close sqlite connections ──
    def test_history_store_closes_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = HistoryStore(path)
            project = store.create_project("A")
            session = store.create_session(project["id"], "S")
            store.append_event(session["id"], "e", {"x": 1}, {"conversation": []})
            store.get_session(session["id"])
            store.list_projects()
            store.list_sessions(project["id"])
            # A leaked handle makes the file undeletable on Windows (WinError 32)
            path.unlink()
            self.assertFalse(path.exists())

    # ── Fix 2: Anthropic adapter must merge consecutive same-role messages ──
    def test_anthropic_adapter_merges_consecutive_tool_results(self):
        msgs = [
            {"role": "user", "content": "build a box"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "execute", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "body count: 1"},
            {"role": "tool", "tool_call_id": "c2", "content": "ok"},
            {"role": "user", "content": "continue"},
        ]
        converted, _system = server._anthropic_convert_messages(msgs)
        roles = [m["role"] for m in converted]
        self.assertEqual(roles, ["user", "assistant", "user"])
        last = converted[-1]["content"]
        tool_results = [b for b in last if b.get("type") == "tool_result"]
        self.assertEqual([b["tool_use_id"] for b in tool_results], ["c1", "c2"])

    def test_anthropic_adapter_merges_adjacent_user_turns(self):
        converted, _system = server._anthropic_convert_messages([
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ])
        self.assertEqual([m["role"] for m in converted], ["user"])
        self.assertIn("first", converted[0]["content"][0]["text"])
        self.assertIn("second", converted[0]["content"][1]["text"])

    # ── Fix 3: image base64 must not consume the text-context budget ──
    def test_large_image_not_counted_against_text_budget(self):
        b64 = "A" * server._MAX_MESSAGE_CHARS  # > 120K chars of base64
        messages, _tools, _tc = server._validate_chat_payload({
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": "hi"},
            ]}],
        })
        self.assertEqual(len(messages), 1)

    def test_oversized_image_rejected(self):
        b64 = "A" * (server._MAX_IMAGE_CHARS + 1)
        with self.assertRaises(ValueError):
            server._validate_chat_payload({"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}]})

    # ── Fix 4: CORS + base_url hardening ──
    def test_cors_allows_loopback_origin(self):
        response = self.client.get("/api/health", headers={"Origin": "http://127.0.0.1:8790"})
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:8790")

    def test_cors_rejects_null_origin(self):
        response = self.client.get("/api/health", headers={"Origin": "null"})
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_cors_blocks_remote_origin(self):
        response = self.client.get("/api/health", headers={"Origin": "https://evil.example.com"})
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_chat_rejects_arbitrary_base_url(self):
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)):
            response = self.client.post("/api/chat", json={
                "messages": [{"role": "user", "content": "hi"}],
                "api_key": "sk-test",
                "base_url": "https://evil.example.com",
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "base_url_not_allowed")

    def test_chat_allows_provider_host_base_url(self):
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", return_value=(200, {"choices": [{"message": {"content": "ok"}}]})):
            response = self.client.post("/api/chat", json={
                "messages": [{"role": "user", "content": "hi"}],
                "api_key": "sk-test",
                "base_url": "https://api.deepseek.com/v1",
            })
        self.assertEqual(response.status_code, 200)

    # ── Review follow-ups ──────────────────────────────────────────────

    def test_anthropic_max_tokens_maps_to_length_finish_reason(self):
        resp = server._anthropic_response_to_openai({
            "id": "msg_1", "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "def run(_context):  # cut off"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 10, "output_tokens": 8192},
        })
        self.assertEqual(resp["choices"][0]["finish_reason"], "length")

    def test_anthropic_end_turn_maps_to_stop_finish_reason(self):
        resp = server._anthropic_response_to_openai({
            "id": "msg_2", "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })
        self.assertEqual(resp["choices"][0]["finish_reason"], "stop")

    def test_chat_sends_low_temperature_by_default(self):
        captured = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={
                "messages": [{"role": "user", "content": "hi"}],
                "api_key": "sk-test",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured.get("temperature"), 0.2)

    def test_chat_temperature_config_override(self):
        cfg = dict(server.DEFAULT_CONFIG)
        cfg["temperature"] = "0.7"
        captured = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(server, "load_config", return_value=cfg), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={
                "messages": [{"role": "user", "content": "hi"}],
                "api_key": "sk-test",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured.get("temperature"), 0.7)

    def test_chat_temperature_none_disables_param(self):
        captured = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={
                "messages": [{"role": "user", "content": "hi"}],
                "api_key": "sk-test",
                "temperature": "none",
            })
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(captured.get("temperature"))

    def test_openai_body_omits_temperature_when_none(self):
        with patch.object(server, "_post_json_with_retry", return_value=(200, {})) as post_mock:
            server._openai_compatible_call(
                base_url="https://api.example.test", api_key="k", model="m",
                messages=[{"role": "user", "content": "hi"}],
                tools=None, tool_choice=None, temperature=None,
            )
        body = post_mock.call_args.kwargs["json_body"]
        self.assertNotIn("temperature", body)
        with patch.object(server, "_post_json_with_retry", return_value=(200, {})) as post_mock:
            server._openai_compatible_call(
                base_url="https://api.example.test", api_key="k", model="m",
                messages=[{"role": "user", "content": "hi"}],
                tools=None, tool_choice=None, temperature=0.2,
            )
        body = post_mock.call_args.kwargs["json_body"]
        self.assertEqual(body["temperature"], 0.2)

    def test_chat_rejects_runaway_execute_script(self):
        huge_script = "def run(_context):\n    pass\n" + "# " + "x" * (server._MAX_SCRIPT_CHARS + 10)
        response = self.client.post("/api/chat", json={
            "messages": [{
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "execute", "arguments": json.dumps({
                        "featureType": "script", "script": huge_script,
                    })},
                }],
            }],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "invalid_request")

    def test_chat_rejects_tool_message_without_tool_call_id(self):
        response = self.client.post("/api/chat", json={
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "tool", "content": "result without id"},
            ],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "invalid_request")

    def test_chat_merges_cad_guidance_into_existing_system_message(self):
        captured = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={
                "messages": [
                    {"role": "system", "content": "You are a UI placeholder."},
                    {"role": "user", "content": "hi"},
                ],
                "api_key": "sk-test",
            })
        self.assertEqual(response.status_code, 200)
        sent_messages = captured["messages"]
        system = [m for m in sent_messages if m["role"] == "system"]
        self.assertEqual(len(system), 1)
        self.assertIn("You are a UI placeholder.", system[0]["content"])
        self.assertIn("SketchTexts.createInput(text, position)", system[0]["content"])

    def test_anthropic_call_passes_temperature(self):
        with patch.object(server, "_post_json_with_retry", return_value=(200, {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}})) as post_mock:
            server._anthropic_call(
                base_url="https://api.anthropic.com", api_key="k", api_version="2023-06-01",
                model="claude-sonnet-5", messages=[{"role": "user", "content": "hi"}],
                tools=None, temperature=0.2,
            )
        body = post_mock.call_args.kwargs["json_body"]
        self.assertEqual(body["temperature"], 0.2)

    # ── Vendor-doc research: attachment handling per provider ──────────

    def test_anthropic_pdf_data_url_becomes_document_block(self):
        converted, _ = server._anthropic_convert_messages([
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:application/pdf;base64,JVBERi0="}},
                {"type": "text", "text": "summarize"},
            ]},
        ])
        blocks = converted[0]["content"]
        doc = [b for b in blocks if b.get("type") == "document"]
        self.assertEqual(len(doc), 1)
        self.assertEqual(doc[0]["source"]["media_type"], "application/pdf")
        self.assertEqual(doc[0]["source"]["data"], "JVBERi0=")

    def test_anthropic_image_media_types_normalized(self):
        for mime, expect in (("image/jpg", "image/jpeg"), ("image/png", "image/png"), ("image/webp", "image/webp")):
            converted, _ = server._anthropic_convert_messages([
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,QQ=="}},
                ]},
            ])
            img = converted[0]["content"][0]
            self.assertEqual(img["type"], "image")
            self.assertEqual(img["source"]["media_type"], expect)

    def test_anthropic_unsupported_media_type_becomes_notice(self):
        # e.g. image/bmp or image/heic: Anthropic documents only
        # jpeg/png/gif/webp — an unknown type must not 400 the whole request.
        converted, _ = server._anthropic_convert_messages([
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/bmp;base64,QQ=="}},
                {"type": "text", "text": "describe"},
            ]},
        ])
        blocks = converted[0]["content"]
        self.assertTrue(all(b.get("type") != "image" for b in blocks))
        self.assertIn("does not accept this file type", blocks[0]["text"])
        self.assertIn("describe", blocks[1]["text"])

    def test_openai_provider_gets_file_part_for_pdf(self):
        adapted = server._adapt_attachments_for_openai_provider("openai", [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:application/pdf;base64,JVBERi0="}, "_filename": "spec.pdf"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
                {"type": "text", "text": "review both"},
            ]},
        ])
        parts = adapted[0]["content"]
        self.assertEqual(parts[0]["type"], "file")
        self.assertEqual(parts[0]["file"]["file_data"], "data:application/pdf;base64,JVBERi0=")
        self.assertEqual(parts[0]["file"]["filename"], "spec.pdf")
        self.assertEqual(parts[1]["type"], "image_url")  # images untouched
        self.assertEqual(parts[2]["type"], "text")

    def test_kimi_and_gemini_pdf_becomes_notice_not_raw_base64(self):
        # Kimi/Gemini/GLM document only image_url parts; an inline PDF must be
        # replaced by a notice instead of being sent as an image_url.
        for pid in ("kimi", "google_gemini", "zhipu", "deepseek"):
            adapted = server._adapt_attachments_for_openai_provider(pid, [
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:application/pdf;base64,JVBERi0="}, "_filename": "doc.pdf"},
                ]},
            ])
            part = adapted[0]["content"][0]
            self.assertEqual(part["type"], "text", pid)
            self.assertIn("does not accept inline PDF", part["text"])
            self.assertNotIn("JVBERi0", part["text"])  # raw base64 must not leak into context

    def test_pdf_attachment_size_cap_larger_than_images(self):
        big_pdf = "data:application/pdf;base64," + "A" * (server._MAX_PDF_CHARS + 1)
        with self.assertRaises(ValueError):
            server._validate_chat_payload({"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": big_pdf}},
            ]}]})
        # just under the cap passes (mind the "data:application/pdf;base64," prefix)
        prefix = "data:application/pdf;base64,"
        ok_pdf = prefix + "A" * (server._MAX_PDF_CHARS - len(prefix) - 1)
        messages, _t, _tc = server._validate_chat_payload({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": ok_pdf}},
        ]}]})
        self.assertEqual(len(messages), 1)

    def test_chat_routes_pdf_through_openai_file_part_end_to_end(self):
        captured = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:application/pdf;base64,JVBERi0="}, "_filename": "a.pdf"},
                    {"type": "text", "text": "read this"},
                ]}],
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "api_key": "sk-test",
            })
        self.assertEqual(response.status_code, 200)
        user_msg = [m for m in captured["messages"] if m["role"] == "user"][0]
        sent = user_msg["content"]
        self.assertEqual(sent[0]["type"], "file")
        self.assertEqual(sent[0]["file"]["filename"], "a.pdf")


if __name__ == "__main__":
    unittest.main()
