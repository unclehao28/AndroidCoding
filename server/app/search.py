"""全文/路径搜索。

两个引擎：
- ripgrep：存在 `rg` 时优先使用。参数以数组形式传给进程，绝不拼接 shell 字符串。
- python-bounded：受限的纯 Python 扫描，仅在缺少 `rg` 时兜底；有文件数、大小、时间上限。

共同约束：并发上限、单次超时、结果条数上限、可取消。
P1 不声称已经解决全库索引问题；这里只是受限的检索，超大 checkouts 的索引方案属于 P3。
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig, RootConfig
from .errors import invalid_request
from .pathtools import decode_source

STATUS_OK = "ok"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "error"
STATUS_BUSY = "busy"


@dataclass
class SearchRequest:
    request_id: str
    root: RootConfig
    query: str
    start_dir: Path
    subpath: str = ""
    literal: bool = True
    case_sensitive: bool = False
    whole_word: bool = False
    include_glob: str | None = None
    limit: int = 500
    timeout_seconds: float = 10.0
    max_file_bytes: int = 1024 * 1024
    max_line_length: int = 400
    exclude_globs: tuple[str, ...] = ()
    respect_ignore_files: bool = True
    python_max_files: int = 20000
    rg_threads: int = 2


@dataclass
class SearchOutcome:
    status: str
    engine: str
    matches: list[dict] = field(default_factory=list)
    truncated: bool = False
    files_scanned: int | None = None
    skipped: dict = field(default_factory=dict)
    reason: str = ""

    def to_payload(self) -> dict:
        return {
            "status": self.status,
            "engine": self.engine,
            "matches": self.matches,
            "matchCount": len(self.matches),
            "filesScanned": self.files_scanned,
            "filesMatched": len({item["path"] for item in self.matches}),
            "truncated": self.truncated,
            "skipped": self.skipped,
            "reason": self.reason,
        }


def _display_text(text: str, max_length: int) -> tuple[str, bool]:
    if len(text) <= max_length:
        return text, False
    return text[:max_length], True


def _match_payload(*, rel_path: str, line0: int, column0: int, text: str, max_line_length: int, source: str) -> dict:
    display, truncated = _display_text(text, max_line_length)
    return {
        "path": rel_path,
        "line": line0,
        "lineDisplay": line0 + 1,
        "column": column0,
        "text": display,
        "textTruncated": truncated,
        "source": source,
    }


class SearchEngine:
    kind = "unknown"
    name = "unknown"
    version = ""

    async def search(self, request: SearchRequest, cancel: threading.Event) -> SearchOutcome:  # pragma: no cover
        raise NotImplementedError

    def info(self) -> dict:
        return {"name": self.name, "version": self.version, "kind": self.kind}


def detect_ripgrep() -> tuple[str, str] | None:
    path = shutil.which("rg")
    if not path:
        return None
    try:
        completed = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001 - 探测失败按不可用处理
        return None
    if completed.returncode != 0:
        return None
    first = (completed.stdout or "").splitlines()
    version = first[0].split()[-1] if first else "unknown"
    return path, version


class RipgrepEngine(SearchEngine):
    kind = "external-ripgrep"

    def __init__(self, path: str, version: str):
        self.path = path
        self.version = version
        self.name = "ripgrep"

    def build_args(self, request: SearchRequest) -> list[str]:
        args = [
            self.path,
            "--json",
            "--no-config",
            "--no-messages",
            "--color",
            "never",
            "--threads",
            str(max(1, request.rg_threads)),
            "--max-filesize",
            str(request.max_file_bytes),
        ]
        if not request.respect_ignore_files:
            # 默认由 rg 尊重 .gitignore/.ignore；配置关闭时才显式忽略它们
            args += ["--no-ignore", "--hidden"]
        # 正则模式交给 rg 自己的 Rust regex 引擎校验，不在这里预判
        if request.literal:
            args.append("--fixed-strings")
        if not request.case_sensitive:
            args.append("--ignore-case")
        if request.whole_word:
            args.append("--word-regexp")
        for pattern in request.exclude_globs:
            args += ["--glob", f"!{pattern}"]
        if request.include_glob:
            args += ["--glob", request.include_glob]
        target = request.subpath or "."
        args += ["--", request.query, target]
        return args

    async def search(self, request: SearchRequest, cancel: threading.Event) -> SearchOutcome:
        args = self.build_args(request)
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(request.root.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        engine_label = f"ripgrep {self.version}"
        matches: list[dict] = []
        truncated = False

        async def consume() -> None:
            nonlocal truncated
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                if cancel.is_set():
                    return
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if event.get("type") != "match":
                    continue
                data = event.get("data", {})
                text = data.get("lines", {}).get("text")
                if text is None:
                    text = data.get("lines", {}).get("bytes", "")
                if isinstance(text, bytes):
                    text = text.decode("utf-8", "replace")
                text = text.rstrip("\r\n")
                submatches = data.get("submatches") or [{}]
                byte_offset = int(submatches[0].get("start", 0))
                try:
                    column0 = len(text.encode("utf-8", "replace")[:byte_offset].decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    column0 = byte_offset
                raw_path = data.get("path", {}).get("text") or data.get("path", {}).get("bytes", "")
                if isinstance(raw_path, bytes):
                    raw_path = raw_path.decode("utf-8", "replace")
                rel = os.path.relpath(os.path.join(str(request.root.path), raw_path), str(request.root.path))
                matches.append(
                    _match_payload(
                        rel_path=rel.replace(os.sep, "/"),
                        line0=int(data.get("line_number", 1)) - 1,
                        column0=column0,
                        text=text,
                        max_line_length=request.max_line_length,
                        source="ripgrep",
                    )
                )
                if len(matches) >= request.limit:
                    truncated = True
                    return

        timed_out = False
        try:
            await asyncio.wait_for(consume(), timeout=request.timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
        except Exception as exc:  # noqa: BLE001
            await _terminate(process)
            return SearchOutcome(STATUS_ERROR, engine_label, matches, False, None, {}, f"检索进程异常：{exc}")

        if cancel.is_set() and not timed_out:
            await _terminate(process)
            return SearchOutcome(STATUS_CANCELLED, engine_label, matches, False, None, {}, "已被客户端取消")
        if timed_out:
            await _terminate(process)
            return SearchOutcome(
                STATUS_TIMEOUT,
                engine_label,
                matches,
                True,
                None,
                {},
                f"超过 {request.timeout_seconds:g} 秒上限，已终止检索进程",
            )
        if truncated:
            await _terminate(process)

        stderr_text = b""
        try:
            _, stderr_text = await asyncio.wait_for(process.communicate(), timeout=5)
        except (asyncio.TimeoutError, ValueError):
            pass
        code = process.returncode
        message = (stderr_text or b"").decode("utf-8", "replace").strip()
        if matches and truncated:
            return SearchOutcome(STATUS_OK, engine_label, matches, True, None, {}, "结果达到上限后提前结束")
        if code in (0, 1):
            return SearchOutcome(STATUS_OK, engine_label, matches, False, None, {}, message)
        return SearchOutcome(STATUS_ERROR, engine_label, matches, False, None, {}, message or f"rg 退出码 {code}")


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=3)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass


class PythonBoundedEngine(SearchEngine):
    """受限的纯 Python 扫描，只作为缺少 rg 时的兜底。"""

    kind = "python-bounded"
    name = "python-bounded"
    version = "1"

    async def search(self, request: SearchRequest, cancel: threading.Event) -> SearchOutcome:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._scan, request, cancel),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError:
            # 线程无法强杀：置位取消标志，让扫描在下一次检查点退出
            cancel.set()
            return SearchOutcome(
                STATUS_TIMEOUT,
                self.name,
                [],
                True,
                None,
                {},
                f"超过 {request.timeout_seconds:g} 秒上限，已请求停止扫描（Python 扫描线程无法强制中断）",
            )
        except Exception as exc:  # noqa: BLE001
            return SearchOutcome(STATUS_ERROR, self.name, [], False, None, {}, f"扫描失败：{exc}")

    def _scan(self, request: SearchRequest, cancel: threading.Event) -> SearchOutcome:
        started = time.monotonic()
        matches: list[dict] = []
        skipped = {"large": 0, "binary": 0, "undecodable": 0, "excludedDirs": 0}
        files_scanned = 0
        truncated = False
        scanned_truncated = False

        def excluded(rel_path: str, name: str) -> bool:
            for pattern in request.exclude_globs:
                candidate = pattern[:-3] if pattern.endswith("/**") else pattern
                if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, candidate):
                    return True
            return False

        if request.literal:
            needle = request.query if request.case_sensitive else request.query.lower()
            regex = None
        else:
            flags = 0 if request.case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(request.query, flags)
            except re.error as exc:
                return SearchOutcome(STATUS_ERROR, self.name, [], False, 0, skipped, f"正则表达式无效：{exc}")
            needle = ""

        word_boundary = re.compile(r"[A-Za-z0-9_]")

        for current_dir, dirnames, filenames in os.walk(request.start_dir, followlinks=False):
            if cancel.is_set():
                return SearchOutcome(STATUS_CANCELLED, self.name, matches, truncated, files_scanned, skipped, "已被客户端取消")
            rel_dir = os.path.relpath(current_dir, str(request.root.path))
            rel_dir = "" if rel_dir == os.curdir else rel_dir.replace(os.sep, "/")
            kept: list[str] = []
            for name in sorted(dirnames):
                if request.respect_ignore_files and (name.startswith(".") or name in ("node_modules",)):
                    skipped["excludedDirs"] += 1
                    continue
                child_rel = f"{rel_dir}/{name}" if rel_dir else name
                if excluded(child_rel, name):
                    skipped["excludedDirs"] += 1
                    continue
                kept.append(name)
            dirnames[:] = kept

            for name in sorted(filenames):
                if cancel.is_set():
                    return SearchOutcome(
                        STATUS_CANCELLED, self.name, matches, truncated, files_scanned, skipped, "已被客户端取消"
                    )
                rel_path = f"{rel_dir}/{name}" if rel_dir else name
                if request.respect_ignore_files and name.startswith("."):
                    continue
                if excluded(rel_path, name):
                    continue
                if request.include_glob and not fnmatch.fnmatch(rel_path, request.include_glob):
                    continue
                if files_scanned >= request.python_max_files:
                    scanned_truncated = True
                    break
                full = os.path.join(current_dir, name)
                if os.path.islink(full):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if size > request.max_file_bytes:
                    skipped["large"] += 1
                    continue
                try:
                    with open(full, "rb") as handle:
                        raw = handle.read()
                except OSError:
                    skipped["undecodable"] += 1
                    continue
                if b"\x00" in raw[:8192]:
                    skipped["binary"] += 1
                    continue
                decoded = decode_source(raw)
                if not decoded.ok:
                    skipped["undecodable"] += 1
                    continue
                files_scanned += 1
                for index, line in enumerate(decoded.lines):
                    if index % 512 == 0 and cancel.is_set():
                        return SearchOutcome(
                            STATUS_CANCELLED, self.name, matches, truncated, files_scanned, skipped, "已被客户端取消"
                        )
                    if regex is not None:
                        found = regex.search(line)
                        if not found:
                            continue
                        column = found.start()
                    else:
                        haystack = line if request.case_sensitive else line.lower()
                        column = haystack.find(needle)
                        if column < 0:
                            continue
                        if request.whole_word:
                            before = line[column - 1] if column > 0 else ""
                            after_index = column + len(request.query)
                            after = line[after_index] if after_index < len(line) else ""
                            if (before and word_boundary.match(before)) or (after and word_boundary.match(after)):
                                continue
                    matches.append(
                        _match_payload(
                            rel_path=rel_path,
                            line0=index,
                            column0=column,
                            text=line,
                            max_line_length=request.max_line_length,
                            source="python-bounded",
                        )
                    )
                    if len(matches) >= request.limit:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated or scanned_truncated:
                break
            if time.monotonic() - started > request.timeout_seconds:
                return SearchOutcome(
                    STATUS_TIMEOUT, self.name, matches, True, files_scanned, skipped, "超过时间上限，已停止扫描"
                )

        reason = ""
        if truncated:
            reason = f"结果达到上限 {request.limit}，可能还有更多匹配"
        elif scanned_truncated:
            reason = f"扫描文件数达到上限 {request.python_max_files}，结果可能不完整"
        elif time.monotonic() - started > request.timeout_seconds:
            return SearchOutcome(STATUS_TIMEOUT, self.name, matches, True, files_scanned, skipped, "超过时间上限，已停止扫描")
        return SearchOutcome(STATUS_OK, self.name, matches, truncated or scanned_truncated, files_scanned, skipped, reason)


class UnavailableEngine(SearchEngine):
    kind = "unavailable"

    def __init__(self, reason: str):
        self.reason = reason
        self.name = "unavailable"

    async def search(self, request: SearchRequest, cancel: threading.Event) -> SearchOutcome:
        return SearchOutcome(STATUS_ERROR, self.name, [], False, None, {}, self.reason)


class SearchManager:
    def __init__(self, config: AppConfig, engine: SearchEngine | None = None, rg_info: tuple[str, str] | None = "auto"):
        self.config = config
        if engine is not None:
            self.engine = engine
        else:
            if rg_info == "auto":
                rg_info = detect_ripgrep()
            if config.search.engine == "ripgrep" and not rg_info:
                self.engine = UnavailableEngine(
                    "配置要求 search.engine=ripgrep，但服务器 PATH 中找不到 rg；请安装 ripgrep 或改回 auto"
                )
            elif rg_info:
                self.engine = RipgrepEngine(rg_info[0], rg_info[1])
            else:
                self.engine = PythonBoundedEngine()
        self._semaphore = asyncio.Semaphore(config.limits.max_concurrent_searches)
        self._running: dict[str, dict] = {}

    def engine_info(self) -> dict:
        info = self.engine.info()
        info["configured"] = self.config.search.engine
        info["concurrencyLimit"] = self.config.limits.max_concurrent_searches
        return info

    @property
    def active_request_ids(self) -> list[str]:
        return list(self._running)

    def cancel(self, request_id: str) -> bool:
        entry = self._running.get(request_id)
        if not entry:
            return False
        entry["cancel"].set()
        return True

    def _register(self, request: SearchRequest) -> dict:
        entry = {"cancel": threading.Event(), "startedAt": time.monotonic()}
        self._running[request.request_id] = entry
        return entry

    async def run(self, request: SearchRequest) -> SearchOutcome:
        # 同名 requestId 先取消，避免客户端重试时留下旧扫描
        self.cancel(request.request_id)
        entry = self._register(request)
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=max(1.0, request.timeout_seconds))
            except asyncio.TimeoutError:
                return SearchOutcome(
                    STATUS_BUSY,
                    self.engine.name,
                    [],
                    False,
                    None,
                    {},
                    f"并发检索已达上限 {self.config.limits.max_concurrent_searches}，请等待或取消其他检索",
                )
            try:
                return await self.engine.search(request, entry["cancel"])
            finally:
                self._semaphore.release()
        finally:
            self._running.pop(request.request_id, None)


def validate_query(query: object, *, literal: bool, max_length: int) -> str:
    if not isinstance(query, str) or not query.strip():
        raise invalid_request("query 不能为空")
    if len(query) > max_length:
        raise invalid_request(f"query 过长（上限 {max_length} 字符）")
    if "\x00" in query:
        raise invalid_request("query 含 NUL 字节")
    if not literal:
        try:
            re.compile(query)
        except re.error as exc:
            raise invalid_request(f"正则表达式无效：{exc}") from exc
    return query
