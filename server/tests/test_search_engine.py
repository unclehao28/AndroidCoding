"""检索引擎单元测试：取消、上限、参数数组构造。"""
from __future__ import annotations

import threading

from app.search import PythonBoundedEngine, RipgrepEngine, STATUS_CANCELLED, STATUS_OK, SearchRequest


def build_request(config, **overrides) -> SearchRequest:
    root = config.root("sample")
    assert root is not None
    values = {
        "request_id": "unit-1",
        "root": root,
        "query": "setBrightness",
        "start_dir": root.path,
        "subpath": "",
        "limit": config.limits.max_search_results,
        "timeout_seconds": config.limits.search_timeout_seconds,
        "max_file_bytes": config.limits.max_search_file_bytes,
        "max_line_length": config.limits.max_line_length,
        "exclude_globs": config.search.exclude_globs,
        "respect_ignore_files": config.search.respect_ignore_files,
        "python_max_files": config.search.python_max_files,
    }
    values.update(overrides)
    return SearchRequest(**values)


def test_ripgrep_version_parsing(monkeypatch):
    """ripgrep 的 banner 是 "ripgrep 15.2.0 (rev e89fff89ac)"，不能把最后一个 token 当版本。"""
    import subprocess

    from app import search as search_module

    class _Completed:
        returncode = 0
        stdout = "ripgrep 15.2.0 (rev e89fff89ac)\nfeatures:+pcre2\nPCRE2 10.45 is available\n"

    monkeypatch.setattr(search_module.shutil, "which", lambda name: "/home/xuhao/bin/rg")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _Completed())
    detected = search_module.detect_ripgrep()
    assert detected == ("/home/xuhao/bin/rg", "15.2.0")


def test_manager_authored_semaphore_is_created_lazily(config):
    """Python 3.8/3.9 的 asyncio.Semaphore 会绑定创建时的 loop：
    在 __init__ 里创建会导致"没有当前 event loop"或跨 loop 报错，所以必须延后创建。"""
    import asyncio

    from app.search import SearchManager

    manager = SearchManager(config)
    assert manager._semaphore is None, "不得在 __init__ 里创建 asyncio.Semaphore"
    # 两次 asyncio.run 会使用两个不同的 event loop
    for _ in range(2):
        outcome = asyncio.run(manager.run(build_request(config, query="setBrightness", limit=1)))
        assert outcome.status == "ok", outcome.reason
    assert manager._semaphore is not None


def test_python_engine_reports_timeout_instead_of_hanging(config):
    import asyncio

    outcome = asyncio.run(
        PythonBoundedEngine().search(build_request(config, timeout_seconds=0.0001), threading.Event())
    )
    assert outcome.status == "timeout"
    assert outcome.truncated is True
    assert "上限" in outcome.reason


def test_python_engine_respects_pre_set_cancel(config):
    cancel = threading.Event()
    cancel.set()
    outcome = PythonBoundedEngine()._scan(build_request(config), cancel)
    assert outcome.status == STATUS_CANCELLED
    assert outcome.matches == []


def test_python_engine_truncates_on_limit(config):
    outcome = PythonBoundedEngine()._scan(build_request(config, limit=1), threading.Event())
    assert outcome.status == STATUS_OK
    assert len(outcome.matches) == 1
    assert outcome.truncated is True


def test_python_engine_reports_skipped_binary_and_large(config):
    outcome = PythonBoundedEngine()._scan(
        build_request(config, query="setBrightness", max_file_bytes=1500),
        threading.Event(),
    )
    assert outcome.skipped["binary"] >= 1
    assert outcome.skipped["large"] >= 1
    assert outcome.files_scanned is not None and outcome.files_scanned >= 1


def test_python_engine_stops_at_file_scan_cap(config):
    outcome = PythonBoundedEngine()._scan(build_request(config, python_max_files=1), threading.Event())
    assert "上限" in outcome.reason or outcome.truncated is True
    assert outcome.files_scanned is not None and outcome.files_scanned <= 2


def test_ripgrep_arguments_are_passed_as_array_without_shell():
    engine = RipgrepEngine("/usr/bin/rg", "14.1.0")
    request = SearchRequest(
        request_id="unit-2",
        root=None,  # build_args 不使用 root
        query="x'; rm -rf / #",
        start_dir=None,  # type: ignore[arg-type]
        subpath="frameworks",
        literal=True,
        case_sensitive=False,
        whole_word=True,
        include_glob="**/*.java",
        exclude_globs=("out/**", ".git/**"),
    )
    args = engine.build_args(request)
    assert isinstance(args, list)
    assert args[-1] == "frameworks"
    assert args[-2] == "x'; rm -rf / #", "查询串必须作为单个 argv 元素传递"
    assert "--fixed-strings" in args
    assert "--word-regexp" in args
    assert "--ignore-case" in args
    assert args.count("--glob") == 3
    assert "!out/**" in args and "**/*.java" in args
    assert "--no-ignore" not in args


def test_ripgrep_arguments_switch_for_regex_and_case_sensitivity():
    engine = RipgrepEngine("/usr/bin/rg", "14.1.0")
    request = SearchRequest(
        request_id="unit-3",
        root=None,
        query=r"setBright\w+",
        start_dir=None,  # type: ignore[arg-type]
        literal=False,
        case_sensitive=True,
        respect_ignore_files=False,
    )
    args = engine.build_args(request)
    assert "--fixed-strings" not in args
    assert "--ignore-case" not in args
    assert "--no-ignore" in args
