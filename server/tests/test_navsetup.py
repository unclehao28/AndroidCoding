"""P2 环境准备（探测 AOSP / 找 clangd / 改配置）的测试。

这些测试覆盖的正是"服务器上手工改 JSON 容易出错"的那部分：
探测的判定规则、按行改写是否保留注释、文件损坏时能否恢复。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from app.config import load_config
from app.navsetup import (
    add_source_root,
    aosp_markers,
    aosp_signature,
    backup_path,
    detect,
    find_aosp_roots,
    is_aosp_root,
    patch_config_file,
    patch_config_text,
    prepare_config,
    update_navigation_config,
)
from conftest import REPO_ROOT, SERVER_DIR

SAMPLE_ROOT = REPO_ROOT / "fixtures" / "demo-tree"


def make_fake_aosp(base: Path, *, name: str = "aosp", clangd_version: str = "clang-r999999") -> Path:
    root = base / name
    (root / ".repo").mkdir(parents=True)
    (root / "build" / "soong").mkdir(parents=True)
    (root / "frameworks" / "base").mkdir(parents=True)
    clangd = root / "prebuilts" / "clang" / "host" / "linux-x86" / clangd_version / "bin" / "clangd"
    clangd.parent.mkdir(parents=True)
    clangd.write_text("#!/bin/sh\necho 'clangd version 18.1.8'\n", encoding="utf-8")
    clangd.chmod(0o755)
    return root


def make_config_text(search_dirs: str = "[]", clangd_path: str = "null") -> str:
    return (
        "{\n"
        "  // 这是注释，必须保留\n"
        '  "roots": [ { "id": "sample", "path": "' + str(SAMPLE_ROOT).replace("\\", "\\\\") + '", "readonly": true } ],\n'
        '  "features": { "write": false, "navigation": true },\n'
        '  "navigation": {\n'
        '    "enabled": true,\n'
        f'    "searchDirs": {search_dirs},\n'
        f'    "clangdPath": {clangd_path}\n'
        "  },\n"
        '  "prototypeDir": "' + str(REPO_ROOT / "prototype").replace("\\", "\\\\") + '"\n'
        "}\n"
    )


# ---------------------------------------------------------------- 探测规则


def test_dot_repo_alone_marks_aosp_root(tmp_path):
    root = tmp_path / "tree"
    (root / ".repo").mkdir(parents=True)
    assert is_aosp_root(root) is True
    assert aosp_markers(root) == [".repo"]


def test_single_generic_marker_is_not_enough(tmp_path):
    root = tmp_path / "maybe"
    (root / "system" / "core").mkdir(parents=True)
    assert is_aosp_root(root) is False
    two = tmp_path / "likely"
    (two / "system" / "core").mkdir(parents=True)
    (two / "frameworks" / "base").mkdir(parents=True)
    assert is_aosp_root(two) is True


def test_prebuilts_clang_alone_marks_aosp_root(tmp_path):
    """有的团队会裁掉 .repo 或只同步部分目录，prebuilts/clang 是足够的特征。"""
    root = tmp_path / "trimmed"
    (root / "prebuilts" / "clang" / "host" / "linux-x86").mkdir(parents=True)
    assert is_aosp_root(root) is True
    assert "prebuilts/clang" in aosp_signature(root)


def test_partial_candidates_reported_when_no_full_tree(tmp_path, monkeypatch):
    import app.lsp.manager as manager_module
    import app.navsetup as navsetup

    partial = tmp_path / "only-frameworks"
    (partial / "frameworks" / "base").mkdir(parents=True)
    candidates = navsetup.find_partial_candidates([tmp_path], max_depth=3)
    assert [item[1] for item in candidates] == [partial]
    assert candidates[0][2] == ["frameworks/base"]

    # detect 在没有完整源码树时要把候选目录带上，供用户确认后 --aosp 指定
    monkeypatch.setattr(navsetup, "default_search_roots", lambda: [tmp_path])
    monkeypatch.setattr(manager_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(navsetup, "system_clangd_candidates", lambda: [])
    result = navsetup.detect()
    assert result.aosp_roots == []
    assert [item["path"] for item in result.candidates] == [str(partial)]


def test_detect_skips_default_scan_when_roots_are_given(tmp_path, monkeypatch):
    """给了源码根就不该再扫 /data /home——在真实服务器上那要几分钟。"""
    import app.navsetup as navsetup

    given = make_fake_aosp(tmp_path / "given")
    other = make_fake_aosp(tmp_path / "other")

    def explode():
        raise AssertionError("不应扫描默认位置")

    monkeypatch.setattr(navsetup, "default_search_roots", explode)
    result = navsetup.detect(search_dirs=[given], scan=True)
    assert result.aosp_roots == [given]
    assert other not in result.aosp_roots


def test_add_root_preserves_comments_and_validates(tmp_path):
    server = make_server_mirror(tmp_path)
    tree = make_fake_aosp(tmp_path)
    before = (server / "config.json").read_text(encoding="utf-8") if (server / "config.json").exists() else ""
    report = add_source_root(server, tree)
    assert report["changed"] is True
    assert "按行插入" in report["strategy"]  # 示例配置的数组闭合行带逗号，也必须能按行插入
    text = (server / "config.json").read_text(encoding="utf-8")
    assert "//" in text  # 注释保留
    config = load_config(server / "config.json")
    added = [root for root in config.roots if root.id == "aosp"]
    assert len(added) == 1
    assert added[0].readonly is True and str(added[0].path) == str(tree)
    assert Path(report["backup"]).is_file()
    assert '"roots"' in Path(report["backup"]).read_text(encoding="utf-8")  # 备份是改动前的真实配置


def test_update_navigation_config_inserts_multiple_missing_keys(tmp_path):
    """多行插入必须给行间补逗号，否则会写出非法 JSON（真踩过）。"""
    server = make_server_mirror(tmp_path)
    config = server / "config.json"
    raw = (SERVER_DIR / "config.example.json").read_text(encoding="utf-8")
    stripped = "".join(
        line
        for line in raw.splitlines(True)
        if not re.match(r'\s*"(javaLsPath|javaHome|javaDataDir|javaArgs)":', line)
    )
    config.write_text(stripped, encoding="utf-8")
    before = config.read_text(encoding="utf-8")

    result = update_navigation_config(server, {"javaLsPath": "/opt/jdtls", "javaHome": "/opt/jdk"})
    assert result["changed"] is True
    assert "新增字段" in result["strategy"]  # 不是整体重写
    text = config.read_text(encoding="utf-8")
    assert "//" in text  # 注释保留
    assert before.count("//") == text.count("//")
    loaded = load_config(config)
    assert loaded.navigation.java_ls_path == "/opt/jdtls"
    assert loaded.navigation.java_home == "/opt/jdk"
    assert not list(server.glob("*.tmp-check"))


def test_update_navigation_config_replaces_existing_values(tmp_path):
    server = make_server_mirror(tmp_path)
    config = server / "config.json"
    shutil.copy(SERVER_DIR / "config.example.json", config)
    first = update_navigation_config(server, {"javaLsPath": "/opt/one"})
    assert first["changed"] is True and first["missing"] == []
    second = update_navigation_config(server, {"javaLsPath": "/opt/two"})
    assert second["changed"] is True
    assert load_config(config).navigation.java_ls_path == "/opt/two"
    # 没有任何变化时不写文件、也不产生备份
    third = update_navigation_config(server, {"javaLsPath": "/opt/two"})
    assert third["changed"] is False


def test_add_root_is_idempotent(tmp_path):
    server = make_server_mirror(tmp_path)
    tree = make_fake_aosp(tmp_path)
    add_source_root(server, tree)
    after_first = (server / "config.json").read_text(encoding="utf-8")
    second = add_source_root(server, tree)
    assert second["changed"] is False
    assert "已经有这个路径" in second["message"]
    assert (server / "config.json").read_text(encoding="utf-8") == after_first


def test_add_root_avoids_id_collision(tmp_path):
    server = make_server_mirror(tmp_path)
    first = make_fake_aosp(tmp_path / "one")
    second = make_fake_aosp(tmp_path / "two")
    add_source_root(server, first, root_id="aosp12")
    report = add_source_root(server, second, root_id="aosp12")
    assert report["entry"]["id"] == "aosp12-2"
    config = load_config(server / "config.json")
    ids = [root.id for root in config.roots]
    assert "aosp12" in ids and "aosp12-2" in ids


def test_add_root_falls_back_to_rewrite_for_empty_roots(tmp_path):
    server = make_server_mirror(tmp_path)
    tree = make_fake_aosp(tmp_path)
    (server / "config.json").write_text(
        "{\n"
        '  "roots": [],\n'
        '  "prototypeDir": "' + str(tmp_path / "prototype").replace("\\", "\\\\") + '"\n'
        "}\n",
        encoding="utf-8",
    )
    report = add_source_root(server, tree)
    assert report["changed"] is True
    assert "整体重写" in report["strategy"]
    config = load_config(server / "config.json")
    assert [str(root.path) for root in config.roots] == [str(tree)]


def test_add_root_refuses_to_write_invalid_config(tmp_path):
    """路径不存在时校验会失败：绝不能把校验不过的配置留在原地。"""
    server = make_server_mirror(tmp_path)
    original = (server / "config.example.json").read_text(encoding="utf-8")
    (server / "config.json").write_text(original, encoding="utf-8")
    report = add_source_root(server, tmp_path / "not-there")
    assert report["changed"] is False
    assert report["problems"] and any("not-there" in item for item in report["problems"])
    assert (server / "config.json").read_text(encoding="utf-8") == original
    assert not list(server.glob("*.tmp-check"))


def test_detect_depth_is_configurable(tmp_path, monkeypatch):
    import app.navsetup as navsetup

    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    make_fake_aosp(deep)
    monkeypatch.setattr(navsetup, "default_search_roots", lambda: [tmp_path])
    assert navsetup.detect(max_depth=3).aosp_roots == []
    assert navsetup.detect(max_depth=6).aosp_roots == [deep / "aosp"]


def test_find_aosp_roots_respects_depth_and_stops_at_hit(tmp_path):
    make_fake_aosp(tmp_path / "shallow")
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    make_fake_aosp(deep)
    found, scanned = find_aosp_roots([tmp_path], max_depth=2)
    assert [item.name for item in found] == ["aosp"]
    assert scanned >= 1
    found_deep, _ = find_aosp_roots([tmp_path], max_depth=6)
    assert len(found_deep) == 2


def test_find_aosp_roots_skips_noise_directories(tmp_path):
    make_fake_aosp(tmp_path / "node_modules", name="aosp")
    found, _ = find_aosp_roots([tmp_path], max_depth=4)
    assert found == []


def test_detect_prefers_configured_clangd(tmp_path):
    aosp = make_fake_aosp(tmp_path)
    explicit = tmp_path / "my-clangd"
    explicit.write_text("#!/bin/sh\necho 'clangd version 19.1.0'\n", encoding="utf-8")
    explicit.chmod(0o755)
    result = detect(configured_clangd=str(explicit), search_dirs=[aosp], scan=False)
    assert result.clangd_path == str(explicit)
    assert result.aosp_roots == [aosp]


def test_detect_finds_aosp_bundled_clangd(tmp_path, monkeypatch):
    # 屏蔽 PATH 分支，避免本机恰好装了 clangd 时结果不确定
    import app.lsp.manager as manager_module
    import app.navsetup as navsetup

    monkeypatch.setattr(manager_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(navsetup, "system_clangd_candidates", lambda: [])
    aosp = make_fake_aosp(tmp_path)
    result = detect(search_dirs=[aosp], scan=False)
    assert result.clangd_path is not None
    assert "prebuilts" in result.clangd_path
    assert "AOSP prebuilts" in result.clangd_source


def test_detect_reports_missing_clangd_with_hint(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = detect(search_dirs=[empty], scan=False)
    if result.clangd_path is None:  # 本机若装了 clangd 则跳过这条断言
        assert any("没有找到 clangd" in note for note in result.notes)


# ---------------------------------------------------------------- 改配置


def test_patch_config_text_keeps_comments_and_other_lines():
    text = make_config_text()
    updated, changed, changes = patch_config_text(text, search_dirs=["/data/aosp"], clangd_path="/opt/clangd")
    assert changed is True
    assert "// 这是注释，必须保留" in updated
    assert '"searchDirs": ["/data/aosp"],' in updated
    assert '"clangdPath": "/opt/clangd"' in updated
    assert updated.count('"roots"') == 1
    assert any("searchDirs" in item for item in changes)


def test_patch_config_file_writes_backup_and_stays_valid(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(make_config_text(), encoding="utf-8")
    result = patch_config_file(config, search_dirs=["/data/aosp"])
    assert result["changed"] is True
    assert result["backup"] and Path(result["backup"]).is_file()
    assert '"searchDirs": ["/data/aosp"]' in config.read_text(encoding="utf-8")
    loaded = load_config(config)
    assert loaded.navigation.search_dirs == ("/data/aosp",)


def test_patch_config_noop_when_nothing_to_write(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(make_config_text(), encoding="utf-8")
    result = patch_config_file(config)
    assert result["changed"] is False
    assert result["backup"] is None


def test_patch_config_falls_back_to_rewrite_without_navigation_section(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        "{\n"
        '  "roots": [ { "id": "sample", "path": "' + str(SAMPLE_ROOT).replace("\\", "\\\\") + '", "readonly": true } ],\n'
        '  "prototypeDir": "' + str(REPO_ROOT / "prototype").replace("\\", "\\\\") + '"\n'
        "}\n",
        encoding="utf-8",
    )
    result = patch_config_file(config, search_dirs=["/data/aosp"])
    assert result["changed"] is True
    assert "整体重写" in result["strategy"]
    loaded = load_config(config)
    assert loaded.navigation.search_dirs == ("/data/aosp",)
    assert json.loads(config.read_text(encoding="utf-8"))["navigation"]["searchDirs"] == ["/data/aosp"]


# ---------------------------------------------------------------- 整体流程


def make_server_mirror(tmp_path: Path) -> Path:
    """造一个和真实仓库同构的最小目录：server/config.example.json + fixtures + prototype。

    示例配置里的 roots 用的是相对路径，必须在同构目录下才有效。
    """
    server = tmp_path / "server"
    server.mkdir()
    shutil.copy(SERVER_DIR / "config.example.json", server / "config.example.json")
    (tmp_path / "fixtures" / "demo-tree").mkdir(parents=True)
    (tmp_path / "fixtures" / "demo-tree" / "keep.txt").write_text("x\n", encoding="utf-8")
    prototype = tmp_path / "prototype"
    prototype.mkdir()
    (prototype / "index.html").write_text("<html></html>\n", encoding="utf-8")
    return server


def test_prepare_config_creates_from_example(tmp_path):
    server = make_server_mirror(tmp_path)
    result = prepare_config(server, scan=False)
    assert result["created"] is True
    assert result.get("blocked") is False
    assert Path(result["configPath"]).is_file()
    load_config(Path(result["configPath"]))


def test_prepare_config_writes_detected_aosp_root(tmp_path):
    server = make_server_mirror(tmp_path)
    aosp = make_fake_aosp(tmp_path)
    result = prepare_config(server, aosp_roots=[aosp], scan=False)
    assert result["patch"]["changed"] is True
    loaded = load_config(Path(result["configPath"]))
    assert str(aosp) in loaded.navigation.search_dirs
    detected = result["detection"]
    assert detected["aospRoots"] == [str(aosp)]
    assert detected["clangdPath"] and "prebuilts" in detected["clangdPath"]


def test_prepare_config_writes_clangd_path_when_no_aosp(tmp_path):
    server = make_server_mirror(tmp_path)
    clangd = tmp_path / "clangd"
    clangd.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
    clangd.chmod(0o755)
    result = prepare_config(server, clangd_path=str(clangd), scan=False)
    loaded = load_config(Path(result["configPath"]))
    assert loaded.navigation.clangd_path == str(clangd)


def test_prepare_config_recovers_broken_file_and_keeps_going(tmp_path):
    """第一次实现里恢复配置后提前返回，用户白跑一轮；必须一次跑完探测。"""
    server = make_server_mirror(tmp_path)
    aosp = make_fake_aosp(tmp_path)
    broken = server / "config.json"
    broken.write_text('{ "roots": [ { "id": "x" ', encoding="utf-8")  # 明显截断
    result = prepare_config(server, aosp_roots=[aosp], scan=False)
    assert result["recovered"] is True
    assert result["blocked"] is False
    assert Path(result["recoveredFrom"]).is_file()
    assert "语法错误" in result["reason"]
    load_config(broken)  # 重建后必须是合法配置
    # 同一次调用里就应该完成探测与写入
    assert result["detection"]["aospRoots"] == [str(aosp)]
    assert result["patch"]["changed"] is True
    assert str(aosp) in load_config(broken).navigation.search_dirs


def test_prepare_config_rebuilt_example_alone_is_enough_to_continue(tmp_path):
    """配置被写成空文件（vi 误操作的真实场景）：备份 + 重建 + 继续，不得抛异常。"""
    server = make_server_mirror(tmp_path)
    (server / "config.json").write_text("", encoding="utf-8")
    result = prepare_config(server, scan=False)
    assert result["recovered"] is True
    assert result["blocked"] is False
    assert result["detection"]["scannedDirs"] == 0  # scan=False
    load_config(Path(result["configPath"]))


def test_two_backups_in_one_run_do_not_overwrite_each_other(tmp_path):
    """一次运行会备份两次（修复前的原文 + 改写前的文件），两份都必须留得住。"""
    server = make_server_mirror(tmp_path)
    broken = '{\n  "roots": [ { "id": "mine", "path": "/data/mine/home" '  # 语法坏，但含用户手工写的内容
    (server / "config.json").write_text(broken, encoding="utf-8")
    aosp = make_fake_aosp(tmp_path)
    result = prepare_config(server, aosp_roots=[aosp], scan=False)
    assert result["recovered"] is True
    backups = sorted(server.glob("config.json.bak-*"))
    if result["patch"]["changed"]:
        assert len(backups) == 2
        assert any("/data/mine/home" in item.read_text(encoding="utf-8") for item in backups)
    else:
        assert len(backups) == 1


def test_backup_path_is_unique_even_within_same_second(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("x", encoding="utf-8")
    first = backup_path(target)
    first.write_text("broken", encoding="utf-8")
    second = backup_path(target)
    assert second != first
    second.write_text("rebuilt", encoding="utf-8")
    assert first.read_text(encoding="utf-8") == "broken"
    assert second.read_text(encoding="utf-8") == "rebuilt"


def test_prepare_config_never_overwrites_valid_json_with_bad_paths(tmp_path):
    """路径不存在属于用户要自己决定的问题：只报告，绝不能覆盖配置文件。"""
    server = make_server_mirror(tmp_path)
    config = server / "config.json"
    original = (
        "{\n"
        '  "roots": [ { "id": "mine", "path": "/data/not-here-yet", "readonly": true } ],\n'
        '  "prototypeDir": "' + str(tmp_path / "prototype").replace("\\", "\\\\") + '"\n'
        "}\n"
    )
    config.write_text(original, encoding="utf-8")
    result = prepare_config(server, scan=False)
    assert result["blocked"] is True
    assert any("not-here-yet" in problem for problem in result["problems"])
    assert config.read_text(encoding="utf-8") == original  # 原样保留

