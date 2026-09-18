# 当前交接状态

更新时间：2026-09-18。版本：device 侧 `0.2.0-p1`（API `p1`）。本轮阶段：**P1（把原型接到真实目录）已完成**。
上一版 0.1.0-prototype 只有前端示例；P1 增加了真实后端、真实文件读取与受限检索。

## 已完成（P1 交付项 1-8）

1. 配置：`server/config.example.json` + `server/app/config.py`，含允许的源码根目录、监听地址/端口、
   文件大小与行段/检索/并发限额、排除规则；未知字段、类型错误、越界、根目录不存在都会启动失败并列出全部问题
   （`features.write`/`features.navigation` 写成 true 也会失败，防止把未实现能力配置成"已开启"）。
2. 真实后端（FastAPI）：`/api/health`、`/api/workspaces`、`/api/config`、`/api/tree`、`/api/file`、
   `/api/search`、`/api/search/cancel`、`/api/navigation`。
3. 前端显式区分两种来源：右上角「示例数据 / 真实服务器」。真实模式只走 `/api`，连不上显示错误，
   **不会**回退到内置示例数据（`prototype/app.js` 中 demo/real 分支分离）。
4. 搜索默认全库；缺 `rg` 时用受限 Python 扫描（文件数、大小、时间、结果上限），默认字面量、正则/大小写/全字为显式选项，
   参数以数组传给进程（不拼接 shell），有并发上限、超时可终止、`requestId` 可取消。
5. 所有文件访问都经 `resolve_under_root`：拒绝 `..`、盘符、UNC、NUL；`resolve()` 后必须仍在根目录内，
   符号链接指向根目录外会被 403 并标记；UTF-8/BOM/UTF-16/GBK/latin-1 解码，NUL 判为二进制，超限文件返回 `too_large`。
6. 页面保留搜索、预览、跳转历史结构；未接入的按钮显示明确未就绪：点击标识符会真实请求 `/api/navigation` 并显示
   `unavailable`（不伪造跳转），「编辑」「diff」在真实模式提示属于 P4，引用面板注明"字面匹配不等于引用"。
7. 换任意目录可用：用一次性脚本把根目录指向临时新建目录（含中文路径与随机文件名），目录/文件/检索 4 项全部通过；
   pytest 也全部基于临时目录而非 `fixtures`。
8. 启动命令、依赖、测试结果见下。

## 本机环境（决定了哪些只能"未测"）

Windows 客户端，Python 3.13.5，Node 18.20.8。**未安装 `rg`、clangd、JDT LS、没有真实 AOSP 源码、没有 javac。**
8765 端口被本机 Clash 占用，因此默认端口改为 **8787**（`netstat -ano | findstr 8787` 可先确认）。

## 验证记录（全部为真实执行）

| 命令 | 结果 |
|---|---|
| `cd server && python -m pytest` | **89 项：86 passed / 3 skipped / 0 failed，4.5s**（skip=符号链接用例，Windows 无创建权限） |
| `python scripts/check-package.py` | 通过：本地资源、3 个 JS 语法、内置示例与 `fixtures/demo-files.json` 一致、12 项导航预期坐标、C++17 语法 |
| `cd server && python -m app --config config.example.json --check-config` | 校验通过，列出 2 个根目录与 1 条 CORS 警告 |
| 启动 `python -m app`（8787）+ `python scripts/verify-p1-http.py` | **21/21 通过**（真实读文件、全库检索跨 6 种文件类型、正则、截断、路径穿越/盘符拒绝、导航 unavailable） |
| `python scripts/verify-p1-browser.py`（本机 Chromium via CDP） | **17/17 通过**（示例→真实切换、真实检索 12 处/6 文件、打开真实文件、点击标识符得 unavailable、真实目录树、无 JS 异常） |
| 换目录一次性脚本（临时目录 + 中文路径 + 随机文件名） | 4/4 通过，随后已删除该脚本 |

实测接口输出举例：`/api/file` 返回 `sha256:2051c958f0d56…`、`encoding=utf-8`、`eol=lf`、18 行；
`/api/search` 12 处/6 文件，引擎 `python-bounded 1`，`indexed=false`；
一次 `query=display` 的全库检索同时命中 `aidl/bp/cpp/java/rc/xml` 六种文件（对应 `docs/ACCEPTANCE.md` P1 第 4 条）。

## 对照 docs/ACCEPTANCE.md 的 P1 九条

| 条目 | 状态 |
|---|---|
| 配置任意源码根、不依赖演示文件名 | 通过（换目录脚本 4/4；pytest 全用临时目录） |
| 真实模式不读取内置 `files` 数组 | 通过（内置数据已隔离到 `demo-data.js`，真实分支不引用；浏览器验收确认） |
| 目录分层加载、只取需要的行段 | 通过（`/api/tree` 单层 + `/api/file` 行段窗口，默认 800 行） |
| 检索覆盖 Java/C/C++/AIDL/XML/Android.bp/rc | 通过（一次查询命中 6 种后缀） |
| 默认全库、不要求岗位身份 | 通过（默认 scope 为空 = 整个工作区，界面上没有模块/岗位选择） |
| 取消后不留扫描进程 | 通过（真实扫描取消测试 + `/api/health` 的 `activeSearches` 归零） |
| 空结果/超时/截断/编码不支持有可见状态 | 通过（空与截断有界面文案；超时/二进制/超大文件返回 status+message；P1 无索引，界面直说"索引：未建立（P3）"） |
| 越界路径与符号链接不可访问 | 通过（`..`/盘符 → 400，根目录外 → 403）；符号链接用例本机跳过，需在 Linux 复测 |
| 默认只监听 127.0.0.1，可用 SSH 转发 | 通过（默认配置 + 文档给出 `ssh -L 8787:127.0.0.1:8787`） |

## 未完成（禁止对外宣称已实现）

- 语义跳转（变量/参数/成员/全局/常量、声明与定义、引用）——P2；clangd/JDT LS 均未接入。
- 全库索引与增量更新、Repo/manifest 版本一致性、个人修改覆盖层——P3。
- 真实文件写入、保存冲突检测、可靠 diff——P4。
- JNI / AIDL / Binder 关联分析——P5。
- 真实 AOSP 源码上的性能、覆盖率、多用户、权限与账号体系（首版不做企业平台）。

## 已知限制

- `indexed=false`：每次检索都是按目录扫描，不是索引；超大 checkout 必须等 P3（优先评估 Zoekt）。
- `rg` 路径只有单元测试（argv 数组、`--fixed-strings`、glob、超时/取消分支）；**本机没有 rg，未真实执行过 rg 引擎**，
  需要在 Linux 服务器上安装后再复测。缺 rg 时的 Python 引擎在正则回溯或磁盘阻塞时无法被强制中断（超时只是置位取消标志）。
- 符号链接越界用例在本机被跳过（Windows 权限），必须在 Linux 上再跑一次 `python -m pytest` 覆盖。
- 前端标识符点击依赖 Chrome/Edge 的 `caretRangeFromPoint`，未在其他浏览器验证；每次打开文件最多读 800 行，可继续加载。
- 只有只读能力，无认证；默认只监听 127.0.0.1，请通过 SSH 端口转发访问，不要把非回环地址暴露到公网。
- 本轮结束时本机仍有一个后端进程监听 8787（日志 `server/run-out.log`、`server/run-err.log`），需要时用
  `Get-Process python | Where-Object {$_.CommandLine -like '*-m app*'}` 找到并结束。

## 下一步（P2 入口，用户说"继续 P2"再开始）

1. `python scripts/prepare-navigation-fixtures.py`，在 Linux 上装 clangd 并用 `fixtures/navigation-cpp` 跑通
   `textDocument/definition|declaration|references`，再补 `compile_commands` 与真实 AOSP 编译参数。
2. 把 `/api/navigation` 从固定 `unavailable` 改为真实调度：请求带 workspace、相对路径、文档 hash、
   行列与导航类型；按 workspace/构建目标管理语言服务进程，控制数量与内存。
3. 校验 UTF-16 行列转换（含中文与非 BMP 字符），同名多结果按作用域/工程身份消歧，无法消歧时返回 `ambiguous` 让用户选择。
4. Java 侧单独记录 Soong 导入适配与失败诊断，不得声称"JDT LS 即支持 AOSP 全部"。
