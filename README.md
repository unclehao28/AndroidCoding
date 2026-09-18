# 安卓源码工作台：原型源码与开发交接包

版本：0.2.0-p1，2026-09-18。上一版为 0.1.0-prototype（只有前端示例）。

目标：源码留在 Linux 服务器，Windows 用户通过统一界面浏览、搜索、跳转和编辑整套安卓源码。浏览范围不能按 Framework、HAL、BSP 等岗位预先缩小。

## 先说明当前是什么

现在有两种**互不混淆**的数据来源，页面右上角可切换：

- **示例数据**：`prototype/demo-data.js` 里的 10 个自编文件，全部在前端，用于演示交互，不是 AOSP 或公司源码；
- **真实服务器**：调用 P1 后端，读取 Linux 服务器上配置的源码根目录。连不上就显示错误，**不会**回退到示例数据。

P1 已实现的真实能力：健康状态、工作区列表、按层目录浏览、按行段读取文件（含编码/二进制/大文件判定与 sha256 版本哈希）、受限全文检索（默认字面量，正则为显式选项，有并发/超时/结果上限、可取消）、路径边界防护。

**仍未实现**（界面会明确显示未就绪，不要当成已完成）：语义跳转（P2）、全库索引（P3）、真实文件写入与冲突检测（P4）、JNI/Binder 关联（P5）。示例模式的“定义候选”是手工登记的演示数据；符号出现位置是文本匹配，不是语义引用。

## 查看原型

两种方式：

1. 只用示例模式：用现代 Chrome / Edge 打开 `prototype/index.html`（不依赖 CDN、在线字体或图标库）。
2. 使用真实源码：按下文启动后端，浏览器打开 `http://127.0.0.1:8787/`（后端同时托管前端）。

示例模式的编辑只存在页面会话内，刷新即丢失，不会写磁盘。

## 目录

- `prototype/index.html`：页面入口。
- `prototype/styles.css`：工作台样式。
- `prototype/demo-data.js`：仅示例模式使用的内置演示数据（与 `fixtures/demo-files.json` 一致，由 `scripts/check-package.py` 校验）。
- `prototype/api.js`：后端接口客户端（健康、工作区、目录、文件、检索、取消、导航）。
- `prototype/app.js`：界面与两种数据来源的切换逻辑；真实模式只走 `/api`，不回退示例数据。
- `server/config.example.json`：后端配置示例（允许的源码根、监听地址、限额、排除规则）。
- `server/app/`：FastAPI 后端（`config.py` 配置校验、`pathtools.py` 路径与编码、`search.py` 检索引擎、`gitremote.py` 远程仓库同步、`main.py` 接口）。
- `server/config.example.json`：带注释的配置示例（支持 `//` 与 `/* */` 注释）。
- `server/tests/`：pytest 测试（配置、路径边界、接口、检索限制与取消、真实扫描取消）。
- `server/requirements.txt`：锁定版本的后端运行时依赖（Python >= 3.10）。
- `server/requirements-py38.txt`：Python 3.8/3.9 服务器的运行时依赖（含 cp38 的编译扩展版本）。
- `server/requirements-dev.txt`、`server/requirements-py38-dev.txt`：测试依赖（pytest、httpx）。
- `server/run.sh`：Linux 启动脚本。
- `docs/DEPLOY.md`：公司服务器上的部署、试用范围、AOSP 配置与故障排查。
- `scripts/check-package.py`：包资源与内置数据一致性检查。
- `scripts/verify-p1-http.py`：对运行中的后端做真实 HTTP 验收。
- `scripts/verify-p1-browser.py`：用本机 Chromium 做前端交互验收（可选）。
- `reference/conversation-fragment.html`：对话中原型的原始快照，仅用于比对；不要与 `prototype` 同时维护两套实现。
- `fixtures/demo-tree/`：10 个示例文件的目录化副本，用来验证真实文件读取和检索；它们是阅读演示，不是可编译的安卓工程。
- `fixtures/demo-files.json`：相同演示数据的清单。
- `fixtures/navigation-cpp/`：可做 C++17 语法检查的微型工程，覆盖局部变量、参数、成员、全局变量和同名遮蔽。
- `fixtures/navigation-java/`：Java 作用域样例，不代表 AOSP 工程导入已完成。
- `fixtures/navigation-cases.json`：精确跳转预期；这些不是已通过的语言服务测试结果。
- `scripts/prepare-navigation-fixtures.py`：生成跳转预期和适合当前机器的 `compile_commands.json`。
- `docs/ACCEPTANCE.md`：各阶段的验收要求。
- `HANDOFF_STATUS.md`：当前事实、验证范围、下一步。
- `DEEPSEEK_TASK_PROMPT.md`：可直接交给开发 Agent 的任务提示词。

## 运行 P1 后端

### Linux 服务器

目录提示：`config.example.json`、`python3 -m app`、`run.sh` 都在 `server/` 下。
在仓库根目录可以直接 `./run.sh`（自动转发到 `server/run.sh`）；要用 `python3 -m app` 必须先 `cd server`。

```bash
cd server
python3 -m pip install -r requirements.txt           # Python >= 3.10
python3 -m pip install --user -r requirements-py38.txt   # Python 3.8/3.9（如 Ubuntu 20.04 自带的 3.8.10）
cp config.example.json config.json             # 只改 roots：允许读取的源码根目录
python3 -m app --config config.json --check-config   # 配置与环境自查，失败会列出全部问题
./run.sh                                       # 自动按解释器版本选择依赖清单后启动
```

服务默认监听 `127.0.0.1:8787`。**不要**把源码根目录写成 `/` 或 `$HOME`：`roots` 只列出确实需要的工作区，服务不会遍历整台服务器。

在真实安卓源码上部署（含内网镜像/完全离线装依赖、ripgrep 安装、AOSP 配置建议、故障排查）见
[`docs/DEPLOY.md`](docs/DEPLOY.md)。测试依赖是单独的 `server/requirements-dev.txt`（Python 3.8 用 `requirements-py38-dev.txt`）。

### 远程 / 内网仓库作为只读工作区

在 `roots[]` 里给远程仓库加 `git` 字段（`url` / `ref` / `depth` / `sparsePaths`），工作台负责 clone、fetch，
页面提供「同步远程仓库」按钮：

```bash
python3 -m app --config config.json --git-status            # 查看本地/远程工作区状态
python3 -m app --config config.json --sync [workspace-id]   # clone 或 fetch 更新
```

只做 `clone` / `fetch` / `checkout --detach` / `merge --ff-only`；不会 `reset --hard`、不会自动提交或推送，
缓存里有本地修改时拒绝更新。认证只用服务器上既有的 git/SSH 凭据（含 `url.insteadOf` 改写），工作台不保存凭据。
公开仓实测：清华 TUNA 的 AOSP 镜像与 GitHub `aosp-mirror` 可达，`android.googlesource.com` 常连不上。

### Windows 客户端

源码不需要复制到 Windows。复用已有的 SSH 连接做端口转发：

```bash
ssh -L 8787:127.0.0.1:8787 USER@SERVER
```

然后在 Windows 浏览器打开 `http://127.0.0.1:8787/`（后端同时托管前端，同源访问，无需 CORS 配置）。

- 端口选 8787 而不是 8765：8765 常被本机代理工具（如 Clash）占用，会导致本地转发绑定失败。可用 `netstat -ano | findstr 8787` 先确认空闲。
- 也可以直接双击 `prototype/index.html` 使用示例模式，然后在右上角「真实服务器」里填写转发后的地址。

### 配置要点

| 配置项 | 作用 | 默认 |
|---|---|---|
| `roots[]` | 允许访问的源码根目录（`id`/`name`/`path`/`readonly`），路径可为相对配置文件 | 示例指向 `fixtures` |
| `server.host/port` | 监听地址与端口，非回环地址会给出警告 | 127.0.0.1:8787 |
| `limits.maxFileBytes` / `maxLineCount` | 单文件读取上限与每次返回的最大行数 | 2 MiB / 2000 |
| `limits.maxSearchResults` / `searchTimeoutSeconds` / `maxConcurrentSearches` | 检索结果上限、单次超时、并发上限 | 500 / 10s / 2 |
| `search.engine` | `auto`（有 rg 用 rg）、`ripgrep`、`python` | auto |
| `search.excludeGlobs` | 默认排除 `.git`、`.repo`、`out`、二进制产物等 | 见示例 |
| `features.write` / `features.navigation` | P1 必须保持 false，写成 true 会启动失败 | false |

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 状态、版本、实际检索引擎、并发设置、未实现能力标记 |
| GET | `/api/workspaces` | 配置的工作区（含服务器绝对路径与是否存在） |
| GET | `/api/tree?workspace=&path=` | 单层目录列表；根目录外的符号链接标记为已阻止 |
| GET | `/api/file?workspace=&path=&startLine=&lineCount=` | 按行段读取，返回 sha256、编码、行尾、总行数 |
| POST | `/api/search` | `query`/`workspace`/`scope`/`regex`/`caseSensitive`/`wholeWord`/`includeGlob`/`limit`/`requestId` |
| POST | `/api/search/cancel` | 按 `requestId` 终止正在运行的检索进程 |
| POST | `/api/navigation` | P1 固定返回 `status=unavailable`、`kind=semantic`、`targets=[]`，不伪造跳转 |

约定：目录/文件接口的行号从 0 开始；检索结果的 `column` 是行内 Unicode 码点偏移（文本匹配），P2 的导航结果才使用 LSP 的 UTF-16 坐标。`/api/search` 的回答里 `indexed=false`，表示没有索引、只是受限扫描。

### 检索的已知限制

- 优先使用 `rg`（参数以数组传给进程，不拼接 shell 字符串，默认字面量、限制文件大小与结果条数、超时可终止）。缺少 `rg` 时退回受限的 Python 扫描：有文件数上限，正则表达式或磁盘阻塞时可能无法立即中断。
- P1 **没有**全库索引，每次检索都是按目录扫描；超大 checkout 的索引属于 P3（优先评估 Zoekt）。
- 仅读取，不写入；语义跳转、索引、写入均按阶段推进。

## 交给 DeepSeek 的方式

1. 在能读写本地文件、执行终端命令的开发 Agent/IDE 中打开解压目录，并选择你准备使用的 DeepSeek 模型。
2. 把 `DEEPSEEK_TASK_PROMPT.md` 内容发送给 Agent。默认先执行 P1，不要求一次做完整个产品。
3. 每一阶段完成后，Agent 更新 `HANDOFF_STATUS.md`，保留实际启动命令、检查结果、已知问题及下一步。
4. 换模型或换会话时，重新打开同一目录，让 Agent 先读交接文件即可。不要重复粘贴整段历史聊天。

如果使用的只是不能操作文件和终端的网页聊天，需要自行应用它返回的代码并运行；仅选择模型名称不会自动获得服务器访问能力。

## 变量跳转应该怎么实现

对“变量”至少要区分局部变量、函数参数、成员字段、全局变量、常量，并正确处理同名遮蔽、声明与定义。需要用“文件 + 位置 + 版本 + 工程配置”解析，不能全库搜索同名变量后直接跳第一项。

C/C++ 优先复用 clangd；Java 可用 JDT LS，但 AOSP 的 Soong 依赖、生成源码、系统内部类路径需要专门适配。JDT LS 不是 Kotlin 语言服务器。其他语言的精准跳转应有单独的支持状态，不能靠一个全局“索引完成”标志冒充全语言完成。

## 生成导航验收数据

```bash
python scripts/prepare-navigation-fixtures.py
```

生成的 `fixtures/navigation-cpp/compile_commands.json` 含本机绝对路径，不应跨机器直接复用；换到 Linux 服务器后重新运行。最终压缩包未包含生成时的机器路径。

`navigation-cases.json` 使用从 0 开始的行号和 UTF-16 字符位置，与常见 LSP 协议对应。实际语言服务结果允许返回完整标识符范围，验收时检查预期标识符是否落在目标范围内。

## 验证边界

真实执行过的命令与结果记录在 `HANDOFF_STATUS.md`，包括：pytest 测试、`scripts/check-package.py`、对运行中后端的 `scripts/verify-p1-http.py`、以及用本机 Chromium 做的 `scripts/verify-p1-browser.py` 交互验收。

未验证的部分要如实区分：**微型工程通过 ≠ 安卓全库可用**。当前环境没有真实 AOSP 源码，也没有安装 `rg`、clangd、JDT LS；服务器端的检索性能、索引、Java/C++ 语义导航都还没有在真实 checkout 上测过。不要把示例数据的检索耗时、语法检查或界面截图当作全量安卓验证。

本包不包含任何真实公司源码、凭证、服务器地址或 AOSP 全量代码。

## 项目复杂度

| 范围 | 判断 |
|---|---|
| 整理现有 UI、接入真实目录和文件读取 | 较容易控制 |
| 真实搜索、版本检查、远程文件编辑 | 中等 |
| 全量安卓索引、个人修改覆盖、资源调度 | 中高 |
| C/C++ 与 Java 变量语义跳转及 Soong 适配 | 高 |
| 自动贯通 JNI、Binder、反射及运行时分派 | 很高，暂不列入第一版承诺 |

先用小项目验证真实语义能力，再接入整套源码做性能和覆盖率测量；这不等于限制最终用户只能浏览一个模块。

## 技术参考

- [Zoekt](https://github.com/sourcegraph/zoekt)：全文和符号检索组件。
- [clangd features](https://clangd.llvm.org/features)：C/C++ 定义、声明、引用与变量信息。
- [clangd remote index](https://clangd.llvm.org/design/remote-index)：共享索引的条件和局限。
- [JDT LS](https://github.com/eclipse-jdtls/eclipse.jdt.ls)：Java 语言服务。
- [Soong compile database](https://android.googlesource.com/platform/build/soong/+/HEAD/docs/compdb.md)：C/C++ 编译参数导出。
