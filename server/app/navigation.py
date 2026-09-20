"""语义跳转服务：把请求变成真实的 LSP 请求，再把真实结果如实映射回契约。

诚实性要求（不要为了"看起来能用"而降低标准）：
- 目标位置一律来自语言服务返回的 Location/LocationLink，不做同名符号猜测；
- 结果为空、编译参数缺失、语言未接入、文件已变化，都如实返回 unavailable/stale 并给出原因；
- 多个合法目标返回 ambiguous 并列出来让用户选，绝不默认取第一个；
- 返回里始终带上请求时的源码版本与当前版本，便于上层判断是否 stale。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import AppConfig, RootConfig
from .lsp.client import LspError, LspProcessError, LspTimeoutError, is_document_not_ready
from .lsp.manager import LanguageServerManager, ServerHandle
from .lsp.positions import make_range, split_lines, word_at

STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNAVAILABLE = "unavailable"
STATUS_STALE = "stale"
KIND_SEMANTIC = "semantic"

CPP_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".h++", ".inl", ".ipp", ".m", ".mm")
LANGUAGE_LABELS = {"cpp": "C/C++ ", "java": "Java "}
LANGUAGE_HINTS = {
    "cpp": [
        "设置 navigation.clangdPath 指向 clangd，或把 AOSP 根目录加入 navigation.searchDirs（可使用 "
        "prebuilts/clang/host/linux-x86/*/bin/clangd）"
    ],
    "java": [
        "设置 navigation.javaLsPath 指向 JDT LS 解包目录（可用 scripts/setup-java.py 自动下载），"
        "必要时用 navigation.javaHome 指定 JDK 21+/17+ 的路径：JDT LS 明确要求较新的 JDK，"
        "AOSP 自带的 prebuilts/jdk/jdk11 通常不够",
        "Soong 不是 Maven/Gradle：JDT LS 无法直接导入 AOSP 工程，跨模块解析需要单独的导入适配（未完成）",
    ],
}
KNOWN_UNSUPPORTED = {
    ".kt": "Kotlin：需要独立的 Kotlin 语言服务（未接入）",
    ".kts": "Kotlin：未接入",
    ".aidl": "AIDL：未接入（AIDL 编译器产物与接口语义属于后续工作）",
    ".rs": "Rust：需要 rust-analyzer（未接入）",
    ".py": "Python：未在支持矩阵中",
    ".js": "JavaScript/TypeScript：未在支持矩阵中",
    ".ts": "JavaScript/TypeScript：未在支持矩阵中",
    ".go": "Go：未接入",
    ".xml": "XML/配置：不属于语义跳转范围",
    ".bp": "Soong 构建文件：不属于语义跳转范围",
    ".mk": "Makefile：不属于语义跳转范围",
    ".rc": "init rc：不属于语义跳转范围",
}
METHOD_BY_KIND = {
    "definition": "textDocument/definition",
    "declaration": "textDocument/declaration",
    "references": "textDocument/references",
}
EVIDENCE_LIMIT = 200
MAX_SOURCE_BYTES = 4 * 1024 * 1024
# "文档还没就绪"时的退避间隔（秒）：实测第一次重试就够了，多留一次以防索引线程正好在忙
NOT_READY_RETRY_DELAYS = (0.25, 1.0)


@dataclass
class SourceSnapshot:
    text: str
    lines: list[str]
    version: str
    size: int

    @staticmethod
    def read(path: Path) -> "SourceSnapshot":
        raw = path.read_bytes()
        text = raw.decode("utf-8", "replace")
        if "\r\n" in text:
            text = text.replace("\r\n", "\n")
        return SourceSnapshot(
            text=text,
            lines=split_lines(text),
            version="sha256:" + hashlib.sha256(raw).hexdigest(),
            size=len(raw),
        )


def uri_to_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]  # Windows: /C:/... → C:/...
    return Path(path)


class NavigationService:
    def __init__(self, config: AppConfig, manager: LanguageServerManager) -> None:
        self.config = config
        self.manager = manager

    # ------------------------------------------------------------------ 状态

    @property
    def enabled(self) -> bool:
        return bool(self.config.features.navigation and self.config.navigation.enabled)

    def status(self) -> dict:
        manager_status = self.manager.status()
        executable, version, source = self.manager.clangd()
        java = self.manager.java_status()
        return {
            "enabled": self.enabled,
            "featureFlag": self.config.features.navigation,
            "supported": {
                "cpp": {
                    "available": executable is not None and self.enabled,
                    "server": f"clangd {version}".strip() if executable else None,
                    "path": executable,
                    "source": source,
                    "languages": ["C", "C++"],
                    "positionUnit": "utf-16",
                    "kinds": ["definition", "declaration", "references"],
                    "engine": "clangd",
                },
                "java": {
                    "available": bool(java["available"] and self.enabled),
                    "server": (
                        f"JDT LS（JDK {java['javaVersion']}）" if java["lsPath"] and java["javaPath"] else None
                    ),
                    "path": java["lsPath"],
                    "javaPath": java["javaPath"],
                    "javaVersion": java["javaVersion"] or None,
                    "requiredJava": java["requiredJava"],
                    "source": java["lsNote"],
                    "reason": java["reason"] or None,
                    "languages": ["Java"],
                    "positionUnit": "utf-16",
                    "kinds": ["definition", "declaration", "references"],
                    "engine": "jdt.ls",
                    "classpathSupport": False,
                    "note": "Soong 工程导入适配未完成：同文件/同源码目录内的解析可用，跨模块依赖可能为空",
                },
            },
            "notImplemented": {
                suffix: reason for suffix, reason in sorted(KNOWN_UNSUPPORTED.items())
            },
            "manager": manager_status,
            "note": "语义能力按语言单独记录：clangd 就绪不等于 Java/AIDL/Rust 就绪",
        }

    # ------------------------------------------------------------------ 主流程

    async def navigate(
        self,
        *,
        root: RootConfig,
        rel_path: str,
        resolved_path: Path,
        position: dict,
        kind: str = "definition",
        source_version: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        suffix = resolved_path.suffix.lower()
        base: dict[str, Any] = {
            "kind": KIND_SEMANTIC,
            "workspaceId": root.id,
            "path": rel_path,
            "requestedKind": kind,
            "position": position,
            "positionUnit": "utf-16",
            "lineBase": 0,
            "sourceVersion": source_version,
            "symbol": None,
            "targets": [],
            "targetCount": 0,
            "reason": "",
            "hints": [],
        }

        if not self.enabled:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = (
                "语义跳转已在配置中关闭"
                if not self.config.features.navigation
                else "navigation.enabled=false：语义跳转已在配置中关闭"
            )
            return self._finish(base, started)

        if suffix in CPP_SUFFIXES:
            language = "cpp"
        elif suffix == ".java":
            language = "java"
        else:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = KNOWN_UNSUPPORTED.get(
                suffix, f"未识别的文件类型（{suffix or '无扩展名'}），语义跳转未接入"
            )
            base["hints"].append(
                "C/C++ 已接入 clangd、Java 已接入 JDT LS；其他语言的能力在 /api/health 的 "
                "navigation.notImplemented 中列出"
            )
            return self._finish(base, started)

        if kind not in METHOD_BY_KIND:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = f"不支持的导航类型：{kind}（可用：{', '.join(METHOD_BY_KIND)}）"
            return self._finish(base, started)

        try:
            snapshot = SourceSnapshot.read(resolved_path)
        except OSError as exc:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = f"读取文件失败：{exc}"
            return self._finish(base, started)

        base["currentVersion"] = snapshot.version
        base["size"] = snapshot.size
        base["totalLines"] = len(snapshot.lines)
        if snapshot.size > MAX_SOURCE_BYTES:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = f"文件超过 {MAX_SOURCE_BYTES} 字节，暂不送交语言服务"
            return self._finish(base, started)

        if source_version and source_version != snapshot.version:
            base["status"] = STATUS_STALE
            base["reason"] = (
                "请求携带的源码版本与服务器当前文件不一致：可能已被其他程序修改或你的页面是旧版本，"
                "请刷新文件后重新发起跳转"
            )
            return self._finish(base, started)

        line = int(position.get("line", 0) or 0)
        character = int(position.get("character", 0) or 0)
        if line < 0 or line >= len(snapshot.lines):
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = f"位置超出文件范围：line={line}，文件共 {len(snapshot.lines)} 行"
            return self._finish(base, started)

        word = word_at(snapshot.lines, line, character)
        base["symbol"] = word["word"] if word else None
        base["symbolRange"] = (
            make_range(line, word["start"], line, word["end"]) if word else None
        )

        if language == "java":
            java = self.manager.java_status()
            if not java["available"]:
                base["status"] = STATUS_UNAVAILABLE
                base["reason"] = f"Java 语义跳转不可用：{java['reason']}"
                base["hints"].extend(LANGUAGE_HINTS["java"])
                return self._finish(base, started)

        try:
            handle = await self.manager.acquire(workspace_id=root.id, language=language)
        except LspProcessError as exc:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = f"{LANGUAGE_LABELS[language].strip()} 语言服务不可用：{exc}"
            base["hints"].extend(LANGUAGE_HINTS[language])
            return self._finish(base, started)

        base["server"] = {
            "name": handle.spec.display_name,
            "language": language,
            "source": handle.spec.source,
            "workspaceId": handle.workspace_id,
        }

        document_language_id = "java" if language == "java" else "cpp"
        uri = resolved_path.as_uri()
        first_open = not handle.has_document(uri)
        document_version = handle.open_document(uri, document_language_id, snapshot.text, version=1)
        base["documentVersion"] = document_version
        if first_open:
            # 刚打开的文档先确认语言服务已经收下，避免第一次请求必然失败（见 warm_up_document 注释）
            warm = await handle.warm_up_document(
                uri, timeout=min(self.config.navigation.request_timeout_seconds, 10)
            )
            base["documentWarmUp"] = warm
            if warm != "ok":
                base["hints"].append(f"文档就绪确认未成功（{warm}），已按重试方式继续")

        params: dict[str, Any] = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        if kind == "references":
            params["context"] = {"includeDeclaration": True}

        method = METHOD_BY_KIND[kind]
        handle.request_count += 1
        try:
            raw = await self._request_navigation(handle, method, params)
        except LspTimeoutError as exc:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = f"语言服务超时：{exc}"
            base["hints"].append("首次请求可能正在加载编译参数；可调大 navigation.requestTimeoutSeconds")
            base["server"]["stderrTail"] = handle.client.stderr_tail(5)
            return self._finish(base, started)
        except LspProcessError as exc:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = f"语言服务异常：{exc}"
            return self._finish(base, started)
        except LspError as exc:
            base["status"] = STATUS_UNAVAILABLE
            base["reason"] = f"语言服务拒绝该请求：{exc}"
            base["hints"].append("clangd 可能不支持该导航类型，可改用 definition/references")
            return self._finish(base, started)
        finally:
            await self.manager.release(handle)


        targets = self._normalize_targets(raw, root, kind)
        base["targets"] = targets
        base["targetCount"] = len(targets)

        if not targets:
            if kind == "references":
                base["status"] = STATUS_RESOLVED
                base["reason"] = "语言服务没有返回引用（注释与字符串中的同名文本不算引用）"
            else:
                base["status"] = STATUS_UNAVAILABLE
                base["reason"] = "语言服务没有返回目标位置"
                if not (handle.root / "compile_commands.json").exists() and not self._compile_db_dir(handle):
                    base["hints"].append(
                        "当前工作区没有 compile_commands.json：跨文件/宏相关的跳转可能不准或为空；"
                        "AOSP 可用 Soong 的 compdb 生成后指向 navigation.compileCommandsDir 或 roots[].compileCommandsDir"
                    )
                base["hints"].append("也可能是该位置不是标识符，或该符号在本编译单元内不可解析")
                base["server"]["stderrTail"] = handle.client.stderr_tail(5)
            return self._finish(base, started)

        if kind != "references" and len(targets) > 1:
            base["status"] = STATUS_AMBIGUOUS
            base["reason"] = f"语言服务返回 {len(targets)} 个候选目标，请选择期望的那个（工作台不会默认取第一个）"
            return self._finish(base, started)

        base["status"] = STATUS_RESOLVED
        base["reason"] = f"语言服务返回 {len(targets)} 个目标"
        return self._finish(base, started)

    # ------------------------------------------------------------------ 结果映射

    def _compile_db_dir(self, handle: ServerHandle) -> str | None:
        for arg in handle.spec.command:
            if arg.startswith("--compile-commands-dir="):
                return arg.split("=", 1)[1]
        return None

    async def _request_navigation(self, handle, method: str, params: dict[str, Any]) -> Any:
        """发导航请求，并在"文档尚未就绪"这类可重试错误上退避重试。

        实测 clangd 12.0.7 会在 didOpen 之后立刻拒绝第一次请求（-32602 non-added document）；
        重试一次即可成功。其它错误原样抛出，交给上层如实上报——不能把它变成"没有结果"。
        导航请求是只读且幂等的，重试安全。
        """
        timeout = self.config.navigation.request_timeout_seconds
        attempt = 0
        while True:
            try:
                return await handle.client.request(method, params, timeout=timeout)
            except LspError as exc:
                if attempt >= len(NOT_READY_RETRY_DELAYS) or not is_document_not_ready(exc):
                    raise
                await asyncio.sleep(NOT_READY_RETRY_DELAYS[attempt])
                attempt += 1

    def _normalize_targets(self, raw: Any, root: RootConfig, kind: str) -> list[dict]:
        entries: list[Any] = []
        if raw is None:
            entries = []
        elif isinstance(raw, list):
            entries = raw
        elif isinstance(raw, dict):
            entries = [raw]
        targets: list[dict] = []
        seen: set[tuple[str, int, int]] = set()
        root_resolved = root.path.resolve()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("targetUri") or entry.get("uri")
            if not isinstance(uri, str):
                continue
            path = uri_to_path(uri)
            if path is None:
                continue
            rng = entry.get("targetSelectionRange") or entry.get("targetRange") or entry.get("range") or {}
            start = rng.get("start") or {}
            end = rng.get("end") or {}
            start_line = int(start.get("line", 0) or 0)
            start_char = int(start.get("character", 0) or 0)
            end_line = int(end.get("line", start_line) or start_line)
            end_char = int(end.get("character", start_char) or start_char)
            try:
                resolved = path.resolve()
                relative = resolved.relative_to(root_resolved).as_posix()
                inside = True
            except (ValueError, OSError):
                relative = str(path)
                resolved = path
                inside = False
            key = (relative, start_line, start_char)
            if key in seen:
                continue
            seen.add(key)
            target: dict[str, Any] = {
                "path": relative,
                "outsideRoot": not inside,
                "kind": "reference" if kind == "references" else kind,
                "range": make_range(start_line, start_char, end_line, end_char),
                "evidence": f"语言服务（{kind}）返回的位置",
            }
            if inside:
                snippet = self._line_snippet(resolved, start_line)
                if snippet:
                    target["evidence"] = f"L{start_line + 1}: {snippet}"
            targets.append(target)
        return targets

    @staticmethod
    def _line_snippet(path: Path, line: int) -> str:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for index, text in enumerate(handle):
                    if index == line:
                        return text.strip()[:EVIDENCE_LIMIT]
        except OSError:
            return ""
        return ""

    @staticmethod
    def _finish(payload: dict[str, Any], started: float) -> dict[str, Any]:
        payload.setdefault("status", STATUS_UNAVAILABLE)
        payload["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
        return payload
