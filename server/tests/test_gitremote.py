"""远程 Git 仓库工作区：配置校验 + 真实 clone/fetch 同步。

全部使用本地 file:// 仓库作为"远程"，不依赖外网；真实公开仓的同步情况见
HANDOFF_STATUS.md 的手工验证记录。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, build_config
from app.gitremote import read_state, sync_root
from app.main import create_app
from conftest import REMOTE_MARKER, default_raw, remote_raw


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


def make_remote_config(tmp_path: Path, bare: Path, **git_overrides):
    root = {
        "id": "remote",
        "name": "远程样例",
        "path": "checkout",
        "readonly": True,
        "git": {"url": file_url(bare), "ref": "main", "depth": 1, **git_overrides},
    }
    cache = tmp_path / "cache"
    raw = remote_raw(root, cache_dir=cache)
    return build_config(raw, source_path=tmp_path / "config.json"), cache


# ---------------------------------------------------------------- 配置校验


@pytest.mark.parametrize(
    "git_block,expect",
    [
        ({"url": "https://gitlab.example.com/aosp/x.git; rm -rf /"}, "git.url"),
        ({"url": "-u"}, "git.url"),
        ({"url": "https://exa mple.com/x.git"}, "git.url"),
        ({"url": "git@host:repo.git", "ref": "main; rm -rf /"}, "git.ref"),
        ({"url": "git@host:repo.git", "depth": 0}, "git.depth"),
        ({"url": "git@host:repo.git", "sparsePaths": ["../etc"]}, "sparsePaths"),
        ({"url": "git@host:repo.git", "sparsePaths": ["/abs"]}, "sparsePaths"),
    ],
)
def test_invalid_git_blocks_are_rejected(tmp_path, git_block, expect):
    root = {"id": "r", "path": "checkout", "readonly": True, "git": git_block}
    raw = remote_raw(root, cache_dir=tmp_path / "cache")
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any(expect in message for message in excinfo.value.messages), excinfo.value.messages


def test_git_url_requires_cache_dir(tmp_path):
    root = {"id": "r", "path": "checkout", "readonly": True, "git": {"url": "https://example.com/x.git"}}
    raw = default_raw(tmp_path)
    raw["roots"] = [root]
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("cacheDir" in message for message in excinfo.value.messages)


def test_git_checkout_must_stay_inside_cache_dir(tmp_path):
    root = {"id": "r", "path": "/tmp/somewhere-else", "readonly": True, "git": {"url": "https://example.com/x.git"}}
    raw = remote_raw(root, cache_dir=tmp_path / "cache")
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("cacheDir" in message for message in excinfo.value.messages)


def test_remote_root_must_be_readonly(tmp_path):
    root = {"id": "r", "path": "checkout", "readonly": False, "git": {"url": "https://example.com/x.git"}}
    raw = remote_raw(root, cache_dir=tmp_path / "cache")
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("readonly" in message for message in excinfo.value.messages)


def test_existing_non_git_dir_blocks_remote_root(tmp_path):
    cache = tmp_path / "cache"
    (cache / "checkout").mkdir(parents=True)
    (cache / "checkout" / "junk.txt").write_text("占用\n", encoding="utf-8")
    root = {"id": "r", "path": "checkout", "readonly": True, "git": {"url": "https://example.com/x.git"}}
    raw = remote_raw(root, cache_dir=cache)
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("不是 git 仓库" in message or "git 仓库存根" in message for message in excinfo.value.messages)


def test_public_config_exposes_remote_metadata(tmp_path, git_source_repo):
    config, cache = make_remote_config(
        tmp_path, git_source_repo, sparsePaths=["libcutils"]
    )
    public = config.to_public()
    assert public["cacheDir"] == str(cache)
    remote = public["roots"][0]
    assert remote["remote"] is True
    assert remote["git"]["ref"] == "main"
    assert remote["git"]["sparsePaths"] == ["libcutils"]
    assert remote["git"]["writeSupported"] is False


# ---------------------------------------------------------------- 同步行为


def test_fixture_bare_repo_default_branch_is_main(git_source_repo):
    """老 git（<2.28）不支持 init.defaultBranch，夹具必须显式改名，否则 ref=main 会找不到分支。"""
    completed = subprocess.run(
        ["git", "--git-dir", str(git_source_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "main"


def test_clone_with_wrong_ref_lists_available_branches(tmp_path, git_source_repo):
    config, cache = make_remote_config(tmp_path, git_source_repo, ref="no-such-branch")
    root = config.root("remote")
    outcome = sync_root(root, cache, timeout=120)
    assert outcome["status"] == "error"
    assert "远端可用分支" in outcome["message"], outcome["message"]
    assert "main" in outcome["message"]


def test_clone_from_remote_and_read_state(tmp_path, git_source_repo):
    config, cache = make_remote_config(tmp_path, git_source_repo)
    root = config.root("remote")
    assert root is not None
    assert read_state(root.path)["isRepo"] is False

    outcome = sync_root(root, cache, timeout=120)
    assert outcome["status"] == "ready", outcome
    assert outcome["steps"] == ["clone"]
    assert (root.path / "libcutils" / "socket_utils.c").is_file()

    state = read_state(root.path)
    assert state["isRepo"] is True
    assert state["head"] == outcome["head"]
    assert state["dirtyFiles"] == 0
    assert state["originUrl"] == file_url(git_source_repo)


def test_sparse_checkout_limits_working_tree(tmp_path, git_source_repo):
    config, cache = make_remote_config(tmp_path, git_source_repo, sparsePaths=["libcutils"])
    root = config.root("remote")
    outcome = sync_root(root, cache, timeout=120)
    assert outcome["status"] == "ready", outcome["message"]
    assert (root.path / "libcutils" / "socket_utils.c").is_file()
    assert not (root.path / "libutils").exists(), "未指定的目录不应出现在稀疏检出里"


def test_second_sync_fast_forwards_new_commit(tmp_path, git_source_repo):
    config, cache = make_remote_config(tmp_path, git_source_repo)
    root = config.root("remote")
    first = sync_root(root, cache, timeout=120)
    assert first["status"] == "ready"

    work = None
    for candidate in git_source_repo.parent.glob("origin-work"):
        work = candidate
    assert work is not None
    identity = ["-c", "user.name=asw-test", "-c", "user.email=asw-test@example.invalid"]
    (work / "libcutils" / "socket_utils.c").write_text(
        f"// updated\nint {REMOTE_MARKER}(int value) {{ return value + 1; }}\n", encoding="utf-8"
    )
    subprocess.run(["git", *identity, "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "update"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", str(git_source_repo), "main"], cwd=work, check=True, capture_output=True)

    second = sync_root(root, cache, timeout=120)
    assert second["status"] == "ready", second["message"] + "\n" + "\n".join(second["log"])
    # 浅克隆更新走 fetch + checkout --detach（浅历史没有共同祖先，不能 merge）
    assert second["steps"] == ["fetch", "checkout --detach"]
    assert second["head"] != first["head"]
    assert "return value + 1" in (root.path / "libcutils" / "socket_utils.c").read_text(encoding="utf-8")


def test_interrupted_clone_is_cleaned_and_retried(tmp_path, git_source_repo):
    """取消/中断会留下只有 .git、没有 HEAD 的半成品，同步时应清掉并重新 clone。"""
    config, cache = make_remote_config(tmp_path, git_source_repo)
    root = config.root("remote")
    root.path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root.path, check=True, capture_output=True)
    assert read_state(root.path)["isRepo"] is True
    assert read_state(root.path)["head"] is None

    outcome = sync_root(root, cache, timeout=120)
    assert outcome["status"] == "ready", outcome["message"] + "\n" + "\n".join(outcome["log"])
    assert any("被中断" in line for line in outcome["log"])
    assert (root.path / "libcutils" / "socket_utils.c").is_file()


def test_dirty_cache_is_never_overwritten(tmp_path, git_source_repo):
    config, cache = make_remote_config(tmp_path, git_source_repo)
    root = config.root("remote")
    assert sync_root(root, cache, timeout=120)["status"] == "ready"
    target = root.path / "libcutils" / "socket_utils.c"
    target.write_text("// 本地手工修改，不能被覆盖\n", encoding="utf-8")

    outcome = sync_root(root, cache, timeout=120)
    assert outcome["status"] == "skipped"
    assert "未执行更新" in outcome["message"]
    assert target.read_text(encoding="utf-8") == "// 本地手工修改，不能被覆盖\n"


def test_sync_reports_timeout_instead_of_hanging(tmp_path, git_source_repo):
    """timeout=0 时必须在第一次检查就判定超时（不依赖真实耗时，避免偶发）。"""
    config, cache = make_remote_config(tmp_path, git_source_repo)
    root = config.root("remote")
    outcome = sync_root(root, cache, timeout=0.0)
    assert outcome["status"] == "timeout"
    assert any("已终止 git" in line for line in outcome["log"])


def test_sync_reports_cancellation(tmp_path, git_source_repo):
    import threading

    config, cache = make_remote_config(tmp_path, git_source_repo)
    root = config.root("remote")
    cancel = threading.Event()
    cancel.set()
    outcome = sync_root(root, cache, cancel=cancel, timeout=120)
    assert outcome["status"] == "cancelled"
    assert any("客户端取消" in line for line in outcome["log"])


def test_sync_refuses_checkout_outside_cache(tmp_path, git_source_repo):
    from app.config import GitSource, RootConfig

    config, cache = make_remote_config(tmp_path, git_source_repo)
    root = config.root("remote")
    assert root is not None
    rogue = RootConfig(
        id="rogue",
        name="越界",
        path=tmp_path / "outside",
        readonly=True,
        git=GitSource(url=file_url(git_source_repo), ref="main", depth=1),
    )
    outcome = sync_root(rogue, cache, timeout=30)
    assert outcome["status"] == "error"
    assert "cacheDir" in outcome["message"]


# ---------------------------------------------------------------- 接口


def test_workspaces_api_reports_remote_state(tmp_path, git_source_repo):
    config, _cache = make_remote_config(tmp_path, git_source_repo)
    with TestClient(create_app(config)) as client:
        body = client.get("/api/workspaces").json()
        remote = body["workspaces"][0]
        assert remote["remote"] is True
        assert remote["exists"] is False
        assert remote["sync"]["isRepo"] is False
        assert body["sync"]["running"] is False

        health = client.get("/api/health").json()
        assert health["git"]["available"] is True
        assert health["git"]["remoteRoots"] == ["remote"]

        tree = client.get("/api/tree", params={"workspace": "remote", "path": ""})
        assert tree.status_code == 409
        assert tree.json()["error"]["code"] == "workspace_not_synced"


def test_sync_api_end_to_end_reads_real_files(tmp_path, git_source_repo):
    config, _cache = make_remote_config(tmp_path, git_source_repo)
    with TestClient(create_app(config)) as client:
        started = client.post("/api/workspace/sync", json={"workspace": "remote"}).json()
        assert started["started"] is True
        import time

        deadline = time.monotonic() + 120
        snapshot = {}
        while time.monotonic() < deadline:
            snapshot = client.get("/api/workspace/sync").json()
            if not snapshot["running"]:
                break
            time.sleep(0.3)
        assert snapshot["running"] is False, "同步未在预期时间内结束"
        assert snapshot["status"] == "ready", snapshot

        tree = client.get("/api/tree", params={"workspace": "remote", "path": "libcutils"}).json()
        assert [item["name"] for item in tree["entries"]] == ["socket_utils.c"]

        file_body = client.get(
            "/api/file", params={"workspace": "remote", "path": "libcutils/socket_utils.c"}
        ).json()
        assert file_body["status"] == "ok"
        assert REMOTE_MARKER in "\n".join(file_body["lines"])
        assert file_body["hash"].startswith("sha256:")

        search = client.post(
            "/api/search", json={"query": REMOTE_MARKER, "workspace": "remote", "requestId": "remote-1"}
        ).json()
        assert search["matchCount"] >= 1
        assert search["matches"][0]["path"] == "libcutils/socket_utils.c"


def test_partial_checkout_is_reported_as_not_synced(tmp_path, git_source_repo):
    """被中断的 clone 留下只有 .git 的目录时，工作区必须报告为未同步而不是已就绪。"""
    config, _cache = make_remote_config(tmp_path, git_source_repo)
    root = config.root("remote")
    root.path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root.path, check=True, capture_output=True)
    with TestClient(create_app(config)) as client:
        item = client.get("/api/workspaces").json()["workspaces"][0]
        assert item["exists"] is False, "半成品目录不能被当成已同步"
        assert item["partial"] is True
        assert item["sync"]["isRepo"] is True and item["sync"]["head"] is None
        # 目录存在但 checkout 不完整时，文件/目录接口也必须给出"未同步"而不是空结果
        tree = client.get("/api/tree", params={"workspace": "remote", "path": ""})
        assert tree.status_code == 409
        assert tree.json()["error"]["code"] == "workspace_not_synced"
        file_response = client.get("/api/file", params={"workspace": "remote", "path": "README.md"})
        assert file_response.status_code == 409


def test_sync_api_rejects_local_workspace(client):
    response = client.post("/api/workspace/sync", json={"workspace": "sample"})
    assert response.status_code == 400
    assert "不需要同步" in response.json()["error"]["message"]


def test_sync_cancel_without_running_task(client):
    body = client.post("/api/workspace/sync/cancel").json()
    assert body["cancelled"] is False
