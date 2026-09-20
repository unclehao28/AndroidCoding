"""Java 语言服务（Eclipse JDT LS）接入的单元测试。

重点是"版本兼容性判断"：JDT LS 对 JDK 有硬要求（最新快照要 21、1.31.0 要 17、1.12.0 要 11），
而 AOSP 自带的 prebuilts/jdk/jdk11 通常只有 11。判断错了用户会看到 JDT LS 启动即退出，
所以这些规则必须有测试守着。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from app.lsp.manager import (
    build_jdtls_command,
    find_jdtls,
    java_home_of,
    jdtls_config_dir,
    jdtls_launcher,
    jdtls_required_java,
)
from conftest import REPO_ROOT

SETUP_JAVA = REPO_ROOT / "scripts" / "setup-java.py"
MOCK_SERVER = Path(__file__).resolve().parent / "mock_lsp_server.py"


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


def test_release_candidates_are_newest_first(tmp_path):
    module = load_setup_java()
    labels = [release["label"] for release in module.release_candidates(21)]
    assert labels == ["最新快照", "1.31.0", "1.12.0"]
    assert [release["label"] for release in module.release_candidates(17)] == ["1.31.0", "1.12.0"]
    assert [release["label"] for release in module.release_candidates(11)] == ["1.12.0"]


def test_jdtls_command_keeps_the_required_jvm_flags(tmp_path):
    target = make_jdtls(tmp_path)
    command = build_jdtls_command(
        "/usr/bin/java", str(target), str(tmp_path / "data"), extra_args=["-Xmx2G"]
    )
    joined = " ".join(command)
    # 少任何一个都会让 JDT LS 启动即退出
    for flag in (
        "-Declipse.application=org.eclipse.jdt.ls.core.id1",
        "-Dosgi.bundles.defaultStartLevel=4",
        "-Declipse.product=org.eclipse.jdt.ls.core.product",
        "-Dosgi.sharedConfiguration.area=",
        "-Dosgi.configuration.cascaded=true",
        "--add-modules=ALL-SYSTEM",
        "--add-opens",
        "java.base/java.lang=ALL-UNNAMED",
    ):
        assert flag in joined
    assert "-jar" in command and "-data" in command
    assert command[-1] == "-Xmx2G"  # 额外参数在最末尾


def test_java_home_of_resolves_bin_layout(tmp_path):
    jdk = tmp_path / "jdk-21"
    (jdk / "bin").mkdir(parents=True)
    java = jdk / "bin" / "java"
    java.write_text("", encoding="utf-8")
    assert java_home_of(str(java)) == str(jdk)
    loose = tmp_path / "solo-java"
    loose.write_text("", encoding="utf-8")
    assert java_home_of(str(loose)) is None


def test_smoke_test_passes_with_a_working_server(tmp_path):
    """冒烟测试是真的启动一个进程并完成 initialize 握手（用 mock LSP 当替身）。"""
    module = load_setup_java()
    command = [sys.executable, str(MOCK_SERVER)]
    ok, detail = asyncio.run(module.smoke_test_jdtls(command, cwd=tmp_path, timeout=30))
    assert ok is True and "initialize" in detail


def test_smoke_test_reports_failure_with_stderr(tmp_path):
    """起不来的语言服务必须被识别成失败，并把 stderr 带出来（这是排错的唯一线索）。"""
    module = load_setup_java()
    command = [sys.executable, "-c", "import sys; sys.stderr.write('boom: class file version 68.0\n'); sys.exit(1)"]
    ok, detail = asyncio.run(module.smoke_test_jdtls(command, cwd=tmp_path, timeout=20))
    assert ok is False
    assert "boom" in detail


def test_install_steps_down_when_newest_release_cannot_start(tmp_path, monkeypatch):
    """最新版起不来时要自动降级到旧版本，而不是留一个起不来的配置给用户。"""
    module = load_setup_java()
    installed: list = []

    def fake_target(release):
        return tmp_path / release["label"]

    def fake_download(url, target):
        make_jdtls(target.parent, required_java=21, config_dir=target.name)
        (target.parent / "jdtls").rename(target)
        installed.append(target.name)

    async def fake_smoke(command, *, cwd, timeout=150.0):
        # "最新快照" 在这台机器上起不来（模拟 class file version 不支持），1.31.0 可以
        if "最新快照" in " ".join(command):
            return False, "UnsupportedClassVersionError: class file version 68.0"
        return True, "initialize 成功"

    monkeypatch.setattr(module, "default_target", fake_target)
    monkeypatch.setattr(module, "download_and_unpack", fake_download)
    monkeypatch.setattr(module, "smoke_test_jdtls", fake_smoke)
    chosen, ls_dir, attempts = module.install_with_fallback("/usr/bin/java", 21)
    assert chosen is not None and chosen["label"] == "1.31.0"
    assert ls_dir == str(tmp_path / "1.31.0")
    assert any("68.0" in detail for _label, detail in attempts)
    assert [label for label, _ in attempts] == ["最新快照", "1.31.0"]


def test_install_reports_failure_when_no_release_works(tmp_path, monkeypatch):
    module = load_setup_java()

    def fake_target(release):
        return tmp_path / release["label"]

    def fake_download(url, target):
        make_jdtls(target.parent, required_java=21, config_dir=target.name)
        (target.parent / "jdtls").rename(target)

    async def always_fail(command, *, cwd, timeout=150.0):
        return False, "Error: Unable to access jarfile"

    monkeypatch.setattr(module, "default_target", fake_target)
    monkeypatch.setattr(module, "download_and_unpack", fake_download)
    monkeypatch.setattr(module, "smoke_test_jdtls", always_fail)
    chosen, ls_dir, attempts = module.install_with_fallback("/usr/bin/java", 21)
    assert chosen is None and ls_dir is None
    assert len(attempts) == 3  # 三个候选都试过了，且都带失败原因
    assert all(detail for _label, detail in attempts)
