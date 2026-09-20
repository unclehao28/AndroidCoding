# 当前交接状态

更新时间：2026-09-20。版本 `0.4.0-p2b`。当前阶段：**P2 完成（C/C++ 走 clangd、Java 走 JDT LS）**；
Kotlin/Rust/AIDL 未接入、AOSP Java 的 Soong 导入适配未做。

## 本轮完成：P2 下半 —— Java 语义跳转（JDT LS）

- `manager.py`：JDT LS 发现（配置 → `JDTLS_HOME` → 常见目录）、JDK 发现（配置 → `JAVA_HOME` → `PATH` →
  各 root 下 `prebuilts/jdk/*/linux-x86/bin/java` → `/usr/lib/jvm/*`）、**从 JDT LS 自带启动脚本读出它要求的
  JDK 主版本**（最新快照 21 / 1.31.0 要 17 / 1.12.0 要 11），并按官方 `bin/jdtls` 的完整参数启动。
- `navigation.py`：`.java` 路由到 JDT LS；`supported.java` 带 `available`/`javaVersion`/`requiredJava`/`reason`/
  `classpathSupport=false`，缺 JDK 或版本不足时**明确说"JDK 版本不足"**而不是含糊失败。
- `scripts/setup-java.py`：体检 / `--install`（按 JDK 版本自动下载匹配的 JDT LS，官方源）/ `--ls-path` `--java-home`。
  配置新增 `navigation.javaLsPath / javaHome / javaDataDir / javaArgs`；旧配置缺这些键时**按行插入**，不整体重写。

### 上一轮：P2 上半 —— C/C++（clangd）

原来 `/api/navigation` 固定返回 `unavailable`，现在接入了真实语言服务：

- `server/app/lsp/`：`protocol.py` stdio 分帧与 JSON-RPC、`positions.py` UTF-16↔码点（中文/emoji 有专测）、
  `client.py` 长驻子进程客户端（超时、进程死亡、**必须应答服务端请求**，否则对方一直等）、
  `manager.py` 按工作区+语言管理实例（懒启动、复用、上限、空闲关闭、文档版本）。
- `server/app/navigation.py`：请求带 workspace、相对路径、文档 hash、行列、导航类型；按扩展名分发语言。
  `POST /api/navigation` 返回 `status`（resolved/ambiguous/unavailable/stale）、`targets[]`（路径+range+evidence）、
  `reason`、`hints[]`、版本字段、`server`、`positionUnit=utf-16`、`elapsedMs`。
- 前端：点击标识符发真实请求；可跳转、`ambiguous` 列候选让用户选、`stale` 提示重新读取、
  不可用显示真实原因。诚实性约束写死在代码里：位置只来自语言服务，**不**用同名符号或文本匹配冒充跳转。

### 配置（`server/config.example.json` 已有注释）

```json
"features": { "write": false, "navigation": true },
"navigation": { "enabled": true, "clangdPath": null, "searchDirs": ["/path/to/aosp"], "javaLsPath": null,
  "javaHome": null, "backgroundIndex": false, "maxInstances": 2, "requestTimeoutSeconds": 20, "maxResults": 50 }
```

clangd 查找顺序：`clangdPath` → `PATH` → `searchDirs` 下的 AOSP `prebuilts/clang/host/linux-x86/*/bin/clangd`
（**AOSP 自带，不需要装**）。可选 `roots[].compileCommandsDir` 指向 Soong 生成的 compdb。

服务器上不要手工编辑 JSON（占位符被原样粘贴、`vi` 里粘进 shell 命令都踩过），一条命令搞定：

```bash
python3 scripts/setup-navigation.py               # 找 AOSP → 找 clangd → 按行写进 config.json（保留注释、先备份）
python3 scripts/setup-navigation.py --add-root /data/home/yangyang/aosp12   # 登记现成源码树为只读工作区
python3 scripts/setup-java.py                     # Java 侧体检；--install 按 JDK 版本装匹配的 JDT LS
```

探测有深度/条目上限，不全盘扫描；找不到就如实报告并给替代方案，不写假路径；只有配置**语法**坏了才重建
（路径不存在这类问题只报告，不覆盖用户填好的 roots）。

## 验证记录（真实执行）

| 命令 | 结果 |
|---|---|
| `cd server && python -m pytest` | **214 项：211 passed / 3 skipped / 0 failed**（skip=Windows 无法建符号链接） |
| **真实 JDT LS**（1.31.0 + JDK 17）跑 5 条 Java 用例 | **5/5 PASS**：参数、字段声明、字段读取、两个同名局部变量（遮蔽）全部 `resolved`、各 1 个目标、位置与预期一致；首请求即成功，总耗时 11s |
| `scripts/setup-java.py`（本机实测） | 体检正确报出 `JDK 17` 与"未找到 JDT LS"；`--install --target <已有目录>` 走复用路径：校验"该版本要求 Java 17 = 本机 17"→ 按行写入 `javaLsPath`/`javaHome`（注释保留）+ 备份；`--config` 指向的配置若非法则**拒绝写入** |
| Java 版本兼容规则（`test_java_language_server.py`，14 项） | 从 `bin/jdtls.py` / `bin/jdtls`（shell）/ `requires at least Java` 三种写法读要求；`pick_release`：21+→最新快照、17–20→1.31.0、11–16→1.12.0、8→拒绝；release 表必须按 JDK 要求降序；所有下载地址是官方 https |
| 环境准备脚本（`test_navsetup.py`，26 项） | AOSP 判定（`.repo` / `prebuilts/clang` 单独命中即确定、单个通用标记不算、跳过 node_modules、深度上限）、clangd 探测与来源、按行改写保留注释、**语法损坏时备份并重建后继续跑完探测**、**路径不存在时绝不覆盖配置**、两次备份不互相覆盖、候选目录诊断、`--add-root` 插入位置/id 去重/重复路径幂等/校验不过则不写、**给了源码根就不再扫 /data /home** |
| 环境准备端到端（每次都在临时目录模拟"服务器仓库 + 一份 AOSP"） | `--add-root`：登记只读根 → 只扫 1 个目录 → 找到自带 clangd → 按行写入（注释保留、备份留住）。空配置：一次运行内备份+重建+探测+写入。无源码时打印候选与 `find` 命令、不硬跑验收 |
| LSP/导航测试（独立进程 mock LSP） | 分帧、握手、服务端请求应答、超时不影响进程、进程崩溃带 stderr；resolved/ambiguous/空结果/stale/位置越界/实例复用/中文路径/emoji 列号/工作区外目标标记 |
| 启动服务 + `verify-p1-http.py` / `verify-p1-browser.py` | **26/26** 与 **24/24**（含坐标基准、能力矩阵如实上报、点击标识符显示真实原因、无 JS 异常） |
| **真机 clangd 12.0.7**（公司服务器 + AOSP12 prebuilts） | 修复前 **5/12**：失败的 2 条正好是 main.cpp / state.cpp 各自的**第一次请求**，原因 `-32602 trying to get AST for non-added document`（刚 didOpen、AST 未就绪）→ 已修（就绪屏障 + 退避重试）→ **修复后 C++ 7/7 全过**，Java 当时报"未接入" |
| `scripts/find-source-server.sh`（伪造 AOSP + manifest） | 三类证据全部提取成功，两个 shell 脚本 `bash -n` 通过 |

## 未完成（禁止对外宣称已实现）

- **Java 的真机状态未验证**：本机（Windows + JDK 17 + JDT LS 1.31.0）5/5 通过，但公司服务器上
  还没有可用的 JDK/JDT LS（AOSP 自带 `prebuilts/jdk/jdk11` 只够配 1.12.0）。需要在服务器上跑
  `python3 scripts/setup-java.py` 才能确定走哪条路。
- **AOSP Java 的跨模块解析**：Soong 不是 Maven/Gradle，JDT LS 无法导入工程；同文件/同目录可用，
  跨模块依赖为空（接口如实返回 `unavailable`，`classpathSupport=false`）。导入适配未开始。
- Kotlin / Rust / AIDL 未接入；AOSP 的 `compile_commands.json`、`--query-driver` 未做；
  全库索引（P3）、写入（P4）、JNI/Binder 关联（P5）未开始。

## 已知限制

- 默认不开 `backgroundIndex`：跨编译单元的定义跳转可能为空（头文件内与同 TU 没问题）；整套 AOSP 打开很吃资源。
- clangd 首次请求要加载编译参数，可能接近 `requestTimeoutSeconds`；超时只影响本次请求，不杀进程。
- `position` 是 UTF-16 列，检索结果的 `column` 是 code point；实例上限 2、空闲 300 秒关闭。
- 只有只读能力、无认证，默认只监听回环地址。

## 公司侧环境现状（2026-09-18 实测）

- `test-car-znh-compile`：Ubuntu + Python 3.8.10、**无 pip、无 rg**；**本机就有十几份完整 AOSP12 源码树**
  （`/home/*/aosp12`、`/data/home/*/aosp12`、`/home/jenkins/jobs/droid-12`；`/home` 与 `/data/home` 是同一批目录）。
  已用 `--add-root /data/home/yangyang/aosp12` 登记为只读工作区（也是 OpenGrok 索引的那份），**不需要 clone**。
  读的是同事的 home，工作台只读、clangd 用 `--background-index=0` 不往源码树写东西；长期读请与团队确认。
- 内网 OpenGrok 1.14.13 在 `http://172.20.36.99:8081/source/`（索引 AOSP12）：浏览/检索服务，**不能 clone**，
  API 返回 401 需凭据，暂不作为数据源。
- git/repo 服务器地址仍待确认：`bash scripts/find-source-server.sh` 会读 `.manifest.xml` 的 `fetch=` 等证据。

## 下一步

1. **你**：在服务器上准备 Java 环境并验收（C++ 已是 7/7，不需要重跑）：

   ```bash
   cd ~/android-source-workbench && git pull
   python3 scripts/setup-java.py                      # 先看有没有可用 JDK/JDT LS
   python3 scripts/setup-java.py --install            # 按 JDK 版本自动装匹配的 JDT LS
   python3 scripts/verify-p2-navigation.py --direct --workspace fixtures   # 期望 12/12
   ```

   若提示"未找到可用的 JDK"或"JDK 版本不足"，把这步输出发我。
2. **我**：按反馈修 P2 解析问题（`--query-driver`、compdb 探测、超时调整），再做 AOSP Java 的
   Soong 导入适配（跨模块解析，会单独记录支持矩阵与失败诊断）。
3. 之后进入 P3（Zoekt 索引），需要源码规模数据（文件数、数据量、机器配置、是否 repo 管理）。
