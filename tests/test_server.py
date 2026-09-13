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
        # 测试不应往仓库的 aifusion_debug.log 里写厂商调用行:重定向到临时目录
        self._log_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._log_dir.cleanup)
        log_patcher = patch.object(
            server, "_DEBUG_LOG_PATH", Path(self._log_dir.name) / "aifusion_debug.log",
        )
        log_patcher.start()
        self.addCleanup(log_patcher.stop)

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

    def test_default_prompt_is_generic_not_task_specific(self):
        # 回归防护:默认提示词只能包含通用设计规则,禁止把某次任务的具体上下文
        # (如 Luckfox 外壳任务中 "USB 最近的孔是 BOOT" 的位置结论)固化进去。
        for leaked in ("current enclosure task", "luckfox", "Luckfox", "nearest USB is BOOT"):
            self.assertNotIn(leaked, server.DEFAULT_SYSTEM_PROMPT)
        # 通用的端口识别规则应保留
        self.assertIn("Port identification rule", server.DEFAULT_SYSTEM_PROMPT)

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

    # ── 清空必须真正删库:旧实现只清面板,快照还在,重开面板就复活 ──
    def test_clear_session_history_removes_events_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            project = store.create_project("Demo")
            session = store.create_session(project["id"], "Main")
            store.append_event(session["id"], "user_message", {}, {"conversation": [{"role": "user", "content": "外壳"}]})
            store.append_event(session["id"], "tool_result", {}, {"conversation": [{"role": "user", "content": "外壳"}]})
            self.assertEqual(len(store.get_session(session["id"])["events"]), 2)

            removed = store.clear_session_history(session["id"])

            self.assertEqual(removed, 2)
            cleared = store.get_session(session["id"])
            # 会话本身保留(标题/归属不变),历史全清
            self.assertEqual(cleared["id"], session["id"])
            self.assertEqual(cleared["events"], [])
            self.assertEqual(cleared["state"]["conversation"], [])

    def test_clear_session_history_unknown_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            with self.assertRaises(KeyError):
                store.clear_session_history("nope")

    def test_clear_history_endpoint_does_not_resurrect_on_restore(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "HistoryStore", return_value=HistoryStore(Path(directory) / "h.sqlite3")):
            client = server.create_app().test_client()
            project = client.post("/api/projects", json={"name": "P"}).json["project"]
            session = client.post(f"/api/projects/{project['id']}/sessions", json={}).json["session"]
            client.post(f"/api/sessions/{session['id']}/events", json={
                "event_type": "user_message", "payload": {},
                "state": {"conversation": [{"role": "user", "content": "长会话"}]}})
            self.assertEqual(len(client.get(f"/api/sessions/{session['id']}").json["session"]["state"]["conversation"]), 1)

            response = client.delete(f"/api/sessions/{session['id']}/history")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["removed_events"], 1)
            # 重开面板走的就是这个 GET:必须拿到空对话
            restored = client.get(f"/api/sessions/{session['id']}").json["session"]
            self.assertEqual(restored["state"]["conversation"], [])
            self.assertEqual(restored["events"], [])

    def test_clear_history_endpoint_404_for_unknown_session(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "HistoryStore", return_value=HistoryStore(Path(directory) / "h.sqlite3")):
            client = server.create_app().test_client()
            self.assertEqual(client.delete("/api/sessions/missing/history").status_code, 404)

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
        # 清空按钮走 DELETE,必须出现在 CORS 允许方法里
        self.assertIn("DELETE", response.headers.get("Access-Control-Allow-Methods", ""))

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

    # ── DeepSeek multimodal (deepseek-flash, 2026-09) ──────────────────

    def test_deepseek_flash_registry_and_aliases(self):
        prov = server.PROVIDERS["deepseek"]
        self.assertIn("deepseek-flash", prov["models"])
        self.assertEqual(prov["vision_models"], ["deepseek-flash"])
        # 旧模型名已下线,应映射到 deepseek-flash(与官方路由行为一致)
        for legacy in ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
            self.assertEqual(server._resolve_model("deepseek", legacy), "deepseek-flash")
        self.assertTrue(server._model_has_vision(prov, "deepseek-flash"))
        self.assertFalse(server._model_has_vision(prov, "deepseek-v4-pro"))

    def test_models_endpoint_reports_deepseek_flash_vision(self):
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)):
            response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 200)
        ds = response.json["providers"]["deepseek"]
        self.assertIn("deepseek-flash", ds["models"])
        self.assertFalse(ds["vision_all"])
        self.assertEqual(ds["vision_models"], ["deepseek-flash"])

    def test_deepseek_flash_keeps_images_but_text_only_model_strips(self):
        image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}
        captured: dict = {}

        def fake_call(**kwargs):
            captured["messages"] = kwargs["messages"]
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        payload = {
            "provider": "deepseek",
            "api_key": "sk-test",
            "messages": [{"role": "user", "content": [
                image_part,
                {"type": "text", "text": "看看这个设计"},
            ]}],
        }

        def user_content():
            return next(m["content"] for m in captured["messages"] if m["role"] == "user")

        # deepseek-flash(原生多模态): image_url 块应原样透传给 OpenAI 兼容端点
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={**payload, "model": "deepseek-flash"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(image_part, user_content())

        # deepseek-v4-pro(纯文本): 图片应被替换为文字提示,而不是 400
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={**payload, "model": "deepseek-v4-pro"})
        self.assertEqual(response.status_code, 200)
        sent = user_content()
        self.assertNotIn(image_part, sent)
        self.assertTrue(any(
            isinstance(p, dict) and p.get("type") == "text" and "已自动忽略" in p.get("text", "")
            for p in sent
        ))

    # ── Zhipu GLM multimodal (glm-5.3-flash, 2026-08) ──────────────────

    def test_glm_flash_registry_and_vision_gating(self):
        prov = server.PROVIDERS["zhipu"]
        self.assertIn("glm-5.3-flash", prov["models"])
        self.assertIn("glm-5.3", prov["models"])
        self.assertEqual(prov["vision_models"], ["glm-5.3-flash"])
        self.assertTrue(server._model_has_vision(prov, "glm-5.3-flash"))
        self.assertFalse(server._model_has_vision(prov, "glm-5.3"))
        # 别名解析:glm5.3-flash / glm5.3 → 规范名
        self.assertEqual(server._resolve_model("zhipu", "glm5.3-flash"), "glm-5.3-flash")
        self.assertEqual(server._resolve_model("zhipu", "glm5.3"), "glm-5.3")

    def test_models_endpoint_reports_glm_flash_vision(self):
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)):
            response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 200)
        zp = response.json["providers"]["zhipu"]
        self.assertIn("glm-5.3-flash", zp["models"])
        self.assertFalse(zp["vision_all"])
        self.assertEqual(zp["vision_models"], ["glm-5.3-flash"])

    def test_glm_flash_keeps_images_but_glm_5_3_strips(self):
        image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}
        captured: dict = {}

        def fake_call(**kwargs):
            captured["messages"] = kwargs["messages"]
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        payload = {
            "provider": "zhipu",
            "api_key": "test",
            "messages": [{"role": "user", "content": [
                image_part,
                {"type": "text", "text": "看看这个设计"},
            ]}],
        }

        def user_content():
            return next(m["content"] for m in captured["messages"] if m["role"] == "user")

        # glm-5.3-flash(原生多模态): image_url 块应原样透传给 OpenAI 兼容端点
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={**payload, "model": "glm-5.3-flash"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(image_part, user_content())

        # glm-5.3(纯文本旗舰): 图片应被替换为文字提示,而不是 400
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={**payload, "model": "glm-5.3"})
        self.assertEqual(response.status_code, 200)
        sent = user_content()
        self.assertNotIn(image_part, sent)
        self.assertTrue(any(
            isinstance(p, dict) and p.get("type") == "text" and "已自动忽略" in p.get("text", "")
            for p in sent
        ))

    # ── Long-session message-window trimming (no more hard 400) ──────────

    def test_oversized_conversation_trimmed_with_pairs_intact(self):
        # 复现真实故障:长设计会话(50 轮工具调用 = 100+ 条消息)曾因 80 条上限
        # 被硬拒绝,且历史只增不减导致会话永久卡死。现在应优雅截断并保持配对。
        messages = [{"role": "user", "content": "design an enclosure for luckfox pico zero"}]
        for i in range(50):
            messages.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{\"queryType\":\"bodies\"}"}},
            ]})
            messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"bodies: {i}"})
        messages.append({"role": "user", "content": "请检查 USB、boot、reset 按钮开孔尺寸是否正确"})
        captured: dict = {}

        def fake_call(**kwargs):
            captured["messages"] = kwargs["messages"]
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
             patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            response = self.client.post("/api/chat", json={
                "provider": "deepseek", "api_key": "sk-test", "messages": messages,
            })
        self.assertEqual(response.status_code, 200)  # 修复前这里返回 400
        sent = captured["messages"]
        non_system = [m for m in sent if m["role"] != "system"]
        self.assertLessEqual(len(non_system), server._MAX_MESSAGES + 1)
        # tool_call 与 tool 响应必须成对出现,不允许孤儿(厂商会 400)
        call_ids = {tc["id"] for m in sent if m["role"] == "assistant" for tc in (m.get("tool_calls") or [])}
        resp_ids = {m["tool_call_id"] for m in sent if m["role"] == "tool"}
        self.assertEqual(call_ids, resp_ids)
        self.assertGreater(len(call_ids), 0)
        # 最初的任务描述与最新请求都要保留
        self.assertTrue(any(m["role"] == "user" and "enclosure" in str(m.get("content")) for m in sent))
        self.assertTrue(any(m["role"] == "user" and "USB" in str(m.get("content")) for m in sent))
        self.assertEqual(sent[-1]["role"], "user")

    def test_trim_messages_to_window_unit(self):
        msgs = [{"role": "user", "content": "task statement"}]
        for i in range(50):
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "execute", "arguments": "{}"}},
            ]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"})
        trimmed = server._trim_messages_to_window(msgs, 20, 10 ** 9)
        self.assertLessEqual(len(trimmed), 21)  # 最初用户请求允许额外保留 1 条
        call_ids = {tc["id"] for m in trimmed if m["role"] == "assistant" for tc in (m.get("tool_calls") or [])}
        resp_ids = {m["tool_call_id"] for m in trimmed if m["role"] == "tool"}
        self.assertEqual(call_ids, resp_ids)
        self.assertTrue(any(m["role"] == "user" and m["content"] == "task statement" for m in trimmed))

    def test_single_oversized_message_still_rejected(self):
        # 截断救不了单条超限消息,必须继续报错而不是把巨石消息发给厂商
        huge = "x" * (server._MAX_MESSAGE_CHARS + 1)
        response = self.client.post("/api/chat", json={"messages": [{"role": "user", "content": huge}]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "invalid_request")

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
        # Kimi/Gemini/GLM/DeepSeek document only image_url *image* parts (no
        # inline PDFs); an inline PDF must be replaced by a notice instead of
        # being sent as an image_url.
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


class ChatUiVisionLoopTests(unittest.TestCase):
    """视觉校验闭环的静态契约:截图检测必须看信封内的 payload。

    线上事故:read(queryType="screenshot") 确实返回了 PNG,但检测函数只看顶层
    result.type,而工具结果被 normalizeToolResult() 包成 {ok, tool, payload},
    于是 shot 永远为 false —— 截图从未注入对话,模型"自检"从未发生。
    """

    @classmethod
    def setUpClass(cls):
        cls.ui = (Path(__file__).resolve().parent.parent
                  / "local_server" / "chat_ui.html").read_text(encoding="utf-8")

    def test_screenshot_detection_reads_payload_envelope(self):
        self.assertIn("function screenshotPayload(r)", self.ui)
        self.assertIn("r.payload", self.ui)
        # 旧的无条件顶层判定已移除,否则信封形状永远不可能命中
        self.assertNotIn("function isScreenshotResult(r)", self.ui)
        self.assertNotIn("const shot = isScreenshotResult(result)", self.ui)

    def test_vision_injection_uses_unwrapped_image_bytes(self):
        # 注入的 data URL 必须取自 payload 内的真实数据,而不是信封(会是 undefined)
        self.assertIn("shotImage.mimeType", self.ui)
        self.assertIn("shotImage.data", self.ui)
        self.assertNotIn("base64,${result.data}", self.ui)

    def test_repeated_screenshots_not_blocked_by_dedupe(self):
        # 修复后必须能再次截图比对(最多 3 轮),不能被同请求去重误判为重复调用
        self.assertIn('const repeatable = toolName === "read" && args.queryType === "screenshot";', self.ui)
        self.assertIn("if (!repeatable && executedCalls.has(callFingerprint))", self.ui)
        self.assertIn("if (!repeatable) executedCalls.add(callFingerprint);", self.ui)


class ChatUiThinkingProcessTests(unittest.TestCase):
    """思考过程可视化的静态契约。

    线上现象:模型每轮都在"边想边做"(实测 53 条带工具调用的 assistant 消息里
    41 条带叙述文字),但 UI 只渲染收尾结论,叙述与 reasoning_content 被静默
    丢弃 —— 面板上只剩工具卡片,用户看不到任何推理过程。
    """

    @classmethod
    def setUpClass(cls):
        cls.ui = (Path(__file__).resolve().parent.parent
                  / "local_server" / "chat_ui.html").read_text(encoding="utf-8")

    def test_narration_attached_to_tool_calls_is_rendered(self):
        self.assertIn("function addNarrationEl(text)", self.ui)
        self.assertIn("addNarrationEl(msg.content)", self.ui)

    def test_reasoning_content_is_surfaced_and_collapsible(self):
        self.assertIn("msg.reasoning_content", self.ui)
        self.assertIn("function addReasoningEl(text)", self.ui)
        self.assertIn('details.className = "reasoning"', self.ui)

    def test_internal_keys_never_sent_to_provider(self):
        # _vision/_reasoning 是客户端内部标记,发给厂商属于协议污染
        self.assertIn('if (!k.startsWith("_"))', self.ui)

    def test_session_restore_replays_reasoning_and_tool_calls(self):
        # 重开会话时必须连思考过程与工具卡片一起恢复,否则只剩结论
        self.assertIn('typeof msg._reasoning === "string"', self.ui)
        self.assertIn("toolResults.set(m.tool_call_id, m)", self.ui)
        self.assertIn("addToolCallEl(tc.function?.name || \"tool\"", self.ui)


class ChatUiClearButtonTests(unittest.TestCase):
    """清空按钮的静态契约:必须落库,且不能谎报成功。"""

    @classmethod
    def setUpClass(cls):
        cls.ui = (Path(__file__).resolve().parent.parent
                  / "local_server" / "chat_ui.html").read_text(encoding="utf-8")

    def test_clear_button_calls_delete_history_endpoint(self):
        self.assertIn("/api/sessions/${historySession.id}/history", self.ui)
        self.assertIn('apiCall("DELETE"', self.ui)

    def test_clear_button_confirms_and_surfaces_failure(self):
        self.assertIn("confirm(", self.ui)
        # 删除失败时必须报错返回,不能把面板清掉假装成功
        self.assertIn("清空失败", self.ui)

    def test_clear_uses_shared_selector_including_new_block_types(self):
        # 思路块/深度思考块必须和后两类元素一起被移除,否则面板会残留孤儿节点
        self.assertIn("function clearConversationView()", self.ui)
        self.assertIn(".narration, details.reasoning", self.ui)
        self.assertNotIn('.msg, .tool-call, .thinking-line")', self.ui)


class AddInReloadContractTests(unittest.TestCase):
    """add-in 重启必须真正重新加载 local_server,否则新路由一直 404。

    实测:server.py 新增 DELETE /api/sessions/<id>/history 后,日志显示
    15:53:52 add-in 重启成功并绑定 8765,但对新路由仍返回 HTML 404 ——
    Flask 跑在 Fusion 自己的 Python 进程里,local_server.server 一直留在
    sys.modules,重启插件只是重新调 create_app() 而已。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent.parent / "AIFusion.py").read_text(encoding="utf-8")

    def test_cached_modules_are_evicted(self):
        self.assertIn("def _reload_local_modules()", self.src)
        self.assertIn("sys.modules.pop(name, None)", self.src)
        self.assertIn("importlib.invalidate_caches()", self.src)

    def test_eviction_runs_before_the_import(self):
        # 调用行行尾无注释,才能与函数定义行 "_reload_local_modules() -> None:" 区分开
        call_at = self.src.index("_reload_local_modules()\n")
        import_at = self.src.index("from local_server.server import create_app")
        self.assertLess(call_at, import_at)


class TruncatedResponseTests(unittest.TestCase):
    """推理吃满输出上限 → HTTP 200 但消息为空。

    实测(deepseek-flash, max_tokens=32):finish_reason=length, content='',
    tool_calls=None, reasoning_content 非空。客户端旧代码只抛一句
    "Empty response" —— 既没有原因也没有退路,这正是 2026-09-13 那次
    "检查筋板尺寸" 卡死 8 分钟后失败的真相。
    """

    TRUNCATED = {
        "id": "x", "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{
            "index": 0, "finish_reason": "length",
            "message": {"role": "assistant", "content": "", "tool_calls": None,
                        "reasoning_content": "thinking about the rib..."},
        }],
        "usage": {"prompt_tokens": 42807, "completion_tokens": 16184,
                  "completion_tokens_details": {"reasoning_tokens": 15886}},
    }

    def setUp(self):
        self.app = server.create_app()
        self.client = self.app.test_client()
        self.ui = (Path(__file__).resolve().parent.parent
                   / "local_server" / "chat_ui.html").read_text(encoding="utf-8")

    # ── 服务端契约 ──────────────────────────────────────────────────

    def test_truncated_response_passes_through_finish_reason(self):
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
                patch.object(server, "_openai_compatible_call", return_value=(200, self.TRUNCATED)):
            response = self.client.post("/api/chat", json={
                "messages": [{"role": "user", "content": "hi"}], "api_key": "sk-test",
            })
        self.assertEqual(response.status_code, 200)
        choice = response.json["choices"][0]
        # finish_reason 必须原样到达客户端,否则"被截断"和"模型坏了"无法区分
        self.assertEqual(choice["finish_reason"], "length")
        self.assertEqual(choice["message"]["content"], "")
        self.assertIsNone(choice["message"]["tool_calls"])
        self.assertTrue(choice["message"]["reasoning_content"])

    def test_summarize_marks_truncated_and_empty_responses(self):
        line = server._summarize_provider_response(200, self.TRUNCATED)
        self.assertIn("TRUNCATED", line)
        self.assertIn("EMPTY_MESSAGE", line)
        self.assertIn("finish_reason=length", line)
        self.assertIn("reasoning_tokens=15886", line)

        normal = server._summarize_provider_response(200, {
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None, "tool_calls": [{"id": "c1"}], "reasoning_content": "r"}}],
        })
        self.assertNotIn("TRUNCATED", normal)
        self.assertNotIn("EMPTY_MESSAGE", normal)
        self.assertIn("tool_calls=1", normal)

    def test_provider_calls_land_in_the_debug_log(self):
        # 旧日志只有启动/关闭行,厂商调用完全不可复盘 —— 这正是"检查日志"查不出东西的原因。
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "aifusion_debug.log"
            with patch.object(server, "_DEBUG_LOG_PATH", log_path), \
                    patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
                    patch.object(server, "_openai_compatible_call", return_value=(200, self.TRUNCATED)):
                response = self.client.post("/api/chat", json={
                    "messages": [{"role": "user", "content": "hi"}], "api_key": "sk-test",
                })
            self.assertEqual(response.status_code, 200)
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("[provider]", text)
            self.assertIn("provider=deepseek", text)
            self.assertIn("finish_reason=length", text)
            self.assertIn("TRUNCATED", text)

    def test_debug_log_rotation_keeps_the_tail(self):
        # 关键:日志不能因为过大就被整个清空 —— 用户恰恰是在失败**之后**才来看它
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "aifusion_debug.log"
            log_path.write_text("A" * (server._DEBUG_LOG_MAX_BYTES + 1000), encoding="utf-8")
            with patch.object(server, "_DEBUG_LOG_PATH", log_path):
                server._debug_log("finish_reason=length TRUNCATED")
            text = log_path.read_text(encoding="utf-8")
        self.assertIn("ROTATED", text)
        self.assertIn("TRUNCATED", text)
        self.assertLess(len(text), server._DEBUG_LOG_MAX_BYTES)
        self.assertGreater(len(text), server._DEBUG_LOG_KEEP_BYTES - 1000)

    def test_read_timeout_is_configurable_and_bounded(self):
        self.assertEqual(server._resolve_read_timeout(None), server._READ_TIMEOUT)
        self.assertEqual(server._resolve_read_timeout("600"), 600)
        self.assertEqual(server._resolve_read_timeout(5), server._READ_TIMEOUT)
        self.assertEqual(server._resolve_read_timeout(99999), server._READ_TIMEOUT)
        self.assertEqual(server._resolve_read_timeout("junk"), server._READ_TIMEOUT)
        # 思考型模型(deepseek-flash 默认思考模式,推理与正文共享 64K 输出上限)长推理会远超 180s
        self.assertGreaterEqual(server._READ_TIMEOUT, 300)
        self.assertIn("provider_read_timeout", server.DEFAULT_CONFIG)

    def test_timeouts_are_not_retried_into_a_dead_hang(self):
        calls = []

        def post(*args, **kwargs):
            calls.append(1)
            raise server.requests.exceptions.Timeout("too slow")

        with patch.object(server.requests, "post", side_effect=post), patch.object(server.time, "sleep"):
            with self.assertRaises(server.requests.exceptions.Timeout):
                server._post_json_with_retry("https://example.test/chat", json_body={}, headers={})
        # 旧行为:同一请求最多 3 次尝试 × 180s ≈ 9 分钟假死(实测 8m24s 后才失败)。
        # 超时只可能是"生成太慢",重试不会更快出结果,因此只发一次。
        self.assertEqual(len(calls), 1)

    def test_reasoning_effort_reaches_capable_provider_only(self):
        captured: dict = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        base = {"messages": [{"role": "user", "content": "hi"}], "api_key": "sk-test"}
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)), \
                patch.object(server, "_openai_compatible_call", side_effect=fake_call):
            self.assertEqual(self.client.post(
                "/api/chat", json={**base, "provider": "deepseek", "reasoning_effort": "low"},
            ).status_code, 200)
            self.assertEqual(captured["reasoning_effort"], "low")

            captured.clear()
            # 未声明支持的厂商:直接忽略该字段,而不是 400(否则恢复路径会把会话弄坏)
            self.assertEqual(self.client.post(
                "/api/chat", json={**base, "provider": "kimi", "reasoning_effort": "low"},
            ).status_code, 200)
            self.assertIsNone(captured["reasoning_effort"])

        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)):
            bad = self.client.post("/api/chat", json={**base, "provider": "deepseek", "reasoning_effort": "extreme"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json["error"]["code"], "invalid_reasoning_effort")

    def test_openai_body_carries_reasoning_effort_only_when_requested(self):
        with patch.object(server, "_post_json_with_retry", return_value=(200, {})) as post_mock:
            server._openai_compatible_call(
                base_url="https://api.example.test", api_key="k", model="m",
                messages=[{"role": "user", "content": "hi"}],
                tools=None, tool_choice=None, reasoning_effort="none",
            )
            self.assertEqual(post_mock.call_args.kwargs["json_body"]["reasoning_effort"], "none")

            server._openai_compatible_call(
                base_url="https://api.example.test", api_key="k", model="m",
                messages=[{"role": "user", "content": "hi"}],
                tools=None, tool_choice=None,
            )
            self.assertNotIn("reasoning_effort", post_mock.call_args.kwargs["json_body"])

    def test_models_endpoint_advertises_reasoning_effort_support(self):
        with patch.object(server, "load_config", return_value=dict(server.DEFAULT_CONFIG)):
            providers = self.client.get("/api/models").json["providers"]
        self.assertTrue(providers["deepseek"]["supports_reasoning_effort"])
        self.assertFalse(providers["kimi"]["supports_reasoning_effort"])
        # GLM-5.3-Flash 的 thinking.type 仅支持 enabled(思考不可关闭),截断降档到 none 会 400,
        # 因此不声明支持 —— 客户端会直接忽略该字段。
        self.assertFalse(providers["zhipu"]["supports_reasoning_effort"])

    # ── 客户端契约 ──────────────────────────────────────────────────

    def test_client_detects_finish_reason_length(self):
        self.assertIn('choice.finish_reason === "length"', self.ui)
        self.assertIn("MAX_TRUNCATION_RECOVERIES", self.ui)
        self.assertIn("reasoningEffortOverride", self.ui)
        self.assertIn("body.reasoning_effort = reasoningEffortOverride", self.ui)

    def test_client_error_is_actionable_not_bare_empty_response(self):
        self.assertNotIn('throw new Error("Empty response")', self.ui)
        self.assertIn("模型输出被截断（finish_reason=length", self.ui)
        self.assertIn("模型返回了空消息", self.ui)

    def test_reasoning_is_shown_before_the_truncation_branch(self):
        # 截断轮次也必须先把推理渲染出来,否则面板全程静止然后弹一个错
        render_at = self.ui.index("if (reasoning) addReasoningEl(reasoning)")
        throw_at = self.ui.index("模型输出被截断（finish_reason=length")
        self.assertLess(render_at, throw_at)

    def test_truncation_retry_is_recorded_in_history(self):
        self.assertIn('saveHistoryEvent("truncation_retry"', self.ui)


if __name__ == "__main__":
    unittest.main()
