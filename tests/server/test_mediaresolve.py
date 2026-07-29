"""spec §10 的策略链。重点是"直链绝不缓存"这条硬约束。"""

import pytest

from photoar.server import mediaresolve as M

ASSET = {"id": "a" * 32, "nas_path": "/share/CloudDrive/aliyun/v.mp4", "bytes": 100}


def test_default_is_nas_serve_only():
    """默认只走本服务转发。CloudDrive2 已把网盘挂成本地路径，这一条覆盖全部
    需求（spec §17），直链纯属优化。"""
    r = M.MediaResolver().resolve(ASSET)
    assert (r.via, r.url, r.absolute, r.supports_range) == (
        M.VIA_NAS_SERVE,
        f"/v1/asset/{ASSET['id']}/stream",
        False,
        True,
    )


def test_nas_serve_url_is_relative():
    """URL 一律相对（spec §7）：服务端不知道客户端此刻走 LAN 还是隧道，
    返回绝对地址会把客户端锁死在一条通道上。"""
    assert not M.MediaResolver().resolve(ASSET).url.startswith("http")


def test_direct_link_wins_when_prefix_matches():
    calls = []

    def resolver(path):
        calls.append(path)
        return "https://cdn.example.com/x.mp4?token=1"

    r = M.MediaResolver(
        strategies=(M.VIA_DIRECT_LINK, M.VIA_NAS_SERVE),
        mounts=(M.DirectLinkMount("/share/CloudDrive/aliyun", resolver),),
    ).resolve(ASSET)
    assert r.via == M.VIA_DIRECT_LINK
    assert r.absolute and r.url.startswith("https://")
    assert calls == ["/share/CloudDrive/aliyun/v.mp4"]


def test_direct_link_is_never_cached():
    """每次 resolve 都必须重新取。阿里云盘直链约 15 分钟过期（spec §10），
    缓存过的直链会在用户真正想看视频时才失效，那时没有任何补救机会。
    """
    n = [0]

    def resolver(path):
        n[0] += 1
        return f"https://cdn.example.com/x.mp4?t={n[0]}"

    r = M.MediaResolver(
        strategies=(M.VIA_DIRECT_LINK,),
        mounts=(M.DirectLinkMount("/share/CloudDrive", resolver),),
    )
    urls = {r.resolve(ASSET).url for _ in range(3)}
    assert n[0] == 3, "直链被缓存了"
    assert len(urls) == 3


def test_direct_link_failure_falls_through_to_nas_serve():
    """取不到直链（未登录 / 会员失效 / 网盘限流）不是错误，落到下一条策略。"""
    r = M.MediaResolver(
        strategies=(M.VIA_DIRECT_LINK, M.VIA_NAS_SERVE),
        mounts=(M.DirectLinkMount("/share/CloudDrive", lambda p: None),),
    ).resolve(ASSET)
    assert r.via == M.VIA_NAS_SERVE


def test_direct_link_prefix_not_matching_falls_through():
    r = M.MediaResolver(
        strategies=(M.VIA_DIRECT_LINK, M.VIA_NAS_SERVE),
        mounts=(M.DirectLinkMount("/share/OtherMount", lambda p: "https://x/y"),),
    ).resolve(ASSET)
    assert r.via == M.VIA_NAS_SERVE


def test_mount_can_declare_range_unsupported():
    """OpenList 的 302 目标不一定支持 Range。客户端拿 supportsRange=False 会
    禁用 seek 并提示（spec §13），比 seek 到一半失败好。"""
    r = M.MediaResolver(
        strategies=(M.VIA_DIRECT_LINK,),
        mounts=(
            M.DirectLinkMount("/share", lambda p: "https://x/y", supports_range=False),
        ),
    ).resolve(ASSET)
    assert r.via == M.VIA_DIRECT_LINK and not r.supports_range


def test_custom_prefix_produces_absolute_url():
    r = M.MediaResolver(
        strategies=(M.VIA_CUSTOM_PREFIX,), custom_prefix="https://media.example.com/"
    ).resolve(ASSET)
    assert r.via == M.VIA_CUSTOM_PREFIX
    assert r.url == f"https://media.example.com/v1/asset/{ASSET['id']}/stream"
    assert r.absolute


def test_custom_prefix_without_prefix_falls_back_instead_of_500():
    """配置写错（启用了 custom_prefix 却没给前缀）时兜底回 nas_serve。

    本服务总能自己吐这个文件，为一个配置笔误返回 500 是没必要的自残。
    """
    r = M.MediaResolver(strategies=(M.VIA_CUSTOM_PREFIX,)).resolve(ASSET)
    assert r.via == M.VIA_NAS_SERVE and not r.absolute


def test_unknown_strategy_is_rejected_at_construction():
    """配置错误必须在启动时就炸，不能等到用户点播放。"""
    with pytest.raises(ValueError):
        M.MediaResolver(strategies=("magic",))


def test_empty_strategy_chain_is_rejected():
    with pytest.raises(ValueError):
        M.MediaResolver(strategies=())
