"""配置校验测试：字段错误必须让启动失败并给出可读原因。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import ConfigError, build_config, load_config, resolve_config_path
from conftest import REPO_ROOT, SERVER_DIR, default_raw


def test_example_config_is_valid_and_points_at_real_dirs():
    config = load_config(SERVER_DIR / "config.example.json")
    assert {root.id for root in config.roots} == {"demo", "fixtures", "aosp-system-core"}
    # 本地根目录必须已存在；远程根目录允许尚未同步
    assert all(root.path.is_dir() for root in config.roots if not root.is_remote)
    assert [root.id for root in config.remote_roots] == ["aosp-system-core"]
    assert config.cache_dir is not None and config.cache_dir.is_dir()
    assert config.features.write is False
    assert config.features.navigation is True  # P2 起语义跳转默认可开
    assert config.navigation.enabled is True
    assert config.server.host == "127.0.0.1"


def test_relative_roots_are_independent_of_current_working_directory(tmp_path, monkeypatch):
    """相对 roots 必须相对配置文件所在目录解析，而不是进程 cwd。

    回归背景：为远程仓库重构路径解析时，这条只留在了远程分支，导致从别的目录
    运行（例如在仓库根目录跑脚本）会报"路径不存在"。
    """
    somewhere_else = tmp_path / "elsewhere"
    somewhere_else.mkdir()
    monkeypatch.chdir(somewhere_else)
    config = load_config(SERVER_DIR / "config.example.json")
    demo = config.root("demo")
    assert demo is not None
    assert demo.path == (REPO_ROOT / "fixtures/demo-tree").resolve()
    assert demo.path.exists()


def test_relative_root_paths_resolve_against_config_location():
    config = load_config(SERVER_DIR / "config.example.json")
    demo_root = config.root("demo")
    assert demo_root is not None
    assert demo_root.path == (REPO_ROOT / "fixtures/demo-tree").resolve()


def test_valid_config_builds(tmp_path, sample_tree):
    config = build_config(default_raw(sample_tree), source_path=tmp_path / "config.json")
    root = config.root("sample")
    assert root is not None and root.path == sample_tree.resolve()
    assert config.limits.max_file_bytes == 2 * 1024 * 1024


def test_unknown_top_level_field_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["limitsx"] = {}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("未知字段" in message for message in excinfo.value.messages)


def test_unknown_section_field_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["limits"] = {"maxFileBytez": 1024}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("maxFileBytez" in message for message in excinfo.value.messages)


def test_missing_root_path_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["roots"] = [{"id": "gone", "path": str(tmp_path / "not-here")}]
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("不存在" in message for message in excinfo.value.messages)


def test_root_path_must_be_directory(tmp_path, sample_tree):
    file_path = tmp_path / "afile.txt"
    file_path.write_text("x", encoding="utf-8")
    raw = default_raw(sample_tree)
    raw["roots"] = [{"id": "afile", "path": str(file_path)}]
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("不是目录" in message for message in excinfo.value.messages)


def test_duplicate_root_id_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["roots"].append({"id": "sample", "path": str(sample_tree)})
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("重复" in message for message in excinfo.value.messages)


def test_limits_out_of_range_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["limits"] = {"maxSearchResults": 999999, "rgThreads": 0}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    messages = " ".join(excinfo.value.messages)
    assert "maxSearchResults" in messages and "rgThreads" in messages


def test_write_must_stay_disabled_but_navigation_is_allowed(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["features"] = {"write": True, "navigation": True}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    messages = " ".join(excinfo.value.messages)
    assert "features.write" in messages
    assert "features.navigation" not in messages  # P2 已接入，不再是错误


def test_navigation_settings_validation(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["navigation"] = {"maxInstances": 99, "idleShutdownSeconds": 1, "pchStorage": "tape", "clangdLogLevel": "loud"}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    messages = " ".join(excinfo.value.messages)
    for key in ("maxInstances", "idleShutdownSeconds", "pchStorage", "clangdLogLevel"):
        assert key in messages
    raw["navigation"] = {"unknownKey": 1}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("未知字段" in message for message in excinfo.value.messages)


def test_background_index_is_only_a_warning(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["navigation"] = {"backgroundIndex": True}
    config = build_config(raw, source_path=tmp_path / "config.json")
    assert config.navigation.background_index is True
    assert any("backgroundIndex" in warning for warning in config.warnings)


def test_per_root_compile_commands_dir(tmp_path, sample_tree):
    db = tmp_path / "out" / "compdb"
    db.mkdir(parents=True)
    raw = default_raw(sample_tree)
    raw["roots"] = [{"id": "sample", "path": str(sample_tree), "compileCommandsDir": str(db)}]
    config = build_config(raw, source_path=tmp_path / "config.json")
    assert config.root("sample").compile_commands_dir == str(db.resolve())
    raw["roots"] = [{"id": "sample", "path": str(sample_tree), "compileCommandsDir": str(tmp_path / "missing")}]
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("compileCommandsDir" in message for message in excinfo.value.messages)


def test_search_engine_value_checked(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["search"] = {"engine": "zoekt"}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("search.engine" in message for message in excinfo.value.messages)


def test_non_loopback_host_is_warning_not_error(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["server"] = {"host": "0.0.0.0", "port": 8765}
    config = build_config(raw, source_path=tmp_path / "config.json")
    assert config.server.host == "0.0.0.0"
    assert any("回环" in warning for warning in config.warnings)


def test_search_limit_larger_than_read_limit_is_only_a_warning(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["limits"] = {"maxFileBytes": 4096}
    config = build_config(raw, source_path=tmp_path / "config.json")
    assert config.limits.max_file_bytes == 4096
    assert any("maxSearchFileBytes" in warning for warning in config.warnings)


def test_config_supports_json_comments_without_breaking_urls(tmp_path, sample_tree):
    path = tmp_path / "config.json"
    path.write_text(
        "{\n"
        "  // 行注释\n"
        '  "roots": [ { "id": "sample", "path": "' + str(sample_tree).replace("\\", "\\\\") + '", "readonly": true } ],\n'
        "  /* 块注释\n"
        "     跨多行 */\n"
        '  "search": { "engine": "auto" },  // 行尾注释\n'
        '  "features": { "write": false, "navigation": false },\n'
        '  "prototypeDir": "' + str(REPO_ROOT / "prototype").replace("\\", "\\\\") + '"\n'
        "}\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.root("sample") is not None
    assert config.search.engine == "auto"


def test_config_comments_keep_line_numbers_accurate(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{\n  // 注释\n  "roots": [}\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = excinfo.value.messages[0]
    assert "config.json:3" in message, message


def test_url_containing_double_slash_survives_comment_stripping(tmp_path):
    from app.config import strip_json_comments

    text = '{"git": {"url": "https://mirrors.example.com/aosp/x.git"}}'
    assert strip_json_comments(text) == text


def test_invalid_json_reports_location(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"roots": [}', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert any("JSON" in message for message in excinfo.value.messages)


def test_config_path_precedence(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ASW_CONFIG", str(tmp_path / "from-env.json"))
    assert resolve_config_path(str(explicit)) == explicit
    assert resolve_config_path(None) == Path(tmp_path / "from-env.json")
    monkeypatch.delenv("ASW_CONFIG")
    assert resolve_config_path(None).name in ("config.json", "config.example.json")


def test_public_config_hides_nothing_but_marks_features_disabled(config):
    public = config.to_public()
    assert public["features"] == {"write": False, "navigation": True}
    assert public["search"]["engine"] == "auto"
    assert public["limits"]["maxSearchResults"] == 500
    json.dumps(public)  # 必须可序列化
