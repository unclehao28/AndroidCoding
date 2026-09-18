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
python3 --version          # 需要 >= 3.10（依赖声明如此，见第 6 节的降级办法）
python3 -m pip --version   # 需要 pip
which rg || echo "no ripgrep"
```

## 2. 取得代码并安装依赖

```bash
git clone https://github.com/unclehao28/AndroidCoding.git ~/android-source-workbench
cd ~/android-source-workbench/server
python3 -m pip install -r requirements.txt --user     # 或用虚拟环境
```

## 3. 安装 ripgrep（强烈建议，否则整套源码上检索会被截断）

Python 回退引擎有文件数上限（`pythonMaxFiles`，默认 20000），AOSP 动辄几十万文件，会提前停止并在界面上
显示"结果已截断"。安装 `rg` 后会自动切换到 ripgrep 引擎（`/api/health` 里能看到引擎名）。

```bash
sudo apt-get install -y ripgrep        # Debian / Ubuntu
sudo yum install -y ripgrep            # RHEL / CentOS
rg --version                           # 确认可用
```

无 root 或无法联网时：在能上网的机器下载 `ripgrep-<版本>-x86_64-unknown-linux-musl.tar.gz`，
`scp` 到服务器，解压后把 `rg` 放到 `~/bin` 并加进 `PATH`（静态二进制，无需安装）。

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

服务器上：

```bash
./run.sh                       # 或 bash run.sh；没有 config.json 时会自动用 config.example.json
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

**Python 低于 3.10**（例如 Ubuntu 20.04 自带 3.8）：锁定的依赖组合无法安装，需要先准备 3.10+ 解释器
（pyenv / conda / 公司内部 Python 包）。如果你只能使用 3.8 或 3.9，把 `python3 --version` 的结果告诉我，
我会补一套在旧解释器上实际跑过测试的依赖版本。

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
cd server && python3 -m pytest -q                                        # 需要 requirements-dev.txt
```

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
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
