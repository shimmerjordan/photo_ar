"""跨帧证据累积。

这组测试钉住的是「识别照片难」那件事的修法，而它的依据是**真机日志**
（`data/catalog.db` 的 `recognize_log`，1633 条）：194 条 `weak` 里有 22 条（11.3%）
的 top1 落在 30~39 —— 也就是「看到照片了，就差几分」，而那 22 条的 runner_up 全是
6~9，比值 3.3 倍以上。它们每一帧都被单独扔掉了。

⚠️ 这条路**新增**了一个误识别面，测试里有一条专门盯着它（见
`test_稳定误配到同一张仍然会被累积放行`）：单帧门槛 40 原本能挡住真实误识别
（`verify.MIN_INLIERS` 那段记录：34 条真实误识别 p95=36、**最大 39**），而累积把
30~39 这一段放进来了。挡住它的不是门槛而是「连续 + 比值」，所以**能稳定误配的那
一类挡不住**。代价未量，因此命中要带专门的 reason 进日志。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from photoar import verify  # noqa: E402
from photoar.streak import StreakTracker  # noqa: E402


def _results(*pairs: tuple[str, int]) -> list[verify.PairResult]:
    """(photo_id, inliers) → 一批候选分数。det 给 1.0（落在合法区间内）。"""
    return [
        verify.PairResult(photo_id=pid, inliers=n, det=1.0, ok=False)
        for pid, n in pairs
    ]


def _strong() -> list[verify.PairResult]:
    """一帧「看到了、就差几分」：35 分，第二名 8 分（真机日志里的典型形状）。"""
    return _results(("photo-a", 35), ("photo-b", 8))


def test_单独一帧不足以命中():
    t = StreakTracker()
    assert t.offer("tok", 0, _strong()) is None


def test_连续够数的帧累积成命中():
    t = StreakTracker(need=3)
    assert t.offer("tok", 0, _strong()) is None
    assert t.offer("tok", 400, _strong()) is None
    d = t.offer("tok", 800, _strong())
    assert d is not None
    assert d.matched
    assert d.photo_id == "photo-a"


def test_累积命中的_reason_可以和单帧命中区分开():
    # 真实代价没量过，所以必须能在 recognize_log 里把这条路挑出来单独统计。
    # 归成 "ok" 的话，这条路带来的误识别会混进单帧命中里，永远量不出来。
    t = StreakTracker(need=2)
    t.offer("tok", 0, _strong())
    d = t.offer("tok", 400, _strong())
    assert d is not None
    assert d.reason == StreakTracker.REASON
    assert d.reason != "ok"


def test_中途换了照片就重新数():
    t = StreakTracker(need=3)
    t.offer("tok", 0, _strong())
    t.offer("tok", 400, _results(("photo-z", 35), ("photo-b", 8)))
    # photo-a 的链断了，这一帧只是 photo-z 的第二帧
    assert t.offer("tok", 800, _strong()) is None


def test_低于软门槛的帧把链打断():
    # 这一帧说明「没看清」，不是「差一点」。忽略它而不打断的话，
    # 「举着手机晃过去偶尔扫到」也会被攒成命中。
    t = StreakTracker(need=3, soft_min=30)
    t.offer("tok", 0, _strong())
    t.offer("tok", 400, _results(("photo-a", 12), ("photo-b", 7)))
    assert t.offer("tok", 800, _strong()) is None


def test_比值不够的帧不算证据():
    # top1 和 top2 分不开时，「top1 是哪张」这件事本身就不可信 ——
    # 这正是 verify.decide 里 ambiguous 那一条的道理，累积不能绕过它。
    t = StreakTracker(need=2, streak_ratio=2.0)
    t.offer("tok", 0, _results(("photo-a", 35), ("photo-b", 30)))
    assert t.offer("tok", 400, _results(("photo-a", 35), ("photo-b", 30))) is None


def test_隔太久的两帧不算连续():
    # 客户端每 400ms 一帧。隔了好几秒说明用户已经转去看别的了，
    # 把那之前的证据接上等于跨场景累积。
    t = StreakTracker(need=2, window_ms=2000)
    t.offer("tok", 0, _strong())
    assert t.offer("tok", 5000, _strong()) is None


def test_不同客户端互不干扰():
    t = StreakTracker(need=2)
    t.offer("tok-1", 0, _strong())
    # 另一个客户端的同一张照片不该帮 tok-1 凑数
    assert t.offer("tok-2", 400, _strong()) is None


def test_命中之后链要清掉():
    # 不清的话紧接着的每一帧都会再命中一次，而每次命中都会让状态机重新装目标。
    t = StreakTracker(need=2)
    t.offer("tok", 0, _strong())
    assert t.offer("tok", 400, _strong()) is not None
    assert t.offer("tok", 800, _strong()) is None


def test_没有候选的帧不炸也不算证据():
    t = StreakTracker(need=2)
    assert t.offer("tok", 0, []) is None
    assert t.offer("tok", 400, _strong()) is None


def test_只有一个候选时按第二名为零算比值():
    # 库里只有一张照片时 runner_up 不存在。这时比值检验必须放行（无从混淆），
    # 否则单张照片的库永远走不到累积这条路。
    t = StreakTracker(need=2)
    t.offer("tok", 0, _results(("photo-a", 35)))
    assert t.offer("tok", 400, _results(("photo-a", 35))) is not None


def test_行列式不合法的帧不算证据():
    # 镜像/退化的单应说明这次几何拟合本身是废的，分数再高也不能当证据。
    t = StreakTracker(need=2)
    bad = [verify.PairResult(photo_id="photo-a", inliers=35, det=-1.0, ok=False)]
    t.offer("tok", 0, bad)
    assert t.offer("tok", 400, bad) is None


def test_按客户端数量设上限不让内存无限涨():
    # 每个 token 一条链，而 token 数量不受我们控制。
    t = StreakTracker(need=3, max_keys=4)
    for i in range(10):
        t.offer(f"tok-{i}", 0, _strong())
    assert t.tracked_keys() <= 4


def test_稳定误配到同一张仍然会被累积放行():
    """这条测试**不是**在验证正确行为，是在把已知代价钉成可执行的文档。

    单帧门槛 40 原本挡住了真实误识别（最大 39 分）。累积把 30~39 放进来之后，
    一张库外照片只要能**稳定**误配到库内同一张（Oxford5k 里「同一建筑的不同照片」
    就是这一类），就会被放行 —— 连续性和比值都拦不住它，因为它每一帧都真的很像。

    所以这条测试的作用是：哪天有人想收紧这条路，他能先看到「现在到底放行了什么」，
    而不是从日志里的一次误识别倒推。真实代价要靠 recognize_log 里
    `reason == StreakTracker.REASON` 那些记录去量。
    """
    t = StreakTracker(need=3)
    fp = _results(("photo-lookalike", 38), ("photo-other", 9))
    t.offer("tok", 0, fp)
    t.offer("tok", 400, fp)
    d = t.offer("tok", 800, fp)
    assert d is not None and d.photo_id == "photo-lookalike", (
        "这是已知代价，不是缺陷。改这条断言之前先读 docstring。"
    )


def test_每次调用可以覆盖需要的帧数():
    # 服务端那两个阈值是**热配置**（管理台上能改，不用重启），而这个对象是长生命周期
    # 的（链要跨请求存活）。构造时定死的话，改配置要么不生效、要么得重建对象而把
    # 攒了一半的链全丢掉。
    t = StreakTracker(need=5)
    assert t.offer("tok", 0, _strong(), need=2) is None
    assert t.offer("tok", 400, _strong(), need=2) is not None


def test_每次调用可以覆盖软门槛():
    t = StreakTracker(need=2, soft_min=30)
    # 把软门槛抬到帧分数之上 → 这一帧不再算证据
    assert t.offer("tok", 0, _strong(), soft_min=100) is None
    assert t.offer("tok", 400, _strong(), soft_min=100) is None


@pytest.mark.parametrize("need", [2, 3, 5])
def test_需要几帧就必须攒够几帧(need: int):
    t = StreakTracker(need=need)
    for i in range(need - 1):
        assert t.offer("tok", i * 400, _strong()) is None
    assert t.offer("tok", (need - 1) * 400, _strong()) is not None
