"""启动入口：python -m app --config config.json

同时提供 --check-config，只做配置校验并打印结果，不启动服务。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import API_VERSION, VERSION, ConfigError, load_config, resolve_config_path


def _use_utf8_console() -> None:
    """Windows 控制台默认代码页不是 UTF-8，中文输出会乱码；能改就改，改不了忽略。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - 非文本流
            pass


ANDROID_MARKERS = (
    ".repo",
    "build/soong",
    "build/make",
    "frameworks/base",
    "hardware/interfaces",
    "system/core",
    "bionic",
    "art",
    "packages/apps",
    "device",
    "vendor",
    "kernel",
)


def _looks_like_android_tree(root_path: Path) -> list[str]:
    """只做少量标记文件探测，不遍历目录（整套源码遍历成本太高）。"""
    return [marker for marker in ANDROID_MARKERS if (root_path / marker).exists()]


def _requirements_file() -> str:
    """依赖清单按解释器版本选择：3.8/3.9 不能安装 requirements.txt 里锁定的版本。"""
    return "requirements-py38.txt" if sys.version_info < (3, 10) else "requirements.txt"


def _print_environment(host: str, port: int) -> None:
    import platform

    requirements = _requirements_file()
    note = f" · 低于 3.10，依赖请用 {requirements}" if sys.version_info < (3, 10) else ""
    print(f"  Python：{platform.python_version()}（{sys.executable}）{note}")
    try:
        from .search import detect_ripgrep

        rg = detect_ripgrep()
    except Exception:  # noqa: BLE001
        rg = None
    if rg:
        print(f"  检索引擎：ripgrep {rg[1]}（{rg[0]}）")
    else:
        print("  检索引擎：未找到 rg，将退回受限的 Python 扫描")
        print("    [提醒] Python 回退有文件数上限，整套安卓源码上结果会不完整；建议在服务器安装 ripgrep")
        print("            Debian/Ubuntu: sudo apt-get install -y ripgrep")
        print("            RHEL/CentOS:   sudo yum install -y ripgrep   或  dnf install -y ripgrep")
        print("            无 root：从 https://github.com/BurntSushi/ripgrep/releases 取静态包解压后加进 PATH")
    from .gitremote import git_version

    git_note = git_version() or "未安装（远程仓库无法同步）"
    print(f"  git：{git_note}")
    print(f"  客户端访问：ssh -L {port}:127.0.0.1:{port} USER@SERVER 然后打开 http://127.0.0.1:{port}/")
    if host != "127.0.0.1":
        print(f"  [提醒] 当前监听 {host}，不是回环地址；P1 没有认证，请自行确认网络边界")


def _print_git_status(config) -> int:
    from .gitremote import git_version, read_state

    print(f"  git：{git_version() or '未安装（远程仓库无法同步）'}")
    if config.cache_dir:
        print(f"  缓存目录：{config.cache_dir}")
    if not config.remote_roots:
        print("  没有配置远程仓库工作区（roots[].git）；本地目录无需同步")
    for root in config.roots:
        if not root.is_remote:
            print(f"  - {root.id}（本地目录）: {root.path}")
            continue
        state = read_state(root.path, url=root.git.url, ref=root.git.ref)
        marker = "已同步" if state["isRepo"] else ("未同步" if not state["checkoutExists"] else "目录异常")
        detail = (
            f"HEAD={state['head']} · 分支={state['branch']} · 浅克隆={state['shallow']} · "
            f"未提交改动={state['dirtyFiles']}"
        )
        print(f"  - {root.id}（远程）: {root.git.url}")
        print(f"      检出目录：{root.path}")
        print(f"      状态：{marker} · {detail}")
        if state.get("error"):
            print(f"      注意：{state['error']}")
    return 0


def _sync_workspaces(config, target: str) -> int:
    from .gitremote import git_binary, sync_root

    if git_binary() is None:
        print("[错误] 服务器上没有 git，无法同步远程仓库", file=sys.stderr)
        return 3
    if not config.remote_roots:
        print("没有配置远程仓库工作区（roots[].git），无需同步")
        return 0
    targets = [root for root in config.remote_roots if not target or root.id == target]
    if not targets:
        print(f"[错误] 没有 id 为 {target} 的远程工作区", file=sys.stderr)
        return 2
    failures = 0
    for root in targets:
        sparse = " · sparse=" + ",".join(root.git.sparse_paths) if root.git.sparse_paths else ""
        print(f"[同步] {root.id} ← {root.git.url}（depth={root.git.depth}{sparse}）", flush=True)
        print(f"       检出目录：{root.path}", flush=True)
        outcome = sync_root(
            root,
            config.cache_dir,
            timeout=config.limits.sync_timeout_seconds,
            on_line=lambda line: print(f"       | {line}", flush=True),
        )
        print(f"       结果：{outcome['status']} · {outcome['message']}", flush=True)
        if outcome.get("head"):
            print(f"       HEAD：{outcome['head']}", flush=True)
        if outcome["status"] not in ("ready", "skipped", "partial"):
            failures += 1
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    _use_utf8_console()
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="安卓源码工作台后端（P1：真实目录读取与受限检索）",
    )
    parser.add_argument("--config", help="配置文件路径；默认 server/config.json，其次 server/config.example.json")
    parser.add_argument("--host", help="覆盖配置中的监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="覆盖配置中的端口")
    parser.add_argument("--check-config", action="store_true", help="只校验配置、打印环境自查并退出")
    parser.add_argument(
        "--git-status",
        action="store_true",
        help="只打印各工作区的 git 状态（本地目录 / 远程仓库是否已同步）后退出",
    )
    parser.add_argument(
        "--sync",
        nargs="?",
        const="",
        metavar="WORKSPACE",
        help="同步远程仓库工作区到本地缓存（省略 id 表示全部远程工作区）。只做 clone/fetch/merge --ff-only",
    )
    args = parser.parse_args(argv)

    config_path = resolve_config_path(args.config)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"[配置错误] {config_path}", file=sys.stderr)
        for message in exc.messages:
            print(f"  - {message}", file=sys.stderr)
        return 2

    # 这两个子命令只需要标准库 + git，不依赖 fastapi，便于在装依赖之前先把源码拉下来
    if args.git_status:
        print(f"[git 状态] {config_path}")
        return _print_git_status(config)
    if args.sync is not None:
        print(f"[同步远程工作区] {config_path}")
        return _sync_workspaces(config, args.sync)

    host = args.host or config.server.host
    port = args.port or config.server.port
    print(f"[配置] {config_path}")
    print(f"  版本：{VERSION}（API {API_VERSION}）")
    print(f"  监听：http://{host}:{port}")
    print(f"  前端目录：{config.prototype_dir}")
    print(f"  根目录：{len(config.roots)} 个（其中远程仓库 {len(config.remote_roots)} 个）")
    for root in config.roots:
        if root.is_remote:
            from .gitremote import read_state

            state = read_state(root.path)
            if state.get("isRepo") and state.get("head"):
                synced = f"已同步 · HEAD={state['head']}"
            elif state.get("isRepo"):
                synced = "半成品（上次同步被中断），下次同步会清除后重来"
            else:
                synced = "未同步，需要执行 --sync"
            sparse = " · sparse=" + ",".join(root.git.sparse_paths) if root.git.sparse_paths else ""
            ref = f" · ref={root.git.ref}" if root.git.ref else ""
            print(f"    - {root.id}: {root.path}（远程只读 · {synced}）")
            print(f"        来源：{root.git.url}{ref} · depth={root.git.depth}{sparse}")
            continue
        markers = _looks_like_android_tree(root.path)
        hint = f" · 安卓源码标记：{', '.join(markers[:4])}{' 等' if len(markers) > 4 else ''}" if markers else " · 未发现常见安卓源码标记"
        print(f"    - {root.id}: {root.path}（readonly={root.readonly}）{hint}")
    for warning in config.warnings:
        print(f"  [警告] {warning}")
    _print_environment(host, port)

    if args.check_config:
        print("环境自查与配置校验通过（--check-config，未启动服务）")
        print("接下来：python -m app --config " + str(config_path.name) + "   然后按上面的客户端访问命令连接")
        return 0

    try:
        import uvicorn
        from .main import create_app
    except ImportError as exc:
        print(f"[错误] 缺少依赖：{exc}", file=sys.stderr)
        print(f"  请先执行：{sys.executable} -m pip install --user -r {_requirements_file()}", file=sys.stderr)
        return 3

    app = create_app(config)
    print("  未实现：写入（P4）、语义导航（P2）、全库索引（P3）——相关接口会明确返回未就绪")

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
