"""单个语言服务进程的异步客户端。

要点：
- 语言服务是长驻子进程：启动一次，多次请求复用，空闲时由 manager 关闭；
- 必须回应服务端发来的请求（例如 clangd 的 `workspace/configuration`），
  否则对方会一直等我们，表现为"点了没反应"；
- 超时只影响本次请求，不杀进程；进程异常退出要让所有等待中的请求立刻失败，
  并把 stderr 尾部带出来，方便定位（例如 compile_commands 缺失的提示）。
"""
from __future__ import annotations

import asyncio
import os
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .protocol import LspProtocolError, MessageBuffer, encode_message

READ_CHUNK = 65536


class LspError(Exception):
    """语言服务交互失败的基类。"""


class LspProcessError(LspError):
    """进程已退出或无法启动。"""


class LspTimeoutError(LspError):
    """请求超时。"""


class LspClient:
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        name: str = "lsp",
        stderr_limit: int = 200,
        env: dict | None = None,
    ) -> None:
        self.command = list(command)
        self.cwd = Path(cwd)
        self.name = name
        self._stderr: deque[str] = deque(maxlen=stderr_limit)
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._notification_handler: Callable[[str, dict], Any] | None = None
        self._exit_reason: str | None = None
        self.diagnostics: dict[str, list] = {}
        self.server_capabilities: dict = {}
        self.log_messages: deque[str] = deque(maxlen=100)

    # ------------------------------------------------------------------ 生命周期

    @property
    def alive(self) -> bool:
        # 读取循环一旦结束（EOF / 协议错误）就视为不可用：此时 returncode 可能还没被 asyncio 更新
        return self._process is not None and self._process.returncode is None and self._exit_reason is None

    @property
    def exit_reason(self) -> str | None:
        return self._exit_reason

    def stderr_tail(self, lines: int = 8) -> list[str]:
        return list(self._stderr)[-lines:]

    async def start(self) -> None:
        if self._process is not None:
            return
        env = dict(os.environ if self._env is None else self._env)
        env.setdefault("LC_ALL", "C.UTF-8")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise LspProcessError(f"无法启动语言服务 {self.name}：{exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def shutdown(self, *, grace: float = 3.0) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            try:
                await self.request("shutdown", None, timeout=grace)
                self.notify("exit", None)
            except (LspError, LspProtocolError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=grace)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=grace)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._fail_pending("语言服务已关闭")
        self._process = None

    # ------------------------------------------------------------------ 收发

    def set_notification_handler(self, handler: Callable[[str, dict], Any] | None) -> None:
        self._notification_handler = handler

    def notify(self, method: str, params: Any = None) -> None:
        if not self.alive:
            raise LspProcessError(f"语言服务 {self.name} 未运行（{self._exit_reason or '已退出'}）")
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        asyncio.get_running_loop().create_task(self._send(payload))

    async def request(self, method: str, params: Any = None, timeout: float = 20.0) -> Any:
        if not self.alive:
            raise LspProcessError(f"语言服务 {self.name} 未运行（{self._exit_reason or '已退出'}）")
        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(message_id, None)
            raise LspTimeoutError(f"{self.name} 的 {method} 超过 {timeout:g} 秒未返回") from None

    async def _send(self, payload: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise LspProcessError(f"语言服务 {self.name} 的 stdin 不可用")
        try:
            process.stdin.write(encode_message(payload))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise LspProcessError(f"语言服务 {self.name} 已断开：{exc}") from exc

    async def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        buffer = MessageBuffer()
        reason = f"语言服务 {self.name} 已退出"
        try:
            while True:
                chunk = await process.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                for message in buffer.feed(chunk):
                    self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except LspProtocolError as exc:
            reason = f"语言服务 {self.name} 协议解析失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 读取线程不能让服务崩溃
            reason = f"读取语言服务 {self.name} 输出失败：{exc}"
        finally:
            tail = self.stderr_tail(3)
            self._exit_reason = reason + (f"（stderr: {' | '.join(tail)}）" if tail else "")
            self._fail_pending(self._exit_reason)

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                self._stderr.append(line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    def _dispatch(self, message: dict) -> None:
        if "id" in message and "method" not in message:
            future = self._pending.pop(message["id"], None)
            if future is None or future.done():
                return
            if "error" in message:
                error = message["error"] or {}
                future.set_exception(LspError(f"{error.get('message', '语言服务返回错误')}（code={error.get('code')}）"))
            else:
                future.set_result(message.get("result"))
            return
        method = message.get("method")
        if method is None:
            return
        params = message.get("params") or {}
        if "id" in message:
            # 服务端 → 客户端的请求，必须回复，否则对方会一直等
            response = {"jsonrpc": "2.0", "id": message["id"], "result": self._server_request_result(method, params)}
            asyncio.get_running_loop().create_task(self._send(response))
            return
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri")
            if isinstance(uri, str):
                self.diagnostics[uri] = params.get("diagnostics") or []
        elif method == "window/logMessage":
            self.log_messages.append(str(params.get("message", "")))
        if self._notification_handler is not None:
            try:
                result = self._notification_handler(method, params)
                if asyncio.iscoroutine(result):
                    asyncio.get_running_loop().create_task(result)
            except Exception:  # noqa: BLE001 - 通知处理异常不能影响协议读取
                pass

    @staticmethod
    def _server_request_result(method: str, params: dict) -> Any:
        if method == "workspace/configuration":
            items = params.get("items") or []
            return [{} for _ in items]
        if method == "workspace/applyEdit":
            return {"applied": False}
        if method in ("client/registerCapability", "client/unregisterCapability"):
            return None
        if method == "window/workDoneProgress/create":
            return None
        if method == "workspace/workspaceFolders":
            return []
        return None

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(LspProcessError(reason))
