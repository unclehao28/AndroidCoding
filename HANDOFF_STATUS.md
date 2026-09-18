# 当前交接状态

更新时间：2026-09-18。版本 `0.2.0-p1`。当前阶段：**P1 + 远程/内网仓库只读工作区**（本轮新增），可从远程仓库复现。

## 本轮新增：远程 / 内网 Git 仓库作为只读工作区

背景：公司服务器账号**没有 root**（`sudo` 不可用），且希望直接看内网仓与公开 AOSP，不想手工 clone。

- 配置：`roots[].git = {url, ref, depth, sparsePaths}` + 顶层 `cacheDir`；`path` 是 cacheDir 内的检出目录。
  未同步的远程根目录不再报普通 404，而是 `409 workspace_not_synced`（带操作提示）。
  配置支持 `//` 与 `/* */` 注释（字符串里的 `https://` 不受影响，报错行列号仍准确）。
- 同步：`python3 -m app --sync [id]`（只依赖标准库 + git，装依赖之前就能用）、`--git-status`；
  页面也有「同步远程仓库 / 取消同步」按钮并轮询 `/api/workspace/sync` 显示进度。
- 安全边界（代码写死）：只执行 `clone` / `fetch` / `checkout --detach` / `merge --ff-only`；
  **不** `reset --hard`、`clean`、`rebase`，**不**自动提交或推送；缓存有本地修改时拒绝更新；
  半成品（只有 `.git` 没有 HEAD）下次同步先清除再重来；地址经白名单校验并以 argv 数组传给 git；
  认证只用服务器既有凭据（`~/.ssh`、credential helper、`url.insteadOf`），SSH 用 `BatchMode` 快速失败，不卡交互输入。
- 公开仓实测（2026-09-18，中国大陆网络）：清华 TUNA `mirrors.tuna.tsinghua.edu.cn/git/AOSP/...` 可 `ls-remote`（clone 会排队）；
  GitHub `aosp-mirror` 可 `ls-remote`；`android.googlesource.com` 连接超时（21s），不要依赖它。

## 无 root 部署路径（服务器：Ubuntu + Python 3.8.10，无 pip、无 rg）

```bash
cd ~/android-source-workbench && git pull
sudo apt-get install -y python3-pip        # 无 sudo：curl -sS https://bootstrap.pypa.io/pip/3.8/get-pip.py -o /tmp/get-pip.py && python3 /tmp/get-pip.py --user
cd server && python3 -m pip install --user -r requirements-py38.txt     # 3.8/3.9 专用清单
mkdir -p ~/bin && curl -sSL https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz | tar xz -C /tmp && cp /tmp/ripgrep-15.2.0-x86_64-unknown-linux-musl/rg ~/bin/ && export PATH="$HOME/bin:$PATH"
cp config.example.json config.json         # 只改 roots（本地目录 + git.url）
python3 -m app --config config.json --check-config
python3 -m app --config config.json --git-status        # 远程仓是否已同步
./run.sh
# 公司电脑：ssh -L 8787:127.0.0.1:8787 xuhao@test-car-znh-compile → http://127.0.0.1:8787/
```

## 验证记录（真实执行）

| 命令 | 结果 |
|---|---|
| `cd server && python -m pytest` | **121 项：118 passed / 3 skipped / 0 failed，约 14s**（skip=Windows 无法创建符号链接） |
| 远程仓库测试（本地 `file://` 裸仓库，不依赖外网） | 真实 clone、稀疏检出、新增提交后 `fetch + checkout --detach` 更新、脏缓存拒绝覆盖、中断半成品清理、超时/取消、路径越界拒绝、API 端到端（同步→列目录→读文件→检索）全部通过 |
| 启动服务 + `python scripts/verify-p1-http.py` | **26/26**（含 git 能力、远程工作区 409、本地工作区拒绝同步、同步状态接口） |
| `python scripts/verify-p1-browser.py`（本机 Chromium via CDP） | **23/23**（含远程工作区标记、未同步提示、点同步触发服务器端 git、同步可取消、无 JS 异常） |
| `python scripts/check-package.py` / `node --check` | 通过 |
| 未完成 | 对 TUNA / 内网仓的真实 clone 未跑完（TUNA 排队 `Waiting in queue`），需在服务器上 `--sync` 实测；clangd/JDT LS 未接入 |

## 未完成（禁止对外宣称已实现）

- 语义跳转（变量/参数/成员/全局/常量、声明与定义、引用）——P2；clangd / JDT LS 均未接入。
- 全库索引与增量更新、Repo/manifest 快照一致性、个人修改覆盖层——P3（用户核心诉求之一）。
- 真实文件写入、保存冲突检测、可靠 diff、对远程仓提交/推送——P4。
- JNI / AIDL / Binder 关联分析——P5；真实 AOSP 上的性能与覆盖率、多用户权限体系未做。

## 已知限制

- 远程工作区只读；`readonly: false` 会直接拒绝启动。
- 检索仍是受限目录扫描（`indexed=false`），AOSP 规模必须等 P3；缺 `rg` 时还会被 `pythonMaxFiles` 截断。
- 远程工作区"可用"的判定依据 git 的有效 HEAD（结果缓存 5 秒，同步结束后失效重算）：
  被中断的 clone 留下的半成品目录会统一返回 `409 workspace_not_synced`，不会给出空目录或空结果。
- 符号链接越界用例在 Windows 被跳过，需在 Linux 上复跑 `pytest` 才能算覆盖。
- 前端标识符点击依赖 Chrome/Edge 的 `caretRangeFromPoint`。
- 只读且无认证，默认只监听回环地址。
- 本机仍有一个后端进程监听 8787：`Get-Process python | ? {$_.CommandLine -like '*-m app*'} | Stop-Process`。

## 下一步

**你在服务器上做**：`git pull` → 装 pip 与 `requirements-py38.txt` → 静态 rg → 把 `roots` 改成公司源码路径
（内网仓用 `git@gitlab.company.com:...` 形式的 `git.url`）→ `--sync` 拉一次 → `./run.sh` → 浏览器试用。
同时把 `python3 -m pytest -q` 的结果发回（3.8 环境的最终确认）。

**我这边接着做 P2（语义导航，你的核心诉求之一）**：

1. 用 clangd 跑通 `fixtures/navigation-cpp` 的 12 条预期（局部/参数/成员/全局/常量、同名遮蔽、声明与定义区分）。
   无 root 时 clangd 可以取：AOSP 自带 `prebuilts/clang/host/linux-x86/*/bin/clangd`，或 LLVM 官方静态包解压到 `~/bin`。
2. `/api/navigation` 从固定 `unavailable` 改为真实调度：请求带 workspace、相对路径、文档 hash、行列、类型；
   按 workspace 管理语言服务进程，控制数量与内存。
3. UTF-16 行列转换（含中文与非 BMP 字符）测试；同名多结果按作用域/工程身份消歧，无法消歧返回 `ambiguous`。
4. 之后是 P3 索引（Zoekt）——你明确提到的另一个核心诉求，需要先有真实 checkout 的规模数据。
