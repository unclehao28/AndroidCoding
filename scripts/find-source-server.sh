#!/usr/bin/env bash
# 只读地收集线索：安卓源码放在哪台机器上、repo/git 服务器地址是什么。
#
# 用法（在服务器上执行，不需要 root，不写任何文件）：
#   bash scripts/find-source-server.sh                    # 常规搜索
#   bash scripts/find-source-server.sh --deep             # 额外扫 /（更慢）
#   bash scripts/find-source-server.sh --probe-host 172.20.36.99   # 探测该主机的 git/gerrit 端口
#   ROOTS="/data /home" MAXDEPTH=6 bash scripts/find-source-server.sh
#
# 最有力的证据是 checkout 里的 .repo/manifest.xml：<remote fetch="..."> 就是仓库服务器地址。
set -u

MAXDEPTH="${MAXDEPTH:-6}"
ROOTS="${ROOTS:-/data /home /workspace /opt /mnt /srv /nfs /media}"
DEEP=0
PROBE_HOST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --deep) DEEP=1 ;;
    --probe-host) shift; PROBE_HOST="${1:-}" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

have() { command -v "$1" >/dev/null 2>&1; }
section() { printf '\n=== %s ===\n' "$1"; }
note() { printf '  - %s\n' "$1"; }
found_any=0

section "主机与身份"
note "$(hostname 2>/dev/null) · $(whoami 2>/dev/null) · $(date 2>/dev/null | cut -d' ' -f1-4)"

section "环境变量（含源码头目录 / repo 相关）"
env 2>/dev/null | grep -iE '^(ANDROID|AOSP|REPO|OUT|TARGET|SRC|SOURCE|GIT)' \
  | while IFS='=' read -r key value; do
      case "$key" in
        *[Ss][Ee][Cc][Rr][Ee][Tt]*|*[Pp][Aa][Ss][Ss]*|*[Tt][Oo][Kk][Ee][Nn]*|*[Kk][Ee][Yy]*) note "$key=<已隐藏>" ;;
        *) note "$key=$value" ;;
      esac
    done
[ -z "$(env 2>/dev/null | grep -iE '^(ANDROID|AOSP|REPO|OUT|TARGET)')" ] && note "没有相关的环境变量"

section "repo 清单（.repo 目录 → manifest 里的 fetch 地址）"
candidates=""
append_candidate() { [ -n "$1" ] && [ -d "$1" ] && candidates="$candidates $1"; }
append_candidate "${ANDROID_BUILD_TOP:-}"
append_candidate "${ANDROID_HOME:-}"
# 从当前目录向上找
walk="$PWD"
while [ -n "$walk" ] && [ "$walk" != "/" ]; do
  append_candidate "$walk"
  walk="$(dirname "$walk")"
done
for root in $ROOTS; do
  [ -d "$root" ] || continue
  found_dirs="$(find "$root" -maxdepth "$MAXDEPTH" -type d -name .repo -print 2>/dev/null | head -20)"
  for dir in $found_dirs; do
    append_candidate "$(dirname "$dir")"
  done
done

seen=""
for repo_root in $candidates; do
  [ -d "$repo_root/.repo" ] || continue
  case " $seen " in *" $repo_root "*) continue ;; esac
  seen="$seen $repo_root"
  found_any=1
  note "源码树：$repo_root"
  for manifest in "$repo_root/.repo/manifest.xml" "$repo_root/.repo/manifests/default.xml"; do
    [ -f "$manifest" ] || continue
    fetches="$(grep -oE 'fetch="[^"]+"' "$manifest" 2>/dev/null | sed 's/fetch="//;s/"$//' | sort -u)"
    for item in $fetches; do
      note "  manifest $manifest → fetch=$item   ← 这通常就是仓库服务器地址"
    done
  done
  if [ -d "$repo_root/.repo/manifests/.git" ] || [ -f "$repo_root/.repo/manifests/.git" ]; then
    url="$(git -C "$repo_root/.repo/manifests" config --get remote.origin.url 2>/dev/null)"
    [ -n "$url" ] && note "  .repo/manifests 的 remote.origin.url=$url"
    ref="$(git -C "$repo_root/.repo/manifests" rev-parse --abbrev-ref HEAD 2>/dev/null)"
    [ -n "$ref" ] && note "  manifest 分支=$ref"
  fi
  if have repo; then
    note "  repo info 前几行："
    (cd "$repo_root" && timeout 20 repo info 2>/dev/null | head -6 | sed 's/^/      /')
  fi
done
[ "$found_any" = 0 ] && note "本机（在 $ROOTS 及当前目录向上，深度 $MAXDEPTH）没有找到 .repo 源码树"

section "git 远程与 URL 重写"
if have git; then
  git config --global --get-regexp '^url\.' 2>/dev/null | sed 's/^/  - 全局 /'
  git config --global --get-regexp '^remote\.' 2>/dev/null | sed 's/^/  - 全局 /'
  git config --system --get-regexp '^url\.' 2>/dev/null | sed 's/^/  - 系统 /'
  note "当前目录的 remote："
  git remote -v 2>/dev/null | sed 's/^/      /'
else
  note "没有 git 命令"
fi

section "ssh 配置与 hosts（内网仓库常见写法）"
for cfg in "$HOME/.ssh/config" /etc/ssh/ssh_config; do
  [ -r "$cfg" ] || continue
  grep -iE '^\s*(host|hostname|user)\s' "$cfg" 2>/dev/null | head -20 | sed "s|^|  - $cfg: |"
done
if [ -r /etc/hosts ]; then
  grep -vE '^\s*#|^\s*$|localhost|127\.0\.0\.1|::1' /etc/hosts 2>/dev/null | head -20 | sed 's/^/  - hosts: /'
fi

section "挂载点（源码可能在 NFS / CIFS / sshfs 上）"
if have mount; then
  mount 2>/dev/null | grep -iE 'nfs|cifs|sshfs|fuse|9p' | head -20 | sed 's/^/  - /'
  mount 2>/dev/null | grep -iqE 'nfs|cifs|sshfs|fuse|9p' || note "没有网络文件系统挂载"
fi

section "docker 容器的挂载（编译环境常在容器里挂源码）"
if have docker; then
  docker ps --format '{{.Names}}' 2>/dev/null | head -5 | while read -r name; do
    note "容器 $name 的挂载："
    docker inspect "$name" --format '{{range .Mounts}}      {{.Source}} → {{.Destination}}{{"\n"}}{{end}}' 2>/dev/null
  done
  docker ps -q >/dev/null 2>&1 || note "docker 存在但当前用户没有权限（可忽略）"
else
  note "没有 docker 命令"
fi

section "其它用户主目录里的源码树"
for pattern in /data/home/* /home/*; do
  [ -d "$pattern" ] || continue
  home_dir="$pattern"
  [ "$(id -un 2>/dev/null)" = "$(basename "$home_dir")" ] && continue
  if [ -d "$home_dir/.repo" ]; then
    note "$home_dir 就是一份源码树（含 .repo）"
  elif [ -d "$home_dir" ]; then
    hit="$(find "$home_dir" -maxdepth 3 -type d -name .repo -print 2>/dev/null | head -3)"
    [ -n "$hit" ] && note "$home_dir 下发现：$(echo "$hit" | tr '\n' ' ')"
  fi
done

section "shell 历史里的 repo init（能直接看出仓库地址）"
for history in "$HOME/.bash_history" /root/.bash_history; do
  [ -r "$history" ] || continue
  grep -h 'repo init' "$history" 2>/dev/null | tail -5 | sed "s|^|  - $history: |"
done
for history in /data/home/*/.bash_history /home/*/.bash_history; do
  [ -r "$history" ] || continue
  grep -h 'repo init' "$history" 2>/dev/null | tail -3 | sed "s|^|  - $history: |"
done

section "本机的 OpenGrok 配置（sourceRoot 就是源码绝对路径）"
opengrok_hits=""
if [ "$DEEP" = 1 ]; then
  opengrok_hits="$(find / -maxdepth 7 -name configuration.xml -path '*opengrok*' -print 2>/dev/null | head -5)"
else
  for root in $ROOTS /opt /usr/local /var; do
    [ -d "$root" ] || continue
    opengrok_hits="$opengrok_hits $(find "$root" -maxdepth 5 -name configuration.xml -path '*opengrok*' -print 2>/dev/null | head -3)"
  done
fi
if [ -n "$(echo "$opengrok_hits" | tr -d ' ')" ]; then
  for cfg in $opengrok_hits; do
    note "$cfg"
    grep -oE '<sourceRoot>[^<]*</sourceRoot>' "$cfg" 2>/dev/null | sed 's/^/      /'
    grep -oE '<projects>[^<]*</projects>' "$cfg" 2>/dev/null | head -3 | sed 's/^/      /'
  done
else
  note "本机没有找到 OpenGrok 的 configuration.xml"
  note "如果 OpenGrok 部署在 172.20.36.99 上，在那台机器上执行本脚本即可拿到 sourceRoot"
fi

if [ -n "$PROBE_HOST" ]; then
  section "端口探测：$PROBE_HOST（只做 TCP 连接，不发送数据）"
  for port in 22 80 443 9418 29418 8080 8081 8443; do
    if timeout 2 bash -c "exec 3<>/dev/tcp/$PROBE_HOST/$port" 2>/dev/null; then
      note "$port 开放"
    else
      note "$port 不通"
    fi
  done
  note "29418 是 Gerrit 默认 SSH 端口；9418 是 git 协议端口；有 29418 基本可以确定是 Gerrit"
fi

section "结论怎么用"
cat <<'EOF'
  把上面出现这些内容的行发我，它们直接给出仓库服务器：
    · manifest 里的 fetch=            → 仓库服务器基地址
    · .repo/manifests 的 remote.origin.url → manifest 仓库完整地址
    · shell 历史里的 repo init -u    → 最初的仓库地址
  如果全部显示"没有找到"：说明这台机器上没有 checkout，源码在别的机器上（OpenGrok 的
  sourceRoot 会告诉你那台机器上的绝对路径），这时需要向同事确认 repo init 用的地址，
  或者把工作台部署到源码所在的那台机器上。
EOF
