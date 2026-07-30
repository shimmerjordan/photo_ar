#!/usr/bin/env python3
"""挑一对更快的 Cloudflare Tunnel 边缘地址，输出可直接粘进 hosts 的两行。

## 为什么需要这个

`cloudflared` 是**它主动去连** Cloudflare 边缘，连的是
`region1.v2.argotunnel.com` / `region2.v2.argotunnel.com` 的 **7844 端口**
（QUIC 用 UDP/7844，http2 用 TCP/7844）。这两个域名各只解析出 20 个地址
（2026-07-30 实测：region1 → `198.41.192.0/24` + `2606:4700:a0::/48`，
region2 → `198.41.200.0/24` + `2606:4700:a8::/48`），而**同一个 /24 里不同地址
到你这条线路的质量能差出一个量级** —— 国内线路尤其明显。DNS 给的是哪 20 个纯属
运气，运气不好时表现为「隧道能连上，但请求时快时慢、偶尔 502」。

这个脚本把两个网段整段扫一遍 7844，按握手耗时排序，挑出最快的地址。
把结果写进 NAS 的 `/etc/hosts` 就等于给 `cloudflared` 换了个入口。

## 两个坑，都会让你以为「优选没用」

1. **两个地址必须落在不同网段**（一个 192.x、一个 200.x）。
   `cloudflared` 默认建 4 条连接、要求分布在两个 region；两行都填同一段
   会让 Tunnel 状态变成 **Degraded**，比不优选还差。
2. **测的必须是 7844，不是 80/443。** Cloudflare 的边缘并非每个地址都在 7844
   上提供服务，拿 443 的延迟去挑地址，挑出来的可能根本连不上隧道 —— 而
   `cloudflared` 会重试到超时再换，表现为启动慢、日志刷 `retrying`。

## 用法

    python3 tools/cf_edge_probe.py                 # 扫 v4
    python3 tools/cf_edge_probe.py --v6            # 有 IPv6 出口时加上
    python3 tools/cf_edge_probe.py --rounds 3      # 多测几轮取最小值

只用标准库，NAS 上有 python3 就能跑（QNAP 的 Container Station 里也可以直接
`docker exec` 进容器跑，容器里那个 python3 一样够用）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import socket
import time

# 2026-07-30 用 dig 实测过的两段。**不要写死到代码里就不管了**：Cloudflare 换过
# 网段的话下面 --check 会发现 —— 它拿域名当前的解析结果比对这两段。
REGIONS = {
    "region1.v2.argotunnel.com": ("198.41.192.0/24", "2606:4700:a0::/48"),
    "region2.v2.argotunnel.com": ("198.41.200.0/24", "2606:4700:a8::/48"),
}
PORT = 7844
# v6 的 /48 有 2^80 个地址，整段扫是不可能的。实测解析出来的都落在 ::1 - ::20
# 这一小段（Cloudflare 就是这么分配的），所以只扫这个范围。
V6_TAIL = 0x30


def probe(ip: str, timeout: float) -> float | None:
    """回 TCP 握手耗时（毫秒），连不上回 None。"""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.connect((ip, PORT))
        return (time.perf_counter() - t0) * 1000
    except OSError:
        return None
    finally:
        s.close()


def candidates(cidr: str, v6: bool) -> list[str]:
    net = ipaddress.ip_network(cidr)
    if not v6:
        return [str(h) for h in net.hosts()]
    base = int(net.network_address)
    return [str(ipaddress.IPv6Address(base + i)) for i in range(1, V6_TAIL + 1)]


def resolved(host: str, v6: bool) -> set[str]:
    fam = socket.AF_INET6 if v6 else socket.AF_INET
    try:
        info = socket.getaddrinfo(host, PORT, fam, socket.SOCK_STREAM)
    except OSError:
        return set()
    return {i[4][0] for i in info}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--v6", action="store_true", help="测 IPv6（需要本机有 v6 出口）")
    ap.add_argument("--rounds", type=int, default=2, help="每个地址测几轮，取最小值")
    ap.add_argument("--timeout", type=float, default=1.5, help="单次握手超时（秒）")
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--top", type=int, default=5, help="每个 region 列前几名")
    args = ap.parse_args()

    print(f"探测 {'IPv6' if args.v6 else 'IPv4'} 的 {PORT} 端口，"
          f"{args.rounds} 轮取最小值\n")

    best: dict[str, list[tuple[float, str]]] = {}
    for host, (v4cidr, v6cidr) in REGIONS.items():
        cidr = v6cidr if args.v6 else v4cidr
        ips = candidates(cidr, args.v6)
        live = resolved(host, args.v6)
        if live and not any(ipaddress.ip_address(i) in ipaddress.ip_network(cidr)
                            for i in live):
            print(f"⚠️  {host} 现在解析到 {sorted(live)[:3]}…，不在脚本里写的 "
                  f"{cidr} 内 —— Cloudflare 换过网段了，改 REGIONS 再跑")

        results: dict[str, float] = {}
        for _ in range(args.rounds):
            with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
                for ip, ms in zip(ips, pool.map(
                        lambda i: probe(i, args.timeout), ips)):
                    if ms is not None:
                        results[ip] = min(results.get(ip, 9e9), ms)
        ranked = sorted((ms, ip) for ip, ms in results.items())
        best[host] = ranked
        print(f"{host}  {cidr}：{len(ips)} 个地址里 {len(ranked)} 个在 "
              f"{PORT} 上应答")
        for ms, ip in ranked[:args.top]:
            mark = "  ← DNS 也给了这个" if ip in live else ""
            print(f"    {ms:7.1f} ms  {ip}{mark}")
        if ranked and live:
            dns_best = min((ms for ms, ip in ranked if ip in live), default=None)
            if dns_best:
                print(f"    DNS 给的 20 个里最快 {dns_best:.1f} ms，"
                      f"整段最快 {ranked[0][0]:.1f} ms")
        print()

    lines = [(host, r[0][1]) for host, r in best.items() if r]
    if len(lines) < 2:
        print("两个 region 至少各要挑出一个地址才有意义 —— 现在不够，"
              "大概是出口被限制了 UDP/TCP 7844，或者根本没有 v6 出口。")
        return 1
    print("写进 NAS 的 /etc/hosts（两行必须来自不同网段，否则 Tunnel 会 Degraded）：")
    for host, ip in lines:
        print(f"{ip}\t{host}")
    print("\n改完 hosts 要重启 cloudflared 才生效；生效后用 "
          "`cloudflared tunnel info <名字>` 看连接落在哪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
