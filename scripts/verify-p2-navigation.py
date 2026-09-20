"""P2 语义跳转验收：用**真实语言服务**逐条核对 fixtures/navigation-cases.json 的预期。

两种运行方式：

  # 1) 直接在服务器上跑（不需要先启动 HTTP 服务，最省事）
  cd ~/android-source-workbench
  python3 scripts/verify-p2-navigation.py --direct --workspace fixtures

  # 2) 对已启动的服务发真实 HTTP 请求
  python3 scripts/verify-p2-navigation.py --base http://127.0.0.1:8787 --workspace fixtures

判定规则（与任务书一致）：
- 语言服务允许返回完整标识符范围，因此判定"预期位置是否落在返回范围内"，而不是逐字符相等；
- 状态必须是 resolved 或 ambiguous（ambiguous 时必须包含预期目标）；
- 未找到 clangd、语言服务未接入、stale 等情况一律算失败，并打印真实原因——不把"跑不了"算成"通过"。

退出码：0 全部通过；1 有用例未通过；2 环境不满足（缺 clangd / 缺依赖），此时**不能**当成通过。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "server"
CASES_PATH = REPO / "fixtures" / "navigation-cases.json"

results: list[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def position_tuple(value: dict) -> tuple:
    return (int(value.get("line", 0)), int(value.get("character", 0)))


def target_matches(targets: list, expected: dict) -> list:
    want_path = expected["path"].replace("\\", "/")
    want_pos = (int(expected["line"]), int(expected["character"]))
    hits = []
    for target in targets:
        path = str(target.get("path", "")).replace("\\", "/")
        if not (path == want_path or path.endswith("/" + want_path)):
            continue
        rng = target.get("range") or {}
        start = position_tuple(rng.get("start") or {})
        end = position_tuple(rng.get("end") or {})
        if start <= want_pos <= end:
            hits.append(target)
    return hits


# ------------------------------------------------------------------ HTTP 模式


class HttpNavigator:
    def __init__(self, base: str, workspace: str) -> None:
        self.base = base.rstrip("/")
        self.workspace = workspace
        self._hashes: dict[str, str] = {}

    def _request(self, method: str, path: str, params=None, body=None, timeout: float = 120.0):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = error.read().decode("utf-8")
            try:
                return error.code, json.loads(payload)
            except ValueError:
                return error.code, {"raw": payload}

    def file_hash(self, path: str) -> str | None:
        if path not in self._hashes:
            status, body = self._request("GET", "/api/file", {"workspace": self.workspace, "path": path})
            if status != 200 or body.get("status") != "ok":
                return None
            self._hashes[path] = body.get("hash")
        return self._hashes[path]

    def navigate(self, path: str, line: int, character: int, kind: str) -> dict:
        return self._request(
            "POST",
            "/api/navigation",
            body={
                "workspace": self.workspace,
                "path": path,
                "kind": kind,
                "position": {"line": line, "character": character},
                "sourceVersion": self.file_hash(path),
            },
        )[1]

    def navigation_status(self) -> dict:
        return self._request("GET", "/api/health")[1].get("navigation", {})


# ------------------------------------------------------------------ direct 模式


class DirectNavigator:
    """在服务器上直接调用服务，不需要先启动 HTTP。"""

    def __init__(self, workspace: str, config_path: str) -> None:
        if str(SERVER) not in sys.path:
            sys.path.insert(0, str(SERVER))
        from app.config import ConfigError, load_config
        from app.lsp.manager import LanguageServerManager
        from app.navigation import NavigationService
        from app.pathtools import resolve_under_root

        try:
            self.config = load_config(config_path)
        except ConfigError as exc:
            print("[配置错误] " + "；".join(exc.messages), file=sys.stderr)
            raise SystemExit(2) from exc
        self.root = self.config.root(workspace)
        if self.root is None:
            print(f"[错误] 配置里没有工作区 {workspace}", file=sys.stderr)
            raise SystemExit(2)
        self._resolve = resolve_under_root
        self.manager = LanguageServerManager(self.config.navigation, self.config.roots)
        self.service = NavigationService(self.config, self.manager)

    def navigation_status(self) -> dict:
        return self.service.status()

    def file_hash(self, path: str) -> str | None:
        resolved, _rel = self._resolve(self.root.path, path)
        if not resolved.is_file():
            return None
        from app.navigation import SourceSnapshot

        return SourceSnapshot.read(resolved).version

    async def navigate(self, path: str, line: int, character: int, kind: str) -> dict:
        # 注意：必须在同一个 event loop 里跑完全部用例——语言服务进程与 transport
        # 绑定在创建它的 loop 上，每次 asyncio.run 都换 loop 会让进程不可用。
        resolved, rel = self._resolve(self.root.path, path)
        return await self.service.navigate(
            root=self.root,
            rel_path=rel,
            resolved_path=resolved,
            position={"line": line, "character": character},
            kind=kind,
            source_version=self.file_hash(path),
        )

    async def shutdown(self) -> None:
        await self.manager.shutdown_all()


# ------------------------------------------------------------------ 主流程


async def run_cases(navigator, cases: list, kind_override: str | None) -> None:
    for case in cases:
        case_id = case["id"]
        source = case["from"]
        expected = case["expected"]
        kind = kind_override or ("references" if case.get("method") == "textDocument/references" else "definition")
        try:
            body = navigator.navigate(source["path"], source["line"], source["character"], kind)
            if asyncio.iscoroutine(body):
                body = await body
        except Exception as exc:  # noqa: BLE001 - 单个用例失败不应中断整轮
            check(case_id, False, f"请求异常：{type(exc).__name__}: {exc}")
            continue
        status = body.get("status")
        targets = body.get("targets") or []
        hits = target_matches(targets, expected)
        detail = (
            f"{source['path']}:{source['line']}:{source['character']} {source['symbol']} → "
            f"status={status} 目标数={len(targets)} 命中={len(hits)}"
        )
        if kind == "definition" and hits and status in ("resolved", "ambiguous"):
            note = "（语言服务返回多个目标，其中包含预期位置）" if status == "ambiguous" else ""
            check(case_id, True, detail + note)
            continue
        if status in ("resolved",) and hits:
            check(case_id, True, detail)
            continue
        raw_reason = body.get("reason") or ""
        # 长原因（例如语言服务退出时带出的 stderr）要保留**尾部**：真正的原因通常在最后
        reason = raw_reason if len(raw_reason) <= 200 else "…" + raw_reason[-200:]
        tail = (body.get("server") or {}).get("stderrTail") or []
        if tail and "stderr 尾部" not in reason:
            reason += " | stderr: " + " / ".join(tail[-3:])
        actual = ", ".join(
            f"{t.get('path')}@{(t.get('range') or {}).get('start')}" for t in targets[:3]
        ) or "无"
        check(case_id, False, f"{detail} 期望={expected['path']}@{expected['line']}:{expected['character']} 实际={actual} 原因={reason}")


async def run_all(args) -> int:
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.limit:
        cases = cases[: args.limit]

    if args.base:
        navigator = HttpNavigator(args.base, args.workspace)
    else:
        config_path = args.config
        if config_path is None:
            if str(SERVER) not in sys.path:
                sys.path.insert(0, str(SERVER))
            from app.config import resolve_config_path

            config_path = str(resolve_config_path(None))
        print(f"[配置] {config_path}")
        navigator = DirectNavigator(args.workspace, config_path)

    status = navigator.navigation_status()
    supported = status.get("supported") or {}
    cpp = supported.get("cpp") or {}
    java = supported.get("java") or {}
    clangd_path = ((status.get("manager") or {}).get("clangd") or {}).get("path")
    print(f"[语言服务] enabled={status.get('enabled')} clangd={clangd_path} cpp可用={cpp.get('available')} 来源={cpp.get('source')}")
    print(
        f"[语言服务] java可用={java.get('available')} JDT LS={java.get('path')} "
        f"JDK={java.get('javaVersion')}（要求 {java.get('requiredJava')}）"
    )
    if java.get("reason"):
        print(f"           java 不就绪原因：{java['reason']}")
    if not status.get("enabled"):
        check("语义跳转已启用", False, "features.navigation 或 navigation.enabled 为 false")
        return 2
    env_ok = True
    if not cpp.get("available"):
        check(
            "clangd 可用",
            False,
            (cpp.get("source") or "未找到 clangd")
            + "；可设置 navigation.clangdPath，或把 AOSP 根目录加入 navigation.searchDirs"
            "（AOSP 自带 prebuilts/clang/host/linux-x86/*/bin/clangd）",
        )
        env_ok = False
    if not java.get("available"):
        check("JDT LS 可用", False, java.get("reason") or "未找到 JDT LS；可运行 scripts/setup-java.py")
        env_ok = False
    if not env_ok:
        # 环境不满足时只跑能跑的那部分用例，并把退出码与"全部通过"区分开
        await run_cases(navigator, cases, args.kind)
        if isinstance(navigator, DirectNavigator):
            await navigator.shutdown()
        return 2

    print(f"[用例] {len(cases)} 条，工作区 {args.workspace}")
    await run_cases(navigator, cases, args.kind)
    if isinstance(navigator, DirectNavigator):
        await navigator.shutdown()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="已启动服务的地址，例如 http://127.0.0.1:8787")
    parser.add_argument("--direct", action="store_true", help="不经过 HTTP，直接在本进程内调用服务")
    parser.add_argument("--config", help="direct 模式使用的配置文件（默认 server/config.json，其次 config.example.json）")
    parser.add_argument("--workspace", default="fixtures", help="包含 fixtures/navigation-cpp 的工作区 id")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--kind", help="覆盖导航类型（默认按用例里的 method）")
    parser.add_argument("--limit", type=int, help="只跑前 N 条用例")
    args = parser.parse_args()

    if not args.base and not args.direct:
        parser.error("请给出 --direct 或 --base")
    env_code = asyncio.run(run_all(args))
    result_code = report()
    # 环境不满足（缺 clangd 等）时返回 2：必须与"全部通过"区分开
    return env_code or result_code


def report() -> int:
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""))
    total = len(results)
    print(f"\n{total - len(failed)}/{total} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
