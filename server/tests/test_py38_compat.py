"""Python 3.8 兼容性守卫。

背景：公司源码服务器是 Ubuntu 20.04 + Python 3.8.10，而本机是 3.13。
语法能通过不代表 3.8 能运行，所以这里用静态检查 + 运行时代码路径检查兜住两件事：

1. FastAPI/pydantic 会在运行时求值注解（`typing.get_type_hints`），
   Python 3.8 无法求值 `dict[str, Any]`（PEP 585）和 `str | None`（PEP 604）。
   对外的入口模块 `app/main.py` 因此必须使用 `typing.Optional/Dict/List`。
2. `asyncio.to_thread`、`str.removeprefix`、`functools.cache` 等 3.9+ API 不能直接使用。
   检索模块改为 `_run_in_thread` 能力探测，这里也要验证没有回退。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import threading
import typing

from app.main import create_app
from app.search import PythonBoundedEngine

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
BUILTIN_GENERICS = {"dict", "list", "tuple", "set", "frozenset", "type", "collections"}
PY39_ONLY_ATTRIBUTES = {
    ("asyncio", "to_thread"),
    ("str", "removeprefix"),
    ("str", "removesuffix"),
    ("functools", "cache"),
}


def _iter_evaluated_annotations(tree: ast.AST):
    """会被运行时求值的注解：函数签名 + 类字段。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in list(args.args) + list(args.kwonlyargs) + list(getattr(args, "posonlyargs", [])):
                if arg.annotation is not None:
                    yield arg.annotation
            for extra in (args.vararg, args.kwarg):
                if extra is not None and extra.annotation is not None:
                    yield extra.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AnnAssign):
                    yield item.annotation


def test_main_module_annotations_are_py38_safe():
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    problems = []
    for annotation in _iter_evaluated_annotations(tree):
        for node in ast.walk(annotation):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                problems.append(f"第 {node.lineno} 行使用了 X | Y（PEP 604）")
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in BUILTIN_GENERICS:
                problems.append(f"第 {node.lineno} 行使用了 {node.value.id}[...]（PEP 585）")
    assert not problems, "app/main.py 的注解在 Python 3.8 上无法求值：" + "；".join(sorted(set(problems)))


def test_no_python39_only_stdlib_apis_in_app():
    problems = []
    for path in sorted(APP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                pair = (node.value.id, node.attr)
                if pair in PY39_ONLY_ATTRIBUTES:
                    problems.append(f"{path.name}:{node.lineno} 使用了 {pair[0]}.{pair[1]}（3.9+）")
    assert not problems, "；".join(problems)


def test_all_route_endpoints_and_models_resolve_annotations(config):
    """FastAPI 会对端点调用 get_type_hints；这里主动做一遍，防止注解写错或漏导入。"""
    from fastapi.routing import APIRoute

    app = create_app(config)
    resolved = 0
    for route in app.routes:
        if isinstance(route, APIRoute):
            typing.get_type_hints(route.endpoint)
            resolved += 1
    assert resolved >= 8, f"只解析到 {resolved} 个端点"


def test_search_engine_falls_back_without_asyncio_to_thread(config, monkeypatch):
    """模拟 Python 3.8：去掉 asyncio.to_thread，检索仍必须能跑。"""
    monkeypatch.delattr(asyncio, "to_thread", raising=False)
    root = config.root("sample")
    assert root is not None
    from app.search import SearchRequest

    request = SearchRequest(
        request_id="py38-fallback",
        root=root,
        query="setBrightness",
        start_dir=root.path,
        limit=20,
        timeout_seconds=config.limits.search_timeout_seconds,
        max_file_bytes=config.limits.max_search_file_bytes,
        max_line_length=config.limits.max_line_length,
        exclude_globs=config.search.exclude_globs,
    )
    outcome = asyncio.run(PythonBoundedEngine().search(request, threading.Event()))
    assert outcome.status == "ok", outcome.reason
    assert outcome.matches, "3.8 回退路径必须仍能返回结果"
