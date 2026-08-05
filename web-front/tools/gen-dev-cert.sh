#!/usr/bin/env bash
# 给"手机在局域网 / Tailscale 里自测"生成证书。
#
# 为什么需要：`getUserMedia` 只在**安全上下文**里存在，而局域网的 `http://192.168.x.x`
# 与 Tailscale 的 `http://100.x.x.x` 都不算。自测时手机前面没有隧道那一层。
#
# ## 三条路，按省事程度排
#
# 1. **Tailscale 真证书**（最省事，手机上什么都不用装）
#    先去 https://login.tailscale.com/admin/dns 页面底部打开 `HTTPS Certificates`，然后
#        tailscale cert <机器>.<tailnet>.ts.net
#    拿到的是 Let's Encrypt 证书，零警告、自动续期。**优先走这条。**
#
# 2. **Android Chrome 的白名单**（零证书）
#    `chrome://flags/#unsafely-treat-insecure-origin-as-secure` 里填
#    `http://<IP>:8964` → Enabled → 重启浏览器。http 也被当安全上下文。iOS 没有等价功能。
#
# 3. **本脚本：自建 CA + 服务器证书**（这条的意义是"装一次，之后永久信任"）
#    上一版直接出一张自签**叶子**证书，于是每次访问都要在警告页盲打 `thisisunsafe`。
#    改成先建一个本地 CA、再用它签服务器证书：把 CA 装进手机一次，之后这台机器上签的
#    所有证书都被信任 —— 换 IP、加域名都不用再装。
#
# 用法：
#   tools/gen-dev-cert.sh                       # 自动带上本机所有 IPv4 与 tailnet 域名
#   tools/gen-dev-cert.sh 192.168.1.10 foo.lan  # 额外再加几个名字
#
# 产物（都在 local/，已被 .gitignore 忽略）：
#   ca.crt   ← **装到手机上的那个**。服务起来后手机浏览器打开 /ca.crt 就能下
#   ca.key   ← CA 私钥，别外传
#   dev.crt / dev.key  ← 服务器用，由 CA 签
set -euo pipefail

OUT_DIR="${OUT_DIR:-$(cd "$(dirname "$0")/.." && pwd)/local}"
mkdir -p "$OUT_DIR"
CA_CRT="$OUT_DIR/ca.crt"
CA_KEY="$OUT_DIR/ca.key"
CRT="$OUT_DIR/dev.crt"
KEY="$OUT_DIR/dev.key"

# ── 收集 SAN ──────────────────────────────────────────────────────────
# **这一步不能省**：现代浏览器对没有 SAN 的证书连"高级 → 继续"都不给，只报
# ERR_CERT_COMMON_NAME_INVALID —— 那看起来像证书生成失败，不像少了个字段。
names=("localhost")
ips=("127.0.0.1")

while read -r ip; do
  [[ -n "$ip" && "$ip" != "127.0.0.1" ]] && ips+=("$ip")
done < <(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]}' || true)

ts_name=""
if command -v tailscale >/dev/null 2>&1; then
  ts_name=$(tailscale status --json 2>/dev/null | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -1 | sed 's/\.$//')
elif command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx tailscale; then
  ts_name=$(docker exec tailscale tailscale status --json 2>/dev/null | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -1 | sed 's/\.$//')
fi
[[ -n "$ts_name" ]] && names+=("$ts_name")

for arg in "$@"; do
  if [[ "$arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then ips+=("$arg"); else names+=("$arg"); fi
done

san=""
i=1; for n in "${names[@]}"; do san+="DNS.$i:$n,"; i=$((i+1)); done
i=1; for p in "${ips[@]}"; do san+="IP.$i:$p,"; i=$((i+1)); done
san="${san%,}"

# ── CA：只建一次 ──────────────────────────────────────────────────────
# 已经有就复用。重新建会让手机上装过的那份作废 —— 而"为什么又不信任了"是最难查的
# 那类问题（证书看起来一切正常，只是签它的 CA 换了）。
if [[ -f "$CA_CRT" && -f "$CA_KEY" ]]; then
  echo "复用已有 CA: $CA_CRT"
else
  echo "新建本地 CA…"
  # ⚠️ **不要**在这里 `-addext basicConstraints`：`req -x509` 自己就会加一条
  #    `basicConstraints=critical,CA:TRUE`（实测 OpenSSL 1.1.1 与 3.x 都会）。再加一条
  #    的结果是证书里出现**两个**同名扩展 —— 那是不合法的 X.509，`openssl verify` 直接
  #    报 `unable to get local issuer certificate`，而手机上的表现是"装了 CA 还是不信任"。
  #    第一版就是这么错的。只补默认没给的 keyUsage。
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$CA_KEY" -out "$CA_CRT" \
    -subj "/CN=photo-ar web-front dev CA/O=photo-ar" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  chmod 600 "$CA_KEY"
fi

# ── 服务器证书：由 CA 签 ──────────────────────────────────────────────
# -days 825：Safari/iOS 拒绝有效期超过 825 天的**服务器**证书（CA 不受这条限制，
# 所以上面给了 10 年）。这条限制在 iOS 上是硬的，超了直接不信任、连绕过都没有。
echo "SAN: $san"
openssl req -newkey rsa:2048 -nodes -keyout "$KEY" -out "$OUT_DIR/dev.csr" \
  -subj "/CN=${names[1]:-localhost}" 2>/dev/null
openssl x509 -req -in "$OUT_DIR/dev.csr" -CA "$CA_CRT" -CAkey "$CA_KEY" \
  -CAcreateserial -out "$CRT" -days 825 -sha256 \
  -extfile <(printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$san") 2>/dev/null
rm -f "$OUT_DIR/dev.csr"
chmod 600 "$KEY"

echo
echo "CA 证书 (装到手机): $CA_CRT"
echo "服务器证书:         $CRT"
echo "服务器私钥:         $KEY"
echo
echo "起服务："
echo "  WEBFRONT_TLS_CERT=$CRT WEBFRONT_TLS_KEY=$KEY \\"
echo "  PHOTOAR_UPSTREAM=http://127.0.0.1:8964 PHOTOAR_LIBRARY=../data/library \\"
echo "  node server/index.js"
echo
echo "把 CA 装到手机（只需一次）："
echo "  手机浏览器打开  https://<本机IP>:8964/ca.crt"
echo "    Android: 下载后 设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书"
echo "             （会提示"网络可能被监控"，那是装了用户 CA 的正常提示）"
echo "    iOS:     下载后 设置 → 已下载描述文件 → 安装，**然后必须再去**"
echo "             设置 → 通用 → 关于本机 → 证书信任设置 里为它打开完全信任"
echo "             （少了后半步 Safari 仍然不认，这是 iOS 上最常见的坑）"
