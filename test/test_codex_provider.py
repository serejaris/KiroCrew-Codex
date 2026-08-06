"""Codex App Server provider contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.providers.codex import CodexAppServerProvider, probe_codex_readiness
from kiro_crew.session_map import SessionMap

_FAKE_SERVER = r"""
import json
import sys


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    request_id = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "thread": {"id": "thread-new"}, "model": "gpt-test"
        }})
    elif method == "thread/resume":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "thread": {"id": msg["params"]["threadId"]}, "model": "gpt-test"
        }})
    elif method == "model/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"data": [
            {"id": "gpt-test", "model": "gpt-test", "displayName": "GPT Test",
             "contextWindow": 123456, "isDefault": True,
             "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
        ], "nextCursor": None}})
    elif method == "turn/start":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "turn": {"id": "turn-1", "status": "inProgress", "items": []}
        }})
        send({"jsonrpc": "2.0", "method": "item/reasoning/summaryTextDelta",
              "params": {"threadId": "thread-new", "turnId": "turn-1",
                         "itemId": "reason-1", "delta": "думаю"}})
        send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
              "params": {"threadId": "thread-new", "turnId": "turn-1",
                         "itemId": "message-1", "delta": "готово"}})
        send({"jsonrpc": "2.0", "method": "item/started", "params": {
            "threadId": "thread-new", "turnId": "turn-1", "item": {
                "type": "commandExecution", "id": "cmd-1", "command": "pwd",
                "cwd": "/tmp", "status": "inProgress", "aggregatedOutput": ""
            }}})
        send({"jsonrpc": "2.0", "method": "item/commandExecution/outputDelta",
              "params": {"threadId": "thread-new", "turnId": "turn-1",
                         "itemId": "cmd-1", "delta": "/tmp\n"}})
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {
            "threadId": "thread-new", "turnId": "turn-1", "item": {
                "type": "commandExecution", "id": "cmd-1", "command": "pwd",
                "cwd": "/tmp", "status": "completed", "aggregatedOutput": "/tmp\n",
                "exitCode": 0, "durationMs": 2
            }}})
        send({"jsonrpc": "2.0", "method": "thread/tokenUsage/updated", "params": {
            "threadId": "thread-new", "turnId": "turn-1",
            "tokenUsage": {"total": {"totalTokens": 50, "inputTokens": 40,
                                       "outputTokens": 10, "cachedInputTokens": 5},
                           "last": {"totalTokens": 50, "inputTokens": 40,
                                    "outputTokens": 10, "cachedInputTokens": 5}},
            "modelContextWindow": 1000
        }})
        send({"jsonrpc": "2.0", "id": 90,
              "method": "item/commandExecution/requestApproval", "params": {
                  "threadId": "thread-new", "turnId": "turn-1", "itemId": "cmd-2",
                  "reason": "needs approval", "command": "echo ok", "cwd": "/tmp"
              }})
    elif request_id == 90:
        assert msg["result"] == {"decision": "acceptForSession"}
        send({"jsonrpc": "2.0", "id": 91,
              "method": "item/permissions/requestApproval", "params": {
                  "threadId": "thread-new", "turnId": "turn-1", "itemId": "cmd-3",
                  "cwd": "/tmp", "startedAtMs": 1,
                  "permissions": {"network": {"enabled": True}}
              }})
    elif request_id == 91:
        assert msg["result"] == {
            "permissions": {"network": {"enabled": True}}, "scope": "session"
        }
        send({"jsonrpc": "2.0", "id": 92,
              "method": "item/tool/requestUserInput", "params": {
                  "threadId": "thread-new", "turnId": "turn-1", "itemId": "ask-1",
                  "questions": [{"id": "choice", "header": "Pick",
                                 "question": "Choose", "options": []}]
              }})
    elif request_id == 92:
        assert msg["result"] == {"answers": {"choice": {"answers": []}}}
        send({"jsonrpc": "2.0", "id": 93,
              "method": "mcpServer/elicitation/request", "params": {
                  "serverName": "demo", "threadId": "thread-new", "turnId": "turn-1",
                  "mode": "url", "elicitationId": "elicit-1",
                  "message": "Open URL", "url": "https://example.com"
              }})
    elif request_id == 93:
        assert msg["result"] == {"action": "decline"}
        send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
            "threadId": "thread-new", "turn": {"id": "turn-1", "status": "completed",
                                                 "items": [], "error": None}
        }})
    elif method == "turn/interrupt":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "thread/compact/start":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
"""


@pytest.fixture
def fake_server(tmp_path: Path) -> list[str]:
    server = tmp_path / "fake_codex_app_server.py"
    server.write_text(_FAKE_SERVER, encoding="utf-8")
    return [sys.executable, str(server)]


class TestCodexAppServerProvider:
    @pytest.mark.asyncio
    async def test_streams_events_and_approval(self, fake_server: list[str], tmp_path: Path):
        provider = CodexAppServerProvider(
            work_dir=tmp_path,
            model="gpt-test",
            command=fake_server,
            reasoning_effort="high",
        )
        await provider.start()

        events = []
        async for event in provider.stream("сделай тест"):
            events.append(event)
            if event.kind == EVENT_PERMISSION_REQUEST:
                await provider.approve_tool(event.request_id, always=True)

        assert [event.kind for event in events] == [
            EVENT_THINKING_CHUNK,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            EVENT_TOOL_RESULT,
            EVENT_PERMISSION_REQUEST,
            EVENT_PERMISSION_REQUEST,
            EVENT_COMPLETE,
        ]
        assert events[1].text == "готово"
        assert events[2].is_shell is True
        assert events[2].shell_command == "pwd"
        assert events[-1].usage.input_tokens == 40
        assert events[-1].usage.output_tokens == 10
        assert provider.session_id == "thread-new"
        assert provider.context_usage_pct() == 5.0
        assert provider.context_window_tokens() == 1000
        await provider.shutdown()

    @pytest.mark.asyncio
    async def test_resumes_thread_and_lists_models(self, fake_server: list[str], tmp_path: Path):
        provider = CodexAppServerProvider(work_dir=tmp_path, command=fake_server)
        provider.set_resume_session_id("thread-existing")
        await provider.start()

        assert provider.session_id == "thread-existing"
        assert provider.resumed is True
        assert await provider.available_models() == [
            {
                "model_name": "gpt-test",
                "description": "GPT Test",
                "context_window": 123456,
                "is_default": True,
                "reasoning_efforts": ["high"],
            }
        ]
        await provider.shutdown()

    def test_config_factory_dispatches_codex(self, tmp_path: Path):
        cfg = KiroCrewConfig()
        cfg.agent.provider = "codex"
        cfg.agent.model = "gpt-5.6-sol"
        cfg.agent.reasoning_effort = "high"

        with patch("kiro_crew.config.loader._session_work_dir", return_value=tmp_path):
            provider = cfg.create_provider_factory()("slot-1")

        assert isinstance(provider, CodexAppServerProvider)
        assert provider.model == "gpt-5.6-sol"
        assert provider.reasoning_effort == "high"

    def test_config_factory_preserves_managed_mcp_controls(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "kirocrew.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.test",
                            "headers": {"X-Test": "yes"},
                            "disabledTools": ["dangerous"],
                        },
                        "off": {"command": "tool", "disabled": True},
                        "stdio": {
                            "command": "tool",
                            "args": ["serve"],
                            "env": {"MODE": "test"},
                            "autoApprove": ["read"],
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig()
        cfg.agent.provider = "codex"

        with (
            patch("kiro_crew.config.loader.kiro_agents_dir", return_value=agents_dir),
            patch("kiro_crew.config.loader._session_work_dir", return_value=tmp_path),
        ):
            provider = cfg.create_provider_factory()("slot-1")

        servers = provider._thread_params()["config"]["mcp_servers"]
        assert servers["remote"] == {
            "url": "https://mcp.example.test",
            "http_headers": {"X-Test": "yes"},
            "disabled_tools": ["dangerous"],
        }
        assert servers["off"]["enabled"] is False
        assert servers["stdio"]["tools"] == {"read": {"approval_mode": "approve"}}

    def test_session_map_keeps_native_thread_without_kiro_files(self, tmp_path: Path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sessions = SessionMap()
            sessions.set("dashboard:codex", "thread-existing", provider="codex")

            assert sessions.get("dashboard:codex") == "thread-existing"
            assert sessions.prune() == 0


class TestCodexReadiness:
    @pytest.mark.asyncio
    async def test_authenticated_cli_is_ready(self):
        class Process:
            returncode = 0

            async def communicate(self):
                return b"Logged in using ChatGPT\n", b""

        spawn = AsyncMock(return_value=Process())
        with patch("kiro_crew.providers.codex.create_subprocess_limited", spawn):
            readiness = await probe_codex_readiness()

        assert readiness.ready is True
        assert readiness.authenticated is True

    @pytest.mark.asyncio
    async def test_missing_cli_is_not_ready(self):
        spawn = AsyncMock(side_effect=FileNotFoundError)
        with patch("kiro_crew.providers.codex.create_subprocess_limited", spawn):
            readiness = await probe_codex_readiness()

        assert readiness.installed is False
        assert readiness.ready is False
