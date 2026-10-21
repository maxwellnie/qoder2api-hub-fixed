#!/bin/bash
# Qoder proxy - 局域网共享模式（Linux / macOS）
# 用法: ./start-qoder-proxy-lan.sh [端口] [API Key]     默认端口 8790
#
# 由上游 start-qoder-proxy-lan.bat (v1.3.4) 移植：
#   监听所有网卡；本模式强制要求 API Key（未提供时网关自动生成随机 Key、持久化并打印）。
#   bat 的 %~dp0 / 四级 Python 查找 / pause 的对应关系见 start-qoder-proxy.sh 顶部注释。

PORT=${1:-8790}
KEY=${2:-}
PYEXE=${PYEXE:-}

# ---- 定位脚本真实目录（等价 bat 的 %~dp0）----
_src=$0
if [ -n "${BASH_VERSION:-}" ]; then
    _src=${BASH_SOURCE[0]}        # bash 下比 $0 可靠（source / 相对路径场景）
fi
case "$_src" in
    */*) ;;
    *)   if [ ! -f "$_src" ]; then
             _hit=$(command -v "$_src" 2>/dev/null) && _src=$_hit
         fi ;;
esac
while [ -L "$_src" ]; do
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

# ---- 找可用的 Python 3（与 start-qoder-proxy.sh 同一套四级查找）----
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
    for _c in "$SCRIPT_DIR/python/bin/python3" "$SCRIPT_DIR/python/bin/python"; do
        probe "$_c" && { PYEXE=$_c; break; }
    done
    if [ -z "$PYEXE" ]; then
        for _c in python3 python; do
            _p=$(command -v "$_c" 2>/dev/null) && probe "$_p" && { PYEXE=$_p; break; }
        done
    fi
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
    echo "          3. 手动指定：PYEXE=/path/to/python3 ./start-qoder-proxy-lan.sh" >&2
    exit 1
fi

echo "==========================================================="
echo "  Qoder proxy - LAN MODE"
echo ""
echo "  端口: $PORT   （API 地址 / 看板链接 / API Key 在网关启动后打印）"
echo "  Python: $PYEXE"
echo ""
echo "  本模式强制要求 API Key：未指定时网关自动生成随机 Key 并持久化。"
echo "  其他设备连不上时，先放行防火墙（本目录下的脚本）："
echo "    ./allow-firewall.sh $PORT      # 自动识别 ufw / firewalld / nftables / iptables"
echo ""
echo "  保持此窗口开启，关闭则停止服务。按 Ctrl+C 停止。"
echo "==========================================================="
echo ""

if [ -z "$KEY" ]; then
    # 未指定 Key：网关生成随机 Key（对应 bat 的 run_nokey 分支）
    "$PYEXE" "$SCRIPT" --port "$PORT" --lan
else
    "$PYEXE" "$SCRIPT" --port "$PORT" --lan --api-key "$KEY"
fi
rc=$?

echo ""
echo "[server exited] (exit=$rc)"
# 等价 bat 的 pause（说明见 start-qoder-proxy.sh）
if [ -t 0 ]; then
    printf '按回车键退出...'
    read -r _dummy
    echo ""
fi
exit "$rc"
