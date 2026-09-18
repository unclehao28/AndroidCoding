"""路径安全解析与文件内容预处理。

规则（P1 验收要求）：
1. 客户端只传相对路径，绝对路径、盘符、`..`、NUL 字节一律拒绝；
2. 解析后必须仍位于配置根目录内，符号链接指向根目录之外时同样拒绝；
3. 编码：UTF-8 优先，其次 UTF-8 BOM、UTF-16 BOM、GBK，最后 latin-1（有损并标记）；
4. 二进制（含 NUL 字节）不按文本返回；
5. 大文件按配置上限拒绝，请勿整份读取进内存。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ApiError, invalid_path, not_a_file, not_found, outside_root

_TEXT_SUFFIX_LANGUAGES = {
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".h": "cpp-header",
    ".c": "c",
    ".aidl": "aidl",
    ".xml": "xml",
    ".bp": "soong",
    ".mk": "make",
    ".rc": "init",
    ".sh": "shell",
    ".py": "python",
    ".rs": "rust",
    ".js": "javascript",
    ".ts": "typescript",
    ".json": "json",
    ".txt": "text",
    ".md": "markdown",
    ".pro": "seccomp",
    ".te": "selinux",
    ".vintf_fragment": "vintf",
    ".map": "text",
}

_SPECIAL_NAMES = {
    "Android.bp": "soong",
    "Android.mk": "make",
    "Makefile": "make",
    "CMakeLists.txt": "cmake",
    "Kconfig": "kconfig",
    "Kbuild": "make",
    "METADATA": "metadata",
}


def detect_language(name: str) -> str:
    if name in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[name]
    return _TEXT_SUFFIX_LANGUAGES.get(Path(name).suffix.lower(), "unknown")


def normalize_rel_path(raw: str) -> str:
    """把客户端传入的相对路径规范化为以 `/` 分隔、无 `.`/`..` 的形式。

    空字符串表示根目录本身。
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise invalid_path("path 必须是字符串")
    value = raw.strip()
    if "\x00" in value:
        raise invalid_path("path 含 NUL 字节")
    # 客户端可能是 Windows：反斜杠按分隔符处理，避免出现 a\..\..\ 之类绕过
    value = value.replace("\\", "/")
    if len(value) >= 2 and value[1] == ":":
        raise invalid_path("不接受盘符或绝对路径", path=raw)
    if value.startswith("//"):
        raise invalid_path("不接受 UNC 路径", path=raw)
    # 单个前导斜杠视为「相对根目录」，直接去掉
    value = value.lstrip("/")
    parts: list[str] = []
    for segment in value.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise invalid_path("path 不允许包含 ..", path=raw)
        parts.append(segment)
    return "/".join(parts)


def is_within(child: Path, parent: Path) -> bool:
    parent_s = os.path.normcase(str(parent))
    child_s = os.path.normcase(str(child))
    if child_s == parent_s:
        return True
    return child_s.startswith(parent_s.rstrip(os.sep) + os.sep)


def resolve_under_root(root: Path, rel: str) -> tuple[Path, str]:
    """返回 (解析后的绝对路径, 规范化相对路径)。

    - `..`、绝对路径：400
    - 符号链接或规范化后落到根目录之外：403
    """
    normalized = normalize_rel_path(rel)
    root = root.resolve()
    candidate = root if not normalized else root.joinpath(*normalized.split("/"))
    resolved = candidate.resolve()
    if not is_within(resolved, root):
        raise outside_root(
            "目标是符号链接或路径重定向，落在配置根目录之外",
            requested=rel,
            resolved=str(resolved),
        )
    return resolved, normalized


def normalize_subpath_for_client(resolved: Path, root: Path) -> str:
    rel = os.path.relpath(str(resolved), str(root))
    return "" if rel == os.curdir else rel.replace(os.sep, "/")


@dataclass(frozen=True)
class DecodedSource:
    status: str  # ok | binary | unsupported_encoding
    encoding: str
    lossy: bool
    lines: tuple[str, ...]
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _looks_binary(chunk: bytes) -> bool:
    if b"\x00" in chunk:
        # UTF-16 文本在 ASCII 区间同样含 NUL，先由 BOM 判断过，这里按二进制处理
        return True
    if not chunk:
        return False
    control = sum(1 for b in chunk if b < 0x09 or (0x0E <= b < 0x20))
    return control / len(chunk) > 0.05


def decode_source(raw: bytes) -> DecodedSource:
    head = raw[:8192]
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            text = raw.decode("utf-8-sig")
            return DecodedSource("ok", "utf-8-sig", False, _split_lines(text))
        except UnicodeDecodeError:
            pass
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = raw.decode("utf-16")
            return DecodedSource("ok", "utf-16", False, _split_lines(text))
        except UnicodeDecodeError:
            pass
    if _looks_binary(head):
        return DecodedSource("binary", "unknown", False, (), "检测到 NUL 或控制字符，按二进制处理")
    for encoding in ("utf-8", "gbk"):
        try:
            text = raw.decode(encoding)
            lossy = encoding != "utf-8"
            return DecodedSource("ok", encoding, lossy, _split_lines(text))
        except UnicodeDecodeError:
            continue
    text = raw.decode("latin-1")
    return DecodedSource("ok", "latin-1", True, _split_lines(text))


def _split_lines(text: str) -> tuple[str, ...]:
    # 统一按 \n 切分，保留 \r 由调用方处理；末尾换行不产生额外空行
    if text.endswith("\n"):
        text = text[:-1]
    elif text.endswith("\r"):
        text = text[:-1]
    if text == "":
        return ()
    return tuple(line[:-1] if line.endswith("\r") else line for line in text.split("\n"))


def detect_eol(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "crlf"
    return "lf"


def ensure_readable_file(resolved: Path, *, requested: str) -> os.stat_result:
    """确认目标是可读普通文件，返回 stat；错误信息里保留客户端原始相对路径。"""
    try:
        stat = resolved.stat()
    except FileNotFoundError:
        raise not_found("文件不存在", path=requested) from None
    except PermissionError:
        raise ApiError(403, "permission_denied", f"服务器无权读取：{requested}", {"path": requested}) from None
    if os.path.isdir(resolved):
        raise not_a_file("目标是目录，不是文件", path=requested)
    return stat
