"""媒体 URL 解析策略链。spec §10。

按配置顺序尝试，第一个命中的即返回。**默认只启用 `nas_serve`** —— 这一条就能
覆盖全部需求，包括网盘：CloudDrive2 已把网盘挂成 NAS 本地路径，对后端就是普通
文件（spec §17）。

`direct_link` 是纯优化，默认关闭，且**每次请求现取、绝不缓存 URL**（spec §10：
阿里云盘直链约 15 分钟、OneDrive 约 1 小时过期）。缓存过的直链在用户真正想看
视频时才失效，那时候没有任何补救机会。这里的实现方式是根本不提供缓存位置：
resolver 是一个每次调用的 callable，没有任何存 URL 的字段。

⚠️ 启用 `direct_link` 的账号风险见 spec §10：**阿里云盘不要启用**（条款禁止
视频外链分发，且禁止账号多 IP 访问 —— 家人多台手机正好同时命中两条，封号
不可解除）。123云盘是唯一官方允许的。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

VIA_DIRECT_LINK = "direct_link"
VIA_NAS_SERVE = "nas_serve"
VIA_CUSTOM_PREFIX = "custom_prefix"

DEFAULT_STRATEGIES = (VIA_NAS_SERVE,)

# nas_path -> 绝对 URL，或 None 表示这个挂载点这次取不到直链（过期 / 未登录 /
# 会员失效）。取不到就落到下一条策略，不是错误。
DirectLinkFn = Callable[[str], str | None]


@dataclass(frozen=True)
class Resolved:
    url: str
    via: str
    supports_range: bool
    # `via == "direct_link"` 时 url 是绝对 URL，客户端直接用不拼前缀。
    # 这是 spec §7 里 "URL 一律返回相对路径" 的唯一例外，由 via 明确区分。
    absolute: bool


@dataclass(frozen=True)
class DirectLinkMount:
    """一个开启了直链的挂载点前缀。"""

    prefix: str
    resolver: DirectLinkFn
    # 直链是否支持 Range。网盘 CDN 基本都支持，但 OpenList 的 302 目标不一定，
    # 所以做成按挂载点配置而不是写死 True —— 客户端拿 supportsRange=False 会
    # 禁用 seek 并提示（spec §13），比 seek 到一半失败要好。
    supports_range: bool = True


@dataclass
class MediaResolver:
    strategies: tuple[str, ...] = DEFAULT_STRATEGIES
    mounts: tuple[DirectLinkMount, ...] = ()
    custom_prefix: str | None = None
    # 只为测试与诊断计数，不参与决策
    direct_link_calls: int = field(default=0, compare=False)

    def __post_init__(self) -> None:
        unknown = [
            s
            for s in self.strategies
            if s not in (VIA_DIRECT_LINK, VIA_NAS_SERVE, VIA_CUSTOM_PREFIX)
        ]
        if unknown:
            raise ValueError(f"未知的媒体解析策略：{unknown}")
        if not self.strategies:
            raise ValueError("媒体解析策略链不能为空")

    def resolve(self, asset: dict[str, Any]) -> Resolved:
        nas_path = str(asset["nas_path"])
        rel = f"/v1/asset/{asset['id']}/stream"
        for strategy in self.strategies:
            if strategy == VIA_DIRECT_LINK:
                for mount in self.mounts:
                    if not nas_path.startswith(mount.prefix):
                        continue
                    self.direct_link_calls += 1
                    url = mount.resolver(nas_path)
                    if url:
                        return Resolved(
                            url=url,
                            via=VIA_DIRECT_LINK,
                            supports_range=mount.supports_range,
                            absolute=True,
                        )
            elif strategy == VIA_NAS_SERVE:
                return Resolved(
                    url=rel, via=VIA_NAS_SERVE, supports_range=True, absolute=False
                )
            elif strategy == VIA_CUSTOM_PREFIX:
                if self.custom_prefix:
                    return Resolved(
                        url=self.custom_prefix.rstrip("/") + rel,
                        via=VIA_CUSTOM_PREFIX,
                        supports_range=True,
                        absolute=True,
                    )
        # 策略链全部未命中（例如只配了 custom_prefix 却没给前缀）。兜底回
        # nas_serve 而不是报错：本服务总能自己吐这个文件，返回 500 是没必要的
        # 自残。但要让调用方能看出这是兜底 —— via 仍是 nas_serve，因为流量
        # 路径确实是 NAS→手机，客户端不需要知道配置写错了。
        return Resolved(url=rel, via=VIA_NAS_SERVE, supports_range=True, absolute=False)
