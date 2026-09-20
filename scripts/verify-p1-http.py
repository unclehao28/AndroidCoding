"""对着已启动的 P1 后端做一轮真实 HTTP 验收（只用标准库）。

用法：
    python scripts/verify-p1-http.py --base http://127.0.0.1:8765

它会读取 fixtures/demo-tree，验证真实文件读取、全库检索、路径边界与未就绪状态。
输出 PASS/FAIL 列表，全部为真实请求结果，不模拟成功。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:  # Windows 控制台默认代码页会导致中文乱码
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

JAVA_PATH = "frameworks/base/services/core/java/demo/display/DisplayController.java"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def request(base: str, method: str, path: str, params=None, body=None, timeout: float = 30.0):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8")
        try:
            return error.code, json.loads(payload)
        except ValueError:
            return error.code, {"raw": payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--workspace", default="demo")
    parser.add_argument("--path", default=JAVA_PATH)
    parser.add_argument("--remote-workspace", default="aosp-system-core", help="示例配置里的远程只读工作区 id")
    args = parser.parse_args()
    base = args.base

    status, health = request(base, "GET", "/api/health")
    check("健康状态可用", status == 200 and health.get("status") == "ok", f"HTTP {status} mode={health.get('mode')}")
    check("真实模式标记", health.get("mode") == "real", str(health.get("mode")))
    check(
        "未实现能力如实标注（写入/索引仍为 false，语义跳转已开启）",
        health.get("features", {}).get("write") is False
        and health.get("features", {}).get("navigation") is True
        and health.get("capabilities", {}).get("index") is False,
        json.dumps(
            {"features": health.get("features"), "index": health.get("capabilities", {}).get("index")},
            ensure_ascii=False,
        ),
    )
    engine = health.get("search", {})
    check("报告实际检索引擎", engine.get("name") in {"ripgrep", "python-bounded", "unavailable"}, json.dumps(engine, ensure_ascii=False))

    status, workspaces = request(base, "GET", "/api/workspaces")
    ids = [item["id"] for item in workspaces.get("workspaces", [])]
    check("列出配置的工作区", status == 200 and args.workspace in ids, f"{ids}")

    status, health_git = request(base, "GET", "/api/health")
    git_info = health_git.get("git", {})
    check(
        "报告 git 能力与远程工作区",
        status == 200 and git_info.get("available") is True and args.remote_workspace in (git_info.get("remoteRoots") or []),
        f"git={git_info.get('version')} remoteRoots={git_info.get('remoteRoots')}",
    )

    status, remote_tree = request(base, "GET", "/api/tree", {"workspace": args.remote_workspace, "path": ""})
    if status == 200:
        # 本机以前同步过这个远程工作区（缓存目录还在）：改为验证"已同步"这一支的行为
        check(
            "远程工作区已同步时可浏览且标记为只读",
            isinstance(remote_tree.get("entries"), list) and remote_tree.get("readonly") is True,
            f"entries={len(remote_tree.get('entries') or [])} readonly={remote_tree.get('readonly')}"
            "（本机已同步过该远程工作区；要验证 409 分支请先删除 .asw-cache 下的缓存目录）",
        )
    else:
        check(
            "未同步的远程工作区返回明确的 409 而不是普通 404",
            status == 409 and remote_tree.get("error", {}).get("code") == "workspace_not_synced",
            f"HTTP {status} code={remote_tree.get('error', {}).get('code')}",
        )

    status, remote_sync_state = request(base, "GET", "/api/workspaces")
    remote_item = next(
        (item for item in remote_sync_state.get("workspaces", []) if item["id"] == args.remote_workspace), None
    )
    check(
        "工作区列表暴露远程来源与本地缓存状态",
        status == 200 and remote_item is not None and remote_item["remote"] is True
        and remote_item["git"]["url"] and remote_item["sync"]["isRepo"] in (True, False),
        f"url={remote_item['git']['url'] if remote_item else None} checkout={remote_item['path'] if remote_item else None}",
    )

    status, local_sync = request(base, "POST", "/api/workspace/sync", body={"workspace": args.workspace})
    check(
        "本地目录工作区拒绝同步请求",
        status == 400 and "不需要同步" in local_sync.get("error", {}).get("message", ""),
        f"HTTP {status} {local_sync.get('error', {}).get('message', '')[:40]}",
    )

    status, sync_status = request(base, "GET", "/api/workspace/sync")
    check("同步状态接口可用", status == 200 and "running" in sync_status, f"running={sync_status.get('running')}")

    status, tree = request(base, "GET", "/api/tree", {"workspace": args.workspace, "path": ""})
    names = [item["name"] for item in tree.get("entries", [])]
    check("分层目录列表（只请求当前层）", status == 200 and "frameworks" in names, f"{names[:6]}")

    status, nested = request(base, "GET", "/api/tree", {"workspace": args.workspace, "path": "frameworks/base/services/core/java/demo/display"})
    nested_names = [item["name"] for item in nested.get("entries", [])]
    check("子目录按需加载", status == 200 and "DisplayController.java" in nested_names, f"{nested_names}")

    status, file_body = request(base, "GET", "/api/file", {"workspace": args.workspace, "path": args.path})
    joined = "\n".join(file_body.get("lines", []))
    check(
        "读取真实文件",
        status == 200 and file_body.get("status") == "ok" and "setBrightness" in joined,
        f"HTTP {status} lines={file_body.get('lineCount')} total={file_body.get('totalLines')} hash={(file_body.get('hash') or '')[:20]}",
    )
    check("返回版本哈希供后续编辑比对", str(file_body.get("hash", "")).startswith("sha256:"), str(file_body.get("hash"))[:24])

    status, window = request(base, "GET", "/api/file", {"workspace": args.workspace, "path": args.path, "startLine": 4, "lineCount": 3})
    check("按行段读取", status == 200 and window.get("startLine") == 4 and window.get("lineCount") == 3, f"lines={window.get('lines')}")

    status, search = request(base, "POST", "/api/search", body={"query": "setBrightness", "workspace": args.workspace, "requestId": "verify-literal"})
    paths = {match["path"] for match in search.get("matches", [])}
    check(
        "全库字面量检索（默认，无需选择模块）",
        status == 200 and search.get("status") == "ok" and len(paths) >= 3,
        f"{search.get('matchCount')} 处 / {search.get('filesMatched')} 文件 / {search.get('elapsedMs')}ms / 引擎 {search.get('engine')}",
    )
    check("检索结果标注为文本来源", all(match["source"] in {"ripgrep", "python-bounded"} for match in search.get("matches", [])), "")
    check("检索结果不冒充索引", search.get("indexed") is False, str(search.get("indexNote"))[:60])

    status, mixed = request(base, "POST", "/api/search", body={"query": "display", "workspace": args.workspace, "requestId": "verify-mixed"})
    suffixes = {match["path"].rsplit(".", 1)[-1] for match in mixed.get("matches", [])}
    check(
        "一次全库检索可同时命中 Java/C++/AIDL/XML/Android.bp/rc",
        status == 200 and {"java", "cpp", "aidl", "xml", "bp", "rc"} <= suffixes,
        f"命中类型 {sorted(suffixes)}",
    )

    status, regex_search = request(
        base, "POST", "/api/search", body={"query": r"nativeSet\w+", "workspace": args.workspace, "regex": True, "caseSensitive": True, "requestId": "verify-regex"}
    )
    check(
        "正则检索为显式选项",
        status == 200 and regex_search.get("status") == "ok" and regex_search.get("options", {}).get("regex") is True,
        f"{regex_search.get('matchCount')} 处",
    )

    status, limited = request(base, "POST", "/api/search", body={"query": "setBrightness", "workspace": args.workspace, "limit": 2, "requestId": "verify-limit"})
    check("结果上限与截断状态", status == 200 and limited.get("truncated") is True and len(limited.get("matches", [])) == 2, f"limit={limited.get('limit')} truncated={limited.get('truncated')}")

    status, traversal = request(base, "GET", "/api/file", {"workspace": args.workspace, "path": "../../etc/passwd"})
    check("拒绝路径穿越", status == 400 and traversal.get("error", {}).get("code") == "invalid_path", f"HTTP {status} {json.dumps(traversal, ensure_ascii=False)[:80]}")

    status, drive = request(base, "GET", "/api/file", {"workspace": args.workspace, "path": "C:/Windows/win.ini"})
    check("拒绝盘符/绝对路径", status == 400, f"HTTP {status}")

    status, missing = request(base, "GET", "/api/file", {"workspace": "no-such-workspace", "path": "a.txt"})
    check("未知工作区返回 404", status == 404, f"HTTP {status}")

    status, nav = request(
        base,
        "POST",
        "/api/navigation",
        body={"workspace": args.workspace, "path": args.path, "kind": "definition", "sourceVersion": file_body.get("hash"), "position": {"line": 6, "character": 21}},
    )
    check(
        "导航结果标注坐标基准与能力来源",
        nav.get("positionUnit") == "utf-16" and nav.get("lineBase") == 0 and "capability" in nav,
        f"unit={nav.get('positionUnit')} capability={json.dumps(nav.get('capability', {}), ensure_ascii=False)}",
    )
    status, health_nav = request(base, "GET", "/api/health")
    nav_status = health_nav.get("navigation", {})
    java_support = (nav_status.get("supported", {}) or {}).get("java", {}) or {}
    if java_support.get("available"):
        # Java 已接入（JDT LS + JDK 就绪）：跳转必须有真实目标，且如实标注 Soong/classpath 未适配
        check(
            "Java 语义跳转在 JDT LS 就绪时给出真实目标",
            nav.get("status") in ("resolved", "ambiguous") and len(nav.get("targets", [])) >= 1
            and nav.get("kind") == "semantic",
            f"status={nav.get('status')} targets={len(nav.get('targets', []))} server={(nav.get('server') or {}).get('name')}",
        )
        check(
            "Java 能力如实标注 Soong/classpath 未适配",
            java_support.get("classpathSupport") is False,
            f"classpathSupport={java_support.get('classpathSupport')}",
        )
    else:
        # JDT LS 不可用：必须说清缺什么（JDT LS / JDK），且绝不伪造目标
        reason = nav.get("reason") or ""
        check(
            "Java 语言服务不可用时说明原因且不伪造目标",
            nav.get("status") == "unavailable" and nav.get("targets") == []
            and ("JDT LS" in reason or "JDK" in reason) and nav.get("kind") == "semantic",
            f"status={nav.get('status')} targets={len(nav.get('targets', []))} reason={reason[:70]}",
        )
    check(
        "健康状态如实报告语言服务能力矩阵",
        "supported" in nav_status and "notImplemented" in nav_status and nav_status.get("supported", {}).get("cpp", {}).get("available")
        in (True, False),
        f"clangd={nav_status.get('manager', {}).get('clangd', {}).get('path')} cpp可用={nav_status.get('supported', {}).get('cpp', {}).get('available')}",
    )

    cancel_observed = {"status": None}

    def long_search():
        _, body = request(
            base, "POST", "/api/search", body={"query": "a", "workspace": args.workspace, "requestId": "verify-cancel", "limit": 500}, timeout=60
        )
        cancel_observed["status"] = body.get("status")

    thread = threading.Thread(target=long_search)
    thread.start()
    status, cancel = request(base, "POST", "/api/search/cancel", body={"requestId": "verify-cancel"})
    thread.join(timeout=60)
    check(
        "取消接口可用（本次结果见详情）",
        status == 200 and "cancelled" in cancel,
        f"cancelled={cancel.get('cancelled')} 检索最终状态={cancel_observed['status']}（已结束则为 ok，属正常）",
    )

    failed = [name for name, ok, _ in results if not ok]
    return 1 if failed else 0


def report() -> int:
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过" + (f"，失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - 中断也要输出已完成的检查项
        check("验收脚本执行中断", False, f"{type(error).__name__}: {error}")
    raise SystemExit(report())
