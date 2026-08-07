# Modified 2026 by Sereja Ris for VibecodersCrew (community fork of Kiro Crew).
# See NOTICE and CHANGELOG.md for the nature of the modifications.
"""OpenAI Codex App Server provider.

This adapter speaks the official JSON-RPC protocol exposed by
``codex app-server``.  Authentication stays with the Codex CLI, so an existing
ChatGPT login can be reused without copying API keys into KiroCrew.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kiro_crew import __version__, platform_compat
from kiro_crew.acp.client import AcpAuthRequired, AcpError, AcpProcessDied
from kiro_crew.acp.types import TurnUsage
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    CancelOutcome,
    LLMEvent,
    LLMProvider,
)
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_SESSION_HOST,
    RLIMIT_PROFILE_TOOL,
    create_subprocess_limited,
    scrub_agent_denied_env,
)

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECS = 30.0
_SHUTDOWN_TIMEOUT_SECS = 5.0
_STDERR_TAIL_LINES = 80
_CHATGPT_CODEX_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


@dataclass(frozen=True)
class CodexReadiness:
    installed: bool
    authenticated: bool
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.installed and self.authenticated


def resolve_codex_bin() -> str:
    """Resolve the Codex CLI used for the App Server transport."""

    configured = os.environ.get("KIROCREW_CODEX_BIN", "").strip()
    if configured:
        return configured
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    if _CHATGPT_CODEX_PATH.is_file():
        return str(_CHATGPT_CODEX_PATH)
    return "codex"


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        platform_compat.kill_process_tree(proc.pid, platform_compat.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()


async def probe_codex_readiness(timeout: float = 10.0) -> CodexReadiness:
    """Check that Codex exists and its own credential store is signed in."""

    command = resolve_codex_bin()
    try:
        proc = await create_subprocess_limited(
            command,
            "login",
            "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            profile=RLIMIT_PROFILE_TOOL,
        )
    except FileNotFoundError:
        return CodexReadiness(False, False, "Codex CLI was not found.")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _stop_process(proc)
        return CodexReadiness(True, False, "Codex login status timed out.")
    detail = (stdout + stderr).decode(errors="replace").strip()[:500]
    return CodexReadiness(True, proc.returncode == 0, detail)


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


class CodexAppServerProvider(LLMProvider):
    """KiroCrew provider backed by the official Codex App Server."""

    def __init__(
        self,
        *,
        work_dir: str | Path,
        model: str = "",
        agent: str | None = None,
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        reasoning_effort: str = "",
        approval_mode: str = "auto",
        dangerously_skip_permissions: bool = False,
        sandbox_mode: str = "workspace-write",
        developer_instructions: str = "",
        command: Sequence[str] | None = None,
        app_server_config: dict[str, Any] | None = None,
    ) -> None:
        self._work_dir = Path(work_dir).expanduser().resolve()
        self.model = model.strip() if isinstance(model, str) else ""
        if self.model == "auto":
            self.model = ""
        self.reasoning_effort = reasoning_effort.strip()
        self._agent = agent or ""
        self._session_key = session_key or ""
        self._channel_id = channel_id or ""
        self._extra_env = dict(extra_env or {})
        self._approval_mode = approval_mode
        self._skip_permissions = dangerously_skip_permissions
        self._sandbox_mode = sandbox_mode
        self._developer_instructions = developer_instructions
        self._command = (
            list(command)
            if command
            else [
                resolve_codex_bin(),
                "app-server",
                "--listen",
                "stdio://",
            ]
        )
        self._app_server_config = dict(app_server_config or {})

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._responses: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending_approvals: dict[str, tuple[Any, str, dict[str, Any]]] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._thread_id = ""
        self._resume_thread_id = ""
        self._resumed = False
        self._turn_id = ""
        self._active_turn = False
        self._unfinished_turn = False
        self._last_activity = time.monotonic()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._context_window = 0
        self._context_used = 0
        self._last_usage = TurnUsage()
        self._compaction_event = asyncio.Event()

    @property
    def session_id(self) -> str:
        return self._thread_id

    @property
    def provider_id(self) -> str:
        return "codex"

    @property
    def resumed(self) -> bool:
        return self._resumed

    @property
    def cwd(self) -> str:
        return str(self._work_dir)

    @property
    def exit_code(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    def set_resume_session_id(self, session_id: str) -> None:
        self._resume_thread_id = str(session_id or "")

    def context_usage_pct(self) -> float:
        if self._context_window <= 0:
            return 0.0
        return min(100.0, self._context_used * 100.0 / self._context_window)

    def context_window_tokens(self) -> int:
        return self._context_window

    def context_used_tokens(self) -> int:
        return self._context_used

    def is_alive(self) -> bool:
        return self.is_process_alive()

    def is_process_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def has_active_turn(self) -> bool:
        return self._active_turn

    def has_unfinished_turn(self) -> bool:
        return self._unfinished_turn

    def touch_activity(self) -> None:
        self._last_activity = time.monotonic()

    def runtime_info(self) -> tuple[int | None, str | None]:
        proc = self._proc
        return (proc.pid if proc is not None and proc.returncode is None else None, None)

    def _thread_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self._work_dir),
            "approvalPolicy": "never" if self._skip_permissions else "untrusted",
            "sandbox": self._codex_sandbox(),
        }
        if self.model:
            params["model"] = self.model
        if self._developer_instructions:
            params["developerInstructions"] = self._developer_instructions
        if self._app_server_config:
            params["config"] = self._app_server_config
        return params

    def _codex_sandbox(self) -> str:
        if self._sandbox_mode in {"read-only", "workspace-write", "danger-full-access"}:
            return self._sandbox_mode
        # KiroCrew's historical values are auto/off. Codex still gets its own
        # native workspace sandbox in either case.
        return "workspace-write"

    async def start(self) -> None:
        if self.is_process_alive():
            return
        self._work_dir.mkdir(parents=True, exist_ok=True)
        env = scrub_agent_denied_env({**os.environ, **self._extra_env})
        try:
            self._proc = await create_subprocess_limited(
                *self._command,
                cwd=str(self._work_dir),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                profile=RLIMIT_PROFILE_SESSION_HOST,
            )
        except FileNotFoundError as exc:
            raise AcpProcessDied(
                "Codex CLI не найден. Установите Codex или задайте KIROCREW_CODEX_BIN."
            ) from exc

        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        try:
            await self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "kirocrew",
                        "title": "KiroCrew",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._notify("initialized", {})

            params = self._thread_params()
            if self._resume_thread_id:
                params["threadId"] = self._resume_thread_id
                result = await self._request("thread/resume", params)
                self._resumed = True
            else:
                result = await self._request("thread/start", params)
                self._resumed = False
            thread = result.get("thread", {}) if isinstance(result, dict) else {}
            self._thread_id = str(thread.get("id") or self._resume_thread_id)
            if not self._thread_id:
                raise AcpError("Codex App Server did not return a thread id")
            served_model = result.get("model") if isinstance(result, dict) else None
            if isinstance(served_model, str) and served_model:
                self.model = served_model
        except BaseException:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        proc = self._proc
        self._proc = None
        self._active_turn = False
        self._unfinished_turn = False
        for future in self._responses.values():
            if not future.done():
                future.cancel()
        self._responses.clear()

        if proc is not None and proc.returncode is None:
            await _stop_process(proc)

        current = asyncio.current_task()
        tasks = [t for t in (self._reader_task, self._stderr_task) if t and t is not current]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None

    async def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            raise AcpProcessDied(self._dead_message())
        wire = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        try:
            async with self._write_lock:
                proc.stdin.write(wire)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(self._dead_message()) from exc
        self.touch_activity()

    async def _request(
        self, method: str, params: dict[str, Any], *, timeout: float = _REQUEST_TIMEOUT_SECS
    ) -> Any:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AcpError(f"Codex App Server timed out during {method}", transient=True) from exc
        finally:
            self._responses.pop(request_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _respond_error(self, request_id: Any, method: str) -> None:
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Unsupported Codex client request: {method}",
                },
            }
        )

    async def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self.touch_activity()
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.debug("Ignoring malformed Codex App Server frame: %r", line[:500])
                    continue
                request_id = message.get("id")
                method = message.get("method")
                if method is None and request_id in self._responses:
                    future = self._responses[request_id]
                    if future.done():
                        continue
                    error = message.get("error")
                    if error is not None:
                        text = _json_text(error)
                        error_cls = (
                            AcpAuthRequired
                            if any(word in text.lower() for word in ("auth", "login", "sign in"))
                            else AcpError
                        )
                        future.set_exception(error_cls(f"Codex App Server: {text}"))
                    else:
                        future.set_result(message.get("result"))
                    continue
                if method:
                    await self._events.put(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex App Server reader failed")
        finally:
            for future in self._responses.values():
                if not future.done():
                    future.set_exception(AcpProcessDied(self._dead_message()))

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode(errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("codex app-server: %s", text)
        except asyncio.CancelledError:
            raise

    def _dead_message(self) -> str:
        detail = self._stderr_tail[-1] if self._stderr_tail else "no stderr"
        code = self.exit_code
        return f"Codex App Server exited (code={code}): {detail}"

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        if not self.is_process_alive():
            raise AcpProcessDied(self._dead_message())
        params: dict[str, Any] = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": message}],
        }
        if self.model:
            params["model"] = self.model
        if self.reasoning_effort:
            params["effort"] = self.reasoning_effort
        self._active_turn = True
        self._unfinished_turn = True
        self._last_usage = TurnUsage()
        try:
            result = await self._request("turn/start", params)
            turn = result.get("turn", {}) if isinstance(result, dict) else {}
            self._turn_id = str(turn.get("id") or "")
            while True:
                if not self.is_process_alive() and self._events.empty():
                    raise AcpProcessDied(self._dead_message())
                raw = await self._events.get()
                method = str(raw.get("method") or "")
                raw_params = raw.get("params")
                event_params = raw_params if isinstance(raw_params, dict) else {}

                if raw.get("id") is not None:
                    if await self._answer_non_approval_request(raw, event_params):
                        continue
                    event = self._approval_event(raw, event_params)
                    if event is not None:
                        yield event
                    else:
                        await self._respond_error(raw["id"], method)
                    continue

                if method == "turn/completed":
                    completed_turn = event_params.get("turn", {})
                    status = str(completed_turn.get("status") or "completed")
                    error = completed_turn.get("error")
                    if status == "failed" or error:
                        raise AcpError(f"Codex turn failed: {_json_text(error or status)}")
                    self._active_turn = False
                    self._unfinished_turn = False
                    yield LLMEvent(
                        kind=EVENT_COMPLETE,
                        stop_reason=status,
                        context_usage_pct=self.context_usage_pct(),
                        usage=self._last_usage,
                    )
                    return

                for event in self._events_from_notification(method, event_params):
                    yield event
        finally:
            self._active_turn = False

    def _events_from_notification(self, method: str, params: dict[str, Any]) -> list[LLMEvent]:
        if method == "item/agentMessage/delta":
            return [LLMEvent(kind=EVENT_TEXT_CHUNK, text=str(params.get("delta") or ""))]
        if method in {
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
        }:
            return [LLMEvent(kind=EVENT_THINKING_CHUNK, text=str(params.get("delta") or ""))]
        if method == "thread/tokenUsage/updated":
            self._update_usage(params)
            return []
        if method in {"thread/compacted", "thread/compact/completed"}:
            self._compaction_event.set()
            return []
        if method == "item/started":
            item = params.get("item")
            return self._tool_started(item if isinstance(item, dict) else {})
        if method == "item/commandExecution/outputDelta":
            return [
                LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id=str(params.get("itemId") or ""),
                    tool_output=str(params.get("delta") or ""),
                    tool_final=False,
                )
            ]
        if method == "item/completed":
            item = params.get("item")
            return self._tool_completed(item if isinstance(item, dict) else {})
        return []

    def _tool_started(self, item: dict[str, Any]) -> list[LLMEvent]:
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        if item_type == "commandExecution":
            command = str(item.get("command") or "")
            return [
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id=item_id,
                    title=command or "Shell command",
                    tool_kind="execute",
                    tool_name="commandExecution",
                    tool_input=_json_text({"command": command, "cwd": item.get("cwd")}),
                    raw_tool_params={"command": command, "cwd": item.get("cwd")},
                    is_shell=True,
                )
            ]
        if item_type in {"mcpToolCall", "dynamicToolCall"}:
            tool = str(item.get("tool") or item.get("name") or item_type)
            server = str(item.get("server") or "")
            arguments = item.get("arguments", {})
            return [
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id=item_id,
                    title=f"{server + '.' if server else ''}{tool}",
                    tool_kind="mcp" if item_type == "mcpToolCall" else "tool",
                    tool_name=tool,
                    mcp_server_name=server,
                    tool_input=_json_text(arguments),
                    raw_tool_params=arguments if isinstance(arguments, dict) else None,
                )
            ]
        if item_type == "fileChange":
            changes = item.get("changes", [])
            return [
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id=item_id,
                    title="File changes",
                    tool_kind="edit",
                    tool_name="fileChange",
                    tool_input=_json_text(changes),
                    raw_tool_params={"changes": changes},
                )
            ]
        if item_type == "webSearch":
            query = str(item.get("query") or "")
            return [
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id=item_id,
                    title=query or "Web search",
                    tool_kind="search",
                    tool_name="webSearch",
                    tool_input=_json_text({"query": query}),
                    raw_tool_params={"query": query},
                )
            ]
        return []

    def _tool_completed(self, item: dict[str, Any]) -> list[LLMEvent]:
        item_type = str(item.get("type") or "")
        if item_type not in {
            "commandExecution",
            "mcpToolCall",
            "dynamicToolCall",
            "fileChange",
            "webSearch",
            "collabToolCall",
        }:
            return []
        output: object
        if item_type == "commandExecution":
            output = item.get("aggregatedOutput", "")
        elif item.get("error"):
            output = item.get("error")
        else:
            output = item.get("result", item.get("changes", item))
        return [
            LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=str(item.get("id") or ""),
                tool_output=_json_text(output),
                tool_final=True,
            )
        ]

    async def _answer_non_approval_request(
        self, raw: dict[str, Any], params: dict[str, Any]
    ) -> bool:
        """Answer App Server prompts that the boolean approval UI cannot collect."""

        request_id = raw.get("id")
        method = str(raw.get("method") or "")
        if method == "item/tool/requestUserInput":
            questions = params.get("questions", [])
            answers: dict[str, dict[str, list[str]]] = {
                str(question.get("id")): {"answers": []}
                for question in questions
                if isinstance(question, dict) and question.get("id")
            }
            await self._respond(request_id, {"answers": answers})
            return True
        if method == "mcpServer/elicitation/request":
            await self._respond(request_id, {"action": "decline"})
            return True
        return False

    def _approval_event(self, raw: dict[str, Any], params: dict[str, Any]) -> LLMEvent | None:
        method = str(raw.get("method") or "")
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            logger.warning("Unsupported Codex approval request: %s", method)
            return None
        request_id = cast(str | int, raw.get("id"))
        key = str(request_id)
        self._pending_approvals[key] = (request_id, method, params)
        command = str(params.get("command") or "")
        item_id = str(params.get("itemId") or "")
        is_shell = method == "item/commandExecution/requestApproval"
        is_permissions = method == "item/permissions/requestApproval"
        title = (
            command
            if is_shell
            else str(
                params.get("reason")
                or ("Additional permissions" if is_permissions else "File changes")
            )
        )
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=request_id,
            tool_call_id=item_id,
            title=title,
            tool_kind="execute" if is_shell else ("permission" if is_permissions else "edit"),
            tool_name=(
                "commandExecution"
                if is_shell
                else ("requestPermissions" if is_permissions else "fileChange")
            ),
            tool_input=_json_text(params),
            raw_tool_params={"command": command, "cwd": params.get("cwd")} if is_shell else params,
            is_shell=is_shell,
            options=[
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {
                    "optionId": "allow_always",
                    "name": "Allow for session",
                    "kind": "allow_always",
                },
                {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
            ],
        )

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        pending = self._pending_approvals.pop(str(request_id), None)
        if pending is None:
            raise AcpError(f"Unknown Codex approval request: {request_id}")
        wire_id, method, params = pending
        if method == "item/permissions/requestApproval":
            permissions = params.get("permissions")
            await self._respond(
                wire_id,
                {
                    "permissions": permissions if isinstance(permissions, dict) else {},
                    "scope": "session" if always else "turn",
                },
            )
        else:
            decision = "acceptForSession" if always else "accept"
            await self._respond(wire_id, {"decision": decision})

    async def reject_tool(self, request_id: str | int) -> None:
        pending = self._pending_approvals.pop(str(request_id), None)
        if pending is None:
            raise AcpError(f"Unknown Codex approval request: {request_id}")
        wire_id, method, _params = pending
        result = (
            {"permissions": {}, "scope": "turn"}
            if method == "item/permissions/requestApproval"
            else {"decision": "decline"}
        )
        await self._respond(wire_id, result)

    def _update_usage(self, params: dict[str, Any]) -> None:
        usage = params.get("tokenUsage", {})
        total = usage.get("total", {}) if isinstance(usage, dict) else {}
        last = usage.get("last", {}) if isinstance(usage, dict) else {}
        window = params.get("modelContextWindow", 0)
        if isinstance(window, (int, float)) and window > 0:
            self._context_window = int(window)
        total_tokens = total.get("totalTokens", 0) if isinstance(total, dict) else 0
        if isinstance(total_tokens, (int, float)):
            self._context_used = int(total_tokens)
        if isinstance(last, dict):
            self._last_usage = TurnUsage(
                input_tokens=int(last.get("inputTokens", 0) or 0),
                output_tokens=int(last.get("outputTokens", 0) or 0),
                cache_read_tokens=int(last.get("cachedInputTokens", 0) or 0),
                num_turns=1,
            )

    async def available_models(self) -> list[dict[str, Any]]:
        result = await self._request("model/list", {"limit": 100, "includeHidden": False})
        rows = result.get("data", []) if isinstance(result, dict) else []
        models: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model") or row.get("id") or "")
            if not model_name:
                continue
            efforts = row.get("supportedReasoningEfforts", [])
            models.append(
                {
                    "model_name": model_name,
                    "description": str(row.get("displayName") or model_name),
                    "context_window": int(row.get("contextWindow") or 0),
                    "is_default": bool(row.get("isDefault", False)),
                    "reasoning_efforts": [
                        str(e.get("reasoningEffort"))
                        for e in efforts
                        if isinstance(e, dict) and e.get("reasoningEffort")
                    ],
                }
            )
        return models

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        if not self._active_turn or not self._turn_id:
            return "no_turn"
        try:
            timeout = wait_ack_timeout if wait_ack_timeout > 0 else _REQUEST_TIMEOUT_SECS
            await self._request(
                "turn/interrupt",
                {"threadId": self._thread_id, "turnId": self._turn_id},
                timeout=timeout,
            )
            self._active_turn = False
            return "acked"
        except AcpError as exc:
            if isinstance(exc.__cause__, asyncio.TimeoutError):
                return "timeout"
            logger.warning("Codex turn interrupt failed", exc_info=True)
            return "error"
        except Exception:
            logger.warning("Codex turn interrupt failed", exc_info=True)
            return "error"

    async def compact(self, context: str = "") -> None:
        self._compaction_event.clear()
        await self._request("thread/compact/start", {"threadId": self._thread_id})

    async def wait_for_compaction(self, timeout: float = 120.0) -> dict:
        try:
            await asyncio.wait_for(self._compaction_event.wait(), timeout=timeout)
            return {"type": "completed"}
        except asyncio.TimeoutError:
            return {"type": "timeout"}


__all__ = [
    "CodexAppServerProvider",
    "CodexReadiness",
    "probe_codex_readiness",
    "resolve_codex_bin",
]
