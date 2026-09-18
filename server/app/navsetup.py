"""P2 语义跳转的环境准备：找 AOSP 源码根、找 clangd、把结果写进 config.json。

为什么需要这个模块：服务器上手工编辑 JSON 很容易出错（占位符被原样粘贴、vi 里贴进 shell 命令、
字段名写错），而这一步恰恰是 P2 能不能用起来的关键。所以把"探测 + 改配置 + 自查"做成一条命令。

原则：
- 探测有明确上限（深度、条目数），不会全盘扫描；
- 每一步都打印证据（命中的标记、clangd 版本、改了哪一行）；
- 找不到就如实说找不到并给出下一步，不会写一个假路径进配置；
- 改配置前先备份，且尽量保留原有注释（按行替换，失败才退回整体重写 JSON）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import ConfigError, load_config, strip_json_comments
from .lsp.manager import find_clangd, version_of

# 命中这些标记就认为是 Android 源码树（`.repo` 单独命中即可确定）
AOSP_MARKERS = (
    ".repo",
    "build/soong",
    "prebuilts/clang",
    "frameworks/base",
    "hardware/interfaces",
    "system/core",
    "bionic",
    "art",
    "packages/apps",
)
# 扫描起点：只在这些目录下找，且跳过系统目录
DEFAULT_SEARCH_ROOTS = ("/data", "/home", "/opt", "/mnt", "/srv", "/workspace")
SKIP_DIR_NAMES = {
    "proc",
    "sys",
    "dev",
    "run",
    "snap",
    "boot",
    "usr",
    "lib",
    "lib64",
    "bin",
    "sbin",
    "etc",
    "tmp",
    "var",
    "node_modules",
    "__pycache__",
}
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_ENTRIES = 200_000
SEARCHDIRS_LINE = re.compile(r'^(\s*)"searchDirs"\s*:\s*\[[^\]]*\](\s*,?\s*)$')
CLANGDPATH_LINE = re.compile(r'^(\s*)"clangdPath"\s*:\s*(?:null|"[^"]*")(\s*,?\s*)$')


@dataclass
class Detection:
    aosp_roots: list = field(default_factory=list)
    clangd_path: str | None = None
    clangd_version: str = ""
    clangd_source: str = ""
    scanned_dirs: int = 0
    notes: list = field(default_factory=list)
    candidates: list = field(default_factory=list)

    def to_public(self) -> dict:
        return {
            "aospRoots": [str(item) for item in self.aosp_roots],
            "aospSignatures": [aosp_signature(item) for item in self.aosp_roots],
            "clangdPath": self.clangd_path,
            "clangdVersion": self.clangd_version,
            "clangdSource": self.clangd_source,
            "scannedDirs": self.scanned_dirs,
            "candidates": list(self.candidates),
            "notes": list(self.notes),
        }


def aosp_markers(path: Path) -> list:
    """返回该目录命中的安卓源码标记（不做递归）。"""
    found = []
    for marker in AOSP_MARKERS:
        try:
            if (path / marker).exists():
                found.append(marker)
        except OSError:
            continue
    return found


def is_aosp_root(path: Path) -> bool:
    markers = aosp_markers(path)
    if ".repo" in markers:
        return True
    # prebuilts/clang 只有 AOSP 系代码树才带，单独命中即确定
    try:
        if (path / "prebuilts" / "clang").is_dir():
            return True
    except OSError:
        pass
    # 单个标记可能是巧合（例如某个项目自带 system/core），要求至少两个
    return len(markers) >= 2


def aosp_signature(path: Path) -> str:
    """返回该目录作为 AOSP 根的依据（用于诊断输出）。"""
    markers = aosp_markers(path)
    if ".repo" in markers:
        return "含 .repo（repo 管理的源码树）"
    try:
        if (path / "prebuilts" / "clang").is_dir():
            return "含 prebuilts/clang（AOSP 自带工具链）"
    except OSError:
        pass
    if len(markers) >= 2:
        return "含 " + "、".join(markers[:4])
    return ""


def default_search_roots() -> list:
    if sys.platform.startswith("win"):
        return [Path.home()]
    roots = [Path(item) for item in DEFAULT_SEARCH_ROOTS]
    roots.append(Path.home())
    return roots


def find_aosp_roots(search_roots, *, max_depth: int = DEFAULT_MAX_DEPTH, max_entries: int = DEFAULT_MAX_ENTRIES):
    """在给定起点下按层扫描，返回 (AOSP 根目录列表, 访问过的目录数)。"""
    found: list = []
    scanned = 0
    seen: set = set()
    for base in search_roots:
        base_path = Path(base).expanduser()
        if not base_path.is_dir():
            continue
        stack = [(base_path, 0)]
        while stack:
            current, depth = stack.pop()
            key = os.path.normcase(str(current))
            if key in seen:
                continue
            seen.add(key)
            scanned += 1
            if scanned > max_entries:
                return found, scanned
            try:
                if is_aosp_root(current):
                    found.append(current)
                    continue  # 命中就不再往下钻
                if depth >= max_depth:
                    continue
                with os.scandir(current) as entries:
                    for entry in entries:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        name = entry.name
                        if name in SKIP_DIR_NAMES or name.startswith("."):
                            continue
                        stack.append((Path(entry.path), depth + 1))
            except (PermissionError, OSError):
                continue
    return found, scanned


def find_partial_candidates(search_roots, *, max_depth: int = DEFAULT_MAX_DEPTH + 2, limit: int = 10):
    """没有找到完整源码树时，列出"像源码的一部分"的目录，供用户确认后手工指定。

    只做诊断用：命中单个标记就收录，但不会拿来当源码根自动写进配置。
    """
    candidates: list = []
    seen: set = set()
    for base in search_roots:
        base_path = Path(base).expanduser()
        if not base_path.is_dir():
            continue
        stack = [(base_path, 0)]
        while stack and len(candidates) < limit * 4:
            current, depth = stack.pop()
            key = os.path.normcase(str(current))
            if key in seen:
                continue
            seen.add(key)
            try:
                markers = aosp_markers(current)
                if markers and not is_aosp_root(current):
                    candidates.append((len(markers), current, markers))
                    continue
                if depth >= max_depth:
                    continue
                with os.scandir(current) as entries:
                    for entry in entries:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        if entry.name in SKIP_DIR_NAMES or entry.name.startswith("."):
                            continue
                        stack.append((Path(entry.path), depth + 1))
            except (PermissionError, OSError):
                continue
    candidates.sort(key=lambda item: (-item[0], str(item[1])))
    return candidates[:limit]


def system_clangd_candidates() -> list:
    if sys.platform.startswith("win"):
        return []
    candidates: list = [Path.home() / "bin" / "clangd", Path("/usr/local/bin/clangd")]
    for pattern in ("/usr/lib/llvm-*/bin/clangd", "/usr/bin/clangd", "/usr/bin/clangd-*", "/opt/*/bin/clangd"):
        candidates.extend(sorted(Path("/").glob(pattern.lstrip("/")), reverse=True))
    return candidates


def detect(
    *,
    configured_clangd: str | None = None,
    search_dirs=None,
    extra_search_roots=None,
    scan: bool = True,
    aosp_hint: str | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Detection:
    """找 AOSP 根与 clangd。scan=False 时只按已给的 searchDirs 找 clangd。"""
    result = Detection()
    roots: list = []
    for item in search_dirs or []:
        roots.append(Path(os.path.expanduser(str(item))))
    if aosp_hint:
        roots.append(Path(os.path.expanduser(aosp_hint)))

    if scan:
        scanned_roots, scanned_count = find_aosp_roots(roots + default_search_roots(), max_depth=max_depth)
        result.aosp_roots = scanned_roots
        result.scanned_dirs = scanned_count
        if not scanned_roots and not roots:
            scanned_labels = " ".join(str(item) for item in default_search_roots())
            result.notes.append(f"在 {scanned_labels} 下按 {max_depth} 层深度没有发现完整的 Android 源码树")
            partial = find_partial_candidates(default_search_roots(), max_depth=max_depth + 1)
            for count, path, markers in partial:
                result.candidates.append(
                    {"path": str(path), "markers": markers, "hint": f"含 {count} 个标记：{'、'.join(markers[:4])}"}
                )
            if result.candidates:
                result.notes.append("下面是只含部分标记的目录（可能不是完整源码树），如果源码就在这里，用 --aosp 明确指定")
    else:
        result.aosp_roots = [item for item in roots if item.is_dir()]
        result.scanned_dirs = 0

    clangd_dirs = list(result.aosp_roots) + list(roots)
    path, version, source = find_clangd(configured_clangd, clangd_dirs)
    if not path:
        for candidate in system_clangd_candidates():
            if candidate.is_file():
                path, version, source = str(candidate), version_of(str(candidate)), f"系统位置（{candidate}）"
                break
    result.clangd_path = path
    result.clangd_version = version
    result.clangd_source = source
    if not path:
        result.notes.append("没有找到 clangd：AOSP 自带 prebuilts/clang/host/linux-x86/*/bin/clangd，可直接复用")
    return result


def _json_escape(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def patch_config_text(text: str, *, search_dirs=None, clangd_path=None) -> tuple:
    """按行替换 navigation.searchDirs / clangdPath，尽量保留注释。

    返回 (新文本, 是否改动, 改动说明)。找不到对应行时返回原文本并由调用方退回整体重写。
    """
    changes: list = []
    lines = text.splitlines(keepends=True)
    if search_dirs:
        content = ", ".join(_json_escape(str(item)) for item in search_dirs)
        for index, line in enumerate(lines):
            match = SEARCHDIRS_LINE.match(line.rstrip("\r\n"))
            if not match:
                continue
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = f'{match.group(1)}"searchDirs": [{content}]{match.group(2)}{newline}'
            changes.append(f'searchDirs → [{content}]')
            break
    if clangd_path:
        for index, line in enumerate(lines):
            match = CLANGDPATH_LINE.match(line.rstrip("\r\n"))
            if not match:
                continue
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = f'{match.group(1)}"clangdPath": {_json_escape(str(clangd_path))}{match.group(2)}{newline}'
            changes.append(f'clangdPath → {clangd_path}')
            break
    updated = "".join(lines)
    return updated, bool(changes), changes


def _rewrite_as_json(text: str, *, search_dirs=None, clangd_path=None) -> tuple:
    """整体重写的兜底路径：会丢掉注释，所以调用方必须先备份并提示。"""
    raw = json.loads(strip_json_comments(text))
    navigation = raw.get("navigation")
    if not isinstance(navigation, dict):
        navigation = {}
        raw["navigation"] = navigation
    if search_dirs:
        navigation["searchDirs"] = [str(item) for item in search_dirs]
    if clangd_path:
        navigation["clangdPath"] = str(clangd_path)
    return json.dumps(raw, ensure_ascii=False, indent=2) + "\n"


def backup_path(path: Path) -> Path:
    """生成不会互相覆盖的备份路径。

    一次运行里可能备份两次（先备份损坏的原文件，再备份改写前的文件），
    秒级时间戳会撞名，后一次会把前一次冲掉——那样用户原来手工写的内容就没了。
    """
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{index}")
        index += 1
    return candidate


def patch_config_file(path: Path, *, search_dirs=None, clangd_path=None) -> dict:
    """就地修改配置文件；返回改动说明。解析失败或没有可替换的行时退回整体重写。"""
    original = path.read_text(encoding="utf-8")
    updated, changed, changes = patch_config_text(original, search_dirs=search_dirs, clangd_path=clangd_path)
    strategy = "按行替换（保留注释）"
    if not changed and (search_dirs or clangd_path):
        updated = _rewrite_as_json(original, search_dirs=search_dirs, clangd_path=clangd_path)
        changed = True
        strategy = "整体重写（注释会丢失，已备份原文件）"
    if updated == original:
        return {"changed": False, "strategy": strategy, "changes": changes, "backup": None}
    backup = backup_path(path)
    backup.write_text(original, encoding="utf-8")
    path.write_text(updated, encoding="utf-8")
    return {"changed": True, "strategy": strategy, "changes": changes, "backup": str(backup)}


def prepare_config(
    server_dir: Path,
    *,
    aosp_roots=None,
    clangd_path=None,
    scan: bool = True,
    config_name: str = "config.json",
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict:
    """确保配置文件存在，并把探测结果写进去。返回结构化结果供命令行打印。

    注意：配置语法损坏时会备份并重建，然后**继续**探测与写配置，
    不会让用户为了"重建"再跑一遍（第一次实现里就是提前返回，白白浪费一轮）。
    """
    example = server_dir / "config.example.json"
    target = server_dir / config_name
    created = False
    if not target.exists():
        if not example.is_file():
            raise ConfigError([f"缺少 {example.name}，无法生成 config.json"])
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        created = True

    text = target.read_text(encoding="utf-8")
    # 只有 JSON 语法坏了才重建：路径不存在、字段写错这类问题必须让用户自己决定怎么改，
    # 覆盖配置文件会直接毁掉他填好的 roots。
    recovered = False
    reason = ""
    recovered_from = None
    try:
        json.loads(strip_json_comments(text))
    except json.JSONDecodeError as exc:
        recovered = True
        reason = f"JSON 语法错误：{exc.msg}（第 {exc.lineno} 行）"
        backup = backup_path(target)
        backup.write_text(text, encoding="utf-8")
        recovered_from = str(backup)
        if example.is_file():
            target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    try:
        config = load_config(target)
    except ConfigError as exc:
        return {
            "recovered": recovered,
            "reason": reason,
            "recoveredFrom": recovered_from,
            "created": created,
            "blocked": True,
            "configPath": str(target),
            "problems": list(exc.messages),
            "detection": {},
            "patch": {"changed": False, "changes": [], "backup": None},
            "navigation": {},
            "hint": (
                "先按上面的问题修好 roots/路径，再重新运行本脚本："
                f"{sys.executable} -m app --config {target} --check-config 会列出全部问题"
            ),
        }

    detection = detect(
        configured_clangd=clangd_path or config.navigation.clangd_path,
        search_dirs=list(config.navigation.search_dirs) + list(aosp_roots or []),
        scan=scan,
        max_depth=max_depth,
    )
    detected_dirs = [str(item) for item in detection.aosp_roots] or None
    effective_clangd = clangd_path or (detection.clangd_path if not detected_dirs else None)
    patch = patch_config_file(target, search_dirs=detected_dirs, clangd_path=effective_clangd)
    try:
        updated_config = load_config(target)
        problems: list = []
    except ConfigError as exc:
        updated_config = config
        problems = list(exc.messages)
    return {
        "recovered": recovered,
        "reason": reason,
        "recoveredFrom": recovered_from,
        "blocked": False,
        "created": created,
        "configPath": str(target),
        "detection": detection.to_public(),
        "patch": patch,
        "problems": problems,
        "navigation": updated_config.navigation.to_public() if hasattr(updated_config, "navigation") else {},
    }
