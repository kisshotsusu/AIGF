from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path
from types import SimpleNamespace

from agent import HomeAgent
from home_modules.code_editor import CodeEditorModule
from self_upgrade import SelfUpgradeManager


class AnswerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_family_system_prompt_does_not_embed_raw_live_chat(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {
            "home": {"scene_file": "workspace/DOES_NOT_EXIST.md", "user_name": "主人"},
            "context_maintenance": {"summary_file": "workspace/DOES_NOT_EXIST.md"},
        }
        agent.workspace = SimpleNamespace(
            recent_memories=lambda _limit: [],
            prompt_documents=lambda _scene: "家庭提示",
        )
        agent.task_store = SimpleNamespace(list=lambda: [], awaiting_acknowledgements=lambda: [])
        agent.list_skills = lambda: []
        prompt = agent._system_prompt()
        self.assertNotIn("近期直播对话", prompt)
        self.assertNotIn("live-chat", prompt)

    async def test_clear_live_context_is_a_model_selected_tool(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent._request_live_context_clear = Mock(return_value={"removed_messages": 3})
        result = await agent._run_tool("clear_live_context", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed_messages"], 3)
        agent._request_live_context_clear.assert_called_once_with()

    def test_registered_character_images_expose_canonical_paths(self) -> None:
        catalog = HomeAgent._character_image_catalog()
        three_view = next(item for item in catalog["images"] if item["filename"] == "角色三视图.png")
        self.assertTrue(Path(three_view["path"]).is_absolute())
        self.assertTrue(Path(three_view["path"]).is_file())
        self.assertEqual(Path(catalog["primary_path"]).name, "64db0915e4bf4f52a63a6e1b4da9bb58.png")

    def test_registered_character_image_resolves_aliases_without_cwd_guessing(self) -> None:
        expected = (Path(__file__).resolve().parents[2] / "workspace" / "character_images" / "角色三视图.png").resolve()
        for alias in ("角色三视图.png", "generated-three-view-20260717", "角色正侧背三视图", "角色三视图"):
            self.assertEqual(HomeAgent._resolve_character_image(alias), expected)
        self.assertEqual(HomeAgent._resolve_character_image("primary").suffix.lower(), ".png")

    async def test_analyze_registered_character_image_uses_resolved_absolute_path(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"computer_control": {"full_access": False, "allowed_roots": []}}
        agent.mimo_multimodal = SimpleNamespace(
            config={"timeout_seconds": 5},
            analyze_image=AsyncMock(return_value={"status": "success", "text": "三视图"}),
        )
        result = await agent._run_tool("analyze_image", {"image": "角色三视图.png", "prompt": "分析固定外观"})
        self.assertEqual(result["status"], "success")
        used_path = agent.mimo_multimodal.analyze_image.await_args.args[1]
        self.assertTrue(used_path.is_absolute())
        self.assertEqual(used_path.name, "角色三视图.png")

    def test_implementation_change_plan_cannot_become_live_screen_task(self) -> None:
        plan = {
            "implementation_change": True,
            "domain": "desktop",
            "operation": "observe_screen",
            "visual_required": True,
            "interaction_mode": "observe",
            "execution_strategy": "vision_loop",
            "requires_mcp": True,
            "site": "cloudmusic",
            "handler": "model_ui",
        }
        result = HomeAgent._apply_implementation_change_plan(plan)
        self.assertEqual(result["domain"], "code")
        self.assertEqual(result["execution_strategy"], "code_loop")
        self.assertFalse(result["visual_required"])
        self.assertFalse(result["requires_mcp"])
        self.assertEqual(result["site"], "")

    def test_window_activity_result_is_summary_not_raw_json(self) -> None:
        result = {
            "status": "success",
            "observation": [{
                "hwnd": "1182508",
                "pid": "49696",
                "bounds": [4739, 15, 6029, 585],
                "process_path": r"C:\Python312\pythonw.exe",
            }],
        }
        detail = HomeAgent._tool_activity_result("ui_list_windows", result)
        self.assertEqual(detail, "找到 1 个可用窗口")
        self.assertNotIn("hwnd", detail)
        self.assertNotIn("49696", detail)

    def test_screen_analysis_activity_does_not_echo_screen_content(self) -> None:
        result = {"status": "success", "observation": "屏幕中显示了用户的私人聊天内容"}
        detail = HomeAgent._tool_activity_result("ui_analyze_screen", result)
        self.assertEqual(detail, "画面识别完成，已获得状态摘要")
        self.assertNotIn("私人聊天", detail)

    async def test_scheduled_reminder_is_published_before_tts(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        task = {"id": "r1", "action": "tts", "message": "该喝水了", "reminder_attempts": 0}
        agent.task_store = SimpleNamespace(
            claim_due=lambda: [task],
            finish=lambda *_: {"status": "waiting_ack"},
        )
        agent.run_context_maintenance = AsyncMock(return_value=None)
        agent.log_event = Mock()
        order: list[str] = []

        async def speak(*_args, **_kwargs):
            order.append("tts")
            return ["reminder.wav"]

        agent._speak_home = speak
        await agent.run_due_tasks(lambda _message: order.append("message"))
        self.assertEqual(["message", "tts"], order)

    async def test_direct_restart_bypasses_model_planning(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.restart_requested = False
        agent.log_event = Mock()
        agent._plan_task = AsyncMock(side_effect=AssertionError("restart must not reach model planner"))
        published = []
        answer = await agent.chat("请现在重启你自己", answer_ready=published.append)
        self.assertTrue(agent.restart_requested)
        self.assertEqual(answer, "好的主人，Home Agent 正在重启。")
        self.assertEqual(published, [answer])
        agent._plan_task.assert_not_awaited()

    def test_task_finalization_preserves_direct_restart_flag(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.restart_requested = True
        agent.self_upgrade = SimpleNamespace(finalize=Mock(return_value=False), clear=Mock())
        agent.log_event = Mock()
        self.assertTrue(agent.finalize_task_recovery("正在重启"))
        self.assertTrue(agent.restart_requested)
        agent.self_upgrade.clear.assert_called_once()
        agent.self_upgrade.finalize.assert_not_called()

    async def test_answer_is_published_before_tts_finishes(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"home": {"max_context_messages": 4, "auto_speak": True}}
        agent.history = []
        root = Path(__file__).resolve().parents[2]
        agent.self_upgrade = SimpleNamespace(
            code_editor=CodeEditorModule(root, root / "HomeAgent"),
            set_self_upgrade=lambda enabled: None,
        )
        agent.log_event = lambda *args, **kwargs: None
        agent._acknowledge_common_response = lambda text: None

        async def plan(text: str, context: str = "") -> dict:
            return {"requires_clarification": True}

        events: list[str] = []

        async def speak(session, text: str, status=None, ignore_cancel: bool = False):
            events.append("tts_started")
            events.append("tts_finished")

        agent._plan_task = plan
        agent._speak_home = speak
        answer = await agent.chat("不明确的任务", answer_ready=lambda text: events.append("message_shown"))

        self.assertTrue(answer)
        self.assertEqual(["message_shown", "tts_started", "tts_finished"], events)

    async def test_agent_executes_local_code_tools_without_codex(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            editor = CodeEditorModule(root, home)
            editor.begin_tracking()
            agent = HomeAgent.__new__(HomeAgent)
            agent.self_upgrade = SimpleNamespace(code_editor=editor)
            agent.current_code_self_edit = False
            written = await agent._run_tool("code_write_file", {"path": "Projects/demo/main.py", "content": "VALUE = 1\n"})
            read = await agent._run_tool("code_read_file", {"path": "Projects/demo/main.py"})
            self.assertTrue(written["ok"])
            self.assertEqual("VALUE = 1\n", read["content"])

    async def test_codex_tool_receives_semantic_task_plan(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_task_plan = {
            "domain": "code", "implementation_change": True, "code_scope": "self",
        }
        agent._run_codex_task = AsyncMock(return_value={"ok": True})
        await agent._run_tool("codex_cli_task", {"task": "继续完成实现"})
        self.assertIs(agent._run_codex_task.await_args.kwargs["task_plan"], agent.current_task_plan)

    async def test_tts_failure_uses_windows_fallback_without_failing_chat(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.tts_execution_lock = __import__("threading").Lock()
        events = []
        agent.log_event = lambda name, **data: events.append((name, data))
        agent._speak_home_unlocked = AsyncMock(side_effect=asyncio.LimitOverrunError("separator missing", 100))
        with patch.object(agent, "_windows_sapi_speak", return_value=True):
            result = await agent._speak_home(None, "任务没有完成。")
        self.assertEqual(result, [])
        self.assertTrue(any(name == "home_tts_fallback" and data["ok"] for name, data in events))

    async def test_pseudo_tool_call_is_never_spoken(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.tts_execution_lock = __import__("threading").Lock()
        events = []
        agent.log_event = lambda name, **data: events.append((name, data))
        agent._speak_home_unlocked = AsyncMock()
        result = await agent._speak_home(None, "<tool_call><function=write_text_file><parameter=path>x.py")
        self.assertEqual(result, [])
        agent._speak_home_unlocked.assert_not_awaited()
        self.assertEqual(events[-1][0], "home_tts_skipped_unsafe_content")

    async def test_post_loop_speech_uses_open_fresh_session(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.project = {"tts": {"timeout_seconds": 5}}
        states = []
        async def speak(session, text, status=None, ignore_cancel=False):
            states.append((session.closed, text, ignore_cancel)); return []
        agent._speak_home = speak
        await agent._speak_with_fresh_session("任务未完成", ignore_cancel=True)
        self.assertEqual(states, [(False, "任务未完成", True)])

    async def test_fresh_failure_speech_uses_primary_tts_before_fallback(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.project = {"tts": {"timeout_seconds": 5}}
        agent.tts_execution_lock = __import__("threading").Lock()
        agent.log_event = Mock()
        agent._speak_home_unlocked = AsyncMock(return_value=["gpt-sovits.wav"])
        agent._windows_sapi_speak = Mock(side_effect=AssertionError("must not fallback when GPT-SoVITS succeeds"))
        result = await agent._speak_with_fresh_session("任务失败")
        self.assertEqual(result, ["gpt-sovits.wav"])
        agent._speak_home_unlocked.assert_awaited_once()
        agent._windows_sapi_speak.assert_not_called()

    async def test_general_text_writer_atomically_creates_external_script(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            agent = HomeAgent.__new__(HomeAgent)
            agent.config = {"computer_control": {"full_access": True, "confirm_before_action": False}}
            target = Path(folder) / "start_demo.bat"
            result = await agent._run_tool("write_text_file", {"path": str(target), "content": "@echo off\necho ready\n"})
            self.assertTrue(result["ok"])
            self.assertIn("echo ready", target.read_text(encoding="utf-8"))


class SelfProgrammingTests(unittest.TestCase):
    def test_code_reader_can_jump_to_search_match_line(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            target = root / "Projects" / "demo.py"
            target.parent.mkdir()
            target.write_text("".join(f"line_{number}\n" for number in range(1, 31)), encoding="utf-8")
            editor = CodeEditorModule(root, home)
            result = editor.read_file("Projects/demo.py", start_line=17, max_lines=3)
        self.assertEqual(result["start_line"], 17)
        self.assertEqual(result["end_line"], 19)
        self.assertEqual(result["content"], "line_17\nline_18\nline_19\n")

    def test_codex_exec_reads_large_prompt_from_stdin(self) -> None:
        command = HomeAgent._codex_exec_command(
            [r"C:\node.exe", r"E:\codex.js"],
            {"skip_git_repo_check": True, "sandbox": "danger-full-access"},
        )
        self.assertEqual(command[-1], "-")
        self.assertEqual(command[:4], [r"C:\node.exe", r"E:\codex.js", "exec", "--json"])
        self.assertNotIn("用户任务", " ".join(command))

    def test_failed_self_upgrade_is_diagnostic_not_auto_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {"enabled": True}})
            manager.begin("修改 HomeAgent 代码")
            manager.fail("Codex CLI 启动失败")
            state = manager.read()
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["last_error"], "Codex CLI 启动失败")
            self.assertEqual(manager.resume_prompt(), "")

    def test_media_stop_and_forced_process_termination_are_distinct_plans(self) -> None:
        self.assertTrue(HomeAgent._is_media_stop_plan({
            "operation": "stop_media", "required_capabilities": ["media_control"],
        }))
        forced = {
            "operation": "terminate_process",
            "handler": "cloudmusic_control",
            "required_capabilities": ["process_termination"],
        }
        self.assertTrue(HomeAgent._allows_application_termination(forced))
        self.assertFalse(HomeAgent._is_media_stop_plan(forced))

    def test_cloudmusic_generic_play_does_not_become_a_search(self) -> None:
        plan = HomeAgent._apply_cloudmusic_handler({
            "site": "cloudmusic", "operation": "play", "query": "音乐", "query_is_explicit": False,
        })
        self.assertEqual(plan["handler"], "model_ui")
        self.assertEqual(plan["query"], "")

    def test_cloudmusic_specific_target_uses_planner_decision(self) -> None:
        plan = HomeAgent._apply_cloudmusic_handler({
            "site": "cloudmusic", "operation": "play", "query": "稻香", "query_is_explicit": True,
        })
        self.assertEqual(plan["handler"], "model_ui")
        self.assertEqual(plan["query"], "稻香")

    def test_malformed_tool_arguments_are_rejected_instead_of_becoming_empty(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            HomeAgent._parse_tool_arguments('{"path":')
        with self.assertRaisesRegex(ValueError, "JSON object"):
            HomeAgent._parse_tool_arguments('["not", "an", "object"]')
        self.assertEqual({"path": "demo.txt"}, HomeAgent._parse_tool_arguments('{"path":"demo.txt"}'))

    def test_truncated_or_filtered_model_responses_are_incomplete(self) -> None:
        for reason in ("length", "content_filter", "repetition_truncation"):
            self.assertTrue(HomeAgent._is_incomplete_model_response(reason), reason)
        for reason in ("stop", "tool_calls", None):
            self.assertFalse(HomeAgent._is_incomplete_model_response(reason), reason)

    def test_task_fallback_does_not_semantically_route_by_keywords(self) -> None:
        plan = HomeAgent._analyze_task("打开网页并点击屏幕上的按钮", "上一轮说过 B 站")
        self.assertFalse(plan["is_task"])
        self.assertFalse(plan["actionable"])
        self.assertEqual("direct_answer", plan["execution_strategy"])
        self.assertEqual("conversation", plan["domain"])

    def test_web_route_comes_from_model_plan_not_keywords(self) -> None:
        self.assertTrue(HomeAgent._should_route_to_web({"is_task": True, "actionable": True, "domain": "web"}))
        self.assertFalse(HomeAgent._should_route_to_web({"is_task": False, "actionable": True, "domain": "web", "goal": "打开网页"}))
        self.assertFalse(HomeAgent._should_route_to_web({"is_task": True, "actionable": False, "domain": "web", "goal": "解释网页是什么"}))

    def test_planner_context_keeps_assistant_source_and_user_reply(self) -> None:
        context = HomeAgent._planner_context([
            {"role": "assistant", "content": "主人，休息一下吧", "source": "proactive_screen_care"},
            {"role": "user", "content": "好的"},
        ])
        self.assertIn('"role": "assistant"', context)
        self.assertIn('"source": "proactive_screen_care"', context)
        self.assertIn('"content": "好的"', context)

    def test_visual_route_comes_from_model_plan_not_keywords(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"vision_mcp": {"enabled": True, "gui_enabled": True}}
        self.assertTrue(agent._should_route_to_vision({"visual_required": True, "interaction_mode": "solve"}))
        self.assertFalse(agent._should_route_to_vision({"visual_required": False, "goal": "看看屏幕"}))

    def test_visual_tool_surface_supports_screen_questions(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = False
        agent.config = {
            "agent": {"prefer_local_code_tools": True},
            "vision_mcp": {"enabled": True}, "codex_cli": {"enabled": False},
            "computer_control": {"enabled": False},
        }
        tools = {item["function"]["name"] for item in agent._tools()}
        self.assertIn("ui_analyze_screen", tools)
        self.assertFalse(hasattr(agent, "is_screen_read_request"))

    def test_restart_command_detection_avoids_questions_and_feature_requests(self) -> None:
        for prompt in ("重启自己", "请现在重启你自己", "重启 HomeAgent", "麻烦重新启动桌宠"):
            self.assertTrue(HomeAgent.is_restart_request(prompt), prompt)
        for prompt in ("不要重启自己", "如何重启自己？", "让他能自己重启自己", "完善重启自己的消息处理功能"):
            self.assertFalse(HomeAgent.is_restart_request(prompt), prompt)

    def test_file_plan_uses_file_tools_not_ui_or_codex(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = False
        agent.current_file_authoring_task = True
        agent.config = {
            "agent": {"prefer_local_code_tools": True},
            "codex_cli": {"enabled": True}, "vision_mcp": {"enabled": True},
            "computer_control": {"enabled": True},
        }
        names = {item["function"]["name"] for item in agent._tools()}
        self.assertIn("write_text_file", names)
        self.assertNotIn("ui_list_windows", names)
        self.assertNotIn("launch_app", names)
        self.assertNotIn("codex_cli_task", names)

    def test_model_receives_shell_and_cmd_tools_when_enabled(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = False
        agent.current_file_authoring_task = False
        agent.config = {
            "agent": {"prefer_local_code_tools": True},
            "codex_cli": {"enabled": False}, "vision_mcp": {"enabled": False},
            "computer_control": {"enabled": True},
            "shell_execution": {"shell_enabled": True, "cmd_enabled": True},
        }
        tools = {item["function"]["name"]: item["function"] for item in agent._tools()}
        self.assertIn("run_shell", tools)
        self.assertIn("run_cmd", tools)
        self.assertIn("由你", tools["run_shell"]["description"])

    def test_execution_model_only_sees_media_tools_authorized_by_plan(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = True
        agent.current_file_authoring_task = False
        agent.current_task_plan = {"domain": "code", "operation": "code", "handler": None}
        agent.config = {
            "agent": {"prefer_local_code_tools": True},
            "codex_cli": {"enabled": False}, "vision_mcp": {"enabled": True},
            "computer_control": {"enabled": False},
        }
        names = {item["function"]["name"] for item in agent._tools(scoped=True)}
        self.assertNotIn("media_stop", names)
        self.assertNotIn("bilibili_open_favorite_video", names)
        self.assertIn("code_validate_project", names)

    def test_cloudmusic_plan_uses_generic_visual_action_tools(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = False
        agent.current_file_authoring_task = False
        agent.current_task_plan = {
            "is_task": True, "actionable": True, "domain": "desktop", "site": "cloudmusic",
            "operation": "play", "handler": "model_ui", "query": "稻香", "query_is_explicit": True,
        }
        agent.config = {
            "agent": {"prefer_local_code_tools": True},
            "codex_cli": {"enabled": False}, "vision_mcp": {"enabled": True},
            "computer_control": {"enabled": True}, "shell_execution": {"shell_enabled": False, "cmd_enabled": False},
        }
        names = {item["function"]["name"] for item in agent._tools(scoped=True)}
        for name in ("ui_list_windows", "ui_analyze_window", "ui_click_window", "ui_type_window", "launch_app"):
            self.assertIn(name, names)

    def test_cloudmusic_completion_is_not_hard_gated_by_window_title_change(self) -> None:
        source = inspect.getsource(HomeAgent.chat)
        self.assertNotIn("unproven_media_success_rejected", source)
        self.assertNotIn("automated_media_target_verified", source)
        self.assertNotIn("只有 ui_double_click_window 自身返回的 after_title", source)
        self.assertIn("verify_completion", source)

    def test_completion_discards_visual_state_superseded_by_later_action(self) -> None:
        evidence = [
            {"tool": "ui_analyze_window", "result": {
                "status": "success",
                "screenshot_captured_at": "2026-07-24T22:55:10+08:00",
                "analysis": "旧歌曲正在播放",
            }},
            {"tool": "ui_click_window", "result": {
                "status": "success",
                "tool_submitted_at": "2026-07-24T22:55:20+08:00",
            }},
            {"tool": "ui_analyze_window", "result": {
                "status": "success",
                "screenshot_captured_at": "2026-07-24T22:55:30+08:00",
                "analysis": "目标歌曲正在播放",
            }},
        ]
        fresh, discarded = HomeAgent._fresh_completion_evidence(
            evidence,
            now=datetime.fromisoformat("2026-07-24T22:55:40+08:00"),
            max_visual_age_seconds=45,
        )
        self.assertEqual([item["tool"] for item in fresh], ["ui_click_window", "ui_analyze_window"])
        self.assertEqual(len(discarded), 1)
        self.assertIn("状态变更", discarded[0]["reason"])
        self.assertEqual(fresh[-1]["result"]["analysis"], "目标歌曲正在播放")

    def test_completion_discards_visual_state_that_is_too_old(self) -> None:
        evidence = [{"tool": "ui_analyze_window", "result": {
            "status": "success",
            "screenshot_captured_at": "2026-07-24T22:54:00+08:00",
            "analysis": "曾经正在播放",
        }}]
        fresh, discarded = HomeAgent._fresh_completion_evidence(
            evidence,
            now=datetime.fromisoformat("2026-07-24T22:55:00+08:00"),
            max_visual_age_seconds=45,
        )
        self.assertFalse(fresh)
        self.assertIn("超过 45 秒", discarded[0]["reason"])

    def test_stale_vision_result_drops_analysis_before_model_context(self) -> None:
        normalized = HomeAgent._normalize_tool_result("ui_analyze_window", {
            "ok": True,
            "stale": True,
            "stale_reason": "标题已变化",
            "analysis": "这段旧识别不能再用",
        })
        self.assertEqual(normalized["status"], "stale")
        self.assertTrue(normalized["discarded_analysis"])
        self.assertNotIn("analysis", normalized)

    def test_code_read_tools_allow_absolute_paths_when_full_access_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as root_folder, tempfile.TemporaryDirectory() as external_folder:
            root = Path(root_folder); home = root / "HomeAgent"; home.mkdir()
            external = Path(external_folder); source = external / "sample.py"; source.write_text("VALUE = 7\n", encoding="utf-8")
            module = CodeEditorModule(root, home, allow_external_read=True, allow_external_write=True)
            self.assertEqual(module.read_file(str(source))["content"], "VALUE = 7\n")
            self.assertIn(str(source), module.list_files(str(external))["files"])
            self.assertEqual(module.search_text("VALUE", str(external))["matches"][0]["path"], str(source))
            module.begin_tracking()
            result = module.write_file(str(source), "VALUE = 8\n", self_edit=True)
            self.assertEqual("VALUE = 8\n", source.read_text(encoding="utf-8"))
            self.assertEqual(str(source), result["path"])
            self.assertIn(str(source), module.changed_files())
            self.assertTrue(module.validate_files(module.changed_files())["ok"])

    def test_code_read_tools_reject_unapproved_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root_folder, tempfile.TemporaryDirectory() as external_folder:
            root = Path(root_folder); home = root / "HomeAgent"; home.mkdir()
            source = Path(external_folder) / "sample.py"; source.write_text("VALUE = 7\n", encoding="utf-8")
            module = CodeEditorModule(root, home)
            with self.assertRaisesRegex(ValueError, "绝对路径不在代码读取权限范围"):
                module.read_file(str(source))
            with self.assertRaises(ValueError):
                module.write_file(str(source), "VALUE = 9\n", self_edit=True)

    def test_code_tool_surface_hides_codex_during_local_code_task(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = True
        agent.config = {
            "agent": {"prefer_local_code_tools": True},
            "codex_cli": {"enabled": True}, "vision_mcp": {"enabled": False}, "computer_control": {"enabled": False},
        }
        names = {item["function"]["name"] for item in agent._tools()}
        self.assertIn("code_write_file", names)
        self.assertIn("code_validate_project", names)
        self.assertNotIn("codex_cli_task", names)

    def test_semantic_code_scope_marks_recovery_as_self_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / "HomeAgent"; home.mkdir()
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {"require_validation": True}})
            manager.begin("由规划器稍后确定范围")
            self.assertFalse(manager.read()["is_self_upgrade"])
            manager.set_self_upgrade(True)
            self.assertTrue(manager.read()["is_self_upgrade"])

    def test_self_upgrade_finalize_rejects_empty_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / "HomeAgent"; home.mkdir()
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {"require_validation": True}})
            manager.begin("升级自己的代码")
            manager.set_self_upgrade(True)
            with self.assertRaisesRegex(RuntimeError, "没有产生任何代码或配置变更"):
                manager.finalize("已经完成")
            self.assertEqual(manager.read()["status"], "validation_failed")

    def test_completed_task_removes_recovery_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / "HomeAgent"; home.mkdir()
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {"require_validation": True}})
            manager.begin("普通聊天任务")
            self.assertTrue(manager.path.exists())
            self.assertFalse(manager.finalize("完成"))
            self.assertFalse(manager.path.exists())
            self.assertEqual(manager.resume_prompt(), "")

    def test_completed_upgrade_restart_does_not_repeat_original_task(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / "HomeAgent"; home.mkdir()
            source = home / "sample.py"; source.write_text("value = 1\n", encoding="utf-8")
            docs = root / "AI Read"; docs.mkdir(); doc = docs / "06_CURRENT_STATE.md"; doc.write_text("before\n", encoding="utf-8")
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {"require_validation": True, "enabled": True, "auto_restart": True}})
            manager.begin("升级自己的代码")
            manager.set_self_upgrade(True)
            source.write_text("value = 2\n", encoding="utf-8"); doc.write_text("after\n", encoding="utf-8")
            self.assertTrue(manager.finalize("升级完成"))
            self.assertEqual(manager.read()["status"], "restart_pending")
            self.assertTrue(manager.read()["task_completed"])
            self.assertEqual(manager.resume_prompt(), "")
            self.assertFalse(manager.path.exists())

    def test_only_running_task_is_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / "HomeAgent"; home.mkdir()
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {}})
            manager.begin("检查尚未完成的任务")
            prompt = manager.resume_prompt()
            self.assertIn("原任务：检查尚未完成的任务", prompt)
            self.assertEqual(manager.read()["status"], "running")

    def test_legacy_running_restart_command_is_cleared_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / "HomeAgent"; home.mkdir()
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {}})
            manager.begin("重启你自己。")
            self.assertTrue(manager.path.exists())
            self.assertEqual(manager.resume_prompt(), "")
            self.assertFalse(manager.path.exists())

    def test_direct_restart_never_creates_recovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); home = root / "HomeAgent"; home.mkdir()
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {}})
            agent = HomeAgent.__new__(HomeAgent)
            agent.cancel_event = __import__("threading").Event(); agent.self_upgrade = manager
            agent.begin_task("重启你自己。")
            self.assertFalse(manager.path.exists())

    def test_self_programming_requires_real_validated_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            source = home / "sample.py"
            source.write_text("value = 1\n", encoding="utf-8")
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {"require_validation": True}})
            manager.begin("修改 HomeAgent 程序")
            self.assertFalse(manager.validate_current_changes(require_changes=True)["ok"])
            source.write_text("value = 2\n", encoding="utf-8")
            result = manager.validate_current_changes(require_changes=True)
            self.assertFalse(result["ok"])
            self.assertIn("AI Read", result["error"])
            docs = root / "AI Read"; docs.mkdir(); (docs / "06_CURRENT_STATE.md").write_text("已同步 sample.py 逻辑\n", encoding="utf-8")
            result = manager.validate_current_changes(require_changes=True)
            self.assertTrue(result["ok"])
            self.assertIn("HomeAgent/sample.py", result["changed"])
            self.assertIn("AI Read/06_CURRENT_STATE.md", result["changed"])

    def test_self_programming_reads_engineering_documents(self) -> None:
        root = Path(__file__).resolve().parents[2]
        module = CodeEditorModule(root, root / "HomeAgent")
        content, loaded = module.load_engineering_documents()
        self.assertIn("README.md", loaded)
        self.assertIn("AI Read/01_ARCHITECTURE.md", loaded)
        self.assertIn("# AI 直播工具箱", content)
        self.assertIn("# 设计架构", content)

    def test_execution_contract_is_owned_by_isolated_module(self) -> None:
        root = Path(__file__).resolve().parents[2]
        module = CodeEditorModule(root, root / "HomeAgent")
        contract, loaded = module.build_execution_contract()
        self.assertTrue(loaded)
        self.assertIn("必须在本机实际完成代码写入", contract)
        self.assertIn("没有写入文件", contract)

    def test_independent_project_contract(self) -> None:
        root = Path(__file__).resolve().parents[2]
        contract, loaded = CodeEditorModule(root, root / "HomeAgent").build_execution_contract(self_edit=False)
        self.assertFalse(loaded)
        self.assertIn("Projects/<简短英文项目名>", contract)
        self.assertIn("自动测试", contract)

    def test_independent_python_project_is_tracked_and_tested(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            module = CodeEditorModule(root, home)
            module.begin_tracking()
            project = root / "Projects" / "demo"
            tests = project / "tests"
            tests.mkdir(parents=True)
            (project / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (tests / "test_calculator.py").write_text(
                "import unittest\nfrom calculator import add\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(3, add(1, 2))\n",
                encoding="utf-8",
            )
            changed = module.changed_files()
            self.assertIn("Projects/demo/calculator.py", changed)
            validation = module.validate_current_changes(require_changes=True)
            self.assertFalse(validation["ok"])
            self.assertIn("README", validation["error"])
            (project / "README.md").write_text("# Demo\n\nCalculator with tested add().\n", encoding="utf-8")
            changed = module.changed_files()
            validation = module.validate_current_changes(require_changes=True)
            self.assertTrue(validation["ok"])
            tests_result = module.run_autonomous_tests(changed, timeout=30)
            self.assertTrue(tests_result["ok"], tests_result)
            self.assertGreaterEqual(len(tests_result["commands"]), 2)

    def test_local_code_file_tools_are_atomic_and_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            module = CodeEditorModule(root, home)
            written = module.write_file("Projects/demo/main.py", "VALUE = 1\n")
            self.assertTrue(written["created"])
            self.assertEqual("VALUE = 1\n", module.read_file("Projects/demo/main.py")["content"])
            replaced = module.replace_text("Projects/demo/main.py", "1", "2")
            self.assertEqual(1, replaced["replaced"])
            self.assertEqual("VALUE = 2\n", module.read_file("Projects/demo/main.py")["content"])
            with self.assertRaises(ValueError):
                module.write_file("HomeAgent/agent.py", "blocked")
            with self.assertRaises(ValueError):
                module.read_file(".env")

    def test_failing_project_tests_block_success(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            module = CodeEditorModule(root, home)
            module.begin_tracking()
            tests = root / "Projects" / "broken" / "tests"
            tests.mkdir(parents=True)
            (tests.parent / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
            (tests / "test_value.py").write_text(
                "import unittest\nfrom value import VALUE\n\n"
                "class ValueTests(unittest.TestCase):\n"
                "    def test_expected_value(self):\n        self.assertEqual(2, VALUE)\n",
                encoding="utf-8",
            )
            result = module.run_autonomous_tests(module.changed_files(), timeout=30)
            self.assertFalse(result["ok"])
            self.assertTrue(result["failed"])


if __name__ == "__main__":
    unittest.main()
