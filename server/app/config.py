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
from typing import Any

VERSION = "0.2.0-p1"
API_VERSION = "p1"

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
}


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
class RootConfig:
    id: str
    name: str
    path: Path
    readonly: bool

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "readonly": self.readonly,
            "exists": self.path.is_dir(),
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
    warnings: tuple[str, ...] = ()

    def root(self, root_id: str) -> RootConfig | None:
        for item in self.roots:
            if item.id == root_id:
                return item
        return None

    def to_public(self) -> dict:
        return {
            "apiVersion": API_VERSION,
            "version": VERSION,
            "server": {"host": self.server.host, "port": self.server.port},
            "roots": [root.to_public() for root in self.roots],
            "limits": self.limits.to_public(),
            "search": {
                "engine": self.search.engine,
                "excludeGlobs": list(self.search.exclude_globs),
                "respectIgnoreFiles": self.search.respect_ignore_files,
            },
            "features": {"write": self.features.write, "navigation": self.features.navigation},
            "warnings": list(self.warnings),
        }


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


ALLOWED_TOP_LEVEL = {"server", "roots", "limits", "search", "features", "prototypeDir"}


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

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    roots: list[RootConfig] = []
    for index, item in enumerate(root_section):
        where = f"roots[{index}]"
        entry = _require_mapping(item, where, problems)
        _check_unknown(entry, {"id", "name", "path", "readonly"}, where, problems)
        root_id = entry.get("id")
        if not isinstance(root_id, str) or not ROOT_ID_RE.match(root_id):
            problems.append(f"{where}.id 必须匹配 {ROOT_ID_RE.pattern}")
            continue
        if root_id in seen_ids:
            problems.append(f"{where}.id 重复：{root_id}")
            continue
        seen_ids.add(root_id)
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            problems.append(f"{where}.path 必须是非空字符串")
            continue
        expanded = os.path.expanduser(raw_path.strip())
        candidate = Path(expanded)
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
                readonly=_bool_setting(entry.get("readonly"), f"{where}.readonly", problems, True),
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
    navigation = _bool_setting(features_raw.get("navigation"), "features.navigation", problems, False)
    if write:
        problems.append("features.write 在 P1 未实现（真实写入属于 P4），必须为 false")
    if navigation:
        problems.append("features.navigation 在 P1 未接入语言服务（属于 P2），必须为 false")

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
        features=Features(write=write, navigation=navigation),
        prototype_dir=prototype_dir,
        warnings=tuple(warnings),
    )


def load_config(path: str | os.PathLike) -> AppConfig:
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise ConfigError([f"配置文件不存在：{source_path}"])
    try:
        raw = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError([f"配置文件不是合法 JSON：{source_path}:{exc.lineno}:{exc.colno} {exc.msg}"]) from exc
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
