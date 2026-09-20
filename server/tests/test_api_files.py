"""健康状态、工作区、目录树、文件读取接口测试。"""
from __future__ import annotations

JAVA_PATH = "frameworks/base/core/java/demo/DisplayController.java"


def test_health_reports_real_mode_and_unimplemented_features(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "real"
    assert body["apiVersion"] == "p2"
    assert body["features"] == {"write": False, "navigation": True}
    assert body["navigation"]["supported"]["cpp"]["available"] in (True, False)
    assert body["capabilities"]["index"] is False
    assert body["search"]["name"] in {"ripgrep", "python-bounded", "unavailable"}
    assert body["activeSearches"] == []


def test_workspaces_lists_only_configured_roots(client, sample_tree):
    body = client.get("/api/workspaces").json()
    assert body["count"] == 1
    workspace = body["workspaces"][0]
    assert workspace["id"] == "sample"
    assert workspace["exists"] is True
    assert workspace["path"].endswith("sample-root")
    assert workspace["name"] == "样例源码"


def test_tree_lists_directories_first_and_files_with_sizes(client):
    body = client.get("/api/tree", params={"workspace": "sample", "path": ""}).json()
    entries = body["entries"]
    kinds = [item["type"] for item in entries]
    assert kinds == sorted(kinds, key=lambda kind: kind != "dir")
    names = [item["name"] for item in entries]
    assert "frameworks" in names and "data" in names
    nested = client.get("/api/tree", params={"workspace": "sample", "path": "data"}).json()
    assert nested["path"] == "data"
    assert "中文目录" in [item["name"] for item in nested["entries"]]
    file_entry = next(item for item in nested["entries"] if item["name"] == "gbk.txt")
    assert file_entry["type"] == "file" and file_entry["size"] > 0
    assert nested["readonly"] is True


def test_tree_rejects_file_path_and_unknown_workspace(client):
    assert client.get("/api/tree", params={"workspace": "sample", "path": "docs/notes.md"}).status_code == 409
    assert client.get("/api/tree", params={"workspace": "nope", "path": ""}).status_code == 404


def test_tree_marks_escaping_symlink_as_inaccessible(client, sample_tree, symlink_escape):
    body = client.get("/api/tree", params={"workspace": "sample", "path": ""}).json()
    entry = next(item for item in body["entries"] if item["name"] == "escape-link")
    assert entry["symlink"] is True
    assert entry["escaping"] is True
    assert entry["accessible"] is False


def test_tree_path_traversal_blocked_by_api(client):
    response = client.get("/api/tree", params={"workspace": "sample", "path": "../"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_path"


def test_read_file_returns_lines_hash_and_language(client):
    body = client.get("/api/file", params={"workspace": "sample", "path": JAVA_PATH}).json()
    assert body["status"] == "ok"
    assert body["language"] == "java"
    assert body["totalLines"] == len(body["lines"])
    assert body["hash"].startswith("sha256:")
    assert body["readonly"] is True
    assert body["eol"] == "lf"
    assert any("setBrightness" in line for line in body["lines"])
    assert body["hasMore"] is False


def test_read_file_line_window_and_clamping(client):
    window = client.get(
        "/api/file",
        params={"workspace": "sample", "path": JAVA_PATH, "startLine": 5, "lineCount": 3},
    ).json()
    assert window["startLine"] == 5
    assert window["lineCount"] == 3
    assert window["hasMore"] is True
    assert all(line.startswith(" ") or line for line in window["lines"])
    whole = client.get("/api/file", params={"workspace": "sample", "path": JAVA_PATH}).json()
    assert window["lines"] == whole["lines"][5:8]

    beyond = client.get(
        "/api/file",
        params={"workspace": "sample", "path": JAVA_PATH, "startLine": 10000},
    ).json()
    assert beyond["status"] == "ok"
    assert beyond["lines"] == [] and beyond["lineCount"] == 0


def test_read_file_enforces_max_line_count(make_client):
    client = make_client(limits={"maxLineCount": 5})
    body = client.get("/api/file", params={"workspace": "sample", "path": "data/many_lines.txt"}).json()
    assert body["lineCount"] == 5
    assert body["lineCountClamped"] is False  # 未显式请求行数，按上限返回
    assert body["totalLines"] == 50
    assert body["hasMore"] is True
    assert body["lines"] == [f"line {index} level" for index in range(1, 6)]

    oversized = client.get(
        "/api/file",
        params={"workspace": "sample", "path": "data/many_lines.txt", "lineCount": 100},
    ).json()
    assert oversized["lineCount"] == 5
    assert oversized["lineCountClamped"] is True
    assert oversized["maxLineCount"] == 5


def test_read_file_flags_binary_and_large_files(make_client):
    client = make_client(limits={"maxFileBytes": 4096})
    binary = client.get("/api/file", params={"workspace": "sample", "path": "data/binary.bin"}).json()
    assert binary["status"] == "binary" and binary["lines"] == []
    large = client.get("/api/file", params={"workspace": "sample", "path": "data/huge.txt"}).json()
    assert large["status"] == "too_large"
    assert large["size"] > 4096


def test_read_file_reports_encoding_for_gbk(client):
    body = client.get("/api/file", params={"workspace": "sample", "path": "data/gbk.txt"}).json()
    assert body["status"] == "ok"
    assert body["encoding"] == "gbk" and body["lossy"] is True


def test_read_file_missing_and_outside_root(client):
    assert client.get("/api/file", params={"workspace": "sample", "path": "nope.txt"}).status_code == 404
    assert client.get("/api/file", params={"workspace": "sample", "path": "docs"}).status_code == 409
    outside = client.get("/api/file", params={"workspace": "sample", "path": "../../etc/passwd"})
    assert outside.status_code == 400
    unknown = client.get("/api/file", params={"workspace": "other", "path": "docs/notes.md"})
    assert unknown.status_code == 404


def test_read_file_hash_changes_with_content(client, sample_tree):
    first = client.get("/api/file", params={"workspace": "sample", "path": "data/empty.txt"}).json()
    (sample_tree / "data" / "empty.txt").write_text("now not empty\n", encoding="utf-8")
    second = client.get("/api/file", params={"workspace": "sample", "path": "data/empty.txt"}).json()
    assert first["hash"] != second["hash"]
    assert second["totalLines"] == 1


def test_navigation_reports_stale_on_version_mismatch_without_fake_targets(client):
    """版本不一致必须返回 stale（而不是拿旧行号硬跳），且不得伪造目标。"""
    response = client.post(
        "/api/navigation",
        json={
            "workspace": "sample",
            "path": JAVA_PATH,
            "kind": "definition",
            "sourceVersion": "sha256:deadbeef",
            "position": {"line": 6, "character": 21},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stale"
    assert body["kind"] == "semantic"
    assert body["targets"] == []
    assert body["sourceVersion"] == "sha256:deadbeef"
    assert "版本" in body["reason"]
    assert body["capability"]["navigation"] is True
    assert body["fallback"]["kind"] == "text"
    assert body["positionUnit"] == "utf-16"


def test_navigation_without_java_language_server_is_honest(client):
    """Java 已接入（JDT LS），但本机没装时必须明确说明缺什么，且不得伪造目标。"""
    current = client.get("/api/file", params={"workspace": "sample", "path": JAVA_PATH}).json()["hash"]
    response = client.post(
        "/api/navigation",
        json={
            "workspace": "sample",
            "path": JAVA_PATH,
            "kind": "definition",
            "sourceVersion": current,
            "position": {"line": 6, "character": 21},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["targets"] == []
    assert "JDT LS" in body["reason"] or "JDK" in body["reason"]
    assert any("javaLsPath" in hint for hint in body["hints"])


def test_navigation_rejects_bad_path_and_unknown_workspace(client):
    bad = client.post("/api/navigation", json={"workspace": "sample", "path": "../x.java"})
    assert bad.status_code == 400
    unknown = client.post("/api/navigation", json={"workspace": "zzz", "path": JAVA_PATH})
    assert unknown.status_code == 404
    missing = client.post("/api/navigation", json={"workspace": "sample", "path": "not/here.java"})
    assert missing.status_code == 404


def test_unknown_api_route_returns_json_error(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_route"


def test_static_frontend_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "安卓源码工作台" in response.text
    assert client.get("/app.js").status_code == 200
