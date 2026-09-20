"""准备 Java 语义跳转（Eclipse JDT LS）：查 JDK → 选匹配的 JDT LS 版本 → 下载解包 → 写进 config.json。

用法（在仓库根目录执行）：

    python3 scripts/setup-java.py                     # 只体检：报告 JDK / JDT LS / 配置现状与下一步
    python3 scripts/setup-java.py --install           # 按 JDK 版本自动下载 JDT LS 并写配置
    python3 scripts/setup-java.py --install --jdk /path/to/jdk-21
    python3 scripts/setup-java.py --ls-path /path/to/jdtls --java-home /path/to/jdk   # 手工指定，只写配置

为什么要按版本选：JDT LS 对 JDK 有硬要求，版本不匹配它会直接退出。
  · 最新快照     需要 Java 21+
  · 1.31.0       需要 Java 17+（本机用 JDK 17 + 1.31.0 实测 5/5 Java 用例通过）
  · 1.12.0       需要 Java 11+
AOSP 自带的 prebuilts/jdk/jdk11 通常是 11，因此多数 AOSP 机器上需要另装一个较新的 JDK。

下载地址都是官方源（download.eclipse.org），脚本会先读该版本自带启动脚本里声明的 JDK 要求，
与机器上的 java 版本比对，不匹配就报错退出——不写一个注定起不来的配置进 config.json。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app.config import ConfigError, load_config  # noqa: E402
from app.navsetup import update_navigation_config  # noqa: E402
from app.lsp.manager import (  # noqa: E402
    jdtls_config_dir,
    jdtls_launcher,
    jdtls_required_java,
    find_java,
    find_jdtls,
)

# 版本 → 官方下载地址（均已实际下载验证过）
JDTLS_RELEASES = (
    {
        "label": "最新快照",
        "min_java": 21,
        "url": "https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz",
    },
    {
        "label": "1.31.0",
        "min_java": 17,
        "url": "https://download.eclipse.org/jdtls/milestones/1.31.0/jdt-language-server-1.31.0-202401111522.tar.gz",
    },
    {
        "label": "1.12.0",
        "min_java": 11,
        "url": "https://download.eclipse.org/jdtls/milestones/1.12.0/jdt-language-server-1.12.0-202206011637.tar.gz",
    },
)
JDK_HELP = (
    "机器上需要一个 JDK（JDT LS 是 Java 程序，必须自己带运行时）：",
    "  · 有 sudo：sudo apt-get install -y openjdk-21-jdk    （或 openjdk-17-jdk）",
    "  · 无 sudo、有网络：下载 Adoptium/Temurin 的 tar.gz 解到 ~/jdk，再用 --jdk ~/jdk 指定",
    "  · AOSP 自带的 prebuilts/jdk/jdk11 只有 Java 11，跑不了较新的 JDT LS",
)


def pick_release(java_version: int) -> dict | None:
    for release in JDTLS_RELEASES:
        if java_version >= release["min_java"]:
            return release
    return None


def default_target(release: dict) -> Path:
    return Path.home() / ".local" / "share" / "asw-jdtls" / release["label"]


def download_and_unpack(url: str, target: Path) -> None:
    archive = target.parent / Path(url).name
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        print(f"[下载] {url}")
        started = time.time()
        try:
            with urllib.request.urlopen(url, timeout=300) as response, archive.open("wb") as handle:
                total = 0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    handle.write(chunk)
        except (urllib.error.URLError, OSError) as exc:
            raise SystemExit(f"[失败] 下载失败：{exc}——内网无外网时请手工下载后解包，再用 --ls-path 指定目录")
        print(f"       完成 {total / 1048576:.1f} MB，用时 {time.time() - started:.0f}s")
    target.mkdir(parents=True, exist_ok=True)
    print(f"[解包] {archive.name} → {target}")
    with tarfile.open(archive) as archive_file:
        try:
            archive_file.extractall(target)
        except TypeError:  # Python 3.12 之前的签名差异
            archive_file.extractall(target)


def configured_search_dirs(config_path: Path) -> tuple:
    """从配置里取 searchDirs 与各 root 路径：AOSP 自带的 JDK 就在这些树下面。"""
    try:
        config = load_config(config_path)
    except (ConfigError, OSError):
        return [], None
    dirs = [Path(item).expanduser() for item in config.navigation.search_dirs]
    dirs += [root.path for root in config.roots if not root.is_remote]
    return dirs, config


def report_only(config_path: Path) -> int:
    search_dirs, config = configured_search_dirs(config_path)
    java, java_version, java_note = find_java(None, search_dirs)
    ls_dir, ls_note = find_jdtls(config.navigation.java_ls_path if config else None, search_dirs)
    if config and config.navigation.java_ls_path:
        ls_note += f"（来自配置：{config.navigation.java_ls_path}）" if ls_dir else ""
    print(f"[JDK] {java_note}")
    print(f"[JDT LS] {ls_note}")
    if ls_dir:
        required = jdtls_required_java(ls_dir)
        print(f"         该版本要求 Java {required if required else '未知'}；launcher={bool(jdtls_launcher(ls_dir))}"
              f" configDir={jdtls_config_dir(ls_dir)}")
    print(f"[配置] {config_path}")
    if not java:
        print("\n下一步：先装一个 JDK：")
        for line in JDK_HELP:
            print("  " + line)
        return 2
    if not ls_dir:
        release = pick_release(java_version)
        if not release:
            print(f"\n[失败] 当前 JDK {java_version} 版本过低：JDT LS 最低需要 Java 11。")
            for line in JDK_HELP:
                print("  " + line)
            return 2
        print(f"\n下一步：python3 scripts/setup-java.py --install   # 会装 {release['label']}（要求 Java {release['min_java']}+）")
        return 2
    print("\n下一步：python3 scripts/verify-p2-navigation.py --direct --workspace fixtures")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="配置文件路径，默认 server/config.json")
    parser.add_argument("--install", action="store_true", help="下载并解包 JDT LS")
    parser.add_argument("--jdk", help="用哪个 JDK（默认自动发现：javaHome 配置 → PATH → AOSP prebuilts/jdk）")
    parser.add_argument("--java-home", help="写入配置的 JDK 路径（不下载，只写配置）")
    parser.add_argument("--ls-path", help="写入配置的 JDT LS 目录（不下载，只写配置）")
    parser.add_argument("--target", help="下载解包到哪个目录，默认 ~/.local/share/asw-jdtls/<版本>")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else SERVER / "config.json"
    server_dir = config_path.parent

    if not args.install and not args.ls_path and not args.java_home:
        return report_only(config_path)

    updates: dict = {}
    if args.java_home:
        updates["javaHome"] = str(Path(os.path.expanduser(args.java_home)))
    if args.ls_path:
        ls_dir = str(Path(os.path.expanduser(args.ls_path)))
        if not (jdtls_launcher(ls_dir) and jdtls_config_dir(ls_dir)):
            print(f"[失败] {ls_dir} 不像 JDT LS 解包目录（缺 plugins/*equinox*.jar 或 config_* 目录）")
            return 2
        updates["javaLsPath"] = ls_dir

    if args.install:
        search_dirs, _config = configured_search_dirs(config_path)
        java, java_version, java_note = find_java(args.jdk, search_dirs)
        print(f"[JDK] {java_note}")
        if not java:
            for line in JDK_HELP:
                print("  " + line)
            return 2
        release = pick_release(java_version)
        if not release:
            print(f"[失败] JDK {java_version} 版本过低（JDT LS 最低 Java 11）")
            for line in JDK_HELP:
                print("  " + line)
            return 2
        target = Path(os.path.expanduser(args.target)) if args.target else default_target(release)
        if not (jdtls_launcher(str(target)) and jdtls_config_dir(str(target))):
            download_and_unpack(release["url"], target)
        else:
            print(f"[复用] {target} 已存在可用的 JDT LS")
        required = jdtls_required_java(str(target))
        launcher = jdtls_launcher(str(target))
        config_dir = jdtls_config_dir(str(target))
        if not launcher or not config_dir:
            print(f"[失败] 解包后仍找不到 launcher/config 目录：{target}")
            return 2
        if required and java_version < required:
            print(f"[失败] 这套 JDT LS 要求 Java {required}+，而 {java} 是 {java_version}")
            return 2
        print(f"[校验] JDT LS 就绪：{target}（要求 Java {required or '未声明'}，launcher={Path(launcher).name}）")
        updates["javaLsPath"] = str(target)
        updates.setdefault("javaHome", str(Path(java).parent.parent))

    if not updates:
        return report_only(config_path)

    result = update_navigation_config(server_dir, updates, config_name=config_path.name)
    if not result.get("changed"):
        print("[未写入] 配置没有被修改，原因：")
        for problem in result.get("problems", []):
            print(f"    - {problem}")
        if not result.get("problems"):
            print("    - 目标值与原值相同")
        print("  原文件保持不动；修好上面的问题后重跑本命令。")
        return 2
    for change in result.get("changes", []):
        print(f"[写入] {change}（{result['strategy']}）")
    if result.get("backup"):
        print(f"       备份：{result['backup']}")
    for key, value in updates.items():
        print(f"[生效] navigation.{key} = {value}")
    print("\n下一步：python3 scripts/verify-p2-navigation.py --direct --workspace fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
