#!/bin/bash
# Qoder proxy - 本机单机模式（Linux / macOS）
# 用法: ./start-qoder-proxy.sh [端口]        默认端口 8790
#
# 由上游 start-qoder-proxy.bat (v1.3.4) 移植，逐项对应：
#   bat 的 %~dp0            -> SCRIPT_DIR 解析（dash 里没有 BASH_SOURCE，得自己算）
#   bat 的四级 Python 查找   -> PYEXE 探测（1 自带运行时 / 2 python3 / 3 python / 4 Codex 运行时）
#   bat 的 pause            -> 结尾的「按回车键退出」（只在交互终端）
#   也可先用环境变量指定解释器：PYEXE=/usr/bin/python3 ./start-qoder-proxy.sh

PORT=${1:-8790}
PYEXE=${PYEXE:-}

# ---- 定位脚本真实目录（等价 bat 的 %~dp0）----
_src=$0
if [ -n "${BASH_VERSION:-}" ]; then
    _src=${BASH_SOURCE[0]}        # bash 下比 $0 可靠（source / 相对路径场景）
fi
case "$_src" in
    */*) ;;                          # 含路径分量，直接用
    *)   if [ ! -f "$_src" ]; then   # 裸名字且当前目录没有 → 反查 PATH
             _hit=$(command -v "$_src" 2>/dev/null) && _src=$_hit
         fi ;;
esac
while [ -L "$_src" ]; do             # 顺着符号链接找到实体文件
    _lnk=$(readlink "$_src")
    case "$_lnk" in
        /*) _src=$_lnk ;;
        *)  _src=$(dirname -- "$_src")/$_lnk ;;
    esac
done
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$_src")" && pwd) || exit 1
SCRIPT="$SCRIPT_DIR/qoder_proxy.py"

# ---- 同目录必须有 qoder_proxy.py（bat 先查这个，再找解释器）----
if [ ! -f "$SCRIPT" ]; then
    echo "[ERROR] 同目录下未找到 qoder_proxy.py" >&2
    echo "        期望位置: $SCRIPT" >&2
    exit 1
fi

# ---- 找可用的 Python 3 ----
probe() {   # 能跑起来、且确实是 Python 3
    [ -n "$1" ] && [ -x "$1" ] || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' >/dev/null 2>&1
}
if [ -n "$PYEXE" ]; then
    if ! probe "$PYEXE"; then
        echo "[ERROR] PYEXE 指定的解释器不可用: $PYEXE" >&2
        exit 1
    fi
else
    # 1) 自带运行时（发布包里的 python/bin 目录，对应 bat 的 %HERE%python 目录）
    for _c in "$SCRIPT_DIR/python/bin/python3" "$SCRIPT_DIR/python/bin/python"; do
        probe "$_c" && { PYEXE=$_c; break; }
    done
    # 2) PATH 上的 python3    3) PATH 上的 python（必须真是 py3，否则跳过）
    if [ -z "$PYEXE" ]; then
        for _c in python3 python; do
            _p=$(command -v "$_c" 2>/dev/null) && probe "$_p" && { PYEXE=$_p; break; }
        done
    fi
    # 4) Codex 运行时缓存（对应 bat 第 4 级；Linux 上基本不会命中）
    if [ -z "$PYEXE" ] && [ -d "$HOME/.cache/codex-runtimes" ]; then
        _p=$(find "$HOME/.cache/codex-runtimes" -maxdepth 4 -name python3 -type f 2>/dev/null | head -1)
        probe "$_p" && PYEXE=$_p
    fi
fi
if [ -z "$PYEXE" ]; then
    echo "[ERROR] 未找到可用的 Python 3。" >&2
    echo "        可选方案:" >&2
    echo "          1. 使用自带运行时的发布包（内含 python/bin/python3）" >&2
    echo "          2. 安装 Python 3.9+：sudo apt install python3" >&2
    echo "          3. 手动指定：PYEXE=/path/to/python3 ./start-qoder-proxy.sh" >&2
    exit 1
fi

echo "==========================================================="
echo "  Qoder proxy"
echo ""
echo "  API      : http://127.0.0.1:$PORT/v1"
echo "  Dashboard: http://127.0.0.1:$PORT/"
echo ""
echo "  Python   : $PYEXE"
echo ""
echo "  保持此窗口开启，关闭则停止服务。按 Ctrl+C 停止。"
echo "==========================================================="
echo ""

"$PYEXE" "$SCRIPT" --port "$PORT"
rc=$?

echo ""
echo "[server exited] (exit=$rc)"
# 等价 bat 的 pause：只在交互终端等回车；管道、systemd、cron 下不阻塞，也不吞退出码
if [ -t 0 ]; then
    printf '按回车键退出...'
    read -r _dummy
    echo ""
fi
exit "$rc"
