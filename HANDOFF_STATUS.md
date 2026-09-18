# 当前交接状态

更新时间：2026-09-18。版本 `0.3.0-p2a`。当前阶段：**P2 上半（C/C++ 语义跳转）已完成**，Java 等语言明确未接入。

## 本轮完成：P2 上半 —— 真实语义跳转（clangd）

原来 `/api/navigation` 固定返回 `unavailable`，现在接入了真实语言服务：

- `server/app/lsp/`：LSP 协议层
  - `protocol.py`：stdio 上的 `Content-Length` 分帧 + JSON-RPC 编解码（容忍额外头部、增量读到半个消息）；
  - `positions.py`：UTF-16 列号 ↔ Python 字符索引（中文是 BMP、emoji 占 2 个单元，易错点有专门测试）；
  - `client.py`：长驻子进程客户端——请求/通知/超时/进程死亡检测，**必须应答服务端请求**
    （clangd 会发 `workspace/configuration`，不应答就会卡住）；
  - `manager.py`：按工作区+语言管理实例（懒启动、复用、实例数上限、空闲自动关闭、文档版本管理）。
- `server/app/navigation.py`：语义跳转服务。请求带 workspace、相对路径、文档 hash、行列、导航类型；
  按扩展名分发语言；C/C++ 走 clangd，其他语言按支持矩阵返回未就绪并说明原因。
- `POST /api/navigation` 返回值：`status`（resolved/ambiguous/unavailable/stale）、`kind=semantic`、
  `targets[]`（路径 + range + evidence）、`reason`、`hints[]`、`sourceVersion`/`currentVersion`、
  `symbol`/`symbolRange`、`server`、`positionUnit=utf-16`、`lineBase=0`、`elapsedMs`。
- 前端：点击标识符发起真实请求；针对目标可直接跳转；`ambiguous` 时列成候选让用户选；
  `stale` 时给"重新读取后重试"；不可用时显示真实原因与配置提示。面板顶部显示语言服务就绪状态。
- 诚实性约束（写死在代码里）：目标位置只来自语言服务；空结果/缺编译参数/语言未接入/版本不一致
  一律 `unavailable`/`stale` 并给出原因；**不**用同名符号或文本匹配冒充跳转；不做置信度百分比。

### 配置（`server/config.example.json` 已有注释）

```json
"features": { "write": false, "navigation": true },
"navigation": { "enabled": true, "clangdPath": null, "searchDirs": ["/path/to/aosp"], "backgroundIndex": false,
  "maxInstances": 2, "idleShutdownSeconds": 300, "requestTimeoutSeconds": 20, "maxResults": 50 }
```

clangd 查找顺序：`clangdPath` → `PATH` → `searchDirs` 下的 AOSP `prebuilts/clang/host/linux-x86/*/bin/clangd`
（**AOSP 自带，不需要装**）。可选 `roots[].compileCommandsDir` 指向 Soong 生成的 compdb。

服务器上不要手工编辑 JSON（占位符被原样粘贴、`vi` 里粘进 shell 命令都踩过），一条命令搞定：

```bash
python3 scripts/setup-navigation.py               # 找 AOSP → 找 clangd → 按行写进 config.json（保留注释、先备份）
python3 scripts/setup-navigation.py --acceptance  # 顺带跑 12 条跳转预期
python3 scripts/setup-navigation.py --print-only  # 只探测不改文件
```

探测有深度/条目上限，不全盘扫描；找不到就如实报告并给替代方案，不写假路径；只有配置**语法**坏了才重建
（路径不存在这类问题只报告，不覆盖用户填好的 roots）。

## 验证记录（真实执行）

| 命令 | 结果 |
|---|---|
| `cd server && python -m pytest` | **193 项：190 passed / 3 skipped / 0 failed**（skip=Windows 无法建符号链接） |
| 环境准备脚本（`test_navsetup.py`，26 项） | AOSP 判定（`.repo` / `prebuilts/clang` 单独命中即确定、单个通用标记不算、跳过 node_modules、深度上限）、clangd 探测与来源、按行改写保留注释、**语法损坏时备份并重建后继续跑完探测**、**路径不存在时绝不覆盖配置**、两次备份不互相覆盖、候选目录诊断、`--add-root` 插入位置/id 去重/重复路径幂等/校验不过则不写、**给了源码根就不再扫 /data /home** |
| `--add-root` 端到端（镜像 + 伪造 AOSP12） | 登记只读根 → 只扫 1 个目录（不再全盘扫）→ 找到自带 clangd → 写 `searchDirs`（注释保留、两次备份都留住）→ 退出码 0 |
| 镜像环境端到端（临时目录模拟"服务器仓库 + 一份 AOSP"） | 空配置文件 → 一次运行内完成：备份 + 重建 + 扫 2974 个目录找到 AOSP + 找到自带 clangd + 按行写入 `searchDirs`（注释保留）→ 输出验收命令；无源码时打印候选与 `find` 命令、且不硬跑验收 |
| LSP/导航测试（独立进程 mock LSP） | 36 项：分帧、握手、服务端请求应答、单请求超时不影响进程、进程崩溃带 stderr；resolved/ambiguous/references 不判歧义/空结果/stale/位置越界/Java 未接入/实例复用/中文路径/emoji 列号/工作区外目标标记 |
| 启动服务 + `scripts/verify-p1-http.py` | **26/26**（含 Java 明确未接入、坐标基准、能力矩阵如实上报） |
| `scripts/verify-p1-browser.py`（本机 Chromium） | **24/24**：真实点击标识符 → 真实 `/api/navigation` → 显示"未就绪 + 真实原因"，无 JS 异常 |
| **真机 clangd 12.0.7**（公司服务器 + AOSP12 prebuilts）跑 12 条预期 | **5/12**：C++ 7 条中 5 过、2 败；Java 5 条按预期报"未接入"。2 个失败集中在 **main.cpp / state.cpp 各自开头的第一次请求**，原因 `-32602 trying to get AST for non-added document`（刚 didOpen、AST 未就绪）→ 已修：**首次打开后先发 documentSymbol 做就绪确认 + 导航请求对这类暂态错误退避重试**（mock LSP 覆盖了冷启动两种情形） |
| `scripts/verify-p2-navigation.py --direct`（本机无 clangd时） | 退出码 **2**：脚本如实报告 FAIL，**没有**把跑不了算成通过 |
| `scripts/find-source-server.sh`（伪造 AOSP + manifest） | 三类证据全部提取成功，两个 shell 脚本 `bash -n` 通过 |

## 未完成（禁止对外宣称已实现）

- 真机 clangd 只跑过一轮（5/12，修复已推送但**尚未重跑**）；C++ 的其它六条里已过 5 条，
  修完预期 7/7，需实测确认。命令：`python3 scripts/verify-p2-navigation.py --direct`。
- Java（JDT LS + Soong/classpath）、Kotlin、Rust、AIDL 未接入；AOSP 的 `compile_commands.json`、
  `--query-driver` 等工具链适配未做；全库索引（P3）、写入（P4）、JNI/Binder 关联（P5）未开始。

## 已知限制

- 默认不开 `backgroundIndex`：跨编译单元的定义跳转可能为空（头文件内与同 TU 没问题）；整套 AOSP 打开很吃资源。
- clangd 首次请求要加载编译参数，可能接近 `requestTimeoutSeconds`；超时只影响本次请求，不杀进程。
- `position` 是 UTF-16 列，检索结果的 `column` 是 code point；实例上限 2、空闲 300 秒关闭。
- 只有只读能力、无认证，默认只监听回环地址。

## 公司侧环境现状（2026-09-18 实测）

- `test-car-znh-compile`：Ubuntu + Python 3.8.10、**无 pip、无 rg**；但**本机就有十几份完整 AOSP12 源码树**
  在同事的 home 下（`/home/*/aosp12`、`/data/home/*/aosp12`、`/home/jenkins/jobs/droid-12`……，
  `/home` 与 `/data/home` 指向同一批目录）。OpenGrok 索引的正是 `/data/home/yangyang/aosp12`。
  用 `python3 scripts/setup-navigation.py --add-root /data/home/yangyang/aosp12` 直接登记为只读工作区即可，
  **不需要 clone**。（读的是同事的目录，已确认可遍历；工作台只读、clangd 用 `--background-index=0`，
  不会往源码树里写东西。要不要长期读别人的 home，请自行和团队确认。）
- 内网 OpenGrok 1.14.13 在 `http://172.20.36.99:8081/source/`（索引 AOSP12）；OpenGrok 是浏览/检索服务，
  **不能 `git clone`**，其 API 返回 401（需要凭据），暂不作为数据源。
- git/repo 服务器地址仍待确认：`bash scripts/find-source-server.sh` 会读 `.repo/manifest.xml` 的 `fetch=` 等证据。

## 下一步

1. **你**：重跑一次验收（配置已就绪，不需要再改任何东西）：

   ```bash
   cd ~/android-source-workbench && git pull
   python3 scripts/verify-p2-navigation.py --direct --workspace fixtures
   ```

   期望 C++ 7 条全过；Java 5 条仍是"未接入"（P2 后半）。把输出发我。
2. **我**：按反馈修 P2 解析问题（`--query-driver`、compdb 探测、超时调整），再做 Java 半边
   （JDT LS + Soong 导入适配，单独记录支持矩阵与失败诊断）。
3. 之后进入 P3（Zoekt 索引），需要源码规模数据（文件数、数据量、机器配置、是否 repo 管理）。
