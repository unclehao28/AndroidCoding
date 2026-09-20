"""配置加载与校验。

格式：JSON（不依赖 Python 3.11+ 的 tomllib，Linux 上 Python 3.9/3.10 也能直接运行）。
校验：未知字段、类型错误、越界数值、不存在的根目录都会导致启动失败并列出全部问题。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import ApiError
from .pathtools import is_within as _is_within
from .pathtools import normalize_rel_path

VERSION = "0.4.0-p2b"
API_VERSION = "p2"

ROOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DEFAULT_EXCLUDE_GLOBS = (
    ".git/**",
    ".repo/**",
    "out/**",
    "**/out/**",
    "*.o",
    "*.a",
    "*.so",
    "*.jar",
    "*.apk",
    "*.img",
    "*.zip",
)

LIMIT_SPECS: dict[str, tuple[float, float, float]] = {
    # 名称: (最小值, 最大值, 默认值)
    "maxFileBytes": (1024, 64 * 1024 * 1024, 2 * 1024 * 1024),
    "maxLineCount": (1, 20000, 2000),
    "maxTreeEntries": (1, 10000, 2000),
    "maxSearchResults": (1, 5000, 500),
    "maxSearchFileBytes": (1024, 64 * 1024 * 1024, 1024 * 1024),
    "searchTimeoutSeconds": (1, 300, 10),
    "maxConcurrentSearches": (1, 16, 2),
    "maxRegexLength": (16, 4096, 200),
    "maxLineLength": (80, 10000, 400),
    "rgThreads": (1, 32, 2),
    # 远程仓库 clone/fetch 的上限：整套 AOSP 子仓可能要几十分钟
    "syncTimeoutSeconds": (60, 86400, 3600),
}

# 允许的 git 远程地址：https/http/ssh/git/file 协议，或 git@host:path 形式。
# 收紧的原因：地址会作为单个 argv 元素传给 git，必须排除空白、引号、反引号等，
# 也顺便排除以 '-' 开头的字符串（否则会被 git 当成选项）。
GIT_URL_RE = re.compile(
    r"^(?:(?:https?|ssh|git|file)://[^\s'\"`<>|;&$]{1,500}|[A-Za-z0-9._-]{1,64}@[A-Za-z0-9._-]{1,253}:[^\s'\"`<>|;&$]{1,500})$"
)
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")


class ConfigError(Exception):
    def __init__(self, messages: list[str]):
        self.messages = list(messages)
        super().__init__("；".join(self.messages))


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    cors_origins: tuple[str, ...]


@dataclass(frozen=True)
class GitSource:
    """远程 / 内网 Git 仓库（只读工作区）的同步配置。"""

    url: str
    ref: str | None = None
    depth: int = 1
    sparse_paths: tuple[str, ...] = ()

    @property
    def remote(self) -> bool:
        return True

    def to_public(self) -> dict:
        return {
            "url": self.url,
            "ref": self.ref,
            "depth": self.depth,
            "sparsePaths": list(self.sparse_paths),
            "credentials": "使用服务器上既有的 git/SSH 凭据；工作台不保存口令或私钥",
            "writeSupported": False,
        }


@dataclass(frozen=True)
class NavigationSettings:
    """语言服务（语义跳转）配置。默认保守：不开后台索引，实例数很小。"""

    enabled: bool = True
    clangd_path: str | None = None
    clangd_args: tuple[str, ...] = ()
    search_dirs: tuple[str, ...] = ()
    compile_commands_dir: str | None = None
    background_index: bool = False
    max_instances: int = 2
    idle_shutdown_seconds: float = 300.0
    request_timeout_seconds: float = 20.0
    initialize_timeout_seconds: float = 60.0
    max_results: int = 50
    stderr_lines: int = 200
    pch_storage: str = "disk"
    clangd_log_level: str = "error"
    # Java（Eclipse JDT LS）：解包目录 / JDK / JDT LS 的工作区数据目录 / 额外参数
    java_ls_path: str | None = None
    java_home: str | None = None
    java_data_dir: str | None = None
    java_args: tuple[str, ...] = ()

    def to_public(self) -> dict:
        return {
            "enabled": self.enabled,
            "clangdPath": self.clangd_path,
            "clangdArgs": list(self.clangd_args),
            "searchDirs": list(self.search_dirs),
            "compileCommandsDir": self.compile_commands_dir,
            "javaLsPath": self.java_ls_path,
            "javaHome": self.java_home,
            "javaDataDir": self.java_data_dir,
            "javaArgs": list(self.java_args),
            "backgroundIndex": self.background_index,
            "maxInstances": self.max_instances,
            "idleShutdownSeconds": self.idle_shutdown_seconds,
            "requestTimeoutSeconds": self.request_timeout_seconds,
            "maxResults": self.max_results,
            "pchStorage": self.pch_storage,
            "note": "P2 只接入 C/C++（clangd）；Java/Kotlin/Rust/AIDL 未接入，接口会明确返回未就绪",
        }


@dataclass(frozen=True)
class RootConfig:
    id: str
    name: str
    path: Path
    readonly: bool
    git: GitSource | None = None
    compile_commands_dir: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.git is not None

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "readonly": self.readonly,
            "exists": self.path.is_dir(),
            "remote": self.is_remote,
            "git": self.git.to_public() if self.git else None,
        }


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int
    max_line_count: int
    max_tree_entries: int
    max_search_results: int
    max_search_file_bytes: int
    search_timeout_seconds: float
    max_concurrent_searches: int
    max_regex_length: int
    max_line_length: int
    rg_threads: int
    sync_timeout_seconds: float

    def to_public(self) -> dict:
        return {
            "maxFileBytes": self.max_file_bytes,
            "maxLineCount": self.max_line_count,
            "maxTreeEntries": self.max_tree_entries,
            "maxSearchResults": self.max_search_results,
            "maxSearchFileBytes": self.max_search_file_bytes,
            "searchTimeoutSeconds": self.search_timeout_seconds,
            "maxConcurrentSearches": self.max_concurrent_searches,
            "maxRegexLength": self.max_regex_length,
            "maxLineLength": self.max_line_length,
            "rgThreads": self.rg_threads,
            "syncTimeoutSeconds": self.sync_timeout_seconds,
        }


@dataclass(frozen=True)
class SearchSettings:
    engine: str  # auto | ripgrep | python
    exclude_globs: tuple[str, ...]
    respect_ignore_files: bool
    python_max_files: int


@dataclass(frozen=True)
class Features:
    write: bool
    navigation: bool


@dataclass(frozen=True)
class AppConfig:
    source_path: Path
    base_dir: Path
    server: ServerConfig
    roots: tuple[RootConfig, ...]
    limits: Limits
    search: SearchSettings
    features: Features
    prototype_dir: Path
    navigation: NavigationSettings = NavigationSettings()
    cache_dir: Path | None = None
    warnings: tuple[str, ...] = ()

    def root(self, root_id: str) -> RootConfig | None:
        for item in self.roots:
            if item.id == root_id:
                return item
        return None

    @property
    def remote_roots(self) -> tuple[RootConfig, ...]:
        return tuple(root for root in self.roots if root.is_remote)

    def to_public(self) -> dict:
        return {
            "apiVersion": API_VERSION,
            "version": VERSION,
            "server": {"host": self.server.host, "port": self.server.port},
            "roots": [root.to_public() for root in self.roots],
            "cacheDir": str(self.cache_dir) if self.cache_dir else None,
            "limits": self.limits.to_public(),
            "search": {
                "engine": self.search.engine,
                "excludeGlobs": list(self.search.exclude_globs),
                "respectIgnoreFiles": self.search.respect_ignore_files,
            },
            "features": {"write": self.features.write, "navigation": self.features.navigation},
            "navigation": self.navigation.to_public(),
            "warnings": list(self.warnings),
        }


def _parse_git_source(value: Any, where: str, problems: list[str]) -> GitSource | None:
    """校验 roots[].git。远程仓库只读，且地址会作为单个 argv 传给 git，必须严格校验。"""
    if value is None:
        return None
    entry = _require_mapping(value, f"{where}.git", problems)
    _check_unknown(entry, {"url", "ref", "depth", "sparsePaths"}, f"{where}.git", problems)
    url = entry.get("url")
    if not isinstance(url, str) or not GIT_URL_RE.match(url.strip()):
        problems.append(
            f"{where}.git.url 必须是 https/http/ssh/git/file 地址或 git@host:path 形式，"
            "且不能包含空白、引号、反引号或 shell 元字符"
        )
        return None
    url = url.strip()
    ref = entry.get("ref")
    if ref is not None:
        if not isinstance(ref, str) or not GIT_REF_RE.match(ref):
            problems.append(f"{where}.git.ref 必须是分支或标签名，匹配 {GIT_REF_RE.pattern}")
            ref = None
    depth = entry.get("depth", 1)
    if isinstance(depth, bool) or not isinstance(depth, int) or not (1 <= depth <= 100000):
        problems.append(f"{where}.git.depth 必须是 1..100000 的整数（浅克隆，1 最省时间与磁盘）")
        depth = 1
    sparse_raw = entry.get("sparsePaths", [])
    sparse: list[str] = []
    if not isinstance(sparse_raw, list):
        problems.append(f"{where}.git.sparsePaths 必须是字符串数组")
    else:
        for pattern in sparse_raw:
            if not isinstance(pattern, str):
                problems.append(f"{where}.git.sparsePaths 含非字符串项：{pattern!r}")
                continue
            if pattern.startswith(("/", "\\")):
                problems.append(f"{where}.git.sparsePaths 必须是仓库内的相对路径：{pattern!r}")
                continue
            try:
                normalized = normalize_rel_path(pattern)
            except ApiError:
                problems.append(f"{where}.git.sparsePaths 含非法路径：{pattern!r}")
                continue
            if not normalized:
                problems.append(f"{where}.git.sparsePaths 不允许空项")
                continue
            sparse.append(normalized)
    return GitSource(url=url, ref=ref, depth=depth, sparse_paths=tuple(sparse))


def _require_mapping(value: Any, where: str, problems: list[str]) -> dict:
    if not isinstance(value, dict):
        problems.append(f"{where} 必须是对象")
        return {}
    return value


def _check_unknown(section: dict, allowed: set[str], where: str, problems: list[str]) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        problems.append(f"{where} 存在未知字段：{', '.join(unknown)}")


def _int_setting(value: Any, where: str, problems: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"{where} 必须是整数")
        return None
    return value


def _bool_setting(value: Any, where: str, problems: list[str], default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        problems.append(f"{where} 必须是布尔值")
        return default
    return value


def _str_list(value: Any, where: str, problems: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        problems.append(f"{where} 必须是非空字符串数组")
        return []
    return list(value)


ALLOWED_TOP_LEVEL = {"server", "roots", "limits", "search", "features", "navigation", "prototypeDir", "cacheDir"}

NAVIGATION_BOOL_KEYS = ("enabled", "backgroundIndex")
NAVIGATION_INT_KEYS = {
    "maxInstances": (1, 8),
    "maxResults": (1, 500),
    "stderrLines": (10, 5000),
}
NAVIGATION_FLOAT_KEYS = {
    "idleShutdownSeconds": (10, 86400),
    "requestTimeoutSeconds": (1, 600),
    "initializeTimeoutSeconds": (1, 600),
}
NAVIGATION_STR_KEYS = (
    "clangdPath",
    "compileCommandsDir",
    "javaLsPath",
    "javaHome",
    "javaDataDir",
)
NAVIGATION_STR_LIST_KEYS = ("clangdArgs", "searchDirs", "javaArgs")
PCH_STORAGE_VALUES = ("disk", "memory")
CLANGD_LOG_LEVELS = ("error", "info", "verbose")


def _parse_navigation(value: Any, problems: list[str], warnings: list[str]) -> NavigationSettings:
    entry = _require_mapping(value if value is not None else {}, "navigation", problems)
    allowed = set(NAVIGATION_BOOL_KEYS) | set(NAVIGATION_INT_KEYS) | set(NAVIGATION_FLOAT_KEYS)
    allowed |= set(NAVIGATION_STR_KEYS) | set(NAVIGATION_STR_LIST_KEYS) | {"pchStorage", "clangdLogLevel"}
    _check_unknown(entry, allowed, "navigation", problems)

    settings: dict = {}
    for key in NAVIGATION_BOOL_KEYS:
        settings[key] = _bool_setting(entry.get(key), f"navigation.{key}", problems, True if key == "enabled" else False)
    for key, (low, high) in NAVIGATION_INT_KEYS.items():
        raw_value = entry.get(key)
        if raw_value is None:
            settings[key] = {"maxInstances": 2, "maxResults": 50, "stderrLines": 200}[key]
            continue
        number = _int_setting(raw_value, f"navigation.{key}", problems)
        if number is None or not (low <= number <= high):
            problems.append(f"navigation.{key} 必须在 {low}..{high} 之间")
            number = {"maxInstances": 2, "maxResults": 50, "stderrLines": 200}[key]
        settings[key] = number
    defaults_float = {"idleShutdownSeconds": 300.0, "requestTimeoutSeconds": 20.0, "initializeTimeoutSeconds": 60.0}
    for key, (low, high) in NAVIGATION_FLOAT_KEYS.items():
        raw_value = entry.get(key)
        if raw_value is None:
            settings[key] = defaults_float[key]
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            problems.append(f"navigation.{key} 必须是数字")
            settings[key] = defaults_float[key]
            continue
        if not (low <= float(raw_value) <= high):
            problems.append(f"navigation.{key} 必须在 {low}..{high} 之间")
            settings[key] = defaults_float[key]
            continue
        settings[key] = float(raw_value)
    for key in NAVIGATION_STR_KEYS:
        raw_value = entry.get(key)
        if raw_value is None:
            settings[key] = None
            continue
        if not isinstance(raw_value, str) or not raw_value.strip():
            problems.append(f"navigation.{key} 必须是非空字符串")
            settings[key] = None
            continue
        settings[key] = raw_value.strip()
    for key in NAVIGATION_STR_LIST_KEYS:
        raw_value = entry.get(key, [])
        if not isinstance(raw_value, list) or any(not isinstance(item, str) or not item for item in raw_value):
            problems.append(f"navigation.{key} 必须是非空字符串数组")
            settings[key] = []
            continue
        settings[key] = [item.strip() for item in raw_value]
    pch_storage = entry.get("pchStorage", "disk")
    if pch_storage not in PCH_STORAGE_VALUES:
        problems.append(f"navigation.pchStorage 必须是 {' 或 '.join(PCH_STORAGE_VALUES)}")
        pch_storage = "disk"
    log_level = entry.get("clangdLogLevel", "error")
    if log_level not in CLANGD_LOG_LEVELS:
        problems.append(f"navigation.clangdLogLevel 必须是 {'/'.join(CLANGD_LOG_LEVELS)}")
        log_level = "error"

    if settings["backgroundIndex"]:
        warnings.append(
            "navigation.backgroundIndex=true：clangd 会为整个工作区建索引，CPU/内存/磁盘开销很大，"
            "整套 AOSP 上请谨慎开启（跨文件的定义跳转才需要它，共享/远程索引属于后续工作）"
        )
    return NavigationSettings(
        enabled=settings["enabled"],
        clangd_path=settings["clangdPath"],
        clangd_args=tuple(settings["clangdArgs"]),
        search_dirs=tuple(settings["searchDirs"]),
        compile_commands_dir=settings["compileCommandsDir"],
        java_ls_path=settings["javaLsPath"],
        java_home=settings["javaHome"],
        java_data_dir=settings["javaDataDir"],
        java_args=tuple(settings["javaArgs"]),
        background_index=settings["backgroundIndex"],
        max_instances=settings["maxInstances"],
        idle_shutdown_seconds=settings["idleShutdownSeconds"],
        request_timeout_seconds=settings["requestTimeoutSeconds"],
        initialize_timeout_seconds=settings["initializeTimeoutSeconds"],
        max_results=settings["maxResults"],
        stderr_lines=settings["stderrLines"],
        pch_storage=pch_storage,
        clangd_log_level=log_level,
    )


def build_config(raw: dict, *, source_path: Path) -> AppConfig:
    problems: list[str] = []
    warnings: list[str] = []
    base_dir = source_path.parent

    unknown_top = sorted(key for key in raw if key not in ALLOWED_TOP_LEVEL and not key.startswith("_"))
    if unknown_top:
        problems.append(f"配置顶层存在未知字段：{', '.join(unknown_top)}")

    root_section = raw.get("roots")
    if not isinstance(root_section, list) or not root_section:
        problems.append("roots 必须是非空数组，且只能列出确实需要的源码根目录")
        root_section = []

    server_raw = _require_mapping(raw.get("server", {}), "server", problems)
    _check_unknown(server_raw, {"host", "port", "corsOrigins"}, "server", problems)
    host = server_raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        problems.append("server.host 必须是非空字符串")
        host = "127.0.0.1"
    else:
        host = host.strip()
    if host not in ("127.0.0.1", "localhost", "::1"):
        warnings.append(
            f"server.host={host} 不是回环地址：P1 未做认证，请确认只在内网/受控环境暴露，"
            "推荐保持 127.0.0.1 并通过 SSH 端口转发访问"
        )
    port = _int_setting(server_raw.get("port", 8765), "server.port", problems)
    if port is None or not (1 <= port <= 65535):
        problems.append("server.port 必须在 1..65535 之间")
        port = 8765
    cors_raw = server_raw.get("corsOrigins", ["*"])
    cors_origins = tuple(_str_list(cors_raw, "server.corsOrigins", problems)) or ("*",)
    if "*" in cors_origins:
        warnings.append("server.corsOrigins 含 *：服务只监听回环地址，仍建议收紧为具体来源")

    # cacheDir：远程仓库的本地缓存父目录。只有出现 git 根目录时才需要。
    cache_dir: Path | None = None
    cache_dir_raw = raw.get("cacheDir")
    if cache_dir_raw is not None:
        if not isinstance(cache_dir_raw, str) or not cache_dir_raw.strip():
            problems.append("cacheDir 必须是非空字符串（远程仓库的本地缓存父目录）")
        else:
            cache_candidate = Path(os.path.expanduser(cache_dir_raw.strip()))
            if not cache_candidate.is_absolute():
                cache_candidate = base_dir / cache_candidate
            cache_dir = cache_candidate.resolve()
            if cache_dir == Path(cache_dir.anchor):
                problems.append("cacheDir 不能是文件系统根目录")
                cache_dir = None
            elif not cache_dir.exists():
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    warnings.append(f"cacheDir 不存在，已创建：{cache_dir}")
                except OSError as exc:
                    problems.append(f"cacheDir 无法创建：{cache_dir}（{exc}）")
                    cache_dir = None
            elif not cache_dir.is_dir():
                problems.append(f"cacheDir 不是目录：{cache_dir}")
                cache_dir = None

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    roots: list[RootConfig] = []
    for index, item in enumerate(root_section):
        where = f"roots[{index}]"
        entry = _require_mapping(item, where, problems)
        _check_unknown(entry, {"id", "name", "path", "readonly", "git", "compileCommandsDir"}, where, problems)
        root_id = entry.get("id")
        if not isinstance(root_id, str) or not ROOT_ID_RE.match(root_id):
            problems.append(f"{where}.id 必须匹配 {ROOT_ID_RE.pattern}")
            continue
        if root_id in seen_ids:
            problems.append(f"{where}.id 重复：{root_id}")
            continue
        seen_ids.add(root_id)

        git_source = _parse_git_source(entry.get("git"), where, problems)
        readonly = _bool_setting(entry.get("readonly"), f"{where}.readonly", problems, True)
        raw_compile_db = entry.get("compileCommandsDir")
        compile_commands_dir: str | None = None
        if raw_compile_db is not None:
            if not isinstance(raw_compile_db, str) or not raw_compile_db.strip():
                problems.append(f"{where}.compileCommandsDir 必须是非空字符串")
            else:
                candidate_db = Path(os.path.expanduser(raw_compile_db.strip()))
                if not candidate_db.is_absolute():
                    candidate_db = base_dir / candidate_db
                if not candidate_db.is_dir():
                    problems.append(
                        f"{where}.compileCommandsDir 目录不存在：{candidate_db}"
                        "（需要包含 compile_commands.json，AOSP 用 Soong 的 compdb 生成）"
                    )
                else:
                    compile_commands_dir = str(candidate_db.resolve())
        if git_source is not None and not readonly:
            problems.append(
                f"{where}.readonly 必须为 true：远程仓库在 P1 只有只读能力"
                "（真实写入与提交属于 P4，且不会自动 push）"
            )
            readonly = True

        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            problems.append(f"{where}.path 必须是非空字符串")
            continue
        expanded = os.path.expanduser(raw_path.strip())
        candidate = Path(expanded)
        if git_source is not None:
            # 远程仓库：path 是缓存检出目录；相对路径以 cacheDir 为准，且必须落在 cacheDir 内
            if not candidate.is_absolute():
                if cache_dir is None:
                    problems.append(f"{where} 是远程仓库，需要顶层配置 cacheDir 才能确定缓存位置")
                    continue
                candidate = cache_dir / candidate
            resolved = candidate.resolve()
            if cache_dir is None or not _is_within(resolved, cache_dir):
                problems.append(f"{where}.path 必须位于 cacheDir（{cache_dir}）之内：{resolved}")
                continue
            if resolved.exists() and not resolved.is_dir():
                problems.append(f"{where}.path 已存在但不是目录：{resolved}")
                continue
            if resolved.exists() and any(resolved.iterdir()) and not (resolved / ".git").exists():
                problems.append(
                    f"{where}.path 已存在且不是 git 仓库存根：{resolved}，请确认是否手工删除后重新同步"
                )
                continue
            key = os.path.normcase(str(resolved))
            if key in seen_paths:
                problems.append(f"{where}.path 与前面的根目录重复：{resolved}")
                continue
            seen_paths.add(key)
            name = entry.get("name", root_id)
            if not isinstance(name, str) or not name.strip():
                problems.append(f"{where}.name 必须是非空字符串")
                name = root_id
            roots.append(
                RootConfig(
                    id=root_id,
                    name=name.strip(),
                    path=resolved,
                    readonly=readonly,
                    git=git_source,
                    compile_commands_dir=compile_commands_dir,
                )
            )
            continue

        # 本地源码根：相对路径以**配置文件所在目录**为准，不能跟着进程的当前工作目录走
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            problems.append(f"{where}.path 不存在：{candidate}")
            continue
        if not resolved.is_dir():
            problems.append(f"{where}.path 不是目录：{resolved}")
            continue
        key = os.path.normcase(str(resolved))
        if key in seen_paths:
            problems.append(f"{where}.path 与前面的根目录重复：{resolved}")
            continue
        seen_paths.add(key)
        name = entry.get("name", root_id)
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where}.name 必须是非空字符串")
            name = root_id
        roots.append(
            RootConfig(
                id=root_id,
                name=name.strip(),
                path=resolved,
                readonly=readonly,
                compile_commands_dir=compile_commands_dir,
            )
        )

    limits_raw = _require_mapping(raw.get("limits", {}), "limits", problems)
    _check_unknown(limits_raw, set(LIMIT_SPECS), "limits", problems)
    limit_values: dict[str, float] = {}
    for name, (low, high, default) in LIMIT_SPECS.items():
        if name not in limits_raw:
            limit_values[name] = default
            continue
        value = limits_raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"limits.{name} 必须是数字")
            limit_values[name] = default
            continue
        if not (low <= value <= high):
            problems.append(f"limits.{name} 必须在 {low}..{high} 之间，当前 {value}")
            limit_values[name] = default
            continue
        limit_values[name] = value
    if limit_values["maxSearchFileBytes"] > limit_values["maxFileBytes"]:
        warnings.append(
            "limits.maxSearchFileBytes 大于 limits.maxFileBytes：搜索结果可能指向无法在页面打开的大文件"
        )

    search_raw = _require_mapping(raw.get("search", {}), "search", problems)
    _check_unknown(search_raw, {"engine", "excludeGlobs", "respectIgnoreFiles", "pythonMaxFiles"}, "search", problems)
    engine = search_raw.get("engine", "auto")
    if engine not in ("auto", "ripgrep", "python"):
        problems.append("search.engine 必须是 auto、ripgrep 或 python")
        engine = "auto"
    exclude_raw = search_raw.get("excludeGlobs", list(DEFAULT_EXCLUDE_GLOBS))
    exclude_globs = tuple(_str_list(exclude_raw, "search.excludeGlobs", problems)) or DEFAULT_EXCLUDE_GLOBS
    python_max_files = _int_setting(search_raw.get("pythonMaxFiles", 20000), "search.pythonMaxFiles", problems)
    if python_max_files is None or not (100 <= python_max_files <= 2_000_000):
        problems.append("search.pythonMaxFiles 必须在 100..2000000 之间")
        python_max_files = 20000

    respect_ignore_files = _bool_setting(
        search_raw.get("respectIgnoreFiles"), "search.respectIgnoreFiles", problems, True
    )

    features_raw = _require_mapping(raw.get("features", {}), "features", problems)
    _check_unknown(features_raw, {"write", "navigation"}, "features", problems)
    write = _bool_setting(features_raw.get("write"), "features.write", problems, False)
    navigation_feature = _bool_setting(features_raw.get("navigation"), "features.navigation", problems, True)
    if write:
        problems.append("features.write 在 P1/P2 未实现（真实写入属于 P4），必须为 false")
    navigation_settings = _parse_navigation(raw.get("navigation"), problems, warnings)
    if not navigation_feature and navigation_settings.enabled:
        warnings.append("features.navigation=false：语义跳转接口会整体关闭，navigation.* 的设置将被忽略")

    prototype_dir_raw = raw.get("prototypeDir", "../prototype")
    if not isinstance(prototype_dir_raw, str) or not prototype_dir_raw.strip():
        problems.append("prototypeDir 必须是非空字符串")
        prototype_dir_raw = "../prototype"
    prototype_candidate = Path(os.path.expanduser(prototype_dir_raw.strip()))
    if not prototype_candidate.is_absolute():
        prototype_candidate = base_dir / prototype_candidate
    prototype_dir = prototype_candidate.resolve()
    if not prototype_dir.is_dir():
        problems.append(f"prototypeDir 不存在：{prototype_dir}")
    elif not (prototype_dir / "index.html").is_file():
        problems.append(f"prototypeDir 缺少 index.html：{prototype_dir}")

    if not roots:
        problems.append("没有有效的源码根目录，服务不会启动")
    if any(root.is_remote for root in roots):
        if cache_dir is None:
            problems.append("有远程仓库根目录时必须配置 cacheDir（本地缓存父目录）")
        else:
            for local_root in (root for root in roots if not root.is_remote):
                if _is_within(cache_dir, local_root.path) or _is_within(local_root.path, cache_dir):
                    warnings.append(
                        f"cacheDir {cache_dir} 与源码根 {local_root.path} 有重叠，"
                        "远程仓库的克隆文件可能出现在检索结果里，建议分开存放"
                    )
    if problems:
        raise ConfigError(problems)

    limits = Limits(
        max_file_bytes=int(limit_values["maxFileBytes"]),
        max_line_count=int(limit_values["maxLineCount"]),
        max_tree_entries=int(limit_values["maxTreeEntries"]),
        max_search_results=int(limit_values["maxSearchResults"]),
        max_search_file_bytes=int(limit_values["maxSearchFileBytes"]),
        search_timeout_seconds=float(limit_values["searchTimeoutSeconds"]),
        max_concurrent_searches=int(limit_values["maxConcurrentSearches"]),
        max_regex_length=int(limit_values["maxRegexLength"]),
        max_line_length=int(limit_values["maxLineLength"]),
        rg_threads=int(limit_values["rgThreads"]),
        sync_timeout_seconds=float(limit_values["syncTimeoutSeconds"]),
    )
    return AppConfig(
        source_path=source_path,
        base_dir=base_dir,
        server=ServerConfig(host=host, port=port, cors_origins=cors_origins),
        roots=tuple(roots),
        limits=limits,
        search=SearchSettings(
            engine=engine,
            exclude_globs=exclude_globs,
            respect_ignore_files=respect_ignore_files,
            python_max_files=python_max_files,
        ),
        features=Features(write=write, navigation=navigation_feature),
        prototype_dir=prototype_dir,
        navigation=navigation_settings,
        cache_dir=cache_dir,
        warnings=tuple(warnings),
    )


def strip_json_comments(text: str) -> str:
    """容忍配置文件里的 // 与 /* */ 注释。

    注释字符会被替换成空格，保证报错时的行列号仍然准确；
    字符串内部的内容（例如 "https://..."）不会被当成注释。
    """
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                out.append(" ")
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            stop = length if end < 0 else end + 2
            for cursor in range(index, stop):
                out.append("\n" if text[cursor] == "\n" else " ")
            index = stop
            continue
        out.append(char)
        index += 1
    return "".join(out)


def load_config(path: str | os.PathLike) -> AppConfig:
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise ConfigError([f"配置文件不存在：{source_path}"])
    text = source_path.read_text(encoding="utf-8")
    try:
        raw = json.loads(strip_json_comments(text))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            [f"配置文件不是合法 JSON：{source_path}:{exc.lineno}:{exc.colno} {exc.msg}（支持 // 与 /* */ 注释）"]
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(["配置文件顶层必须是对象"])
    return build_config(raw, source_path=source_path)


def resolve_config_path(explicit: str | None = None) -> Path:
    """优先级：命令行 --config > 环境变量 ASW_CONFIG > server/config.json > server/config.example.json"""
    if explicit:
        return Path(explicit)
    env = os.environ.get("ASW_CONFIG")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent.parent
    preferred = here / "config.json"
    return preferred if preferred.is_file() else here / "config.example.json"
