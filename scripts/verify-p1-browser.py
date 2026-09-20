"""浏览器端交互验收（可选，需要本机 Chromium 与 websockets）。

用法：
    python scripts/verify-p1-browser.py --base http://127.0.0.1:8787
    ASW_CHROME=/path/to/chrome python scripts/verify-p1-browser.py

它通过 CDP 打开真实页面，按顺序验证：示例模式渲染 → 切换到真实模式 →
真实检索 → 打开真实文件 → 点击标识符得到「未就绪」→ 真实目录树 → 无 JS 异常。
没有 Chromium 或 websockets 时直接报告跳过，不伪造结果。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CHROME_CANDIDATES = [
    os.environ.get("ASW_CHROME", ""),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return str(path)
        if path.is_dir() and "ms-playwright" in candidate:
            found = sorted(path.glob("chromium*/chrome-win64/chrome.exe"))
            if found:
                return str(found[-1])
    for name in ("chrome", "chromium", "google-chrome", "msedge"):
        located = shutil.which(name)
        if located:
            return located
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CDP:
    def __init__(self, websocket):
        self.ws = websocket
        self.counter = 0

    async def call(self, method: str, params: dict | None = None, timeout: float = 30.0):
        self.counter += 1
        message_id = self.counter
        await self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            event = json.loads(raw)
            if event.get("id") == message_id:
                if "error" in event:
                    raise RuntimeError(f"{method} 失败：{event['error']}")
                return event.get("result", {})
            if event.get("method") == "Runtime.exceptionThrown":
                details = event["params"].get("exceptionDetails", {})
                results.append(("未捕获异常（事件）", False, json.dumps(details.get("exception", {}).get("description", details))[:200]))

    async def evaluate(self, expression: str, timeout: float = 30.0):
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"])[:300])
        return result.get("result", {}).get("value")

    async def wait_for(self, expression: str, description: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await self.evaluate(expression, timeout=10):
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.25)
        check(f"等待「{description}」", False, f"超过 {timeout:g} 秒仍未满足：{expression[:120]}")
        return False


CLICK_BY_ID = "document.getElementById(%s).click(); true"


async def run_checks(cdp: CDP, base: str) -> None:
    await cdp.call("Runtime.enable")
    await cdp.call("Page.enable")
    await cdp.call("Page.navigate", {"url": base + "/"})
    if not await cdp.wait_for("!!document.querySelector('#cw-list') && document.querySelector('#cw-list').children.length > 0", "示例模式渲染"):
        return
    await cdp.evaluate(
        "window.__aswErrors=[];"
        "window.addEventListener('error',event=>window.__aswErrors.push('error: '+event.message));"
        "window.addEventListener('unhandledrejection',event=>window.__aswErrors.push('rejection: '+String(event.reason)));"
        "true"
    )
    tag = await cdp.evaluate("document.getElementById('cw-mode-tag').textContent")
    check("示例模式标记明确", "示例" in tag, tag)
    results_html = await cdp.evaluate("document.getElementById('cw-list').innerHTML")
    check("示例搜索结果渲染", "DisplayController.java" in results_html, f"长度 {len(results_html)}")
    check("示例模式不显示服务器输入框", await cdp.evaluate("document.getElementById('cw-serverwrap').hidden") is True, "")

    await cdp.evaluate(CLICK_BY_ID % "'cw-use-real'")
    if not await cdp.wait_for("document.getElementById('cw-status').textContent.includes('真实源码模式') && !document.getElementById('cw-workwrap').hidden", "切换到真实模式"):
        return
    footer = await cdp.evaluate("document.getElementById('cw-footright').textContent")
    check("真实模式报告后端引擎与未实现能力", ("引擎" in footer and "P3" in footer and "P4" in footer), footer)

    await cdp.evaluate(
        "const input=document.getElementById('cw-search');"
        "input.value='setBrightness';"
        "input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));true"
    )
    if not await cdp.wait_for("!!document.querySelector('#cw-list [data-real]')", "真实检索返回结果"):
        return
    first = await cdp.evaluate("const el=document.querySelector('#cw-list [data-real]');({path:el.dataset.real,line:el.dataset.line})")
    check("真实检索结果来自服务器文件", first["path"].endswith(".java") or "/" in first["path"], json.dumps(first, ensure_ascii=False))
    count_text = await cdp.evaluate("document.getElementById('cw-count').textContent")
    check("检索计数以文件为单位展示", "文件" in count_text, count_text)

    await cdp.evaluate("document.querySelector('#cw-list [data-real]').click(); true")
    if not await cdp.wait_for("!!document.querySelector('#cw-realcode .cw-line')", "打开真实文件内容"):
        return
    code_text = await cdp.evaluate("document.getElementById('cw-realcode').textContent")
    check("真实文件内容正确渲染", "setBrightness" in code_text and len(code_text) > 100, f"字符数 {len(code_text)}")
    meta = await cdp.evaluate("document.getElementById('cw-path').textContent")
    check("显示真实路径与版本哈希", "sha256:" in meta and meta.startswith("真实文件") and first["path"] in meta, meta[:100])

    click_result = await cdp.evaluate(
        "(() => {const lines=[...document.querySelectorAll('#cw-realcode .cw-line')];"
        "for (const line of lines) {const span=line.querySelector('.cw-linecode'); if(!span) continue;"
        "const walker=document.createTreeWalker(span, NodeFilter.SHOW_TEXT); let node;"
        "while ((node=walker.nextNode())) {const idx=node.nodeValue.indexOf('setBrightness'); if(idx<0) continue;"
        "const range=document.createRange(); range.setStart(node,idx+2); range.setEnd(node,idx+2);"
        "const rect=range.getBoundingClientRect();"
        "line.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:Math.round(rect.left)+1,clientY:Math.round(rect.top)+2}));"
        "return JSON.stringify({line:line.dataset.lineno,x:Math.round(rect.left),y:Math.round(rect.top)});}}"
        "return 'not-found';})()"
    )
    check("代码中可以定位标识符并触发点击", click_result not in ("not-found", None), str(click_result))
    if not await cdp.wait_for("document.getElementById('cw-relation-note').textContent.includes('最近一次')", "跳转请求返回状态"):
        return
    definitions = await cdp.evaluate("document.getElementById('cw-definitions').textContent")
    note = await cdp.evaluate("document.getElementById('cw-relation-note').textContent")
    reported = (note + " " + definitions)
    check(
        "点击标识符返回真实语义状态，不伪造目标",
        "跳转" in note and ("已解析" in reported or "未就绪" in reported or "需要你选择" in reported),
        f"{note} / {definitions[:80]}",
    )
    cap = await cdp.evaluate("document.getElementById('cw-refs-count').textContent")
    check("引用面板区分语义引用与字面匹配", "字面" in cap or "引用" in cap, cap)
    refs = await cdp.evaluate("document.getElementById('cw-refs').textContent")
    check("引用面板说明字面匹配不等于引用", "不等于引用" in refs and "字面匹配" in refs, refs[:80])

    await cdp.evaluate("document.querySelector('[data-nav=files]').click(); true")
    if not await cdp.wait_for("!!document.querySelector('#cw-list [data-tree]')", "真实目录树加载"):
        return
    tree_text = await cdp.evaluate("document.getElementById('cw-list').textContent")
    check("目录树显示真实目录名", "frameworks" in tree_text and "hardware" in tree_text, tree_text[:80])

    await cdp.evaluate("document.querySelector('#cw-list [data-tree=\"frameworks\"]').click(); true")
    check("目录可继续展开", await cdp.wait_for("!!document.querySelector('#cw-list [data-tree=\"frameworks/base\"]')", "展开子目录"), "")

    await cdp.evaluate(CLICK_BY_ID % "'cw-edit'")
    edit_notice = await cdp.evaluate("document.getElementById('cw-banner').textContent")
    check("真实模式编辑入口说明未开放", "P4" in edit_notice, edit_notice[:80])

    # 远程只读工作区：标记、未同步提示、同步与取消
    options = await cdp.evaluate("Array.from(document.querySelectorAll('#cw-workspace option')).map(o=>o.textContent)")
    check("工作区列表标出远程只读项", any("远程" in text for text in (options or [])), str(options)[:140])
    selected = await cdp.evaluate(
        "(()=>{const sel=document.getElementById('cw-workspace');"
        "const opt=[...sel.options].find(o=>o.textContent.includes('远程'));"
        "if(!opt) return null; sel.value=opt.value; sel.dispatchEvent(new Event('change',{bubbles:true})); return opt.value;})()"
    )
    check("可以选中远程工作区", selected is not None, str(selected))
    if selected and await cdp.wait_for("document.getElementById('cw-sync').hidden === false", "显示同步按钮"):
        listed = await cdp.evaluate("document.getElementById('cw-list').textContent.replace(/\\s+/g,' ')")
        unsynced = "尚未同步" in listed
        if unsynced:
            check("未同步的远程工作区给出明确操作提示", "尚未同步" in listed and "--sync" in listed, listed[:120])
        else:
            # 本机以前同步过（缓存目录还在）：改为验证已同步时树能正常加载
            check(
                "远程工作区已同步时目录可正常加载",
                len(listed.strip()) > 0 and "尚未同步" not in listed,
                f"{listed[:100]}（本机缓存里已有该远程工作区，故走已同步分支）",
            )
        await cdp.evaluate("document.querySelector('[data-nav=files]').click(); true")
        if unsynced:
            await cdp.wait_for("document.getElementById('cw-list').textContent.includes('尚未同步')", "文件页同样提示未同步")
        else:
            await cdp.wait_for("document.getElementById('cw-list').textContent.trim().length > 0", "文件页加载远程目录")
        await cdp.evaluate(CLICK_BY_ID % "'cw-sync'")
        if await cdp.wait_for("document.getElementById('cw-sync-cancel').hidden === false", "同步开始并显示取消按钮", timeout=30):
            banner = await cdp.evaluate("document.getElementById('cw-banner').textContent")
            check("同步过程有可见状态", "同步" in banner, banner[:140])
            check(
                "同步是服务器端 git 操作，不是前端模拟",
                "clone" in banner or "fetch" in banner or "准备中" in banner,
                banner[:140],
            )
            await cdp.evaluate(CLICK_BY_ID % "'cw-sync-cancel'")
            check(
                "同步可以由界面取消",
                await cdp.wait_for(
                    "document.getElementById('cw-sync-cancel').hidden === true", "同步已取消并恢复", timeout=60
                ),
                "",
            )

    errors = await cdp.evaluate("JSON.stringify(window.__aswErrors||[])")
    check("页面无未捕获 JS 异常", errors in ("[]", None), str(errors))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    try:
        import websockets
    except ImportError:
        check("浏览器验收可执行", False, "未安装 websockets；请 pip install websockets 或跳过本检查")
        return 1

    chrome = find_chrome()
    if not chrome:
        check("浏览器验收可执行", False, "未找到 Chromium/Chrome，可用 ASW_CHROME 指定路径")
        return 1

    try:
        with urllib.request.urlopen(args.base + "/api/health", timeout=10) as response:
            health = json.loads(response.read().decode("utf-8"))
        check("被检查的服务真实可用", health.get("status") == "ok", args.base)
    except Exception as error:  # noqa: BLE001
        check("被检查的服务真实可用", False, f"{type(error).__name__}: {error}")
        return 1

    port = free_port()
    profile = tempfile.mkdtemp(prefix="asw-chrome-")
    process = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--window-size=1400,1000",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        target = None
        while time.monotonic() < deadline and target is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                target = next((item for item in targets if item.get("type") == "page"), None)
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        check("Chromium 远程调试已就绪", target is not None, chrome)
        if target:
            async with websockets.connect(target["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024) as ws:
                await run_checks(CDP(ws), args.base)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001
        check("浏览器验收中断", False, f"{type(error).__name__}: {error}")
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过" + (f"，失败：{failed}" if failed else ""))
    raise SystemExit(1 if failed else 0)
