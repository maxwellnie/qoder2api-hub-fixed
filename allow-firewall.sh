#!/bin/bash
# allow-firewall.sh - 放行 Qoder 网关端口（Linux / macOS）
# 用法: ./allow-firewall.sh [端口]          默认端口 8790
#
# 对应上游 allow-firewall.bat：局域网模式（start-qoder-proxy-lan.sh）下其他设备
# 连不上时，执行一次放行本机入站 TCP 端口。会自动识别后端并按需提权：
#   ufw -> firewalld -> nftables -> iptables

PORT=${1:-8790}

echo "==========================================================="
echo "  Qoder 网关 - 放行入站 TCP $PORT"
echo "==========================================================="
echo ""

# 端口必须是数字（下面要拿它拼特权命令）
case "$PORT" in
    ''|*[!0-9]*)
        echo "[ERROR] 端口必须是数字，收到: $PORT" >&2
        exit 2 ;;
esac

# ---- 工具查找：Debian/Ubuntu 的 /usr/sbin 不在普通用户 PATH 里，得显式找 ----
have() {
    command -v "$1" >/dev/null 2>&1 && return 0
    for _p in /usr/sbin/"$1" /sbin/"$1" /usr/local/sbin/"$1" /usr/bin/"$1" /usr/local/bin/"$1"; do
        [ -x "$_p" ] && return 0
    done
    return 1
}
resolve() {
    command -v "$1" 2>/dev/null && return 0
    for _p in /usr/sbin/"$1" /sbin/"$1" /usr/local/sbin/"$1" /usr/bin/"$1" /usr/local/bin/"$1"; do
        [ -x "$_p" ] && { echo "$_p"; return 0; }
    done
    return 1
}

# ---- 提权：非 root 时用 sudo（对应 bat 的 UAC 自我提权）----
SUDO_PREFIX=""
if [ "$(id -u)" = "0" ]; then
    asroot() { "$@"; }
    echo "  权限: 已以 root 运行"
else
    if SUDO_BIN=$(resolve sudo); then
        asroot() { "$SUDO_BIN" "$@"; }
        SUDO_PREFIX="sudo "
        echo "  权限: 将通过 sudo 提权（会提示输入密码）"
    else
        echo "[ERROR] 需要 root 权限，但系统里没有 sudo。" >&2
        echo "        请以 root 身份重跑: su -c 'sh $0 $PORT'" >&2
        exit 1
    fi
fi
echo ""

REMOVE_HINT=""
PERSIST_HINT=""

if have ufw; then
    UFW=$(resolve ufw)
    echo "  后端: ufw ($UFW)"
    if asroot "$UFW" status 2>/dev/null | grep -q "^Status: active"; then
        echo "  状态: 已启用"
    else
        echo "  状态: 未启用（规则先记下，等你 ufw enable 后生效）"
    fi
    if asroot "$UFW" status 2>/dev/null | grep -q "$PORT/tcp"; then
        echo "  已有 $PORT/tcp 放行规则，无需重复添加。"
    else
        echo "  即将执行: $SUDO_PREFIX$UFW allow $PORT/tcp"
        asroot "$UFW" allow "$PORT/tcp" || { echo "[ERROR] ufw 添加规则失败。" >&2; exit 1; }
    fi
    REMOVE_HINT="  sudo $UFW delete allow $PORT/tcp"
    PERSIST_HINT="  ufw 规则本身持久（存在 /etc/ufw），重启后仍有效。"

elif have firewall-cmd; then
    FW=$(resolve firewall-cmd)
    echo "  后端: firewalld ($FW)"
    if asroot "$FW" --state >/dev/null 2>&1; then
        if asroot "$FW" --query-port="$PORT/tcp" >/dev/null 2>&1; then
            echo "  已有 $PORT/tcp 放行规则，无需重复添加。"
        else
            echo "  即将执行: $SUDO_PREFIX$FW --add-port=$PORT/tcp --permanent"
            echo "            $SUDO_PREFIX$FW --reload"
            asroot "$FW" --add-port="$PORT/tcp" --permanent && asroot "$FW" --reload || { echo "[ERROR] firewalld 添加规则失败。" >&2; exit 1; }
        fi
    else
        echo "  状态: firewalld 未在运行，端口本来就不被它拦。"
    fi
    REMOVE_HINT="  sudo $FW --remove-port=$PORT/tcp --permanent && sudo $FW --reload"

elif have nft; then
    NFT=$(resolve nft)
    echo "  后端: nftables ($NFT)"
    if asroot "$NFT" list chain inet filter input >/dev/null 2>&1; then
        if asroot "$NFT" list chain inet filter input 2>/dev/null | grep -q "dport $PORT"; then
            echo "  已有 $PORT/tcp 放行规则，无需重复添加。"
        else
            echo "  即将执行: $SUDO_PREFIX$NFT insert rule inet filter input tcp dport $PORT accept"
            asroot "$NFT" insert rule inet filter input tcp dport "$PORT" accept || { echo "[ERROR] nftables 添加规则失败。" >&2; exit 1; }
            echo "  已插入 inet filter input 链。"
        fi
        REMOVE_HINT="  sudo $NFT -a list chain inet filter input | grep 'dport $PORT'   # 记下 handle 后执行下一行"
        REMOVE_HINT="$REMOVE_HINT
  sudo $NFT delete rule inet filter input handle <编号>"
    else
        echo "  没有现成的 inet filter input 链，改为建独立表 inet qoder-proxy"
        echo "  即将执行: $SUDO_PREFIX$NFT add table inet qoder-proxy"
        echo "            $SUDO_PREFIX$NFT add chain inet qoder-proxy input { type filter hook input priority -10 ; policy accept ; }"
        echo "            $SUDO_PREFIX$NFT add rule inet qoder-proxy input tcp dport $PORT accept"
        asroot "$NFT" add table inet qoder-proxy && asroot "$NFT" add chain inet qoder-proxy input '{ type filter hook input priority -10 ; policy accept ; }' && asroot "$NFT" add rule inet qoder-proxy input tcp dport "$PORT" accept || { echo "[ERROR] nftables 添加规则失败。" >&2; exit 1; }
        REMOVE_HINT="  sudo $NFT delete table inet qoder-proxy"
    fi
    PERSIST_HINT="  nft 规则默认只在内存里，重启即失效；要持久化请把规则写进 /etc/nftables.conf，"
    PERSIST_HINT="$PERSIST_HINT
  然后 systemctl enable --now nftables。"

elif have iptables; then
    IPT=$(resolve iptables)
    echo "  后端: iptables ($IPT)"
    if asroot "$IPT" -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
        echo "  已有 $PORT/tcp 放行规则，无需重复添加。"
    else
        echo "  即将执行: $SUDO_PREFIX$IPT -I INPUT -p tcp --dport $PORT -j ACCEPT"
        asroot "$IPT" -I INPUT -p tcp --dport "$PORT" -j ACCEPT || { echo "[ERROR] iptables 添加规则失败。" >&2; exit 1; }
    fi
    REMOVE_HINT="  sudo $IPT -D INPUT -p tcp --dport $PORT -j ACCEPT"
    PERSIST_HINT="  iptables 规则重启会丢；要持久化：sudo apt install iptables-persistent，然后用 sudo netfilter-persistent save 保存。"

else
    echo "  没有检测到 ufw / firewalld / nftables / iptables 任何一个前端。"
    echo ""
    echo "  这通常意味着两种可能："
    echo "    1) 本机确实没有防火墙在拦（Debian 桌面常见）——先在另一台设备上确认："
    echo "         curl -v http://<本机IP>:$PORT/health"
    echo "       能通就不必放行，连不上再往下看。"
    echo "    2) 装了但没在 PATH/常见位置里——最省事是补装 ufw："
    echo "         sudo apt install ufw && sudo ufw allow $PORT/tcp"
    echo "       或者直接建 nftables 规则："
    echo "         sudo nft add table inet qoder-proxy"
    echo "         sudo nft add chain inet qoder-proxy input '{ type filter hook input priority -10 ; policy accept ; }'"
    echo "         sudo nft add rule inet qoder-proxy input tcp dport $PORT accept"
    echo ""
    echo "  云主机另需在控制台安全组里放行 TCP $PORT。"
    echo ""
    if [ -t 0 ]; then
        printf '按回车键退出...'
        read -r _dummy
        echo ""
    fi
    exit 0
fi

echo ""
echo "完成。网关保持运行的前提下，其他设备现在应能访问："
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "$IP" ]; then
    echo "    http://$IP:$PORT/"
else
    echo "    http://<本机局域网IP>:$PORT/"
fi
echo ""
echo "要撤销这条放行规则："
echo "$REMOVE_HINT"
if [ -n "$PERSIST_HINT" ]; then
    echo ""
    echo "持久化说明："
    echo "$PERSIST_HINT"
fi
echo ""
# 等价 bat 的 pause：只在交互终端等回车
if [ -t 0 ]; then
    printf '按回车键退出...'
    read -r _dummy
    echo ""
fi
exit 0
