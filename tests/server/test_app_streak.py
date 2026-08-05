"""跨帧证据累积接在识别端点上。

纯逻辑在 `tests/test_streak.py`（`photoar.streak.StreakTracker`）。这里只钉接线，
而接线有两件事必须钉住：

1. 累积命中要走**与单帧命中完全相同**的后续路径 —— 尤其是授权检查。选「服务端累积」
   而不是「把 top1 回给客户端自己累积」的全部理由就是这个：weak 那一支不跑授权检查，
   把 photoId 回给客户端就是一次信息泄漏。如果接线绕过了 `_may_see`，那这个理由就
   白说了，而漏洞不会有任何症状。
2. 命中要能在历史里和单帧命中区分开。这条路新增的误识别面代价没量过，混进 "ok" 里
   就永远量不出来（见 `streak.py` 的模块 docstring）。

怎么造出「看到了但没过门槛」这种帧：把 `recog.min_inliers` 抬到够不到的值，让一个
本来能命中的帧变成 `weak`。这比去合成一张恰好 35 分的图可控得多 —— 帧的实际分数
（100 以上）仍然远高于累积的软门槛 30，所以走的正是要测的那条路。
"""

import cv2
import pytest


HIGH_BAR = 500  # 单帧门槛抬到这个值，任何真实帧都过不了


@pytest.fixture
def photo(env):
    """入库一张有纹理的照片，返回 (photoId, 能认出它的查询帧 jpeg)。"""
    img = env.textured(seed=31)
    ref = env.nas / "photos" / "streak.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(ref), img)
    pid = env.ingest_ok(ref)
    return pid, env.jpeg_of(img)


def _raise_bar(env, **extra):
    patch = {"recog.min_inliers": HIGH_BAR}
    patch.update(extra)
    assert env.patch_json("/v1/admin/config", patch).status == 200


def test_单帧过不了门槛时连续几帧累积成命中(env, photo):
    pid, frame = photo
    # 先确认这一帧本来是能命中的，否则下面测的可能只是「一直不命中」
    assert env.body_json(env.post_frame("/v1/recognize", frame))["matched"] is True

    _raise_bar(env, **{"recog.streak_need": 3})
    first = env.body_json(env.post_frame("/v1/recognize", frame))
    assert first["matched"] is False and first["reason"] == "weak"
    second = env.body_json(env.post_frame("/v1/recognize", frame))
    assert second["matched"] is False
    third = env.body_json(env.post_frame("/v1/recognize", frame))
    assert third["matched"] is True, "攒够 3 帧就该命中"
    assert third["photoId"] == pid


def test_累积命中在历史里能和单帧命中区分开(env, photo):
    pid, frame = photo
    _raise_bar(env, **{"recog.streak_need": 2})
    env.post_frame("/v1/recognize", frame)
    assert env.body_json(env.post_frame("/v1/recognize", frame))["matched"] is True

    entries = env.body_json(env.get("/v1/history"))["entries"]
    hit = next(e for e in entries if e.get("photoId") == pid)
    assert hit["reason"] == "streak", (
        "混进 'ok' 的话这条路带来的误识别永远量不出来"
    )


def test_把需要的帧数设成零就关掉这条路(env, photo):
    _, frame = photo
    _raise_bar(env, **{"recog.streak_need": 0})
    for _ in range(6):
        body = env.body_json(env.post_frame("/v1/recognize", frame))
        assert body["matched"] is False, "关掉之后不管发几帧都不该命中"


def test_低于软门槛的帧不参与累积(env, photo):
    _, frame = photo
    # 软门槛也抬到够不到 —— 此时每一帧都「没看清」，累积不该发生
    _raise_bar(env, **{"recog.streak_need": 2, "recog.streak_soft_min": HIGH_BAR})
    for _ in range(5):
        assert env.body_json(env.post_frame("/v1/recognize", frame))["matched"] is False


def test_累积命中照旧受授权约束(env, photo):
    """**安全测试。** 累积必须走与单帧命中同一条授权路径。

    绕过 `_may_see` 的话，一个没被授权这张照片的访客会通过累积拿到 photoId 和
    可播地址 —— 而单帧路径上他拿到的是 `forbidden`。这个漏洞在界面上没有任何症状。
    """
    pid, frame = photo
    _raise_bar(env, **{"recog.streak_need": 2})
    viewer = env.viewer(grant_all=False)
    env.post_frame("/v1/recognize", frame, headers=viewer.headers)
    body = env.body_json(env.post_frame("/v1/recognize", frame, headers=viewer.headers))
    assert body["matched"] is False
    assert body["reason"] == "forbidden", (
        f"累积绕过了授权检查，访客拿到了 {pid}"
    )


def test_不同客户端的帧不互相凑数(env, photo):
    pid, frame = photo
    _raise_bar(env, **{"recog.streak_need": 2})
    viewer = env.viewer(grant_all=True)
    # 管理员一帧 + 访客一帧 != 同一个客户端的两帧
    env.post_frame("/v1/recognize", frame)
    body = env.body_json(env.post_frame("/v1/recognize", frame, headers=viewer.headers))
    assert body["matched"] is False, "两个不同身份的帧不该被攒成一次命中"
