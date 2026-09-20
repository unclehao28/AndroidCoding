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
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .client import LspClient, LspError, LspProcessError, is_document_not_ready


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

    def has_document(self, uri: str) -> bool:
        return uri in self.documents

    async def warm_up_document(self, uri: str, *, timeout: float = 10.0) -> str:
        """didOpen 之后确认语言服务真的收下了这个文档。

        实测 clangd 12.0.7：刚 didOpen 的文档如果立刻发 textDocument/definition，会回
        `trying to get AST for non-added document (code=-32602)`——第一次请求失败，第二次就正常。
        这里发一个同样需要 AST 的轻量请求（documentSymbol）并等它返回，把这段竞态消掉；
        这个请求本身也可能撞上同一个暂态错误，所以同样重试；都失败也不影响后续流程，
        真正的导航请求还有退避重试兜底。
        """
        attempt = 0
        while True:
            try:
                await self.client.request(
                    "textDocument/documentSymbol", {"textDocument": {"uri": uri}}, timeout=timeout
                )
                return "ok"
            except LspError as exc:
                if attempt >= len(WARM_UP_RETRY_DELAYS) or not is_document_not_ready(exc):
                    return f"未完成：{exc}"
                await asyncio.sleep(WARM_UP_RETRY_DELAYS[attempt])
                attempt += 1

    def close_document(self, uri: str) -> None:
        if uri in self.documents and self.client.alive:
            self.client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self.documents.pop(uri, None)


WARM_UP_RETRY_DELAYS = (0.2, 0.8)

JDTLS_CONFIG_DIRS = {
    "linux": "config_linux",
    "darwin": "config_mac",
    "win32": "config_win",
}
JDTLS_SEARCH_DIRS = (
    "~/jdtls",
    "~/jdt-language-server",
    "~/.local/share/jdtls",
    "~/.local/share/asw-jdtls",
    "~/bin/jdtls",
    "/opt/jdtls",
    "/opt/jdt-language-server",
)
JAVA_SEARCH_GLOBS = (
    "prebuilts/jdk/*/linux-x86/bin/java",
    "prebuilts/jdk/*/darwin-x86/bin/java",
    "prebuilts/jdk/*/*/bin/java",
    "/usr/lib/jvm/*/bin/java",
    "/usr/local/bin/java",
)
JDTLS_MIN_JAVA_FALLBACK = 21
JDTLS_REQUIRED_JAVA_PATTERNS = (
    re.compile(r"java_major_version\s*<\s*(\d+)"),        # bin/jdtls.py（新版包装脚本，Python）
    re.compile(r'JAVA_MAJOR_VERSION["\']?\s*-lt\s*(\d+)'),  # bin/jdtls（旧版 shell：["$JAVA_MAJOR_VERSION" -lt 11]）
    re.compile(r'"\$\{?JAVA_VERSION\}?"\s*-lt\s*(\d+)'),
    re.compile(r"requires at least Java\s*(\d+)"),
)


def jdtls_required_java(ls_dir) -> int | None:
    """从 JDT LS 自带的启动脚本里读出它要求的 JDK 主版本。

    最新快照要求 Java 21，而 AOSP12 自带的是 prebuilts/jdk/jdk11——版本不匹配时
    JDT LS 会直接退出，所以这里先读清楚，避免用户去猜。
    """
    if not ls_dir:
        return None
    base = Path(ls_dir)
    for name in ("jdtls.py", "jdtls", "jdtls.bat"):
        script = base / "bin" / name
        if not script.is_file():
            continue
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in JDTLS_REQUIRED_JAVA_PATTERNS:
            match = pattern.search(text)
            if match:
                return int(match.group(1))
    return None


def jdtls_config_dir(ls_dir) -> str | None:
    """返回该平台上要用的 JDT LS 配置目录（config_linux / config_win / config_mac）。"""
    if not ls_dir:
        return None
    base = Path(ls_dir)
    if sys.platform.startswith("linux"):
        preferred = "config_linux"
    elif sys.platform == "darwin":
        preferred = "config_mac"
    elif sys.platform.startswith("win"):
        preferred = "config_win"
    else:
        preferred = None
    candidates = [preferred] if preferred else []
    candidates += [name for name in sorted(JDTLS_CONFIG_DIRS.values())]
    for name in candidates:
        if name and (base / name).is_dir():
            return str(base / name)
    return None


def jdtls_launcher(ls_dir) -> str | None:
    if not ls_dir:
        return None
    plugins = Path(ls_dir) / "plugins"
    if (plugins / "org.eclipse.equinox.launcher.jar").is_file():
        return str(plugins / "org.eclipse.equinox.launcher.jar")  # mason-registry 打包
    for match in sorted(plugins.glob("org.eclipse.equinox.launcher_*.jar"), reverse=True):
        return str(match)
    return None


def find_jdtls(configured: str | None, extra_dirs=None) -> tuple:
    """返回 (JDT LS 解包目录, 说明)。找不到时第一个元素为 None，第二个元素是原因。"""
    candidates: list = []
    if configured:
        candidates.append(Path(os.path.expanduser(configured)))
    env_home = os.environ.get("JDTLS_HOME")
    if env_home:
        candidates.append(Path(os.path.expanduser(env_home)))
    candidates += [Path(os.path.expanduser(item)) for item in JDTLS_SEARCH_DIRS]
    for item in extra_dirs or []:
        candidates.append(Path(os.path.expanduser(str(item))))
    for path in candidates:
        if not path.is_dir():
            continue
        if jdtls_launcher(str(path)) and jdtls_config_dir(str(path)):
            return str(path), f"JDT LS 目录：{path}"
    if configured:
        return None, f"配置的 navigation.javaLsPath 不像可用的 JDT LS 解包目录：{configured}"
    return None, "未找到 JDT LS（可设置 navigation.javaLsPath，或运行 scripts/setup-java.py 自动下载）"


def java_major_version(executable: str) -> int | None:
    try:
        completed = subprocess.run([executable, "-version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r'version\s+"(\d+)(?:\.(\d+))?', text)
    if not match:
        return None
    major = int(match.group(1))
    return 1 if major == 1 and match.group(2) else major  # 1.8 → 8


def find_java(configured: str | None, extra_dirs=None) -> tuple:
    """返回 (java 可执行文件, JDK 版本, 说明)。"""
    candidates: list = []
    if configured:
        base = Path(os.path.expanduser(configured))
        candidates.append(base / "bin" / ("java.exe" if sys.platform.startswith("win") else "java"))
        candidates.append(base)
    env_home = os.environ.get("JAVA_HOME")
    if env_home:
        candidates.append(
            Path(env_home) / "bin" / ("java.exe" if sys.platform.startswith("win") else "java")
        )
    located = shutil.which("java")
    if located:
        candidates.append(Path(located))
    for directory in extra_dirs or []:
        for pattern in JAVA_SEARCH_GLOBS:
            candidates += [Path(item) for item in sorted(glob.glob(str(Path(directory) / pattern)), reverse=True)]
    for path in candidates:
        if not Path(path).is_file():
            continue
        version = java_major_version(str(path))
        if version is None:
            continue
        return str(path), version, f"JDK {version}：{path}"
    if configured:
        return None, 0, f"配置的 navigation.javaHome 下没有可用的 java：{configured}"
    return None, 0, "未找到可用的 JDK（可设置 navigation.javaHome，或把 java 放进 PATH）"


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
        self._java_status: dict | None = None
        self.start_count = 0
        self.stop_count = 0

    # ------------------------------------------------------------------ 发现

    def clangd(self) -> tuple[str | None, str, str]:
        if self._clangd is None:
            search_dirs = [Path(item).expanduser() for item in self.config.search_dirs]
            search_dirs += [root.path for root in self.root_configs.values()]
            self._clangd = find_clangd(self.config.clangd_path, search_dirs)
        return self._clangd

    def java_status(self) -> dict:
        """Java 侧就绪状态（含不就绪的具体原因，供导航接口如实上报）。"""
        if self._java_status is None:
            search_dirs = [Path(item).expanduser() for item in self.config.search_dirs]
            search_dirs += [root.path for root in self.root_configs.values()]
            ls_dir, ls_note = find_jdtls(self.config.java_ls_path, search_dirs)
            java, java_version, java_note = find_java(self.config.java_home, search_dirs)
            required = jdtls_required_java(ls_dir)
            reason = ""
            available = bool(ls_dir and java)
            if not ls_dir:
                reason = ls_note
            elif not java:
                reason = java_note
            elif required and java_version < required:
                available = False
                reason = (
                    f"JDK 版本不足：这套 JDT LS 要求 Java {required}+，当前 java 是 {java_version}"
                    f"（{java}）。AOSP 自带的 prebuilts/jdk/jdk11 通常是 11，需要单独准备 JDK"
                )
            self._java_status = {
                "available": available,
                "reason": reason,
                "lsPath": ls_dir,
                "lsNote": ls_note,
                "requiredJava": required,
                "javaPath": java,
                "javaVersion": java_version,
                "javaNote": java_note,
                "dataDir": self.config.java_data_dir or str(
                    Path(tempfile.gettempdir()) / "asw-jdtls" / f"ws-{self._java_data_key()}"
                ),
            }
        return self._java_status

    def _java_data_key(self) -> str:
        roots = sorted(str(root.path) for root in self.root_configs.values())
        return hashlib.sha1("|".join(roots).encode("utf-8")).hexdigest()[:12] if roots else "default"

    def _java_spec(self) -> LanguageServerSpec | None:
        status = self.java_status()
        if not status["available"]:
            return None
        ls_dir = status["lsPath"]
        launcher = jdtls_launcher(ls_dir)
        config_dir = jdtls_config_dir(ls_dir)
        data_dir = Path(status["dataDir"])
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # 参数按 JDT LS 自带 bin/jdtls 脚本的写法来（-Dosgi.* / --add-opens 一项都不能少）
        args = [
            status["javaPath"],
            "-Declipse.application=org.eclipse.jdt.ls.core.id1",
            "-Dosgi.bundles.defaultStartLevel=4",
            "-Declipse.product=org.eclipse.jdt.ls.core.product",
            "-Dosgi.checkConfiguration=true",
            f"-Dosgi.sharedConfiguration.area={config_dir}",
            "-Dosgi.sharedConfiguration.area.readOnly=true",
            "-Dosgi.configuration.cascaded=true",
            f"-Dlog.level={self.config.clangd_log_level}",
            "-Xms256m",
            "-Xmx1G",
            "--add-modules=ALL-SYSTEM",
            "--add-opens",
            "java.base/java.util=ALL-UNNAMED",
            "--add-opens",
            "java.base/java.lang=ALL-UNNAMED",
            "-jar",
            launcher,
            "-data",
            str(data_dir),
        ]
        args.extend(self.config.java_args)
        return LanguageServerSpec(
            language="java",
            command=args,
            display_name=f"JDT LS（JDK {status['javaVersion']}）",
            source=status["lsNote"],
            initialization_options={
                "settings": {
                    "java": {
                        # AOSP 用的是 Soong，不是 Maven/Gradle：默认关掉这两个导入器，
                        # 避免 JDT LS 去跑不存在的构建工具，把失败信息说清楚由 P2 后半的适配来做。
                        "import": {"maven": {"enabled": False}, "gradle": {"enabled": False}},
                        "autobuild": {"enabled": False},
                        "signatureHelp": {"enabled": False},
                        "completion": {"enabled": True},
                        "references": {"includeDecompiledSources": False},
                    }
                },
                "extendedClientCapabilities": {"progressReportProvider": False, "classFileContentsSupport": False},
            },
        )

    def spec_for(self, language: str, workspace_id: str) -> LanguageServerSpec | None:
        if language == "java":
            return self._java_spec()
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
