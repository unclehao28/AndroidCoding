"""远程 / 内网 Git 仓库作为只读工作区。

设计原则（务必保持）：
- 只使用服务器上**既有**的 git 与 SSH 凭据（~/.ssh、credential helper、url.insteadOf 改写），
  工作台本身不保存任何口令或私钥；
- 所有 git 调用都以数组形式传参，并用 `--` 隔开路径，禁止拼接 shell 字符串；
- 只执行 clone / fetch / merge --ff-only：绝不 reset --hard、clean、rebase、自动提交或推送；
- 认证失败要快速失败（BatchMode + GIT_TERMINAL_PROMPT=0），不允许挂在交互式输入上；
- 缓存目录里出现本地修改时，拒绝自动更新，如实报告，绝不覆盖。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig, RootConfig

GIT_SHORT_TIMEOUT = 20.0
MAX_LOG_LINES = 400


def git_binary() -> str | None:
    return shutil.which("git")


def git_version() -> str | None:
    binary = git_binary()
    if not binary:
        return None
    try:
        completed = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=GIT_SHORT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip() or None


def _git_env() -> dict:
    """非交互环境：不弹账号密码、不让 SSH 等待输入，但仍读取用户既有配置与凭据。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    env["LC_ALL"] = "C"
    return env


def run_git(args: list[str], *, cwd: Path | None = None, timeout: float = GIT_SHORT_TIMEOUT) -> tuple[int, str]:
    binary = git_binary()
    if not binary:
        return 127, "服务器上没有找到 git"
    try:
        completed = subprocess.run(
            [binary, *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args[:2])} 超时（{timeout:g}s）"
    except OSError as exc:
        return 126, f"git 调用失败：{exc}"
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output.strip()


def is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    code, output = run_git(["-C", str(path), "rev-parse", "--is-inside-work-tree"])
    return code == 0 and output.splitlines()[-1].strip() == "true"


def _one_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    return lines[0].strip() if lines else ""


def read_state(path: Path, *, url: str | None = None, ref: str | None = None) -> dict:
    """读取本地缓存仓库状态；不做任何写操作。"""
    state: dict = {
        "hasGit": git_binary() is not None,
        "checkoutExists": path.is_dir(),
        "isRepo": False,
        "head": None,
        "branch": None,
        "shallow": None,
        "dirtyFiles": None,
        "lastCommit": None,
        "originUrl": url,
        "requestedRef": ref,
        "error": None,
    }
    if not state["checkoutExists"]:
        return state
    if not state["hasGit"]:
        state["error"] = "服务器没有 git，无法同步远程仓库"
        return state
    if not is_git_repo(path):
        state["error"] = "目录存在但不是 git 仓库（可能是上次 clone 失败留下的），确认后手工删除再同步"
        return state
    state["isRepo"] = True
    code, output = run_git(["-C", str(path), "rev-parse", "--short", "HEAD"])
    state["head"] = _one_line(output) if code == 0 else None
    code, output = run_git(["-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
    branch = _one_line(output) if code == 0 else None
    state["branch"] = branch
    state["detached"] = branch == "HEAD"
    code, output = run_git(["-C", str(path), "rev-parse", "--is-shallow-repository"])
    state["shallow"] = (_one_line(output) == "true") if code == 0 else None
    code, output = run_git(["-C", str(path), "log", "-1", "--format=%cI"])
    state["lastCommit"] = _one_line(output) if code == 0 else None
    code, output = run_git(["-C", str(path), "status", "--porcelain", "--untracked-files=no"])
    state["dirtyFiles"] = len([line for line in output.splitlines() if line.strip()]) if code == 0 else None
    code, output = run_git(["-C", str(path), "config", "--get", "remote.origin.url"])
    if code == 0:
        state["originUrl"] = _one_line(output)
    return state


def _pump(stream, sink: list[str], on_line=None) -> None:
    """git 的进度用 \\r 刷新，这里按 \\r 和 \\n 一起切分，尽量实时。"""
    buffer = ""
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            buffer += chunk
            while True:
                positions = [index for index in (buffer.find("\r"), buffer.find("\n")) if index >= 0]
                if not positions:
                    break
                cut = min(positions)
                line = buffer[:cut].strip()
                buffer = buffer[cut + 1 :]
                if line:
                    sink.append(line)
                    if len(sink) > MAX_LOG_LINES:
                        del sink[: len(sink) - MAX_LOG_LINES]
                    if on_line is not None:
                        on_line(line)
        tail = buffer.strip()
        if tail:
            sink.append(tail)
            if on_line is not None:
                on_line(tail)
    except (ValueError, OSError):
        pass


def _run_streaming(
    args: list[str],
    *,
    cwd: Path | None,
    timeout: float,
    cancel: threading.Event,
    log: list[str],
    on_line=None,
) -> tuple[str, int]:
    """返回 (status, exit_code)。

    status 只描述进程生命周期：ok（正常结束，退出码见 exit_code）/ cancelled / timeout / spawn-error。
    退出码非 0 仍然算 "ok"，由调用方按 code 判断，避免把"git 报错"和"被终止"混在一起。
    """
    binary = git_binary()
    if not binary:
        return "spawn-error", 127
    log.append("$ git " + " ".join(args))
    try:
        process = subprocess.Popen(
            [binary, *args],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_env(),
        )
    except OSError as exc:
        log.append(f"启动 git 失败：{exc}")
        return "spawn-error", 126
    reader = threading.Thread(target=_pump, args=(process.stdout, log, on_line), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    status = "ok"
    while process.poll() is None:
        if cancel.is_set():
            status = "cancelled"
            break
        if time.monotonic() >= deadline:
            status = "timeout"
            break
        time.sleep(0.2)
    if status != "ok":
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.append("已终止 git（" + ("客户端取消" if status == "cancelled" else "超过时间上限") + "）")
        return status, -1
    reader.join(timeout=5)
    return "ok", (process.returncode or 0)


def remote_branch_hint(url: str, wanted: str | None) -> str:
    """clone 失败时给出远端可用分支，避免只看到一句 "Remote branch not found"。"""
    code, output = run_git(["ls-remote", "--heads", url], timeout=GIT_SHORT_TIMEOUT)
    if code != 0:
        return ""
    heads = [line.split("refs/heads/")[-1] for line in output.splitlines() if "refs/heads/" in line][:10]
    if not heads:
        return ""
    note = "，配置的 ref 不在其中" if wanted and wanted not in heads else ""
    return f"；远端可用分支：{', '.join(heads)}{note}"


def sync_root(
    root: RootConfig,
    cache_dir: Path | None,
    *,
    cancel: threading.Event | None = None,
    timeout: float = 3600.0,
    on_line=None,
) -> dict:
    """把远程仓库同步到本地缓存目录。返回结构化结果，不抛异常。"""
    cancel = cancel or threading.Event()
    log: list[str] = []
    result: dict = {
        "status": "error",
        "workspaceId": root.id,
        "checkout": str(root.path),
        "url": root.git.url if root.git else None,
        "steps": [],
        "message": "",
        "head": None,
        "log": log,
    }
    if root.git is None:
        result["message"] = "该工作区不是远程仓库"
        return result
    if cache_dir is None:
        result["message"] = "配置缺少 cacheDir，无法确定缓存位置"
        return result
    if git_binary() is None:
        result["message"] = "服务器上没有 git，无法同步远程仓库"
        return result

    checkout = root.path
    try:
        checkout.relative_to(cache_dir)
    except ValueError:
        result["message"] = f"缓存路径 {checkout} 不在 cacheDir {cache_dir} 之内，已拒绝"
        return result

    created = False
    if checkout.exists() and not is_git_repo(checkout):
        if any(checkout.iterdir()):
            result["message"] = f"{checkout} 已存在且不是 git 仓库，请先确认是否要手工删除"
            return result
        checkout.rmdir()

    state = read_state(checkout) if checkout.exists() else {"isRepo": False, "head": None, "dirtyFiles": None}
    # 上一次 clone/fetch 被取消或中断时，会留下只有 .git、没有有效 HEAD 的半成品；
    # 这种目录没有可用的提交，也不会有本地修改，直接删掉重新 clone 比在上面继续操作更安全。
    if state.get("isRepo") and not state.get("head"):
        log.append("检测到上次 clone/fetch 被中断（仓库没有有效 HEAD），删除该缓存目录后重新 clone")
        shutil.rmtree(checkout, ignore_errors=True)
        state = {"isRepo": False, "head": None, "dirtyFiles": None}

    if state.get("isRepo"):
        if state.get("dirtyFiles"):
            result["status"] = "skipped"
            result["message"] = (
                f"缓存工作区有 {state['dirtyFiles']} 个已修改文件，未执行更新；"
                "工作台不会丢弃或覆盖本地修改，请先自行处理"
            )
            result["head"] = state.get("head")
            return result
        result["steps"].append("fetch")
        status, code = _run_streaming(
            ["fetch", "--depth", str(root.git.depth), "--no-tags", "origin"],
            cwd=checkout,
            timeout=timeout,
            cancel=cancel,
            log=log,
            on_line=on_line,
        )
        if status != "ok":
            result["status"] = status
            result["message"] = {
                "cancelled": "已取消",
                "timeout": "fetch 超时",
                "spawn-error": "git 无法启动，见上面的输出",
            }.get(status, "fetch 未完成：见上面的 git 输出")
            return result
        if code != 0:
            result["message"] = "fetch 失败：通常是网络或凭据问题，见上面的 git 输出"
            return result
        # 浅克隆里新旧提交之间没有共同祖先，merge --ff-only 会报 "unrelated histories"；
        # 只读镜像用 checkout --detach 把工作树切到新提交即可，不需要合并，也不会丢弃任何东西
        # （工作树脏时 git 自己会拒绝，前面也已提前拦截）。
        if state.get("shallow"):
            result["steps"].append("checkout --detach")
            status, code = _run_streaming(
                ["checkout", "--detach", "FETCH_HEAD"],
                cwd=checkout,
                timeout=timeout,
                cancel=cancel,
                log=log,
                on_line=on_line,
            )
            if status != "ok":
                result["status"] = status
                result["message"] = {
                    "cancelled": "已取消",
                    "timeout": "checkout 超时",
                    "spawn-error": "git 无法启动，见上面的输出",
                }.get(status, "checkout 未完成：见上面的 git 输出")
                return result
            if code != 0:
                result["status"] = "skipped"
                result["message"] = "无法更新工作树（git 拒绝了检出，可能因为有未提交修改），保持原状"
                result["head"] = read_state(checkout).get("head")
                return result
        else:
            result["steps"].append("merge --ff-only")
            status, code = _run_streaming(
                ["merge", "--ff-only", "FETCH_HEAD"],
                cwd=checkout,
                timeout=timeout,
                cancel=cancel,
                log=log,
                on_line=on_line,
            )
            if status != "ok":
                result["status"] = status
                result["message"] = {
                    "cancelled": "已取消",
                    "timeout": "merge 超时",
                    "spawn-error": "git 无法启动，见上面的输出",
                }.get(status, "merge 未完成：见上面的 git 输出")
                return result
            if code != 0:
                result["status"] = "skipped"
                result["message"] = "无法快进到远端（可能是本地分支有分叉），保持原状，未做任何重置"
                result["head"] = read_state(checkout).get("head")
                return result
    else:
        result["steps"].append("clone")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "clone",
            "--depth",
            str(root.git.depth),
            "--no-tags",
            "--single-branch",
            "--filter=blob:none",
        ]
        if root.git.ref:
            args += ["--branch", root.git.ref]
        if root.git.sparse_paths:
            args += ["--sparse"]
        args += ["--", root.git.url, str(checkout)]
        status, code = _run_streaming(args, cwd=None, timeout=timeout, cancel=cancel, log=log, on_line=on_line)
        if status != "ok":
            result["status"] = status
            result["message"] = {
                "cancelled": "已取消",
                "timeout": "clone 超过时间上限",
                "spawn-error": "git 无法启动，见上面的输出",
            }.get(status, "clone 未完成：见上面的 git 输出")
            _cleanup_failed_clone(checkout, created=True)
            return result
        if code != 0:
            hint = remote_branch_hint(root.git.url, root.git.ref)
            result["message"] = "clone 失败：检查地址、分支名或凭据（见上面的 git 输出）" + hint
            _cleanup_failed_clone(checkout, created=True)
            return result
        created = True
        if root.git.sparse_paths:
            result["steps"].append("sparse-checkout")
            status, code = _run_streaming(
                ["sparse-checkout", "set", "--cone", *root.git.sparse_paths],
                cwd=checkout,
                timeout=timeout,
                cancel=cancel,
                log=log,
                on_line=on_line,
            )
            if status == "ok" and code != 0:
                # 老版本 git（例如 Ubuntu 20.04 自带的 2.25）可能不认 --cone，退回基础写法
                log.append("sparse-checkout --cone 不被当前 git 支持，改用不带 --cone 的写法重试")
                result["steps"].append("sparse-checkout(no-cone)")
                status, code = _run_streaming(
                    ["sparse-checkout", "set", *root.git.sparse_paths],
                    cwd=checkout,
                    timeout=timeout,
                    cancel=cancel,
                    log=log,
                    on_line=on_line,
                )
            if status != "ok" or code != 0:
                result["status"] = status if status != "ok" else "partial"
                result["message"] = (
                    f"稀疏检出未完成（{status}），当前只包含顶层文件"
                    if status != "ok"
                    else "clone 成功但稀疏检出失败，当前只包含顶层文件"
                )
                result["head"] = read_state(checkout).get("head")
                return result

    state = read_state(checkout)
    result["status"] = "ready"
    result["head"] = state.get("head")
    result["shallow"] = state.get("shallow")
    result["dirtyFiles"] = state.get("dirtyFiles")
    action = "clone" if created else "fetch + merge --ff-only"
    result["message"] = f"已同步（{action}），HEAD={state.get('head')}"
    return result


def _cleanup_failed_clone(checkout: Path, *, created: bool) -> None:
    """clone 失败后清掉刚创建的半成品目录，避免下次同步被"目录已存在"挡住。"""
    if not created or not checkout.exists():
        return
    marker = checkout / ".git"
    if marker.exists():
        return
    try:
        shutil.rmtree(checkout, ignore_errors=True)
    except OSError:
        pass


@dataclass
class SyncManager:
    """单用户 MVP：同一时刻只允许一个同步任务。"""

    config: AppConfig
    running: bool = False
    workspace_id: str | None = None
    status: str = "idle"
    message: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    steps: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    _cancel: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "status": self.status,
                "workspaceId": self.workspace_id,
                "message": self.message,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "elapsedSeconds": round((self.finished_at or time.time()) - self.started_at, 1) if self.started_at else None,
                "steps": list(self.steps),
                "log": list(self.log[-40:]),
            }

    def start(self, root: RootConfig) -> tuple[bool, str]:
        with self._lock:
            if self.running:
                return False, f"已有同步任务在进行（工作区 {self.workspace_id}），请等待或取消"
            self.running = True
            self.workspace_id = root.id
            self.status = "running"
            self.message = f"正在同步 {root.id}"
            self.started_at = time.time()
            self.finished_at = None
            self.steps = []
            self.log = []
            self._cancel = threading.Event()
        thread = threading.Thread(target=self._work, args=(root, self._cancel), daemon=True)
        thread.start()
        return True, "已开始同步"

    def _work(self, root: RootConfig, cancel: threading.Event) -> None:
        try:
            outcome = sync_root(
                root, self.config.cache_dir, cancel=cancel, timeout=self.config.limits.sync_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - 同步失败不能影响服务
            outcome = {"status": "error", "message": f"同步异常：{exc}", "steps": [], "log": [], "head": None}
        with self._lock:
            self.running = False
            self.status = outcome.get("status", "error")
            self.message = outcome.get("message", "")
            self.steps = list(outcome.get("steps", []))
            self.log = list(outcome.get("log", []))
            if outcome.get("head"):
                self.log.append(f"HEAD={outcome['head']}")
            self.finished_at = time.time()

    def cancel(self) -> bool:
        with self._lock:
            if not self.running:
                return False
            self._cancel.set()
            self.message = "正在取消…"
            return True
