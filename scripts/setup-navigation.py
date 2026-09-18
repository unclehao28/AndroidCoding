"""一键准备 P2 语义跳转：找 AOSP 源码根、找 clangd、写进 config.json、自查。

用法（在仓库根目录执行，不需要先启动服务）：

    python3 scripts/setup-navigation.py                  # 探测 + 写配置
    python3 scripts/setup-navigation.py --acceptance     # 顺带跑 12 条跳转预期验收
    python3 scripts/setup-navigation.py --print-only     # 只探测，不改任何文件
    python3 scripts/setup-navigation.py --depth 6        # 探测更深（默认 4 层）
    python3 scripts/setup-navigation.py --aosp /data/aosp --clangd /opt/clangd/bin/clangd

它做三件事：
1. 在 /data /home /opt /mnt /srv /workspace 下按层找 Android 源码树（有深度与条目上限，不全盘扫描）；
2. 找 clangd：配置里指定的 → PATH → AOSP 自带 prebuilts/clang/host/linux-x86/*/bin/clangd → 常见系统位置；
3. 把结果按行写进 server/config.json（保留注释，先备份；文件语法损坏时从 config.example.json 重建后继续）。

输出里给出的命令都是可以直接复制执行的，不会出现需要你替换的占位符。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
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

from app.config import ConfigError  # noqa: E402
from app.navsetup import (  # noqa: E402
    DEFAULT_SEARCH_ROOTS,
    add_source_root,
    default_search_roots,
    detect,
    find_aosp_roots,
    find_partial_candidates,
    prepare_config,
)

VERIFY_ACCEPTANCE = ROOT / "scripts" / "verify-p2-navigation.py"


def _cmd(*parts) -> str:
    return " ".join(str(part) for part in parts)


def print_clangd_help() -> None:
    print("\n这套语义跳转需要 clangd，但目前没找到。按成本从低到高：")
    print("  1) 如果源码在这台机器上，AOSP 自带就够用（不需要装任何东西），把源码根告诉本脚本：")
    print(f"       {_cmd('python3', 'scripts/setup-navigation.py', '--aosp', '/data/aosp')}")
    print("       （把 /data/aosp 换成上面列出的真实目录）")
    print("  2) Debian/Ubuntu 且可以用 sudo：sudo apt-get install -y clangd")
    print("  3) 没有 sudo 但能访问软件源：")
    print("       mkdir -p ~/local && cd /tmp && apt-get download clangd-12 && dpkg -x clangd-12_*.deb ~/local")
    print("       ~/local/usr/bin/clangd --version    # 报缺库时用 ldd 看缺哪个，再同样解包 libclang-cpp*/libllvm*")
    print("  4) 离线：在能联网的机器取 LLVM 官方静态包（clang+llvm-*-x86_64-linux-gnu-ubuntu-*.tar.xz），")
    print("     解压出的 bin/clangd 放进 ~/bin，再执行：")
    print(f"       {_cmd('python3', 'scripts/setup-navigation.py', '--clangd', '~/bin/clangd')}")
    print("  每条路都会打印实际命令的输出，失败时把输出发我，我按真实报错继续。")


def print_report(result: dict) -> int:
    print(f"[配置] {result['configPath']}")
    if result.get("created"):
        print("  已从 config.example.json 生成 config.json（原来不存在）")
    if result.get("recovered"):
        print(f"  [已修复] 原配置无法解析：{result.get('reason', '')}")
        print(f"           备份：{result.get('recoveredFrom')}（脚本已重建为标准配置，下面继续写探测结果）")

    if result.get("blocked"):
        print("  [配置有错，未修改文件] 请先修好下面这些问题：")
        for problem in result.get("problems", []):
            print(f"    - {problem}")
        if result.get("hint"):
            print(f"  {result['hint']}")
        return 2

    detection = result.get("detection", {})
    roots = detection.get("aospRoots") or []
    signatures = detection.get("aospSignatures") or []
    print(f"[探测] 扫描目录 {detection.get('scannedDirs', 0)} 个，发现 Android 源码树 {len(roots)} 个")
    for index, item in enumerate(roots):
        sign = signatures[index] if index < len(signatures) else ""
        print(f"    - {item}" + (f"（{sign}）" if sign else ""))
    for note in detection.get("notes", []):
        print(f"  [提示] {note}")
    for candidate in detection.get("candidates", []):
        print(f"    候选：{candidate['path']}（{candidate.get('hint', '')}）")
        print(f"          若这就是源码，执行：{_cmd('python3', 'scripts/setup-navigation.py', '--aosp', candidate['path'])}")

    clangd = detection.get("clangdPath")
    if clangd:
        print(f"[clangd] {detection.get('clangdVersion') or '版本未知'} · {clangd}")
        print(f"         来源：{detection.get('clangdSource')}")
    else:
        print("[clangd] 未找到")

    patch = result.get("patch") or {}
    if patch.get("changed"):
        print(f"[写入] {patch.get('strategy')}")
        for change in patch.get("changes", []):
            print(f"    {change}")
        if patch.get("backup"):
            print(f"    备份：{patch['backup']}")
    else:
        print("[写入] 配置无需改动（没有新的探测结果）")

    navigation = result.get("navigation") or {}
    if navigation:
        print("[当前语义跳转配置]")
        print(f"    enabled={navigation.get('enabled')} clangdPath={navigation.get('clangdPath')}")
        print(f"    searchDirs={navigation.get('searchDirs')}")
        print(f"    compileCommandsDir={navigation.get('compileCommandsDir')}")

    for problem in result.get("problems", []):
        print(f"  [配置问题] {problem}")

    if clangd and roots:
        print("\n下一步（在服务器上验收 12 条跳转预期，不需要先启动服务）：")
        print(f"  {_cmd('python3', 'scripts/verify-p2-navigation.py', '--direct', '--workspace', 'fixtures')}")
        return 0

    if not roots:
        print("\n没有找到 Android 源码树，语义跳转拿不到编译参数与工具链。按顺序试：")
        print(f"  1) 扫得更深一点（默认只扫 {DEFAULT_SEARCH_ROOTS[0]} /home 等位置 {4} 层）：")
        print(f"       {_cmd('python3', 'scripts/setup-navigation.py', '--print-only', '--depth', 6)}")
        print("  2) 源码不在这些位置时，先在服务器上找它：")
        print('       find / -maxdepth 5 -type d -name prebuilts 2>/dev/null | head -20')
        print("     找到后把它的上一级（也就是源码根）传给本脚本，例如源码根是 /data/aosp 时：")
        print(f"       {_cmd('python3', 'scripts/setup-navigation.py', '--aosp', '/data/aosp')}   # /data/aosp 按实际情况替换")
    print_clangd_help()
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="配置文件路径，默认 server/config.json")
    parser.add_argument("--aosp", action="append", default=[], help="手工指定 AOSP 源码根（可重复）")
    parser.add_argument(
        "--add-root",
        action="append",
        default=[],
        help="把该目录登记成只读工作区（可重复），用于浏览/检索真实源码；不需要手工编辑 JSON",
    )
    parser.add_argument("--clangd", help="手工指定 clangd 可执行文件")
    parser.add_argument("--depth", type=int, default=4, help="探测深度上限，默认 4")
    parser.add_argument("--print-only", action="store_true", help="只探测，不修改配置")
    parser.add_argument("--acceptance", action="store_true", help="配好后直接运行 scripts/verify-p2-navigation.py")
    args = parser.parse_args()

    extra = [Path(item).expanduser() for item in args.aosp]

    if args.print_only:
        search_roots = extra + default_search_roots()
        roots, scanned = find_aosp_roots(search_roots, max_depth=args.depth)
        detection = detect(search_dirs=extra, configured_clangd=args.clangd, max_depth=args.depth, scan=not extra)
        print(f"[探测] 扫描 {scanned} 个目录（深度上限 {args.depth}），发现完整源码树 {len(roots)} 个")
        for item in roots:
            print(f"    - {item}")
        if not roots:
            for count, path, markers in find_partial_candidates(search_roots, max_depth=args.depth + 1):
                print(f"    候选：{path}（含 {count} 个标记：{'、'.join(markers[:4])}）")
        print(f"[clangd] {detection.clangd_path or '未找到'}")
        if not roots and not detection.clangd_path:
            print_clangd_help()
        return 0 if (roots and detection.clangd_path) else 2

    config_path = Path(args.config) if args.config else SERVER / "config.json"
    # 先把 --add-root 指定的目录登记成工作区，再让探测把它们当作 clangd 的搜索线索
    added = [Path(item).expanduser() for item in args.add_root]
    try:
        for item in added:
            report = add_source_root(config_path.parent, item, config_name=config_path.name)
            print(f"[源码根] {report['message']}")
            if report.get("backup"):
                print(f"    备份：{report['backup']}")
            for problem in report.get("problems", []):
                print(f"    [问题] {problem}")
        result = prepare_config(
            config_path.parent,
            aosp_roots=extra + added or None,
            clangd_path=args.clangd,
            config_name=config_path.name,
            max_depth=args.depth,
        )
    except ConfigError as exc:
        print("[配置错误] " + "；".join(exc.messages), file=sys.stderr)
        return 2

    code = print_report(result)
    if not args.acceptance:
        return code
    if code != 0:
        print("\n[验收未执行] 还缺 clangd 或源码根，先按上面的提示处理后重跑本命令。")
        return code
    print(f"\n[验收] {_cmd('python3', 'scripts/verify-p2-navigation.py', '--direct', '--workspace', 'fixtures')}")
    return subprocess.run(
        [sys.executable, str(VERIFY_ACCEPTANCE), "--direct", "--workspace", "fixtures"],
        cwd=str(ROOT),
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
