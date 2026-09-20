"""Java 语言服务（Eclipse JDT LS）接入的单元测试。

重点是"版本兼容性判断"：JDT LS 对 JDK 有硬要求（最新快照要 21、1.31.0 要 17、1.12.0 要 11），
而 AOSP 自带的 prebuilts/jdk/jdk11 通常只有 11。判断错了用户会看到 JDT LS 启动即退出，
所以这些规则必须有测试守着。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.lsp.manager import (
    find_jdtls,
    jdtls_config_dir,
    jdtls_launcher,
    jdtls_required_java,
)
from conftest import REPO_ROOT

SETUP_JAVA = REPO_ROOT / "scripts" / "setup-java.py"


def load_setup_java():
    """把 scripts/setup-java.py 当模块加载，测它的版本选择逻辑。"""
    if "asw_setup_java" in sys.modules:
        return sys.modules["asw_setup_java"]
    spec = importlib.util.spec_from_file_location("asw_setup_java", SETUP_JAVA)
    module = importlib.util.module_from_spec(spec)
    sys.modules["asw_setup_java"] = module
    spec.loader.exec_module(module)
    return module


def make_jdtls(base: Path, *, required_java: int | None = 21, config_dir: str = "config_linux") -> Path:
    target = base / "jdtls"
    (target / "plugins").mkdir(parents=True)
    (target / "plugins" / "org.eclipse.equinox.launcher_1.8.0.v20260804-1928.jar").write_bytes(b"jar")
    (target / config_dir).mkdir()
    (target / "bin").mkdir()
    if required_java is not None:
        (target / "bin" / "jdtls.py").write_text(
            f"import os\nif java_major_version < {required_java}:\n    raise Exception('x')\n",
            encoding="utf-8",
        )
    return target


def test_required_java_is_read_from_python_wrapper(tmp_path):
    target = make_jdtls(tmp_path, required_java=21)
    assert jdtls_required_java(str(target)) == 21


def test_required_java_is_read_from_shell_wrapper(tmp_path):
    target = make_jdtls(tmp_path, required_java=None)
    (target / "bin" / "jdtls").write_text(
        'if [ "$JAVA_MAJOR_VERSION" -lt 11 ]; then\n  exit 1\nfi\n', encoding="utf-8"
    )
    assert jdtls_required_java(str(target)) == 11


def test_required_java_is_none_when_not_declared(tmp_path):
    target = make_jdtls(tmp_path, required_java=None)
    assert jdtls_required_java(str(target)) is None
    assert jdtls_required_java(None) is None


def test_config_dir_and_launcher_are_located(tmp_path):
    target = make_jdtls(tmp_path, config_dir="config_linux")
    assert jdtls_config_dir(str(target)).endswith("config_linux")
    assert Path(jdtls_launcher(str(target))).name.startswith("org.eclipse.equinox.launcher_")


def test_find_jdtls_reports_why_when_configured_path_is_wrong(tmp_path):
    bogus = tmp_path / "not-a-jdtls"
    bogus.mkdir()
    found, note = find_jdtls(str(bogus), [])
    assert found is None
    assert "javaLsPath" in note
    # 配置里有 JDT LS 时要能被发现
    target = make_jdtls(tmp_path)
    found, note = find_jdtls(str(target), [])
    assert found == str(target)
    assert "JDT LS" in note


def test_find_jdtls_finds_bundled_dir_inside_extra_dirs(tmp_path):
    """AOSP 树里如果放了 JDT LS，给出那棵树也能被找到（走的是"目录本身是 JDT LS"的判定）。"""
    target = make_jdtls(tmp_path / "workspace")
    found, _note = find_jdtls(None, [target])
    assert found == str(target)


@pytest.mark.parametrize(
    "java_version,expected",
    [(21, "最新快照"), (22, "最新快照"), (17, "1.31.0"), (20, "1.31.0"), (11, "1.12.0"), (16, "1.12.0")],
)
def test_release_matches_jdk_version(java_version, expected):
    module = load_setup_java()
    release = module.pick_release(java_version)
    assert release is not None and release["label"] == expected


def test_release_rejects_too_old_jdk():
    module = load_setup_java()
    assert module.pick_release(8) is None
    assert module.pick_release(1) is None


def test_every_release_url_is_https_and_official():
    module = load_setup_java()
    for release in module.JDTLS_RELEASES:
        assert release["url"].startswith("https://download.eclipse.org/jdtls/")
        assert release["url"].endswith(".tar.gz")
    # 版本越高的 JDK 要求，必须排在越前面（pick_release 依赖这个顺序）
    limits = [release["min_java"] for release in module.JDTLS_RELEASES]
    assert limits == sorted(limits, reverse=True)
