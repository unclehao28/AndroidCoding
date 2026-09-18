"""一键准备 P2 语义跳转：找 AOSP 源码根、找 clangd、写进 config.json、自查。

用法（在仓库根目录执行，不需要先启动服务）：

    python3 scripts/setup-navigation.py                 # 探测并自动写配置
    python3 scripts/setup-navigation.py --print-only    # 只探测，不动配置
    python3 scripts/setup-navigation.py --aosp /data/aosp --clangd /data/aosp/prebuilts/clang/host/linux-x86/clang-r547379/bin/clangd
    python3 scripts/setup-navigation.py --acceptance    # 配好之后直接跑 12 条预期验收

它做三件事：
1. 在 /data /home /opt /mnt /srv /workspace 下按层找 Android 源码树（有深度与条目上限，不全盘扫描）；
2. 找 clangd：配置里指定的 → PATH → AOSP 自带 prebuilts/clang/host/linux-x86/*/bin/clangd → 常见系统位置；
3. 把结果按行写进 server/config.json（保留注释，先备份；文件损坏时从 config.example.json 重建）。

找不到 clangd 时会打印可选的替代方式，不会往配置里写假路径。
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
from app.navsetup import default_search_roots, detect, find_aosp_roots, prepare_config  # noqa: E402

MANUAL_HINTS = (
    "AOSP 自带 clangd，优先复用它：",
    "  1) 确认源码根：ls -d <某个目录>/prebuilts/clang/host/linux-x86/*/bin/clangd",
    "  2) 把源码根传给本脚本：python3 scripts/setup-navigation.py --aosp <那个目录>",
    "如果源码根本机没有（例如代码在另一台机器上），再考虑：",
    "  3) 免 root 装一个：在能联网的机器下载 LLVM 官方静态包（x86_64-linux-gnu-ubuntu-*.tar.xz，约 1GB），",
    "     解压后把 bin/clangd 放进 ~/bin，再执行 python3 scripts/setup-navigation.py --clangd ~/bin/clangd",
    "  4) Debian/Ubuntu 且有 sudo：sudo apt-get install -y clangd",
)


def print_report(result: dict) -> int:
    print(f"[配置] {result['configPath']}")
    if result.get("created"):
        print("  已从 config.example.json 生成 config.json（原来不存在）")
    if result.get("recovered"):
        print(f"  [注意] 原配置无法解析（{result.get('reason', '')}），已备份并重建：")
        print(f"    备份：{result['recoveredFrom']}")
        print("  请重新运行本脚本以写入探测结果")
        return 0

    if result.get("blocked"):
        print("  [配置有错，未修改文件] 请先修好下面这些问题：")
        for problem in result.get("problems", []):
            print(f"    - {problem}")
        if result.get("hint"):
            print(f"  {result['hint']}")
        return 2

    detection = result.get("detection", {})
    roots = detection.get("aospRoots") or []
    print(f"[探测] 扫描目录 {detection.get('scannedDirs', 0)} 个，发现 Android 源码树 {len(roots)} 个")
    for item in roots[:5]:
        print(f"    - {item}")
    if len(roots) > 5:
        print(f"    …… 其余 {len(roots) - 5} 个未列出")
    for note in detection.get("notes", []):
        print(f"  [提示] {note}")

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

    for problem in result.get("problems", []):
        print(f"  [配置问题] {problem}")

    if clangd and roots:
        print("\n下一步（在服务器上验收 12 条跳转预期）：")
        print("  python3 scripts/verify-p2-navigation.py --direct --workspace fixtures")
        return 0

    print("\n还差 clangd，语义跳转暂时只能返回未就绪。可选做法：")
    for line in MANUAL_HINTS:
        print(f"  {line}")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="配置文件路径，默认 server/config.json")
    parser.add_argument("--aosp", action="append", default=[], help="手工指定 AOSP 源码根（可重复）")
    parser.add_argument("--clangd", help="手工指定 clangd 可执行文件")
    parser.add_argument("--depth", type=int, default=None, help="探测深度上限")
    parser.add_argument("--print-only", action="store_true", help="只探测，不修改配置")
    parser.add_argument("--acceptance", action="store_true", help="配好后直接运行 scripts/verify-p2-navigation.py")
    args = parser.parse_args()

    if args.print_only:
        extra = [Path(item) for item in args.aosp]
        roots, scanned = find_aosp_roots(extra + default_search_roots())
        detection = detect(search_dirs=extra, configured_clangd=args.clangd)
        print(f"[探测] 扫描 {scanned} 个目录，发现 AOSP 根 {len(roots)} 个")
        for item in roots:
            print(f"    - {item}")
        print(f"[clangd] {detection.clangd_path or '未找到'}")
        return 0 if detection.clangd_path else 2

    config_path = Path(args.config) if args.config else SERVER / "config.json"
    try:
        result = prepare_config(
            config_path.parent,
            aosp_roots=[Path(item) for item in args.aosp] or None,
            clangd_path=args.clangd,
            config_name=config_path.name,
        )
    except ConfigError as exc:
        print("[配置错误] " + "；".join(exc.messages), file=sys.stderr)
        return 2

    code = print_report(result)
    if args.acceptance and code == 0:
        print("\n[验收] scripts/verify-p2-navigation.py --direct --workspace fixtures")
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify-p2-navigation.py"), "--direct", "--workspace", "fixtures"],
            cwd=str(ROOT),
        ).returncode
    return code


if __name__ == "__main__":
    raise SystemExit(main())
