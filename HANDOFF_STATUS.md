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
"navigation": {
  "enabled": true, "clangdPath": null, "searchDirs": ["/path/to/aosp"],
  "backgroundIndex": false, "maxInstances": 2, "idleShutdownSeconds": 300,
  "requestTimeoutSeconds": 20, "maxResults": 50, "pchStorage": "disk"
}
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
| `cd server && python -m pytest` | **181 项：178 passed / 3 skipped / 0 failed，26s**（skip=Windows 无法建符号链接） |
| 环境准备脚本（`test_navsetup.py`，16 项） | AOSP 判定（`.repo` 单独命中 / 单个通用标记不算 / 跳过 node_modules / 深度上限）、clangd 探测与来源、按行改写保留注释、备份、语法损坏才重建、**路径不存在时绝不覆盖配置** |
| 镜像环境端到端（临时目录模拟"服务器仓库 + 一份 AOSP"） | 自动生成 config.json → 扫 2974 个目录找到 AOSP → 找到 `prebuilts/clang/host/linux-x86/clang-*/bin/clangd` → 按行写入 `searchDirs`（注释保留、生成备份）→ 输出验收命令 |
| LSP 客户端测试（独立进程 mock LSP） | 18 项：分帧、握手、服务端请求应答、通知、单请求超时不影响进程、进程崩溃带出 stderr、多目标 |
| 导航服务测试（真协议 + 构造结果） | 18 项：resolved/ambiguous/references 不判歧义/空结果/stale/位置越界/Java 未接入/关闭开关/实例复用/中文路径/emoji 列号/工作区外目标标记 |
| 启动服务 + `scripts/verify-p1-http.py` | **26/26**（含 Java 明确未接入、坐标基准、能力矩阵如实上报） |
| `scripts/verify-p1-browser.py`（本机 Chromium） | **24/24**：真实点击标识符 → 真实 `/api/navigation` → 显示"未就绪 + 真实原因"，无 JS 异常 |
| `scripts/verify-p2-navigation.py --direct --workspace fixtures`（本机） | 退出码 **2**：本机没有 clangd，脚本如实报告"clangd 可用 = FAIL"，**没有**把跑不了算成通过 |
| 该脚本同时抓到一个回归并已修 | 为远程仓库重构时，本地 `roots[].path` 的相对路径变成按进程 cwd 解析（从别的目录运行就会报"路径不存在"）；已恢复按配置文件目录解析并加回归测试 |

## 未完成（禁止对外宣称已实现）

- **真机 clangd 的 12 条预期尚未跑过**（本机没有 clangd；LLVM Windows 包 467–860MB，没有下载）。
  需要你在服务器上执行：`python3 scripts/verify-p2-navigation.py --direct --workspace fixtures`。
- Java（JDT LS + Soong/classpath 适配）、Kotlin、Rust、AIDL 的语义能力：未接入（`/api/health` 的
  `navigation.notImplemented` 会如实列出）。
- AOSP 真实工程的 `compile_commands.json`、`--query-driver` 等工具链适配未做。
- 全库索引（P3）、写入与提交（P4）、JNI/Binder 关联（P5）均未开始。

## 已知限制

- 默认不开 `backgroundIndex`：跨编译单元的定义跳转可能为空（头文件内的声明与同 TU 内没问题）；
  开启后资源开销很大，整套 AOSP 请谨慎。共享/远程索引属于后续工作。
- clangd 首次请求需要加载编译参数，可能接近 `requestTimeoutSeconds`；超时只影响本次请求，不杀进程。
- `position` 的单位是 UTF-16 列；检索结果的 `column` 是 code point（两者都已在文档与响应里标注）。
- 实例上限默认 2、空闲 300 秒关闭；`/api/health` 的 `navigation.manager.instances` 可看到当前进程与请求数。
- 只有只读能力、无认证，默认只监听回环地址。

## 下一步

1. **你**：在服务器上执行两条命令，把输出发我：

   ```bash
   cd ~/android-source-workbench && git pull
   python3 scripts/setup-navigation.py --acceptance     # 找 AOSP/clangd、写配置、跑 12 条预期
   ```

   如果它说"还差 clangd"，把当时的输出发我（脚本会打印替代方案）；如果这台机器没有源码，
   用 `--aosp 你的源码路径` 或 `--clangd clangd路径` 再跑一次。
   然后在页面上点几个真实 `frameworks/base` 的 C++ 符号，告诉我哪些跳得准、哪些为空。
2. **我**：按你的反馈修 P2 的解析问题（例如加 `--query-driver`、compdb 路径探测、超时调整），
   然后做 Java 半边（JDT LS + Soong 导入适配，会单独记录支持矩阵与失败诊断）。
3. 之后进入 P3（Zoekt 索引），需要你给的源码规模数据（文件数、数据量、机器配置、是否 repo 管理）。
