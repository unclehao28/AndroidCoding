# 在公司的安卓源码服务器上部署与试用（P1）

目标：源码留在 Linux 服务器，公司电脑只通过浏览器使用，不需要把源码复制到本机。

## 0. 这一版试用时能做什么、不能做什么

能：浏览真实目录树（按层加载）、打开文件与行段（返回 sha256 版本哈希、编码、行尾、总行数）、
全库检索（默认字面量，正则为显式选项，有并发/超时/结果上限、可取消）、路径边界防护（`..`、盘符、
根目录外符号链接都会被拒绝）。

不能（界面会明确显示未就绪，不是 bug）：语义跳转属于 P2；全库索引属于 P3；真实编辑保存属于 P4；
JNI/AIDL/Binder 关联属于 P5。点击代码里的标识符会真实请求后端并显示 `unavailable`。

## 1. 服务器前置检查（三条命令）

```bash
python3 --version          # 决定用哪份依赖清单，见第 2 节
python3 -m pip --version   # 需要 pip；没有时见第 2 节
which rg || echo "no ripgrep"   # 建议安装，见第 3 节
```

## 2. 取得代码并安装依赖

```bash
git clone https://github.com/unclehao28/AndroidCoding.git ~/android-source-workbench
cd ~/android-source-workbench/server
```

依赖清单按解释器版本二选一（`./run.sh` 会自动选，也会打印它用了哪份）：

| 服务器 Python | 依赖清单 | 说明 |
|---|---|---|
| >= 3.10 | `requirements.txt` | fastapi 0.141 / starlette 1.6 / uvicorn 0.53 |
| 3.8 / 3.9（如 Ubuntu 20.04 自带 3.8.10） | `requirements-py38.txt` | fastapi 0.115 / pydantic 2.10 / uvicorn 0.33，已确认存在 cp38 的 wheel |

```bash
python3 -m pip install --user -r requirements.txt         # Python >= 3.10
python3 -m pip install --user -r requirements-py38.txt    # Python 3.8 / 3.9
```

**如果 `python3 -m pip` 报 `No module named pip`**（Ubuntu 20.04 的常见情况），任选一种：

```bash
sudo apt-get install -y python3-pip        # 有 sudo，最省事
# 无 sudo，但能访问外网（注意必须用 3.8 专用的 get-pip）：
curl -sS https://bootstrap.pypa.io/pip/3.8/get-pip.py -o /tmp/get-pip.py
python3 /tmp/get-pip.py --user
# 公司内网源：见第 6 节
```

安装完可以自查一次（会打印 Python 版本、建议的依赖清单、检索引擎、安卓源码标记）：

```bash
python3 -m app --config config.example.json --check-config
```

## 3. 安装 ripgrep（强烈建议，否则整套源码上检索会被截断）

Python 回退引擎有文件数上限（`pythonMaxFiles`，默认 20000），AOSP 动辄几十万文件，会提前停止并在界面上
显示"结果已截断"。安装 `rg` 后会自动切换到 ripgrep 引擎（`--check-config` 与 `/api/health` 里都能看到）。

```bash
sudo apt-get install -y ripgrep        # Debian / Ubuntu
sudo yum install -y ripgrep            # RHEL / CentOS
rg --version                           # 确认可用
```

无 sudo 或软件源不可用时，用静态二进制（不需要 root、不需要编译）：

```bash
mkdir -p ~/bin
curl -sSL https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz \
  | tar xz -C /tmp
cp /tmp/ripgrep-15.2.0-x86_64-unknown-linux-musl/rg ~/bin/
export PATH="$HOME/bin:$PATH"          # 建议写进 ~/.bashrc
rg --version
```

## 3.5 远程 / 内网 Git 仓库作为只读工作区

场景：公司内网仓（GitLab / Gerrit / 自建）或公开 AOSP 镜像，不想在服务器上手工 clone 一遍再配路径。
在 `roots[]` 里加上 `git` 字段，工作台负责 clone / fetch，Windows 侧只读浏览与检索。

```json
{
  "cacheDir": "/data/home/you/.asw-cache",
  "roots": [
    {
      "id": "company-frameworks-base",
      "name": "内网 platform/frameworks/base",
      "path": "frameworks-base",                      // 相对 cacheDir 的检出目录
      "readonly": true,
      "git": {
        "url": "git@gitlab.company.com:aosp/platform_frameworks_base.git",
        "ref": "main",                                 // 分支或标签，可省略
        "depth": 1,                                    // 浅克隆，1 最省时间与磁盘
        "sparsePaths": ["services/core/java", "core/java"]   // 只要这几个目录，可省略
      }
    },
    {
      "id": "aosp-system-core",
      "name": "AOSP platform/system/core（TUNA 镜像）",
      "path": "aosp-system-core",
      "readonly": true,
      "git": {
        "url": "https://mirrors.tuna.tsinghua.edu.cn/git/AOSP/platform/system/core",
        "ref": "main",
        "depth": 1,
        "sparsePaths": ["libcutils", "adb"]
      }
    }
  ]
}
```

同步与查看状态（也可以在页面上点「同步远程仓库」）：

```bash
python3 -m app --config config.json --git-status      # 只看状态，不做任何写操作
python3 -m app --config config.json --sync            # 同步全部远程工作区
python3 -m app --config config.json --sync aosp-system-core   # 只同步一个
```

行为保证（代码里写死，不是口头承诺）：

- 只执行 `clone` / `fetch` / `checkout --detach` / `merge --ff-only`；**不会** `reset --hard`、`clean`、`rebase`，
  也**不会**自动提交或推送。浅克隆更新走 `checkout --detach`（浅历史没有共同祖先，无法 merge）。
- 缓存目录里出现**本地修改**时拒绝更新，如实报告，绝不覆盖。
- 上一次 clone 被取消或中断留下的半成品（只有 `.git`、没有 HEAD）会在下次同步时先清掉再重来。
- 认证只用服务器上**既有**的 git/SSH 凭据（`~/.ssh`、`credential helper`、`url.insteadOf` 改写）；
  工作台不保存口令或私钥。SSH 用 `BatchMode=yes`、HTTPS 用 `GIT_TERMINAL_PROMPT=0`，
  凭据不可用时立即失败并给出 git 原始输出，不会卡在交互式输入上。
- 远程工作区在 P1 只有只读能力（配置 `readonly: false` 会直接拒绝启动）；真实写入与提交属于 P4。

公开 AOSP 来源实测（2026-09-18，中国大陆网络）：

| 来源 | 可达性 |
|---|---|
| `https://mirrors.tuna.tsinghua.edu.cn/git/AOSP/platform/<repo>` | 可 `git ls-remote`；clone 可能排队（`remote: Waiting in queue...`），适合用 `depth` + `sparsePaths` 缩小规模 |
| `https://github.com/aosp-mirror/<repo>` | 可 `git ls-remote`（`platform_frameworks_base` 有 `main`/`master`） |
| `https://android.googlesource.com/platform/<repo>` | 本次实测连接超时（21s），内网多半也不通 |

内网 GitLab 仓如果只在公司网络可达，把地址换成 SSH 形式即可；服务器上已有的免密 key 会被直接复用。

## 3.5 先搞清楚源码在哪台机器、仓库地址是什么

工作台需要**能读到源码文件**（P1 读文件、P2 要 clangd 与编译参数）。如果不知道源码放在哪，
在服务器上执行这个只读脚本，它只收集证据、不写任何文件、不需要 root：

```bash
bash scripts/find-source-server.sh
bash scripts/find-source-server.sh --deep                     # 额外扫 /，慢
bash scripts/find-source-server.sh --probe-host 172.20.36.99  # 探测该主机是否有 Gerrit(29418)/git(9418)
```

它最有力的三类证据：

| 线索 | 含义 |
|---|---|
| `.repo/manifest.xml` 里的 `fetch="..."` | **repo 仓库服务器基地址**（就是你要找的地址） |
| `.repo/manifests` 的 `remote.origin.url` | manifest 仓库的完整地址 |
| shell 历史里的 `repo init -u ...` | 最初使用的仓库地址 |

它还会检查：环境变量（`ANDROID_BUILD_TOP` 等）、`git config url.*.insteadOf`、`~/.ssh/config`、`/etc/hosts`、
NFS/CIFS 挂载（源码可能不在本机磁盘上）、docker 容器的挂载、其它用户主目录、以及本机 OpenGrok 的
`configuration.xml` 里的 `<sourceRoot>`（那台机器上源码的绝对路径）。

拿到线索后按优先级选路径：

1. **源码在本机或可挂载** → 最简单：`roots[].path` 直接指向源码根，能力最全（P1/P2/P3 都能做）。
2. **只能通过 git 访问** → 用 `roots[].git` 做**只读**浅克隆（配置注释里有 `depth` 与 `sparsePaths` 示例）。
   克隆用的是服务器自己的 `~/.ssh` 与 credential helper，**不要**把私钥或口令写进配置或仓库。
3. **只有 OpenGrok 网页可访问** → 需要新增一个后端适配（用 OpenGrok 的文件/检索接口）。
   这要先把接口实测清楚再实现，目前**没有**这个能力，不要当成已支持。

## 3.6 一键准备语义跳转（推荐先跑这个）

不要手工编辑 JSON——服务器上手工改配置很容易出错（占位符被原样粘贴、字段名写错、在 `vi` 里粘进 shell 命令）。
在仓库根目录执行一条命令即可：它会找 Android 源码树、找 clangd、把结果按行写进 `server/config.json`（**保留注释**、先备份），
最后给出验收命令。

```bash
cd ~/android-source-workbench
python3 scripts/setup-navigation.py --add-root /data/aosp12   # 登记只读工作区 + 找 clangd + 写配置（推荐）
python3 scripts/setup-navigation.py                 # 探测 + 写配置
python3 scripts/setup-navigation.py --acceptance    # 顺带跑 12 条跳转预期验收
python3 scripts/setup-navigation.py --print-only    # 只看探测结果，不改任何文件
python3 scripts/setup-navigation.py --depth 6       # 默认只扫 4 层，找不到源码时加深
```

`--add-root` 会把这个目录作为**只读**工作区写进 `roots[]`（按行插入、保留注释、先备份、
写入前用真正的校验器验证，校验不过就一个字节都不写），这样浏览/检索真实源码不需要你手改 JSON。
给了 `--aosp` / `--add-root` 之后脚本**不会**再去扫 `/data`、`/home`——那份目录可能有几十万文件，
扫一遍要几分钟，而你既然说出了源码位置就没必要再找。

`--direct` 验收路径只依赖标准库（`app.config` / `app.navigation` / `app.lsp.*` 都不需要 fastapi），
所以**在 pip 装好之前就能先拿到 P2 的验收结果**。

配置如果是语法坏的（例如在 `vi` 里误操作留下了空文件），脚本会先备份再重建，**并在同一次运行里继续探测**，
不需要你再跑第二遍；备份文件名带秒级时间戳且保证不互相覆盖，你原来手工写的内容不会丢。

找不到完整源码树时，它会列出"只含部分标记"的候选目录，并直接把可以复制执行的 `--aosp` 命令打印出来
（带上真实路径，不需要你替换任何占位符）。此时 `--acceptance` **不会**硬跑验收，而是明确告诉你还差什么。

输出示例（真实运行）：

```
[探测] 扫描目录 2974 个，发现 Android 源码树 1 个
    - /data/aosp
[clangd] 18.1.8 · /data/aosp/prebuilts/clang/host/linux-x86/clang-r547379/bin/clangd
         来源：AOSP prebuilts（/data/aosp）
[写入] 按行替换（保留注释）
    searchDirs → ["/data/aosp"]
    备份：.../config.json.bak-20260918-160400
下一步（在服务器上验收 12 条跳转预期）：
  python3 scripts/verify-p2-navigation.py --direct --workspace fixtures
```

它的行为边界（与其它部分一致）：探测有深度与条目上限，不会全盘扫描；找不到就如实报告并给替代方案，
**不会**往配置里写假路径；只有在配置**语法**损坏时才重建（路径不存在这类问题只报告、绝不覆盖你的配置）。

如果源码不在默认扫描位置（`/data /home /opt /mnt /srv /workspace`），直接告诉它：

```bash
python3 scripts/setup-navigation.py --aosp /data/aosp           # 指定源码根（可重复传多个）
python3 scripts/setup-navigation.py --clangd /home/you/bin/clangd
```

如果这台机器上压根没有 Android 源码（例如代码在另一台机器），clangd 需要单独准备，按成本从低到高：

1. 有 sudo：`sudo apt-get install -y clangd`（Debian/Ubuntu 自带包，最省事）。
2. 有 `apt-get` 但无 sudo：`apt-get download clangd-12`（或 `clangd-11`/`clangd-10`）→ `dpkg -x clangd-12_*.deb ~/local`
   → 若 `~/local/usr/bin/clangd --version` 报缺库，用 `ldd` 看缺哪个，再用同样的方式解包对应的 `libclang-cpp*` / `libllvm*`，
   然后 `LD_LIBRARY_PATH=~/local/usr/lib/x86_64-linux-gnu ~/local/usr/bin/clangd --version` 验证。
   *这条路我无法在本机复现（本机没有 Debian 环境），请把命令输出发我，我按实际报错继续。*
3. 不能联网也不许装包：在能上网的机器下载 LLVM 官方静态包（`clang+llvm-*-x86_64-linux-gnu-ubuntu-*.tar.xz`，约 1GB），
   解压后取 `bin/clangd` 放进 `~/bin`，再 `python3 scripts/setup-navigation.py --clangd ~/bin/clangd`。

装好后用 `python3 -m app --config config.json --check-config` 复核，它现在会直接打印 clangd 路径与版本。

## 3.65 Java 语义跳转（Eclipse JDT LS）

C/C++ 用 clangd，Java 用 JDT LS。JDT LS 是 Java 程序、**自带硬性 JDK 要求**，所以脚本按版本匹配：

```bash
cd ~/android-source-workbench
python3 scripts/setup-java.py             # 体检：报告 JDK、JDT LS、配置现状与下一步
python3 scripts/setup-java.py --install   # 按 JDK 版本自动下载匹配的 JDT LS，并写进 config.json
```

| 机器上的 JDK | 会自动装 | 说明 |
|---|---|---|
| 21+ | 最新快照 | 官方 `snapshots/latest` |
| 17–20 | 1.31.0 | 2024-01 里程碑；**本机用 JDK 17 + 1.31.0 实测 5/5 Java 用例通过** |
| 11–16 | 1.12.0 | 较老里程碑 |
| 11 以下 | 不装，并提示先装 JDK | JDT LS 无法运行 |

脚本会先读该版本自带启动脚本里声明的 JDK 要求，与机器上的 `java` 比对，**不匹配就报错退出，不写配置**。
JDK 查找顺序：`navigation.javaHome` → `JAVA_HOME` → `PATH` 里的 `java` → `searchDirs`/各 root 下的
`prebuilts/jdk/*/linux-x86/bin/java`（AOSP 自带，一般是 11，只够配 1.12.0）→ `/usr/lib/jvm/*/bin/java`。

**安装时会真的启动一次（冒烟测试）**：只校验目录结构是不够的——实测最新快照在 JDK 21 上会
「启动即退出」（class file version 不支持）。所以脚本会用与运行期完全相同的命令拉起 JDT LS，
完成 `initialize` 握手才算通过；**起不来就自动降级**到下一个候选版本（最新快照 → 1.31.0 → 1.12.0），
并把每一档失败时的 stderr 尾部打印出来。三档都起不来则不写配置，并给出具体报错。

配置项（都由脚本写入，不必手工编辑）：

```json
"navigation": { "javaLsPath": "~/.local/share/asw-jdtls/1.31.0", "javaHome": "/path/to/jdk-17",
                "javaDataDir": null, "javaArgs": [] }
```

**必须如实知道的限制**（也是产品约束里"不许把候选说成精准"的一部分）：

- **Soong 不是 Maven/Gradle**：JDT LS 无法导入 AOSP 工程。现在能做的是**同文件/同源码目录内**的解析
  （局部变量、参数、字段、同目录类）。跨模块依赖（例如 `frameworks/base` 里的类引用
  `system/core` 的接口）需要单独的导入适配，**尚未完成**；接口会返回 `unavailable` 并说明原因。
- `/api/health` 的 `navigation.supported.java` 会带上 `classpathSupport: false` 与具体原因，
  不要把"JDT LS 起来了"当成"AOSP Java 全部支持"。

## 3.7 语义跳转（P2，C/C++）与 clangd

P2 已接入 **C/C++**（clangd）；Java/Kotlin/Rust/AIDL 明确返回未就绪，不会用文本匹配冒充。

服务端按这个顺序找 clangd：`navigation.clangdPath` → `PATH` → `navigation.searchDirs` 下的
`prebuilts/clang/host/linux-x86/*/bin/clangd`。**AOSP 自带 clangd，不需要额外安装**：

```bash
ls -d /path/to/aosp/prebuilts/clang/host/linux-x86/*/bin/clangd | tail -1
```

在 `config.json` 里加（`searchDirs` 指向 AOSP 根目录即可，会自动挑最新的那个 clangd）：

```json
"navigation": {
  "enabled": true,
  "searchDirs": ["/home/xuhao/aosp"],
  "backgroundIndex": false,
  "maxInstances": 2,
  "idleShutdownSeconds": 300
}
```

C/C++ 的解析质量取决于编译参数，请按实际情况处理：

- 有 `compile_commands.json` 时最准。AOSP 用 Soong 生成（`SOONG_GEN_COMPDB=1 m <目标>`，产物在
  `out/soong/development/ide/compdb/compile_commands.json`），然后把 `navigation.compileCommandsDir`
  或某个 `roots[].compileCommandsDir` 指向该目录。
- 没有它时 clangd 仍能解析单个编译单元内的符号，但跨文件/宏相关的跳转会不准或为空；
  此时接口返回 `unavailable` 并把"缺 compile_commands.json"作为提示给出，**不会伪造结果**。
- 整套 AOSP 上**不要**开 `backgroundIndex`（默认关）：它会为整个工作区建索引，CPU/内存/磁盘开销很大；
  只有需要跨文件定义跳转时才考虑打开。共享/远程索引属于后续工作。

验收（在服务器上一条命令，不需要先启动服务；退出码 2 表示没找到 clangd，**不算通过**）：

```bash
cd ~/android-source-workbench
python3 scripts/verify-p2-navigation.py --direct --workspace fixtures
```

它用真实语言服务逐条核对 `fixtures/navigation-cases.json` 里 12 条预期（局部变量、同名遮蔽、参数、
成员、全局、常量），判定规则是"预期位置是否落在语言服务返回的范围内"，输出每条的真实状态与目标位置。
真实 AOSP 树上的跳转请用页面点击抽查，或 `curl -X POST /api/navigation`。

## 4. 配置允许访问的源码根目录

```bash
cp config.example.json config.json
```

AOSP 场景的建议配置（把 path 换成你的实际路径，其余按需调整）：

```json
{
  "server": { "host": "127.0.0.1", "port": 8787, "corsOrigins": ["*"] },
  "roots": [
    { "id": "aosp", "name": "AOSP 主干", "path": "/home/yourname/aosp", "readonly": true },
    { "id": "vendor", "name": "vendor 与内核", "path": "/home/yourname/vendor-kernel", "readonly": true }
  ],
  "limits": {
    "maxFileBytes": 2097152, "maxLineCount": 2000, "maxTreeEntries": 2000,
    "maxSearchResults": 1000, "maxSearchFileBytes": 2097152, "searchTimeoutSeconds": 15,
    "maxConcurrentSearches": 2, "maxRegexLength": 200, "maxLineLength": 400, "rgThreads": 4
  },
  "search": {
    "engine": "auto", "respectIgnoreFiles": true, "pythonMaxFiles": 20000,
    "excludeGlobs": [".git/**", ".repo/**", "out/**", "**/out/**", "**/.intermediates/**",
                     "*.o", "*.a", "*.so", "*.jar", "*.apk", "*.img", "*.zip"]
  },
  "features": { "write": false, "navigation": false }
}
```

要点：

- `roots` 只列确实需要的源码根目录，**不要写 `/`**，服务不会遍历整台服务器；AOSP 与 vendor/kernel 分开存放时配多个 root。
- `prototypeDir` 省略时默认为 `../prototype`，正好适用于克隆出来的目录结构。
- `out/`、`.repo/`、`.git/` 已在默认排除列表里；如果 checkout 中还有别的大目录（镜像、二进制产物），加进 `excludeGlobs`。
- `features.write`/`features.navigation` 在 P1 必须是 `false`，写成 `true` 会直接拒绝启动（防止把未实现能力当成已开启）。
- 只读：P1 不提供写入接口，`readonly: true` 符合当前能力。

自查（会打印 Python 版本、检索引擎、各根目录是否像安卓源码树）：

```bash
python3 -m app --config config.json --check-config
```

## 5. 启动与客户端访问

注意：`config.example.json`、`python3 -m app`、`run.sh` 都在 **server/** 目录里。
在仓库根目录可以直接用 `./run.sh`（它会转发到 `server/run.sh`）；要用 `python3 -m app` 就必须先 `cd server`。

```bash
cd ~/android-source-workbench/server
./run.sh                       # 没有 config.json 时会自动用 config.example.json（只有 fixtures 与示例远程仓）
python3 -m app --config config.json --check-config   # 配置与环境自查（必须在 server 目录里执行）
```

公司电脑上（复用已有的 SSH 登录，不需要额外开端口）：

```bash
ssh -L 8787:127.0.0.1:8787 USER@SERVER
```

然后浏览器打开 `http://127.0.0.1:8787/`。页面右上角切到「真实服务器」即可（从该地址打开时默认就是同源后端）。

- 本地 8787 被占用时，只改隧道左侧端口：`ssh -L 8899:127.0.0.1:8787 USER@SERVER`，然后访问 `http://127.0.0.1:8899/`。
- 默认端口是 8787 而不是 8765：8765 常被本机代理工具（Clash 等）占用，会导致本地转发绑定失败。
- 服务只监听 `127.0.0.1`，不需要开放防火墙，也不要把端口暴露到公网：P1 没有认证。

## 6. 依赖安装的三种情形

1. 能访问外网 PyPI：`python3 -m pip install -r requirements.txt`
2. 只能访问内网镜像：
   ```bash
   python3 -m pip install -r requirements.txt -i https://<公司源>/simple --trusted-host <公司源>
   # 例如清华源：-i https://pypi.tuna.tsinghua.edu.cn/simple
   ```
3. 完全离线：在能联网的机器上抓取与服务器匹配的 linux wheel，再拷进去离线安装：
   ```bash
   pip download -r requirements.txt -d wheels --only-binary=:all: \
       --platform manylinux2014_x86_64 --python-version 311 --implementation cp
   # 把 wheels/ 拷到服务器后：
   python3 -m pip install --no-index --find-links wheels -r requirements.txt
   ```
   `pydantic-core`、`httptools`、`watchfiles` 是编译扩展，必须用与服务器 Python 版本、架构匹配的 wheel。
   实在拿不到时可以先只装 `fastapi uvicorn`（不要 `uvicorn[standard]`）：功能一样，只是没有加速项。

**Python 3.8 / 3.9**（例如 Ubuntu 20.04 自带 3.8.10）：直接用 `requirements-py38.txt`，不需要另装解释器，
按第 2 节的说明安装即可。这一组合是 pip 针对 cp38 解析出来的：`pydantic-core 2.27.2` 有
`cp38-manylinux2014_x86_64` 的 wheel，`uvicorn 0.33.0` 用不带 `[standard]` 的纯 Python 版本（无 httptools/watchfiles 加速项，
功能一致）。项目代码本身已按 3.8 兼容处理：入口模块不使用 `dict[str, ...]` / `X | None` 这类 3.8 无法求值的注解，
检索的线程池调用做了 `asyncio.to_thread` 能力探测（`server/tests/test_py38_compat.py` 会守住这两条）。

如果公司要求统一用新解释器（pyenv / conda / 内部 Python 包），那就回到 `requirements.txt` 那一行。

## 7. 自检：确认真的读到了源码

```bash
curl -s http://127.0.0.1:8787/api/health
curl -s "http://127.0.0.1:8787/api/tree?workspace=aosp&path="
curl -s -X POST http://127.0.0.1:8787/api/search \
     -H 'Content-Type: application/json' \
     -d '{"query":"setBrightness","workspace":"aosp","limit":50}'
```

仓库内的验收脚本默认校验示例工作区 `demo`，可用来确认后端自身正常：

```bash
python3 scripts/verify-p1-http.py --base http://127.0.0.1:8787           # 期望 21/21
cd server && python3 -m pytest -q                                        # 先装测试依赖：
#   Python >= 3.10：python3 -m pip install --user -r requirements-dev.txt
#   Python 3.8/3.9：python3 -m pip install --user -r requirements-py38-dev.txt
```

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `python3 -m pip` 报 `No module named pip` | 见第 2 节：`apt install python3-pip` 或 3.8 专用的 get-pip |
| 启动报 `缺少依赖：No module named 'fastapi'` | 用错依赖清单：3.8/3.9 必须用 `requirements-py38.txt`（`run.sh` 会自动选） |
| `pip install` 报 `Could not find a version that satisfies...` | Python 版本与清单不匹配：3.8 装 `requirements.txt` 必然失败 |
| 启动报 `Address already in use` | 端口被占用：`ss -ltnp \| grep 8787`，换端口或结束后占进程 |
| 浏览器打不开页面 | 隧道未建立或本地端口冲突；确认 `ssh -L` 左侧端口在本机空闲 |
| 页面提示「后端连接失败」 | 后端没起来或地址不对；看服务器终端输出 |
| 页面提示「结果已截断」 | 达到 `maxSearchResults` 或 `pythonMaxFiles` 上限；安装 rg 或调大限额 |
| 检索很慢 | 正在使用 Python 回退引擎（见第 3 节）；P3 会引入真正的全库索引 |
| 文件显示「无法作为文本显示」 | 二进制文件或超过 `maxFileBytes`，按需调整配置 |
| 路径被 400 拒绝 | `..`、盘符、UNC 等边界防护，属预期行为 |
| 点标识符提示「未就绪」 | 语义跳转是 P2 内容，属预期而非故障 |
| 目录里有条目显示「根目录外，已阻止」 | 该符号链接指向配置根之外，被安全策略阻止 |

## 9. 试用时请顺手记录（P3/P5 验收需要）

源码分支与 manifest 快照、构建目标、仓库数/文件数/数据量、冷热检索耗时（p50/p95）、超时与截断情况、
内存与磁盘占用、语义导航（P2 之后）的准确/候选/失败数量。这些数据决定了后面要不要上 Zoekt、怎么调限额。
