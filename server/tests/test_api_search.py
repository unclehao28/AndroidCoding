"""检索接口测试：结果内容、限制、取消、并发上限。"""
from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient

from app.main import create_app
from app.search import SearchEngine, SearchManager, SearchOutcome

EXPECTED_LITERAL_HITS = {
    "frameworks/base/core/java/demo/DisplayController.java",
    "frameworks/native/DisplayService.cpp",
    "docs/notes.md",
    "data/gbk.txt",
    "data/utf8bom.txt",
    "data/中文目录/说明.txt",
}


def search(client, **body):
    payload = {"query": "setBrightness", "workspace": "sample", **body}
    return client.post("/api/search", json=payload)


def test_literal_search_is_default_and_ignores_glob_and_hidden(client):
    body = search(client).json()
    assert body["status"] == "ok"
    paths = {match["path"] for match in body["matches"]}
    assert EXPECTED_LITERAL_HITS <= paths
    assert not any(path.startswith("out/") for path in paths), "out/ 默认排除"
    assert not any(path.startswith(".hidden/") for path in paths), "隐藏文件默认跳过"
    assert body["options"]["regex"] is False
    assert body["lineBase"] == 0
    assert body["columnUnit"] == "unicode-code-points"
    assert body["indexed"] is False
    assert body["truncated"] is False
    assert body["limit"] == 500
    assert body["elapsedMs"] >= 0
    assert body["requestId"]


def test_literal_match_payload_has_line_column_and_text(client):
    body = search(client, scope="data/中文目录").json()
    assert body["matches"], "中文路径下应能命中"
    match = body["matches"][0]
    assert match["path"] == "data/中文目录/说明.txt"
    assert match["line"] == 0 and match["lineDisplay"] == 1
    assert match["column"] == 0
    assert match["text"].startswith("setBrightness")
    assert match["source"] in {"ripgrep", "python-bounded"}


def test_search_is_case_insensitive_by_default_and_case_sensitive_on_request(client):
    loose = search(client, query="SETbrightness").json()
    assert loose["matches"], "默认忽略大小写"
    strict = search(client, query="SETbrightness", caseSensitive=True).json()
    assert strict["matches"] == []


def test_search_scope_limits_to_subdirectory(client):
    body = search(client, scope="docs").json()
    paths = {match["path"] for match in body["matches"]}
    assert paths == {"docs/notes.md"}


def test_search_whole_word_and_include_glob(client):
    globbed = search(client, includeGlob="**/*.java").json()
    paths = {match["path"] for match in globbed["matches"]}
    assert paths == {"frameworks/base/core/java/demo/DisplayController.java"}
    whole = search(client, query="setBright", wholeWord=True).json()
    assert whole["matches"] == []


def test_regex_mode_is_explicit_and_validated(client):
    valid = search(client, query=r"setBright\w+\(int", regex=True).json()
    assert valid["status"] == "ok"
    assert valid["options"]["regex"] is True
    assert any(match["path"].endswith("DisplayController.java") for match in valid["matches"])
    broken = search(client, query="setBrightness(", regex=True)
    assert broken.status_code == 400
    assert "正则" in broken.json()["error"]["message"]


def test_search_rejects_empty_query_and_unknown_workspace(client):
    assert search(client, query="   ").status_code == 400
    assert client.post("/api/search", json={"query": "x", "workspace": "nope"}).status_code == 404
    assert client.post("/api/search", json={"query": "x", "workspace": "sample", "typo": 1}).status_code == 422


def test_search_result_limit_marks_truncation(client):
    body = search(client, limit=2).json()
    assert len(body["matches"]) == 2
    assert body["truncated"] is True
    assert body["limit"] == 2
    assert body["reason"]


def test_search_limit_cannot_exceed_configured_maximum(make_client):
    client = make_client(limits={"maxSearchResults": 3})
    body = search(client, limit=1000).json()
    assert body["limit"] == 3
    assert len(body["matches"]) <= 3


def test_search_path_traversal_and_file_scope_rejected(client):
    assert search(client, scope="../").status_code == 400
    assert search(client, scope="docs/notes.md").status_code == 409


def test_search_reports_excluded_and_skipped_counts(client):
    body = search(client, query="setBrightness", scope="").json()
    assert isinstance(body["skipped"], dict)
    assert body["filesMatched"] == len({match["path"] for match in body["matches"]})


class _SlowEngine(SearchEngine):
    """测试替身：按取消标志退出，用来验证取消链路，不参与功能测试断言。"""

    name = "test-slow"
    kind = "test"
    version = "0"

    def __init__(self, seconds: float = 10.0):
        self.seconds = seconds
        self.started = threading.Event()
        self.observed_cancel = threading.Event()
        self.finished = threading.Event()

    async def search(self, request, cancel):
        self.started.set()
        loops = int(self.seconds / 0.05)
        for _ in range(max(1, loops)):
            if cancel.is_set():
                self.observed_cancel.set()
                self.finished.set()
                return SearchOutcome("cancelled", self.name, [], False, 0, {}, "测试替身收到取消")
            await asyncio.sleep(0.05)
        self.finished.set()
        return SearchOutcome("ok", self.name, [], False, 0, {}, "")


def test_cancel_endpoint_stops_running_search(config):
    engine = _SlowEngine(seconds=15)
    app = create_app(config, search_manager=SearchManager(config, engine=engine))
    result: dict = {}
    with TestClient(app) as client:
        thread = threading.Thread(
            target=lambda: result.update(client.post("/api/search", json={"query": "setBrightness", "workspace": "sample", "requestId": "slow-1"}).json())
        )
        thread.start()
        assert engine.started.wait(5), "检索未开始"
        cancel_body = client.post("/api/search/cancel", json={"requestId": "slow-1"}).json()
        thread.join(timeout=10)
        assert cancel_body["cancelled"] is True
    assert engine.observed_cancel.is_set(), "引擎应观察到取消标志"
    assert result["status"] == "cancelled"
    assert result["requestId"] == "slow-1"


def test_cancel_unknown_request_id_reports_false(client):
    body = client.post("/api/search/cancel", json={"requestId": "never-started"}).json()
    assert body["cancelled"] is False
    assert body["activeSearches"] == []
    assert body["note"]


def test_concurrency_limit_returns_busy(make_config):
    config = make_config(limits={"maxConcurrentSearches": 1, "searchTimeoutSeconds": 1})
    engine = _SlowEngine(seconds=6)
    app = create_app(config, search_manager=SearchManager(config, engine=engine))
    first: dict = {}
    second: dict = {}
    with TestClient(app) as client:
        thread = threading.Thread(
            target=lambda: first.update(
                client.post("/api/search", json={"query": "a", "workspace": "sample", "requestId": "busy-1"}).json()
            )
        )
        thread.start()
        assert engine.started.wait(5)
        second.update(
            client.post("/api/search", json={"query": "b", "workspace": "sample", "requestId": "busy-2"}).json()
        )
        client.post("/api/search/cancel", json={"requestId": "busy-1"})
        client.post("/api/search/cancel", json={"requestId": "busy-2"})
        thread.join(timeout=15)
    assert second["status"] == "busy"
    assert "并发" in second["reason"]
    assert engine.finished.wait(5)
