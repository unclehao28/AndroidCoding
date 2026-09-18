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
    print(f"  客户端访问：ssh -L {port}:127.0.0.1:{port} USER@SERVER 然后打开 http://127.0.0.1:{port}/")
    if host != "127.0.0.1":
        print(f"  [提醒] 当前监听 {host}，不是回环地址；P1 没有认证，请自行确认网络边界")


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
    args = parser.parse_args(argv)

    config_path = resolve_config_path(args.config)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"[配置错误] {config_path}", file=sys.stderr)
        for message in exc.messages:
            print(f"  - {message}", file=sys.stderr)
        return 2

    host = args.host or config.server.host
    port = args.port or config.server.port
    print(f"[配置] {config_path}")
    print(f"  版本：{VERSION}（API {API_VERSION}）")
    print(f"  监听：http://{host}:{port}")
    print(f"  前端目录：{config.prototype_dir}")
    print(f"  根目录：{len(config.roots)} 个")
    for root in config.roots:
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
