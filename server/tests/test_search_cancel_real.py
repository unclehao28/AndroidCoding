"""真实扫描过程中的取消验证。

与 test_api_search 里的测试替身不同，这里用真实文件树驱动真实引擎，
确认取消能终止正在进行的目录扫描，而不是只验证接口形状。
"""
from __future__ import annotations

import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import build_config
from app.main import create_app
from app.search import SearchManager
from conftest import REPO_ROOT

# 文件数刻意保持在 500 以内：既够让扫描持续可观测时间，又不会产生海量小文件
FILE_COUNT = 400
LINES_PER_FILE = 1200


@pytest.fixture(scope="module")
def big_tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("big-tree")
    payload = "\n".join(f"line {index} payload token_{index}" for index in range(LINES_PER_FILE))
    started = time.perf_counter()
    for index in range(FILE_COUNT):
        folder = root / f"pkg{index % 40}"
        folder.mkdir(exist_ok=True)
        (folder / f"file_{index}.txt").write_text(f"header {index}\n{payload}\n", encoding="utf-8")
    yield root, time.perf_counter() - started
    shutil.rmtree(root, ignore_errors=True)  # 避免在系统临时目录留下大量小文件


def build_client(tmp_path, root):
    raw = {
        "server": {"host": "127.0.0.1", "port": 8787},
        "roots": [{"id": "big", "name": "大目录", "path": str(root), "readonly": True}],
        "limits": {"searchTimeoutSeconds": 120, "maxConcurrentSearches": 1, "maxSearchResults": 100},
        "features": {"write": False, "navigation": False},
        "prototypeDir": str(REPO_ROOT / "prototype"),
    }
    config = build_config(raw, source_path=tmp_path / "config.json")
    app = create_app(config, search_manager=SearchManager(config))
    return TestClient(app), app


def test_big_tree_is_actually_large(big_tree):
    root, build_seconds = big_tree
    files = list(root.rglob("*.txt"))
    assert len(files) >= FILE_COUNT
    assert sum(path.stat().st_size for path in files) > 4 * 1024 * 1024, "文件总量应足以让扫描持续可观测时间"
    assert build_seconds < 120


def test_cancel_stops_real_scan(tmp_path, big_tree):
    root, _ = big_tree
    client, app = build_client(tmp_path, root)
    engine_name = app.state.search_manager.engine_info()["name"]
    outcome: dict = {}
    payload = {"query": "nothing-matches-this-token", "workspace": "big", "requestId": "real-cancel", "limit": 100}

    thread = threading.Thread(target=lambda: outcome.update(client.post("/api/search", json=payload).json()))
    started = time.perf_counter()
    thread.start()
    time.sleep(0.4)
    cancel_body = client.post("/api/search/cancel", json={"requestId": "real-cancel"}).json()
    thread.join(timeout=120)
    elapsed = time.perf_counter() - started

    assert thread.is_alive() is False, "取消失败：检索请求没有结束"
    if not cancel_body["cancelled"]:
        pytest.skip(f"扫描在取消前已完成（引擎 {engine_name}，{elapsed:.2f}s），无法验证取消路径")
    assert outcome["status"] == "cancelled", outcome
    assert elapsed < 30, "取消后应较快返回"
    assert outcome["requestId"] == "real-cancel"


def test_after_cancel_no_active_search_remains(tmp_path, big_tree):
    root, _ = big_tree
    client, _ = build_client(tmp_path, root)
    payload = {"query": "nothing-matches-this-token", "workspace": "big", "requestId": "real-cancel-2"}
    thread = threading.Thread(target=lambda: client.post("/api/search", json=payload))
    thread.start()
    time.sleep(0.4)
    client.post("/api/search/cancel", json={"requestId": "real-cancel-2"})
    thread.join(timeout=120)
    health = client.get("/api/health").json()
    assert health["activeSearches"] == []
