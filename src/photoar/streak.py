"""跨帧证据累积：把「连续几帧都指向同一张」当成一次命中。

## 为什么要有它

单帧判定（`verify.decide`）在分数分布的形状上是有代价的，而这个代价是**量出来的**，
不是推的。两份数据：

1. **真机日志**（`recognize_log`，1633 条）：194 条 `weak` 里 22 条（11.3%）的 top1
   落在 30~39 —— 「看到照片了，就差几分」。而这 22 条的 runner_up 全是 6~9，比值
   3.3 倍以上，也就是 **top1 是毫无争议的第一名，只是没到绝对门槛 40**。它们每一帧
   都被单独扔掉了。
2. **bench/simcam**（同一张真实婚礼照，20 个随机视角，见 bench/README.md 里
   「repeat=3 是虚假的精度」）：占比 0.4 那一档分数是 39~150，跨度 4 倍，而门槛是
   40 —— 有 1/20 落在 39，差 1 分。这与 `verify.MIN_INLIERS` 那段自己写的真阳性
   p1=9／p5=53 一致：**门槛 40 天生要吃掉 1%~5% 的真阳性**。

结论不是「40 定错了」—— 那个数是拟合在 34 个真实误识别事件上的，动它要重跑
`bench/threshold_scan.py`。结论是**每一帧都在独立赌一次视角运气**，而连续几帧的
一致性是单帧判定完全没用上的一份免费证据。

## 这条路新增的误识别面，以及为什么仍然做

必须写在最前面：**单帧门槛 40 原本挡住了真实误识别**（`verify.MIN_INLIERS` 记录的
34 条真实误识别 p95=36、**最大 39**），而这里把 30~39 放进来了。挡住它的不是门槛，
是「连续 [STREAK_NEED] 帧 + 每帧比值 ≥ [STREAK_RATIO]」。

所以挡不住的恰好是**能稳定误配的那一类**：一张库外照片与库内某张几何上真的相似
（`verify` 里说的「Oxford5k 的语料属性：同一被摄物体的不同照片」就是这一类），
它每一帧都真的很像，连续性和比值都拦不住。`tests/test_streak.py` 里有一条测试专门
把这个已知代价钉住。

仍然做，因为两侧的量级不对称：漏检是**每次扫描都在发生**的（真机日志 11.3%），
而稳定误配要求库里恰好有一张几何上高度相似的照片 —— 而这个项目的入库路径本来就
有去重闸门（`dedup`）在挡这一类。

**代价未量**，所以命中带专门的 [StreakTracker.REASON] 进 `recognize_log`：不这么做
的话，这条路带来的误识别会混进单帧命中里，永远量不出来。要量就查
`select * from recognize_log where reason = 'streak'`。

## 为什么状态在服务端而不是客户端

客户端累积要把「未命中时的最佳猜测」回给客户端，而照片是分权的（服务端识别路径上
有 `forbidden` 分支）—— weak 那一支目前不跑授权检查，直接回 photoId 就是一次信息
泄漏。放服务端则授权检查照旧只在**真的命中**时做一次，没有新增的暴露面。

代价是服务端从无状态变成有一个内存字典。可以接受：它是纯缓存，重启丢了最多少一次
累积（下一次扫描 1.2 秒内又攒回来），不落盘、不进数据库。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass

from .verify import DET_MAX, DET_MIN, Decision, PairResult

#: 进入累积的**软**门槛。低于它的帧说明「没看清」而不是「差一点」，不算证据。
#:
#: 30 的依据是真机日志里那 22 条的下沿（30~39 那一段）。**它落在真实误识别的区间
#: 之内**（最大 39），这是明知故犯的取舍，理由见模块 docstring 那一节。
STREAK_SOFT_MIN_INLIERS = 30

#: 要连续几帧。
#:
#: 3 而不是 2：客户端每 400ms 一帧，3 帧是 1.2 秒 —— 对「举着手机等视频出来」这件事
#: 仍然是瞬间，而它把「偶尔一帧巧合」压掉了一个数量级。往下调到 2 会让这条路更容易
#: 触发，也更容易被巧合触发；往上调到 5 就是 2 秒，用户会先以为没认出来。
STREAK_NEED = 3

#: 累积路径上的比值门槛，**比单帧的 `verify.RATIO`(1.5) 严**。
#:
#: 2.0 的依据是真机那 22 条的实际形状：top1 30~39 对 runner_up 6~9，比值 3.3 倍以上，
#: 所以 2.0 一条都不会挡掉。而它挡住的是「top1 和 top2 分不开」那一类 —— 那种情况下
#: 「top1 是哪张」本身就不可信，累积不能绕过 `verify.decide` 里 ambiguous 的道理。
STREAK_RATIO = 2.0

#: 两帧之间最多隔多久还算「连续」。
#:
#: 客户端每 400ms 一帧（`ScanController.FRAME_INTERVAL_MS`），2 秒容得下 5 帧，
#: 足够容忍一两次抓帧失败或网络抖动。隔得更久说明用户已经转去看别的了，把那之前的
#: 证据接上等于跨场景累积。
STREAK_WINDOW_MS = 2_000

#: 最多同时跟踪多少个客户端。超了按最久未用淘汰。
#:
#: 每个 token 一条链，而 token 数量不受我们控制（婚礼现场几十个宾客同时扫）。
#: 一条链只有 [STREAK_NEED] 个元素，256 条链是几十 KB —— 上限存在是为了让内存占用
#: 有个说得出的上界，不是因为它紧。
STREAK_MAX_KEYS = 256


@dataclass(frozen=True)
class _Frame:
    photo_id: str
    inliers: int
    at_ms: int


class StreakTracker:
    """按客户端累积「连续几帧都是同一张」。**有状态，非线程安全。**

    调用方只在**单帧判定没通过**时把候选分数交进来（[offer]），拿到非 None 就当一次
    命中处理 —— 走与单帧命中完全相同的授权与响应路径。

    线程安全交给调用方：服务端的识别路径是单线程处理一个请求的，而这个对象上的每次
    操作都是几个字典读写。真要并发，外面加一把锁比在这里做细粒度同步简单得多，也
    不会有「锁住的和判定用的不是同一份状态」那类问题。
    """

    #: 累积命中的 reason。**必须与单帧命中的 "ok" 不同**，理由见模块 docstring。
    REASON = "streak"

    def __init__(
        self,
        *,
        need: int = STREAK_NEED,
        soft_min: int = STREAK_SOFT_MIN_INLIERS,
        streak_ratio: float = STREAK_RATIO,
        window_ms: int = STREAK_WINDOW_MS,
        max_keys: int = STREAK_MAX_KEYS,
    ) -> None:
        self._need = int(need)
        self._soft_min = int(soft_min)
        self._ratio = float(streak_ratio)
        self._window_ms = int(window_ms)
        self._max_keys = int(max_keys)
        # OrderedDict 当 LRU：命中/更新时 move_to_end，超上限从头 popitem。
        self._chains: OrderedDict[str, deque[_Frame]] = OrderedDict()

    def tracked_keys(self) -> int:
        """当前跟踪着几个客户端。给测试与运维用。"""
        return len(self._chains)

    def offer(
        self,
        key: str,
        now_ms: int,
        results: list[PairResult],
        *,
        need: int | None = None,
        soft_min: int | None = None,
    ) -> Decision | None:
        """把**未命中**那一帧的候选分数交进来。

        @param key 客户端标识。调用方给什么都行，只要同一个客户端稳定是同一个值 ——
            服务端那边传的是 token 的哈希，不是 token 本身（这个对象会被打进诊断输出）。
        @param now_ms 这一帧的墙上时间，只用来算两帧间隔。
        @param results `recognizer.verify_candidates` 的原样输出，不必排序。
        @param need 这一次要求几帧，覆盖构造时的值。服务端那两个阈值是**热配置**
            （管理台上能改、不用重启），而这个对象是长生命周期的（链要跨请求存活）。
            构造时定死的话，改配置要么不生效、要么得重建对象而把攒了一半的链全丢掉。
        @param soft_min 同上，这一次的软门槛。
        @return 攒够了就是一个 `matched=True` 的 [Decision]，reason 是 [REASON]；
            否则 None。
        """
        want = self._need if need is None else int(need)
        bar = self._soft_min if soft_min is None else int(soft_min)
        frame = self._evidence(now_ms, results, bar)
        if frame is None:
            # 这一帧不算证据 —— 链要**断掉**而不是忽略。忽略的话「举着手机晃过去
            # 偶尔扫到」也会被攒成命中，而那不是「用户在看这张照片」。
            self._chains.pop(key, None)
            return None

        chain = self._chains.get(key)
        if chain is None:
            # **不设 maxlen**：要求的帧数可以逐次不同（热配置），而 maxlen 是建 deque
            # 时定死的。链在攒够时就被删掉，所以它的长度天然不超过 `want`。
            chain = deque()
            self._chains[key] = chain
        self._chains.move_to_end(key)
        self._evict()

        last = chain[-1] if chain else None
        if last is not None and (
            last.photo_id != frame.photo_id
            or now_ms - last.at_ms > self._window_ms
            # 时间倒流（换了机器、NTP 校时）当成断链：负的间隔算不出「连续」。
            or now_ms < last.at_ms
        ):
            chain.clear()
        chain.append(frame)

        if len(chain) < want:
            return None
        # 攒够了。链必须清掉 —— 不清的话紧接着的每一帧都会再命中一次，而每次命中
        # 都会让客户端状态机重新装一遍目标。
        del self._chains[key]
        return Decision(
            matched=True,
            photo_id=frame.photo_id,
            inliers=frame.inliers,
            reason=self.REASON,
        )

    def _evidence(
        self, now_ms: int, results: list[PairResult], soft_min: int
    ) -> _Frame | None:
        """这一帧算不算一份证据。算就返回它，不算返回 None。"""
        if not results:
            return None
        ranked = sorted(results, key=lambda r: -r.inliers)
        top1 = ranked[0]
        if top1.inliers < soft_min:
            return None
        # 行列式照旧要在合法区间里：镜像/退化的单应说明这次几何拟合本身是废的，
        # 分数再高也不能当证据。这里刻意复用 verify 的那两个常量而不是另立一对 ——
        # 两边判的是同一件事，分叉了不会报错，只会让两条路的口径悄悄不一样。
        if not (DET_MIN <= top1.det <= DET_MAX):
            return None
        runner_up = ranked[1].inliers if len(ranked) > 1 else 0
        # 只有一个候选时 runner_up 是 0，比值检验自动放行 —— 库里只有一张照片时
        # 无从混淆，卡住它等于让单张照片的库永远走不到这条路。
        if top1.inliers < self._ratio * runner_up:
            return None
        return _Frame(photo_id=top1.photo_id, inliers=top1.inliers, at_ms=int(now_ms))

    def _evict(self) -> None:
        while len(self._chains) > self._max_keys:
            self._chains.popitem(last=False)
