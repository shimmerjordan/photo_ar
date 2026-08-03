"""服务端预建的**整库多目标** `.imgdb`：端上离线识别的那个库。

架构上这个模块是一次收敛：服务器只做资源索引、传输、管理，识别与 AR 贴合全在
手机上。Android 侧的状态机本来就是端上优先的（`ScanController.onTracking` 在
SCANNING 状态下一报出目标就 `tryLocalHit`，不等服务端往返），缺的只是"端上那个
多图库是谁建的"：以前是端上拿 640px 缩略图现建，代价是 `addImage` 每张约 30ms
（ARCore 官方数字，200 张约 6 秒）、特征提自缩略图所以跟踪质量低一档
（`NoticeKind.LOCAL_HIT` 提示的就是这件事）、还受端上缓存条数上限约束。
预建之后手机只需要下一个文件、反序列化 10-20ms（官方数字，5MB 的库）。

## 为什么按**内容哈希**存，而不是按用户存一份

版本 = 一份规范化清单的 sha256，清单的每一行是 `photo_id|print_width_m|ref_sha256`
（照片 id 升序）。于是：

- 授权集相同的两个用户天然共用同一个文件。家里五口人都 grantAll 的话，磁盘上就
  是一个文件、CPU 上就是一次构建。按用户存的话是五份完全相同的字节，而且入库一张
  照片要重建五次。
- 任何一张参考图的内容变了、或者授权集变了，版本**自动**变。不需要任何一处
  "记得去 invalidate 一下" —— 而那种"记得"是这类缓存唯一真正的失效原因。
- ETag 直接用这个版本，客户端的 304 判断因此是精确的（而不是"文件 mtime 没变
  所以大概没变"）。

共用文件**不是**一个授权洞：接口上没有"给我这个版本"这样的参数 ——
`GET /v1/targets/db` 永远只发调用者自己那一套（版本由服务端按他的授权集算）。
版本号因此不是凭证，猜中一个也换不到任何东西。做成带 version 参数的形式会省掉
"每次重算一遍 plan"，代价是把一个内容哈希变成一把钥匙：谁抄到别人的版本号
（它出现在日志、ETag、代理缓存键里）就拿到了别人那套照片的全部特征。

## 版本号里**没有**什么，以及为什么

- **没有 title / fitMode / hasVideo**。它们是 manifest 里的元数据，不是 `.imgdb`
  的输入。放进版本号的后果是：改一个标题就让全体客户端重下一遍整库（几百 KB 到
  几 MB），而库里的每一个字节都没变。manifest 本身是 `no-store` 的、每次都现算，
  所以元数据的改动无论如何都立刻可见 —— 不需要版本号帮忙。
- **没有 arcoreimg 的版本**。换一个版本的二进制理论上会让产出的字节不同，而版本
  号不会变。这与既有的单目标 `.imgdb`（入库时建一次、之后永不重建）是同一个约定：
  换工具就得手工清一次 `data_dir/targets/`。写在这里是为了那一天有人能查到。
- **没有实时的文件内容哈希**。用的是 catalog 里记着的 `asset.sha256`，它由
  `integrity` 那条路维护（解析前校验 + 每周一次的 `verify` 全量）。请求路径上重新
  哈希 1000 张原图是几百 MB 到几 GB 的读盘，而 `/v1/ping` 会走到这条路。代价是
  "有人在 NAS 上直接换掉了一张参考图、而 verify 还没跑过"这段时间里版本号不变 ——
  与 `ref_stale` 的可见性完全一样，不是新的坑。

## 一致性：manifest 与 db 为什么不可能配错

危险的形态是"db 里有的照片 manifest 里没有" —— 端上认出来一个目标，却找不到它的
元数据（printWidthM / fitMode），于是视频要么不播要么贴错。这里从两个方向堵死：

1. **版本 → 照片集合是一个函数**。`<version>.imgdb` 这个文件的内容由 version 完全
   决定（同一个 version 的清单逐行相同），manifest 的条目也由同一个 plan 算出来。
   所以任意一对 version 相同的 (manifest, db) 都是自洽的 —— 即使客户端先取
   manifest、中间管理员入库了十张、几分钟后才取 db：那时 db 的 ETag 会是新版本，
   客户端一眼看得出对不上，重取 manifest 即可。**不需要**服务端把两次请求绑在
   一个事务里（那也做不到）。
2. **manifest 是构建集合的超集**。构建时读不到的参考图会被跳过（见
   `_build`），所以 db ⊆ manifest 恒成立，"db 有而 manifest 没有"这个方向不可能
   发生。反方向（manifest 多一条）的后果是那张照片在端上永远不命中，自动落回
   服务端 `/v1/recognize` —— 用户完全看不出来，且下一次版本变化就自愈。

## 构建为什么在后台线程里，请求为什么直接 503

`arcoreimg build-db` 的真实耗时**没有测量过**（闭源二进制、不在仓库里，见
`quality.build_multi_target_db` 的 docstring）。它可能是几秒，也可能是几十秒。
同步在请求里等它的两个后果都不能接受：一是这个请求穿过 Cloudflare 隧道时可能撞上
代理的响应超时（那边是百秒量级的硬限制），客户端拿到的是一个与"服务器挂了"无法
区分的 5xx；二是几个客户端同时来的话，要么排队要么并发建同一个库。

所以构建永远在后台线程里跑，请求立刻拿到 `Building` → HTTP 503 + `Retry-After`。
"还没建好"因此是一个**正常状态**，而不是一次失败。
"""

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import quality
from .appconfig import AppConfig
from .auth import Principal, photo_filter
from .config import ServerConfig
from .db import Catalog, effective_fit_mode, ref_aspect

# 503 里给客户端的 `Retry-After`（秒）。
#
# 这个数是**猜的**，因为真实建库耗时未测量。选它的依据只是两边的代价不对称：猜小了
# 客户端多问几次（一次 GET，几百字节），猜大了用户举着手机对着照片干等。所以宁可
# 偏小。
RETRY_AFTER_S = 5

# `data_dir/targets/` 下保留最近几个版本。
#
# 必须清理：每入库一张照片就产生一个新版本，而每个版本是一个几百 KB 到几 MB 的
# 文件 —— 不清理就是无界增长，且增长速度正比于"管理员今天入了多少张"。
#
# 为什么留 3 个而不是 1 个：删掉一个版本的那一刻，可能正好有客户端在下载它
# （`Response.file` 是在发送时才打开的）。Linux 上删一个已打开的文件是安全的，但
# "刚 resolve 完、还没打开文件"这个窗口是真的存在 —— 踩到的表现是一次 404
# （`_static_file` 判的是文件在不在），客户端重新取一遍 manifest + db 就好。
# 留 3 个意味着要在那几毫秒里连出三个新版本（= 连入三张照片）才会踩到，所以刻意
# 不为它加一条分支：那条分支永远不会被任何测试真的执行到。
KEEP_VERSIONS = 3

# 一次构建失败之后，多久内不再重试。
#
# 不能立刻重试：失败最可能的原因是 arcoreimg 不在或者磁盘满，这两个都不会自己好。
# 而客户端拿到错误会按自己的节奏重试 —— 每次重试起一个后台线程去跑一个必然失败的
# 建库，就是一个线程炸弹。也不能永久记住：磁盘满是会被解决的，而"解决完还得重启
# 服务"这件事没有任何地方会写着。
FAILURE_COOLDOWN_S = 30.0

# 残留的临时文件多久算过期。进程被 kill 在 rename 之前时会留下一个 `.tmp-*`，
# 它永远不会有人来收 —— 而它和成品一样大。
TMP_STALE_S = 3600.0


class BuildFailed(RuntimeError):
    """整库构建失败。带上版本号与原文，HTTP 层原样转给运维。

    这是**服务端故障**而不是"还没建好"：`Building` 是正常状态，这个不是。两者共用
    一个状态码的话，运维在管理台上看到的就只是"一直在建"。
    """

    def __init__(self, version: str, reason: str) -> None:
        super().__init__(f"整库目标 {version} 构建失败：{reason}")
        self.version = version
        self.reason = reason


@dataclass(frozen=True)
class TargetSet:
    """一套建好的整库目标。"""

    # 内容哈希，截断到 16 个 hex（64 bit）。
    #
    # 够用的理由：它不是安全边界（谁能拿到哪些照片仍然由 ACL 决定，版本号不是
    # 凭证，猜中一个也换不到任何东西），唯一的要求是"内容不同 → 版本不同"在一个
    # 部署的生命周期内成立。一个部署里的版本数量级是"入库次数"，几万也算多了，
    # 而 64 bit 空间里 10^4 个值的碰撞概率约 3e-12。全长 64 hex 换不到任何东西，
    # 只会让每一行日志、每一个 ETag、每一条 URL 都长 48 个字符。
    version: str
    # 这一套**计划**包含的照片。构建时读不到的参考图不在 `.imgdb` 里，但仍然在这个
    # 元组里（也仍然在 manifest 里）—— 理由见模块 docstring 的"一致性"一节。
    photo_ids: tuple[str, ...]
    # 因为容量上限被排除在外的张数（不含参考图缺失被跳过的）。
    overflow: int
    path: Path
    bytes: int
    # 构建这个版本时跳过的照片（参考图读不到）。
    #
    # 由 `TargetStore` 在**进程内**记着，所以缓存命中也拿得到；重启之后丢
    # （变成空元组，而磁盘上那个文件仍然是对的）。刻意不存 sidecar 文件：那是又一个
    # 会和现实不一致的状态，而它唯一的用途是排查 —— 真正的记录是构建当时那行日志。
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class Building:
    """这个版本正在建。HTTP 层回 503 + `Retry-After`。"""

    version: str
    retry_after_s: int = RETRY_AFTER_S


@dataclass(frozen=True)
class _Entry:
    """一张照片在整库里需要的全部东西：建库的三列 + manifest 的元数据。"""

    photo_id: str
    ref_path: str
    print_width_m: float
    ref_sha256: str
    title: str | None
    fit_mode: str
    has_video: bool
    ref_aspect: float | None


@dataclass(frozen=True)
class _Plan:
    version: str
    entries: tuple[_Entry, ...]  # 按 photo_id 升序（= 算版本号用的那个顺序）
    overflow: int


@dataclass
class _Failure:
    reason: str
    at: float = field(default_factory=time.monotonic)


class TargetStore:
    """按内容哈希缓存的整库目标。

    比任务里那个签名多一个 `config`（热配置）。不是可选参数：manifest 里的
    `fitMode` 在照片自己没指定时要跟随全局默认（`video.fit_mode`），而那个值只在
    热配置里。省掉它就只能在这里写一个字面量默认值，后果是同一张照片经
    `/v1/recognize` 命中与经离线 manifest 命中拿到不同的 fitMode —— 表现是"同一张
    照片有时候视频铺满、有时候留边"，两边的代码各自看起来都对。
    """

    def __init__(
        self,
        cfg: ServerConfig,
        catalog: Catalog,
        config: AppConfig,
        *,
        max_targets: int = quality.MAX_TARGETS_PER_DB,
        keep_versions: int = KEEP_VERSIONS,
    ) -> None:
        if max_targets < 1:
            raise ValueError(f"max_targets 必须为正数，收到 {max_targets!r}")
        if max_targets > quality.MAX_TARGETS_PER_DB:
            # 提前拒绝，而不是等构建时 `TooManyTargets`。配了 2000 的话，那个失败
            # 会发生在**每一次**请求上，而配置本身看起来只是一个数字。
            raise ValueError(
                f"max_targets 不能超过 ARCore 的库容量上限 "
                f"{quality.MAX_TARGETS_PER_DB}，收到 {max_targets}"
            )
        self._cfg = cfg
        self._catalog = catalog
        self._config = config
        self._max_targets = int(max_targets)
        self._keep = int(keep_versions)
        # 只保护下面那三个进程内状态，**不**保护构建本身：构建可能要几十秒，把它放在
        # 锁里等于让另一个授权集的用户也一起等。
        self._lock = threading.Lock()
        self._building: set[str] = set()
        self._failed: dict[str, _Failure] = {}
        # version → 那次构建跳过的 photo_id。见 `TargetSet.skipped`。
        self._skipped: dict[str, tuple[str, ...]] = {}

    # ---- 对外 ----

    def resolve(self, principal: Principal) -> TargetSet | Building:
        """这个人可见的那一套整库目标。没建过就**在后台**开始建并返回 `Building`。"""
        return self._resolve(self._plan(principal))

    def manifest(self, principal: Principal) -> dict[str, Any]:
        """端上要的元数据。字段名与 `/v1/recognize` 命中响应里的同名字段语义一致。

        **顺手把构建踢起来**（不等它）。客户端的顺序一定是"先 manifest 再 db"，所以
        这一下预热是免费的时间：不预热的话，第一次 db 一定是 503，用户要多等一个
        `Retry-After` 周期。多调 manifest 也不会多建 —— 同一个版本只有一个构建
        （`_resolve` 里的 `_building` 守卫）。
        """
        plan = self._plan(principal)
        try:
            got: TargetSet | Building | None = self._resolve(plan)
        except BuildFailed:
            # manifest **不为构建失败而失败**：它的内容是元数据，与那个文件建不建得
            # 出来无关，而客户端还要靠它显示"这套库有多少张"。失败已经在构建当时打进
            # 日志了，并且会在真正需要它的那个请求（`GET /v1/targets/db`）上以 500
            # 露出来。在这里也 500 的话，一个 arcoreimg 挂了的部署连元数据都取不到。
            got = None
        return {
            "version": plan.version,
            "count": len(plan.entries),
            "overflow": plan.overflow,
            "maxTargets": self._max_targets,
            # 客户端据此决定"现在去拿 db 会不会 503"。少这一个字段的话，503 对它
            # 就是一次需要猜原因的失败。
            "building": isinstance(got, Building),
            "targets": [self._describe(e) for e in plan.entries],
        }

    def status(self, principal: Principal) -> dict[str, Any]:
        """`/v1/ping` 上那四个字段。

        **不触发构建**：ping 是客户端在每次网络变化时对四个 endpoint 并行探的，
        让它顺手启动一次几十秒的建库是把一个"通不通"的探测变成一个副作用。
        """
        plan = self._plan(principal)
        return {
            "targetsVersion": plan.version,
            "targetsCount": len(plan.entries),
            "targetsOverflow": plan.overflow,
            "targetsBuilding": self.is_building(plan.version),
        }

    def is_building(self, version: str) -> bool:
        with self._lock:
            return version in self._building

    def path_for(self, version: str) -> Path:
        return self._cfg.targets_dir / f"{version}.imgdb"

    # ---- 选照片 / 算版本 ----

    def _plan(self, principal: Principal) -> _Plan:
        """这个人此刻应该拿到哪一套。纯读，无副作用。

        选哪些：**该用户被授权的**那些（admin / grantAll 的人取全部）。判据绕
        `auth.photo_filter`，那里是"谁能看全部"这条策略的唯一实现处 —— 在这里直接
        写 `is_admin or grant_all` 的话，以后多一种"能看全部"的条件就会有一处漏掉，
        而漏掉的方向是把照片发给错的人（这里发出去的是**特征**，不是原图，但一个
        人不该知道另一个人的照片存在）。

        超过容量上限时取 `created_at` 最新的 N 张。这个规则可以接受，理由很具体：
        端上没命中不是一个错误状态 —— `ScanController` 在 SCANNING 下会继续把帧发去
        `/v1/recognize`，那条路能认出全库任何一张。也就是说被截掉的照片只是**慢一
        点**（多一次网络往返），不是"扫不出来"，而且不需要任何额外逻辑去兜。取最新的
        则是因为"刚入库的那张"几乎一定是马上要拿去扫的那张。

        参考图 `asset.missing` 的照片直接不进这一套：它的特征已经无从谈起，硬塞进
        清单只会让整次建库失败（一张照片的文件被挪走不该让所有人的离线识别都坏掉）。
        这不是静默丢弃 —— missing 这个事实由 `integrity` 维护、在 `/v1/photo/*` 上
        以 `refMissing` 露出来，而这里的 `count` 也会比库里的张数少。
        """
        rows = self._catalog.list_photo_targets(user_id=photo_filter(principal))
        # 一次读，供这一批全部行用：AppConfig 有 2 秒的进程内缓存，但"读一次"这件事
        # 本身就该只发生一次 —— 否则同一个 manifest 里前后两条的 fitMode 可能来自
        # 两个不同的配置快照（管理员正好在这中间点了保存）。
        default_fit = str(self._config.get("video.fit_mode"))
        usable = [r for r in rows if not r["ref_missing"]]
        # rows 已经是 created_at DESC（见 `Catalog.list_photo_targets`），所以切片
        # 就是"最新的 N 张"。
        selected = usable[: self._max_targets]
        entries = tuple(
            sorted(
                (self._entry_of(r, default_fit) for r in selected),
                key=lambda e: e.photo_id,
            )
        )
        return _Plan(
            version=_version_of(entries),
            entries=entries,
            overflow=len(usable) - len(selected),
        )

    @staticmethod
    def _entry_of(row: dict[str, Any], default_fit: str) -> _Entry:
        return _Entry(
            photo_id=str(row["id"]),
            ref_path=str(row["ref_path"]),
            print_width_m=float(row["print_width_m"]),
            ref_sha256=str(row["ref_sha256"]),
            title=row["title"],
            fit_mode=effective_fit_mode(row, default_fit),
            has_video=row["video_asset_id"] is not None,
            ref_aspect=ref_aspect(row["ref_width_px"], row["ref_height_px"]),
        )

    @staticmethod
    def _describe(e: _Entry) -> dict[str, Any]:
        """manifest 里的一条。

        字段名与 `/v1/recognize` 命中响应逐个对齐（`photoId` / `printWidthM` /
        `refAspect` / `fitMode` / `mediaUrl` / `imgdbUrl`），`title` 与 `hasVideo`
        与 `/v1/photos` 对齐。客户端解析命中元数据的代码是共用的一份，多一个名字
        就是多一条只在离线路径上才走到的分支。

        `refAspect` 与 `title` 可能是 null（尺寸探不到 / 没起标题），键**总是在**：
        与 `/v1/photos` 一致，也让客户端不必区分"字段缺失"和"值是空"。
        （`/v1/recognize` 那边 refAspect 是缺失而不是 null，那是既有行为，两边都
        是"没有值"这一个语义。）

        URL 是相对路径（app.py 的模块 docstring：服务端不知道客户端此刻走哪条
        通道）。这两条路径字面量与 app.py 的路由表重复了一次 —— 反向依赖会成环，
        所以由 `tests/server/test_targets.py` 里那条"manifest 给的 URL 真的取得到"
        的测试来钉住它们一致。
        """
        return {
            "photoId": e.photo_id,
            "printWidthM": e.print_width_m,
            "refAspect": e.ref_aspect,
            "fitMode": e.fit_mode,
            "title": e.title,
            "hasVideo": e.has_video,
            "mediaUrl": f"/v1/photo/{e.photo_id}/media",
            "imgdbUrl": f"/v1/photo/{e.photo_id}/imgdb",
        }

    # ---- 构建 ----

    def _resolve(self, plan: _Plan) -> TargetSet | Building:
        path = self.path_for(plan.version)
        if not plan.entries:
            # 一张照片都没有（新部署，或者一个还没被授权任何照片的 viewer）。这是
            # 一个**正常**状态，不该有文件、也不该有构建：0 目标的 `.imgdb` 建不出
            # 有意义的东西（见 `quality.build_multi_target_db` 里那个 ValueError）。
            # 版本号照样是一个确定的值（空清单的哈希），所以 ETag 语义仍然成立。
            return TargetSet(
                version=plan.version,
                photo_ids=(),
                overflow=plan.overflow,
                path=path,
                bytes=0,
            )
        with self._lock:
            # 先看文件、再看"正在建"：构建线程是先 rename 再从 `_building` 里摘掉
            # 自己的，所以这个顺序下不存在"文件已经好了却回 503"的窗口。
            if path.is_file():
                return self._set_of(plan, path)
            failure = self._failed.get(plan.version)
            if failure is not None:
                if time.monotonic() - failure.at < FAILURE_COOLDOWN_S:
                    raise BuildFailed(plan.version, failure.reason)
                del self._failed[plan.version]  # 冷却过了，允许再试一次
            if plan.version in self._building:
                return Building(plan.version)
            self._building.add(plan.version)
            self._start_build(plan, path)
        return Building(plan.version)

    def _set_of(self, plan: _Plan, path: Path) -> TargetSet:
        """**必须在持有 `_lock` 时调用**（读 `_skipped`）。"""
        return TargetSet(
            version=plan.version,
            photo_ids=tuple(e.photo_id for e in plan.entries),
            overflow=plan.overflow,
            path=path,
            bytes=path.stat().st_size,
            skipped=self._skipped.get(plan.version, ()),
        )

    def _start_build(self, plan: _Plan, path: Path) -> None:
        """起一个后台守护线程去建。**必须在持有 `_lock` 时调用**（调用方已经把
        version 记进了 `_building`，两件事之间不能有窗口）。

        每次一个新线程而不是一个线程池：并发的构建数天然被 `_building` 限制在
        "不同授权集的数量"上（家庭规模就是个位数），而线程池要么多一个永驻线程、
        要么多一个"队列满了怎么办"的问题。守护线程是因为服务退出时一次没建完的
        构建没有任何价值 —— 残留的 `.tmp-*` 由 `_prune` 收。
        """
        threading.Thread(
            target=self._build_and_publish,
            args=(plan, path),
            name=f"targets-build-{plan.version}",
            daemon=True,
        ).start()

    def _build_and_publish(self, plan: _Plan, path: Path) -> None:
        try:
            skipped = self._build(plan, path)
        except Exception as exc:  # noqa: BLE001 —— 后台线程，异常没有别的去处
            # `_build` 自己抛的 `BuildFailed` 已经是一句人话，别再套一层类名 ——
            # 那句话是要原样显示给运维的。
            reason = (
                exc.reason
                if isinstance(exc, BuildFailed)
                else f"{type(exc).__name__}: {exc}"
            )
            with self._lock:
                self._failed[plan.version] = _Failure(reason)
                self._building.discard(plan.version)
            print(
                f"[photoar] ❌ 整库目标 {plan.version} 构建失败：{reason}\n"
                f"[photoar]    端上离线识别会一直用不了（在线识别不受影响）。"
                f"{FAILURE_COOLDOWN_S:.0f} 秒内不会重试。",
                flush=True,
            )
            return
        with self._lock:
            # 先记跳过的、再摘掉"正在建" —— 反过来的话，紧跟着的那次 resolve 会拿到
            # 一个 `skipped=()` 的 TargetSet，而它其实少了几张。
            self._skipped[plan.version] = skipped
            self._building.discard(plan.version)
        if skipped:
            print(
                f"[photoar] ⚠️ 整库目标 {plan.version} 跳过了 {len(skipped)} 张"
                f"（参考图读不到）：{', '.join(skipped[:5])}"
                f"{' …' if len(skipped) > 5 else ''}。"
                f"这些照片在端上扫不出来，仍然能走服务端识别。",
                flush=True,
            )
        self._prune()

    def _build(self, plan: _Plan, path: Path) -> tuple[str, ...]:
        """真正建库。返回被跳过的 photo_id。

        参考图读不到就跳过这一张，**不让整次建库失败**：一个人把一张照片挪走，
        不该让全家人的离线识别一起坏掉。判据是 `is_file()` 而不是 catalog 里的
        `missing` 标记 —— 那个标记由 `integrity` 维护，可能还没跑过。

        路径里的字面 '|' 不用在这里防：入库时就被拒了（`ingest` 的 `bad_ref_path`），
        而 `asset.nas_path` 之后不会变。

        先写临时文件再 `os.replace`：`arcoreimg` 是一个外部进程，它被 kill、磁盘满、
        或者只写了一半就退出的时候，目标路径上不能出现一个"存在但不完整"的文件 ——
        `_resolve` 判的是 `path.is_file()`，一个半成品会被当成成品发给每一个客户端，
        而 ARCore 加载失败的表现是"离线识别静默失效"。
        """
        items: list[tuple[str, str, float]] = []
        skipped: list[str] = []
        for e in plan.entries:
            if Path(e.ref_path).is_file():
                items.append((e.photo_id, e.ref_path, e.print_width_m))
            else:
                skipped.append(e.photo_id)
        if not items:
            # 一张都读不到（整个挂载点没了之类）。这不是"跳过几张"，是真的建不出来。
            raise BuildFailed(
                plan.version,
                f"{len(plan.entries)} 张照片的参考图一张都读不到，"
                f"整库建不出来（挂载点掉了？）",
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        )
        t0 = time.perf_counter()
        try:
            result = quality.build_multi_target_db(
                items, tmp, arcoreimg=self._cfg.arcoreimg
            )
            os.replace(tmp, path)
        finally:
            # 失败时别把半成品留在磁盘上（`_prune` 也会收，但那是一小时之后）。
            Path(tmp).unlink(missing_ok=True)

        # arcoreimg 剔掉的那些必须算进 skipped，manifest 才不会声称它们在库里。
        #
        # ⚠️ 这一条是实测出来的，不是防御性编程：`build-db` 只要有一张图提不出足够
        # 关键点就**整体失败**（Oxford5k 1000 张里有 17 张这样），所以
        # `build_multi_target_db` 会剔掉坏图重试。剔掉之后如果 manifest 还按原计划
        # 报，端上就会拿到"manifest 说有、库里其实没有"的照片 —— 表现是这几张永远
        # 扫不出来，而且没有任何提示。
        skipped = list(skipped) + [name for name, _ in result.dropped]
        if result.dropped:
            print(
                f"[photoar] 整库目标 {plan.version}：arcoreimg 剔掉 "
                f"{len(result.dropped)} 张（关键点不足），它们不会进 manifest",
                flush=True,
            )
        print(
            f"[photoar] 整库目标 {plan.version} 建好了："
            f"{len(result.names)} 个目标、{result.bytes} 字节、"
            f"{int((time.perf_counter() - t0) * 1000)}ms",
            flush=True,
        )
        return tuple(skipped)

    def _prune(self) -> None:
        """只留最近 `keep_versions` 个版本，外加收掉过期的临时文件。

        按 mtime 排序而不是按"我知道哪些版本还有用"：后者需要一份"谁在用哪个版本"
        的登记，而客户端不登记。mtime 最新的那几个恰好是最可能被要的
        （刚建的那个一定在里面），这就够了 —— 删错的代价只是某个客户端重下一次。

        正在建的版本不会被删：它此刻只有 `.tmp-*`，而成品那一档只 glob
        `*.imgdb`。刚建好的那个也不会：它的 mtime 最新。
        """
        d = self._cfg.targets_dir
        # mtime 在 glob 之后**立刻**读进来，而不是当排序的 key 现读：两个构建同时
        # 结束时会有两次 prune 并行，一次删掉的文件会让另一次的 `stat()` 抛
        # FileNotFoundError —— 而那个异常发生在一个后台线程的最后一步，表现是
        # "偶尔有一次构建的日志后面跟着一个栈"，而库其实是好的。
        stamped: list[tuple[float, Path]] = []
        try:
            candidates = list(d.glob("*.imgdb"))
        except OSError:
            return
        for p in candidates:
            try:
                stamped.append((p.stat().st_mtime, p))
            except OSError:
                continue
        stamped.sort(reverse=True)
        files = [p for _, p in stamped]
        for old in files[self._keep:]:
            old.unlink(missing_ok=True)
        # 跟着文件一起收 `_skipped`，否则一个长期运行的进程会把每一个历史版本的
        # 跳过记录都攒着 —— 每入库一张就多一条，永远不减。
        alive = {p.stem for p in files[: self._keep]}
        with self._lock:
            for version in [v for v in self._skipped if v not in alive]:
                del self._skipped[version]
        now = time.time()
        for tmp in d.glob("*.imgdb.tmp-*"):
            try:
                if now - tmp.stat().st_mtime > TMP_STALE_S:
                    tmp.unlink(missing_ok=True)
            except OSError:
                continue


def _version_of(entries: tuple[_Entry, ...]) -> str:
    """规范化清单的 sha256（截断到 16 hex，够用的理由见 `TargetSet.version`）。

    清单里每行三样东西，缺一不可：
    - `photo_id` —— 它就是 `.imgdb` 里那个目标名（与端上 `LocalTargetDb` 一致，
      客户端靠它把 ARCore 报出来的目标接回照片元数据）。
    - `print_width_m` —— 已知时物理宽度会**烘进** `.imgdb`（清单第三列）；为 0 时
      表示未知，那一行不写宽度列，改由 ARCore 在端上自己量。两种情况都必须进版本
      哈希：它决定了库文件的内容，改了而版本号不变的话，端上会拿旧库贴视频，表现
      是照片认得出来而视频尺寸不对。**"从填了 30cm 改成不填"也是一次真实的变更**，
      0 与 0.3 哈希不同正好覆盖它。
    - `ref_sha256` —— 参考图的内容。换了内容就是换了特征。

    entries 必须已按 photo_id 升序（`_plan` 保证）。顺序参与哈希，所以"同一个集合
    两种顺序算出两个版本"必须不可能发生 —— 那会让客户端在两次请求之间无限重下。
    """
    text = "\n".join(
        f"{e.photo_id}|{e.print_width_m:.6f}|{e.ref_sha256}" for e in entries
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
