"""启动入口：python -m app --config config.json

同时提供 --check-config，只做配置校验并打印结果，不启动服务。
"""
from __future__ import annotations

import argparse
import sys

from .config import API_VERSION, VERSION, ConfigError, load_config, resolve_config_path


def _use_utf8_console() -> None:
    """Windows 控制台默认代码页不是 UTF-8，中文输出会乱码；能改就改，改不了忽略。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - 非文本流
            pass


def main(argv: list[str] | None = None) -> int:
    _use_utf8_console()
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="安卓源码工作台后端（P1：真实目录读取与受限检索）",
    )
    parser.add_argument("--config", help="配置文件路径；默认 server/config.json，其次 server/config.example.json")
    parser.add_argument("--host", help="覆盖配置中的监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="覆盖配置中的端口")
    parser.add_argument("--check-config", action="store_true", help="只校验配置并退出")
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
        print(f"    - {root.id}: {root.path}（readonly={root.readonly}）")
    for warning in config.warnings:
        print(f"  [警告] {warning}")

    if args.check_config:
        print("配置校验通过（--check-config，未启动服务）")
        return 0

    from .main import create_app

    app = create_app(config)
    engine = app.state.search_manager.engine_info()
    print(f"  检索引擎：{engine['name']} {engine.get('version', '')}（配置={engine['configured']}，并发上限={engine['concurrencyLimit']}）")
    print("  未实现：写入（P4）、语义导航（P2）、全库索引（P3）——相关接口会明确返回未就绪")

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
