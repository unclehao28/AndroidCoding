# 当前交接状态

更新时间：2026-09-18。版本 `0.2.0-p1`（API `p1`）。本轮阶段：**P1（把原型接到真实目录）已完成并可从远程仓库复现**。
上一版 0.1.0-prototype 只有前端示例；P1 增加了真实后端、真实文件读取、受限检索与部署文档。

## 已完成（P1 交付项 1-8）

- **配置**（`server/config.example.json` + `app/config.py`）：源码根、监听、文件大小/行段/检索/并发限额、排除规则；
  未知字段、类型错误、越界、根目录不存在都会启动失败并列出全部问题，`features.write/navigation` 写 true 也拒绝启动。
  `--check-config` 与启动日志会打印 Python 版本、实际检索引擎、是否找到 `rg`、各根目录命中的安卓源码标记。
- **后端**（FastAPI）：`/api/health`、`/api/workspaces`、`/api/config`、`/api/tree`、`/api/file`、
  `/api/search`、`/api/search/cancel`、`/api/navigation`。
- **前端**：右上角「示例数据 / 真实服务器」显式分离（内置数据隔离在 `prototype/demo-data.js`）；真实模式只走 `/api`，
  连不上显示错误，**不回退**示例数据。
- **检索**：默认全库字面量，正则/大小写/全字为显式选项；rg 优先（argv 数组、不拼 shell），缺 rg 时用受限 Python 扫描；
  有并发上限、超时可终止、`requestId` 可取消。
- **路径与内容**：拒绝 `..`/盘符/UNC/NUL；`resolve()` 后必须在根内，越界符号链接 403 并标记；
  UTF-8/BOM/UTF-16/GBK/latin-1 解码，NUL 判二进制，超限文件返回 `too_large`。
- **未就绪状态**：点击标识符真实请求 `/api/navigation` 得到 `unavailable`（不伪造跳转）；编辑/对比在真实模式提示属于 P4；
  引用面板注明"字面匹配不等于引用"。
- **换目录可用**：临时目录（中文路径 + 随机文件名）实测 4/4；pytest 全部基于临时目录而非 `fixtures`。
- **部署文档**：`docs/DEPLOY.md`（内网镜像与完全离线装依赖、ripgrep 安装、AOSP 配置建议、故障排查、验收命令）。

## 在公司服务器上试用（最短路径，不需要再改代码）

```bash
git clone https://github.com/unclehao28/AndroidCoding.git ~/android-source-workbench
cd ~/android-source-workbench/server
python3 -m pip install -r requirements.txt         # 运行时依赖；需要 Python >= 3.10
cp config.example.json config.json                 # 只改 roots，指向真实源码根
python3 -m app --config config.json --check-config # 环境自查：引擎、rg、安卓源码标记
./run.sh                                           # 监听 127.0.0.1:8787
# 公司电脑：ssh -L 8787:127.0.0.1:8787 USER@SERVER，浏览器打开 http://127.0.0.1:8787/
```

试用范围：真实目录浏览、文件/行段读取（含 sha256 版本哈希）、全库检索与取消、路径边界防护。
语义跳转点下去会明确显示未就绪（P2），编辑保存未开放（P4），没有索引（P3）。

两个硬前提：

1. **服务器 Python 必须 >= 3.10**（fastapi 0.141 / starlette 1.6 / uvicorn 0.53 均声明 >=3.10）。
   若只有 3.8/3.9（如 Ubuntu 20.04），需先准备 3.10+ 解释器；把 `python3 --version` 告我，我补一套旧解释器上实测过的依赖版本。
2. **建议装 ripgrep**：缺 `rg` 时用受限 Python 扫描，AOSP 上会因 `pythonMaxFiles` 上限提前截断（界面显示"结果已截断"）。
   `sudo apt-get install -y ripgrep`，或放一个静态二进制到 `PATH`。

## 验证记录（全部为真实执行）

| 命令 | 结果 |
|---|---|
| GitHub 全新 clone 后复核（HEAD 06f75e4） | 52 个文件齐全；`run.sh` 为 100755 且无 CR；`--check-config` 通过；pytest 89 项 0 失败；启动后 HTTP 验收 21/21 |
| `cd server && python -m pytest` | **89 项：86 passed / 3 skipped / 0 failed，4.5s**（skip=符号链接用例，Windows 无创建权限） |
| `python scripts/check-package.py` | 通过：本地资源、3 个 JS 语法、内置示例与 `fixtures/demo-files.json` 一致、12 项导航预期坐标、C++17 语法 |
| 启动 `python -m app`（8787）+ `python scripts/verify-p1-http.py` | **21/21**：真实读文件、全库检索跨 6 种文件类型、正则、截断、路径穿越/盘符拒绝、导航 unavailable |
| `python scripts/verify-p1-browser.py`（本机 Chromium via CDP） | **17/17**：示例→真实切换、真实检索 12 处/6 文件、打开真实文件、点击标识符得 unavailable、真实目录树、无 JS 异常 |
| 换目录一次性脚本（临时目录 + 中文路径 + 随机文件名） | 4/4，随后已删除该脚本 |

实测输出举例：`/api/file` 返回 `sha256:2051c958f0d56…`、`encoding=utf-8`、`eol=lf`、18 行；`/api/search` 12 处/6 文件、
引擎 `python-bounded 1`、`indexed=false`；一次 `query=display` 同时命中 `aidl/bp/cpp/java/rc/xml` 六种后缀。

## 对照 docs/ACCEPTANCE.md 的 P1 九条

九条均已通过：任意源码根（换目录脚本 4/4）、真实模式不读内置 `files`、分层目录 + 行段读取（每次最多 800 行）、
检索覆盖 Java/C/C++/AIDL/XML/Android.bp/rc、默认全库不选岗位、取消后 `activeSearches` 归零、
空结果/超时/截断/编码不支持均有可见状态（P1 无索引因而直接标注"索引：未建立（P3）"）、
越界路径 400 与越界符号链接 403、默认只监听 127.0.0.1 并给出 SSH 转发命令。
例外：符号链接越界用例在 Windows 上被跳过（无法创建符号链接），需在 Linux 上复跑一次 `pytest` 才算覆盖。

## 未完成（禁止对外宣称已实现）

- 语义跳转（变量/参数/成员/全局/常量、声明与定义、引用）——P2；clangd/JDT LS 均未接入。
- 全库索引与增量更新、Repo/manifest 版本一致性、个人修改覆盖层——P3。
- 真实文件写入、保存冲突检测、可靠 diff——P4。
- JNI / AIDL / Binder 关联分析——P5。
- 真实 AOSP 源码上的性能与覆盖率、多用户与权限体系（首版不做企业平台）。

## 已知限制

- `indexed=false`：每次检索都是按目录扫描，不是索引；超大 checkout 必须等 P3（优先评估 Zoekt）。
- 本机没有 `rg`，**rg 引擎未真实执行过**（只做了 argv 构造、超时与取消分支的单元测试）；需在装好 rg 的 Linux 上复测。
  缺 rg 时 Python 引擎在正则回溯或磁盘阻塞时无法被强制中断（超时只置位取消标志）。
- 符号链接越界用例在本机被跳过（Windows 权限），必须在 Linux 复测。
- 前端标识符点击依赖 Chrome/Edge 的 `caretRangeFromPoint`，未在其他浏览器验证。
- 只读且无认证，默认只监听回环地址；不要把非回环地址暴露到公网。
- 本机仍有一个后端进程监听 8787（日志 `%TEMP%\asw-server-*.log`），结束命令：
  `Get-Process python | Where-Object {$_.CommandLine -like '*-m app*'} | Stop-Process`。

## 下一步

**立刻可做（不需要我改代码）**：按上面的最短路径部署，在公司电脑上试用 P1。同时把服务器上
`python3 --version`、`python3 -m pip --version`、`which rg` 三条命令的输出告诉我，用于决定是否补降级依赖。

**然后推进 P2（语义导航，核心价值）**：

1. `python scripts/prepare-navigation-fixtures.py`；在 Linux 上装 clangd，用 `fixtures/navigation-cpp` 跑通
   `textDocument/definition|declaration|references`，逐条对 `navigation-cases.json` 的 12 项预期，再补 `compile_commands`。
2. 把 `/api/navigation` 从固定 `unavailable` 改为真实调度：请求带 workspace、相对路径、文档 hash、行列与导航类型；
   按 workspace/构建目标管理语言服务进程，控制数量与内存，不要每次点击都新起进程。
3. 校验 UTF-16 行列转换（含中文与非 BMP 字符）；同名多结果按作用域/工程身份消歧，无法消歧返回 `ambiguous` 让用户选择。
4. Java 侧单独记录 Soong 导入适配与失败诊断，不得声称"JDT LS 即支持 AOSP 全部"。
