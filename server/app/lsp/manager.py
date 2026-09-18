"""按工作区/语言管理语言服务进程。

规则：
- 懒启动：第一次真正需要时才起进程，启动后复用，**不会每次点击都新起一个**；
- 有上限：实例数超过 `maxInstances` 时按最后使用时间淘汰最久未用的；
- 空闲关闭：`idleShutdownSeconds` 内没有请求就关掉，避免长期占着构建服务器的内存；
- 文档版本管理：每个实例自己维护 uri → version，内容变了发 full-text didChange；
- 全部关闭：服务停机时（FastAPI lifespan）统一 shutdown，不留孤儿进程。
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .client import LspClient, LspError, LspProcessError


@dataclass
class LanguageServerSpec:
    language: str
    command: list[str]
    display_name: str
    initialization_options: dict = field(default_factory=dict)
    source: str = ""

    def describe(self) -> dict:
        return {
            "language": self.language,
            "command": self.command,
            "displayName": self.display_name,
            "source": self.source,
        }


class ServerHandle:
    """一个已启动的语言服务实例，外加它自己的文档版本表。"""

    def __init__(self, spec: LanguageServerSpec, client: LspClient, workspace_id: str, root: Path):
        self.spec = spec
        self.client = client
        self.workspace_id = workspace_id
        self.root = root
        self.started_at = time.monotonic()
        self.last_used = time.monotonic()
        self.documents: dict[str, dict] = {}
        self.capabilities: dict = {}
        self.request_count = 0

    @property
    def key(self) -> str:
        return f"{self.workspace_id}:{self.spec.language}"

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    def open_document(self, uri: str, language_id: str, text: str, version: int) -> int:
        """第一次打开发 didOpen，内容变化发 full-text didChange；返回当前版本号。"""
        current = self.documents.get(uri)
        if current is None:
            self.documents[uri] = {"version": version, "text": text}
            self.client.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": language_id, "version": version, "text": text}},
            )
            return version
        if current["text"] == text and current["version"] == version:
            return version
        next_version = current["version"] + 1
        self.documents[uri] = {"version": next_version, "text": text}
        self.client.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": next_version},
                "contentChanges": [{"text": text}],
            },
        )
        return next_version

    def close_document(self, uri: str) -> None:
        if uri in self.documents and self.client.alive:
            self.client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self.documents.pop(uri, None)


CLANGD_PREBUILT_GLOBS = (
    "prebuilts/clang/host/linux-x86/*/bin/clangd",
    "prebuilts/clang/host/darwin-x86/*/bin/clangd",
    "prebuilts/clang/host/windows-x86/*/bin/clangd.exe",
)


def version_of(executable: str) -> str:
    try:
        completed = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (completed.stdout or completed.stderr or "").splitlines()
    if not first:
        return ""
    match = re.search(r"(\d+\.\d+\.\d+)", first[0])
    return match.group(1) if match else first[0].strip()[:60]


def find_clangd(configured: str | None, search_dirs: list[Path] | None = None) -> tuple[str | None, str, str]:
    """返回 (可执行文件, 版本, 来源说明)。优先配置，其次 PATH，最后 AOSP prebuilts。"""
    if configured:
        candidate = Path(os.path.expanduser(configured)).resolve()
        if candidate.is_file():
            return str(candidate), version_of(str(candidate)), "配置 navigation.clangdPath"
        return None, "", f"配置的 clangdPath 不存在：{candidate}"
    located = shutil.which("clangd")
    if located:
        return located, version_of(located), "PATH 中的 clangd"
    for directory in search_dirs or []:
        for pattern in CLANGD_PREBUILT_GLOBS:
            for match in sorted(glob.glob(str(Path(directory) / pattern)), reverse=True):
                if Path(match).is_file():
                    return match, version_of(match), f"AOSP prebuilts（{directory}）"
    return None, "", "未找到 clangd：可设置 navigation.clangdPath，或把 AOSP 根目录加入 navigation.searchDirs"


class LanguageServerManager:
    def __init__(self, navigation_config, root_configs) -> None:
        self.config = navigation_config
        self.root_configs = {root.id: root for root in root_configs}
        self._handles: dict[str, ServerHandle] = {}
        self._lock = asyncio.Lock()
        self._clangd: tuple[str | None, str, str] | None = None
        self.start_count = 0
        self.stop_count = 0

    # ------------------------------------------------------------------ 发现

    def clangd(self) -> tuple[str | None, str, str]:
        if self._clangd is None:
            search_dirs = [Path(item).expanduser() for item in self.config.search_dirs]
            search_dirs += [root.path for root in self.root_configs.values()]
            self._clangd = find_clangd(self.config.clangd_path, search_dirs)
        return self._clangd

    def spec_for(self, language: str, workspace_id: str) -> LanguageServerSpec | None:
        if language not in ("c", "cpp"):
            return None
        executable, version, source = self.clangd()
        if not executable:
            return None
        args = [executable, f"--log={self.config.clangd_log_level}"]
        args.append("--background-index" if self.config.background_index else "--background-index=0")
        args.append(f"--limit-results={self.config.max_results}")
        args.append("--header-insertion=never")
        args.append(f"--pch-storage={self.config.pch_storage}")
        compile_dir = self.config.compile_commands_dir
        root = self.root_configs.get(workspace_id)
        if root is not None and root.compile_commands_dir:
            compile_dir = root.compile_commands_dir
        if compile_dir:
            args.append(f"--compile-commands-dir={Path(compile_dir).expanduser()}")
        args.extend(self.config.clangd_args)
        display = f"clangd {version}".strip()
        return LanguageServerSpec(language=language, command=args, display_name=display, source=source)

    # ------------------------------------------------------------------ 实例管理

    async def acquire(self, *, workspace_id: str, language: str) -> ServerHandle:
        spec = self.spec_for(language, workspace_id)
        if spec is None:
            raise LspProcessError("没有可用的语言服务（clangd 未找到或该语言未接入）")
        key = f"{workspace_id}:{language}"
        async with self._lock:
            await self._sweep_locked()
            handle = self._handles.get(key)
            if handle is not None and handle.client.alive:
                handle.touch()
                return handle
            if handle is not None:  # 进程已经死了，清掉重建
                self._handles.pop(key, None)
                self.stop_count += 1
            await self._enforce_limit_locked(exclude=key)
            root = self.root_configs[workspace_id]
            client = LspClient(
                spec.command,
                cwd=root.path,
                name=spec.display_name or f"{language}-server",
                stderr_limit=self.config.stderr_lines,
            )
            await client.start()
            try:
                result = await client.request(
                    "initialize",
                    {
                        "processId": None,
                        "rootUri": root.path.as_uri(),
                        "workspaceFolders": [{"uri": root.path.as_uri(), "name": workspace_id}],
                        "capabilities": {
                            "textDocument": {
                                "definition": {"linkSupport": False},
                                "declaration": {"linkSupport": False},
                                "references": {},
                                "synchronization": {"didSave": False},
                            },
                            "workspace": {"configuration": False, "workspaceFolders": True},
                        },
                        "initializationOptions": spec.initialization_options,
                    },
                    timeout=self.config.initialize_timeout_seconds,
                )
            except Exception:
                await client.shutdown()
                raise
            client.notify("initialized", {})
            handle = ServerHandle(spec, client, workspace_id, root.path)
            handle.capabilities = (result or {}).get("capabilities", {})
            self._handles[key] = handle
            self.start_count += 1
            return handle

    async def release(self, handle: ServerHandle) -> None:
        handle.touch()

    async def sweep_idle(self) -> list[str]:
        async with self._lock:
            return await self._sweep_locked()

    async def _sweep_locked(self) -> list[str]:
        closed: list[str] = []
        for key, handle in list(self._handles.items()):
            if handle.idle_seconds() >= self.config.idle_shutdown_seconds:
                await self._shutdown_handle(key, handle)
                closed.append(key)
        return closed

    async def _enforce_limit_locked(self, *, exclude: str) -> None:
        while len([key for key in self._handles if key != exclude]) >= self.config.max_instances:
            oldest_key = min(
                (key for key in self._handles if key != exclude),
                key=lambda key: self._handles[key].last_used,
            )
            await self._shutdown_handle(oldest_key, self._handles[oldest_key])

    async def _shutdown_handle(self, key: str, handle: ServerHandle) -> None:
        self._handles.pop(key, None)
        try:
            await handle.client.shutdown()
        finally:
            self.stop_count += 1

    async def shutdown_all(self) -> None:
        async with self._lock:
            for key, handle in list(self._handles.items()):
                await self._shutdown_handle(key, handle)

    # ------------------------------------------------------------------ 状态

    def status(self) -> dict:
        executable, version, source = self.clangd()
        return {
            "enabled": self.config.enabled,
            "instances": [
                {
                    "key": handle.key,
                    "language": handle.spec.language,
                    "workspaceId": handle.workspace_id,
                    "command": handle.spec.command,
                    "pid": handle.client._process.pid if handle.client._process else None,
                    "uptimeSeconds": round(time.monotonic() - handle.started_at, 1),
                    "idleSeconds": round(handle.idle_seconds(), 1),
                    "openDocuments": len(handle.documents),
                    "requests": handle.request_count,
                }
                for handle in self._handles.values()
            ],
            "clangd": {"available": executable is not None, "path": executable, "version": version, "source": source},
            "limits": {
                "maxInstances": self.config.max_instances,
                "idleShutdownSeconds": self.config.idle_shutdown_seconds,
                "requestTimeoutSeconds": self.config.request_timeout_seconds,
                "backgroundIndex": self.config.background_index,
            },
            "counters": {"started": self.start_count, "stopped": self.stop_count},
        }
