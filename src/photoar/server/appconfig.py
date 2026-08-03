"""热配置：运行时能改的那些参数，存在 `app_config` 表里。

## 什么放这里，什么留在 ServerConfig

分界线是"**改它需不需要重新决定进程启动时做过的事**"：

- 留在 `ServerConfig` / 环境变量：数据目录、监听端口、词汇树路径、白名单根目录、
  运维 token、ffmpeg/arcoreimg 路径。这些在启动时就已经被用掉了（目录建好了、
  端口 bind 了、vocab 加载进内存了），运行时改一个变量不会让已经发生的事回退，
  只会让"配置显示的"与"实际生效的"分叉 —— 而那种分叉查起来极其费时间。
- 放这里：阈值、闸门开关、贴图方式、会话时长。这些每次用到时都会重新读一遍。

## needs_restart 是一句需要接线才成立的承诺

`needs_restart=False` 的字段意味着"读取方每次用的时候来 AppConfig 取当前值"。
本模块**无法**保证这件事：如果识别路径继续 `from ..verify import MIN_INLIERS`
直接用模块常量，那么在管理台上改 `recog.min_inliers` 会显示成功、也确实写进了
库，但识别行为一点变化都没有。接线由调用方负责（HTTP 层/识别路径），这里只负责
"值是什么"以及"改它要不要重启"。

## 默认值从代码常量取，不另写字面量

`recog.min_inliers` 的默认值就是 `photoar.verify.MIN_INLIERS`。这样"默认值"永远
等于"代码里那个经过标定的值"（那个 40 背后有 29740 次查询的实测依据，写在
verify.py 的注释里）。抄一份字面量到这里的后果是：以后有人按新语料重新标定并改了
verify.py，热配置的默认值还停在旧数字上，而"我没改过配置"的用户跑的是旧阈值。

## 缓存

每个读请求都查一次 SQL 是可以工作的（家庭规模），但识别请求在扫描时是每秒好几
次、每次要读好几个 key，所以加一个很短的进程内缓存。TTL 取 2 秒：管理台改完刷新
一下页面就能看到，而识别路径上连续几十次读只落一次 SQL。TTL 可注入是为了测试能把
它设成 0（"每次都重新读"）而不必 sleep 真实时间。
"""

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .. import backend as recog_backend
from .. import quality, recognizer, verify
from . import auth, db, framedump

# 进程内缓存的默认 TTL（秒）。
CACHE_TTL_S = 2.0

KIND_BOOL = "bool"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_STR = "str"
KIND_ENUM = "enum"


class ConfigRejected(ValueError):
    """patch 的输入不合法。HTTP 层可以整类映射到 400。"""


class BadConfigKey(ConfigRejected):
    """不认识的 key。

    刻意不忽略未知 key：管理台把 `recog.minInliers`（驼峰）写错成这样时，静默
    忽略的表现是"保存成功但值没变"，而用户会以为是缓存问题反复重试。
    """


class BadConfigValue(ConfigRejected):
    """类型或取值范围不对。"""


@dataclass(frozen=True)
class Field:
    """一个热配置字段的完整声明。

    显式的字段表（而不是"库里有什么就是什么"）是这里唯一可行的做法：管理台要能
    在**还没有人改过任何配置**的时候把表单画出来（标签、范围、默认值、哪些改了
    要重启），这些信息只能来自代码。
    """

    key: str
    kind: str
    default: Any
    label: str  # 管理台表单的字段名
    help: str  # 一句话说明改它会发生什么
    needs_restart: bool = False
    minimum: float | None = None  # int/float 的闭区间下界
    maximum: float | None = None  # int/float 的闭区间上界
    choices: tuple[str, ...] = ()  # enum 的取值集合


FIELDS: tuple[Field, ...] = (
    Field(
        key="recog.backend",
        kind=KIND_ENUM,
        default=recog_backend.ORB,
        choices=recog_backend.NAMES,
        label="识别后端",
        help=(
            "ORB 是二值描述子、纯 CPU；XFeat 是 64 维浮点描述子 + ONNX 推理，更准但更慢。"
            "换后端必须重启：词汇树、描述子库的 slot 布局（每条记录多少字节）和 ONNX "
            "会话都是启动时按后端定下来的，而且两个后端的库文件互不兼容 —— 换完要重建全库索引。"
        ),
        needs_restart=True,
    ),
    Field(
        key="recog.min_inliers",
        kind=KIND_INT,
        default=verify.MIN_INLIERS,
        minimum=1,
        maximum=500,
        label="命中所需最少内点数",
        help=(
            f"默认 {verify.MIN_INLIERS} 是实测标定值（依据见 verify.py 的注释：真实误识别的"
            "最大内点数是 39，库内真阳性的 p5 是 69）。调低会开始出现"
            "「扫 A 播 B」的误识别，调高会让命中率跌破 95%。"
            "下界给到 1 是为了现场排查（「到底是特征提不出来还是判定太严」）能临时放宽，"
            "不是说 1 是个可用的值。"
        ),
    ),
    Field(
        key="recog.ratio",
        kind=KIND_FLOAT,
        default=verify.RATIO,
        minimum=1.0,
        maximum=10.0,
        label="第一名/第二名内点数比值下限",
        help=(
            f"默认 {verify.RATIO}。第一名不到第二名的这个倍数就判「分不清」，宁可不播。"
            "填 1.0 等于**关掉**这条判定（第一名恒不小于第二名），"
            "而它正是挡住近重复照片互相顶掉的那道判据。"
        ),
    ),
    Field(
        key="recog.top_k",
        kind=KIND_INT,
        default=recognizer.TOP_K,
        minimum=1,
        maximum=200,
        label="粗排候选数",
        help=(
            f"默认 {recognizer.TOP_K}。词汇树粗排取前 N 个候选送去做几何校验。"
            "调大提高召回但每次识别要多跑几次 RANSAC（延迟线性增长）；"
            "调小会让「排在第 21 名的正确答案」永久漏检。"
        ),
    ),
    Field(
        key="ingest.quality_gate",
        kind=KIND_BOOL,
        default=True,
        label="入库时检查图像质量",
        help=(
            "开启时质量分低于下面那个阈值的照片会被拒绝入库。关掉它照片能入库，"
            "但 ARCore 跟踪会明显抖动 —— 而这个后果要等到有人举着手机扫的时候才看得到，"
            "那时已经忘了是因为关过这个开关。"
        ),
    ),
    Field(
        key="ingest.min_quality_score",
        kind=KIND_INT,
        default=quality.MIN_QUALITY_SCORE,
        minimum=0,
        maximum=100,
        label="最低图像质量分",
        help=(
            f"arcoreimg 给出的 0-100 分，默认 {quality.MIN_QUALITY_SCORE}。"
            "只在上面那个开关打开时起作用。"
        ),
    ),
    Field(
        key="ingest.dedup_gate",
        kind=KIND_BOOL,
        default=True,
        label="入库时检查近重复",
        help=(
            "开启时与库内已有照片过于相似的新照片会被拒（409，并列出冲突的是哪几张）。"
            "关掉它两张近重复照片都能入库，代价是它们会互相判成「分不清」，"
            "**两张都永久扫不出来**，而现象是「识别器坏了」。"
        ),
    ),
    Field(
        key="video.fit_mode",
        kind=KIND_ENUM,
        default=db.FIT_FILL,
        choices=db.FIT_MODES,
        label="视频贴合方式",
        help=(
            "fill=居中裁切填满照片区域（画面边缘会被切掉）；"
            "fit=完整放进去、四周留边。默认 fill：留边意味着在实体照片上盖一圈黑边，"
            "在 AR 里看起来像是没对齐，而裁掉一点边缘几乎没人会注意。"
            "单张照片可以在 photo.fit_mode 上单独覆盖，那一列为 NULL 时才用这个全局值。"
        ),
    ),
    Field(
        key="session.viewer_days",
        kind=KIND_INT,
        default=auth.VIEWER_TTL_DAYS,
        minimum=1,
        maximum=365,
        label="访客会话有效期（天）",
        help=(
            f"默认 {auth.VIEWER_TTL_DAYS} 天。改它需要重启，两个原因："
            "Auth 的 TTL 是构造时传进去的；而且**已经签发的会话不受影响** —— "
            "过期时刻在登录那一刻就算死写进库了，改这个值只影响之后的登录。"
        ),
        needs_restart=True,
    ),
    Field(
        key="session.admin_hours",
        kind=KIND_INT,
        default=auth.ADMIN_TTL_HOURS,
        minimum=1,
        maximum=720,
        label="管理员会话有效期（小时）",
        help=(
            f"默认 {auth.ADMIN_TTL_HOURS} 小时，比访客短得多：管理员能改配置、"
            "能删用户（不可撤销）、能看全库。理由与重启要求同上。"
        ),
        needs_restart=True,
    ),
    Field(
        key="debug.dump_frames",
        kind=KIND_BOOL,
        default=False,
        label="留下识别用的帧（排查用）",
        help=(
            f"开了之后每次识别都把那一帧原样存进 `{framedump.DIR_NAME}/`，文件名带"
            "命中与否和内点数，供离线重放（`bench/replay_frames.py`）。"
            "这是「对着照片扫了没反应」唯一能查下去的办法：日志里的 inliers=6 说不出"
            "是帧糊了、拍歪了、还是拍的不是入过库的那张，而帧本身能说。"
            f"查完请关掉 —— 开着每 400ms 落一个 ~50KB 文件，而且存的是别人家的照片。"
            f"最多留 {framedump.MAX_FILES} 个，超了自动删最旧的（防止忘关把盘写满）。"
        ),
    ),
)

_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}

_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


def _reject(field: Field, value: Any, why: str) -> BadConfigValue:
    return BadConfigValue(f"{field.key} 的值不合法（{why}）：{value!r}")


def _check_range(field: Field, number: float, value: Any) -> None:
    if field.minimum is not None and number < field.minimum:
        raise _reject(field, value, f"不能小于 {field.minimum}")
    if field.maximum is not None and number > field.maximum:
        raise _reject(field, value, f"不能大于 {field.maximum}")


def coerce(field: Field, value: Any) -> Any:
    """把管理台丢过来的东西转成这个字段的规范类型，不合法就抛 `BadConfigValue`。

    为什么要"转"而不是只"验"：值可能来自 JSON（`40`、`true`）、也可能来自 HTML
    表单（`"40"`、`"on"`）。只验类型的话，同一个界面在两种提交方式下一个能用一个
    不能用；只转不验的话，`int("abc")` 的 ValueError 会变成 500。

    宽容的边界划在"**书写形式**不同"上，不划在"值不同"上：接受 `"40"`，但不接受
    `40.5` 当整数（悄悄截断成 40 会让用户以为界面没保存成功），也不接受 `2` 当
    布尔（那更像是填错了字段）。
    """
    kind = field.kind
    if kind == KIND_BOOL:
        # bool 必须排在 int 前面判：Python 里 `isinstance(True, int)` 是 True，
        # 反过来写的话 True 会被当成整数 1 一路走下去。
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in _TRUE_WORDS:
                return True
            if text in _FALSE_WORDS:
                return False
        raise _reject(field, value, "要 true / false")

    if kind == KIND_INT:
        if isinstance(value, bool):
            raise _reject(field, value, "要整数，不是布尔")
        if isinstance(value, int):
            number = value
        elif isinstance(value, float):
            # JSON 里的 40 可能被某些客户端序列化成 40.0，那是同一个整数；
            # 40.5 不是，截断它等于替用户改主意。
            if not value.is_integer():
                raise _reject(field, value, "要整数")
            number = int(value)
        elif isinstance(value, str):
            try:
                number = int(value.strip(), 10)
            except ValueError:
                raise _reject(field, value, "要整数") from None
        else:
            raise _reject(field, value, "要整数")
        _check_range(field, number, value)
        return number

    if kind == KIND_FLOAT:
        if isinstance(value, bool):
            raise _reject(field, value, "要数字，不是布尔")
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                raise _reject(field, value, "要数字") from None
        else:
            raise _reject(field, value, "要数字")
        # NaN 会让后面所有比较都返回 False：`inliers >= nan` 恒假，也就是"永远不
        # 命中"，而 NaN 在管理台上显示出来就是个普通的 nan，没人会觉得它是原因。
        # float("inf") 同理（恒真/恒假）。两者都必须在入口拦掉。
        if number != number or number in (float("inf"), float("-inf")):
            raise _reject(field, value, "要有限的数字")
        _check_range(field, number, value)
        return number

    if kind == KIND_ENUM:
        if isinstance(value, str) and value.strip() in field.choices:
            return value.strip()
        raise _reject(field, value, f"只能是 {field.choices} 之一")

    if kind == KIND_STR:
        if isinstance(value, str):
            return value
        raise _reject(field, value, "要字符串")

    # 走到这里说明 FIELDS 里写了一个 coerce 不认识的 kind。这是代码错误而不是
    # 用户输入错误，所以不是 BadConfigValue。
    raise AssertionError(f"未实现的字段类型：{field.kind!r}（key={field.key}）")


class AppConfig:
    def __init__(
        self,
        catalog: db.Catalog,
        *,
        ttl_s: float = CACHE_TTL_S,
        now_ms: Callable[[], int] = db.now_ms,
    ) -> None:
        self._cat = catalog
        self._ttl_ms = float(ttl_s) * 1000.0
        self._now = now_ms
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0

    # ---- 读 ----

    def get(self, key: str) -> Any:
        if key not in _BY_KEY:
            raise BadConfigKey(f"没有这个配置项：{key!r}")
        return self._snapshot()[key]

    def all(self) -> dict[str, Any]:
        """全部字段的当前值（库里没存的用默认值）。返回的是缓存字典的**副本**。

        不返回缓存本身：调用方拿到 dict 之后随手 `cfg["recog.top_k"] = 5` 就会
        改到所有线程共享的那份缓存，而且在 TTL 到期前一直有效 —— 一个没有任何人
        写库的"配置被改了"。
        """
        return dict(self._snapshot())

    def describe(self) -> list[dict[str, Any]]:
        """给管理台的字段表：声明 + 当前值，一次调用够画出整个表单。

        key 用驼峰（`needsRestart`），与 app.py 里其它 JSON 响应一致
        （`photoId`、`printWidthM`），这样 HTTP 层直接 json.dumps 就行，不需要再
        做一层改名 —— 那层改名迟早会漏掉一个新加的字段。
        """
        values = self._snapshot()
        out = []
        for f in FIELDS:
            out.append(
                {
                    "key": f.key,
                    "kind": f.kind,
                    "value": values[f.key],
                    "default": f.default,
                    "label": f.label,
                    "help": f.help,
                    "needsRestart": f.needs_restart,
                    "min": f.minimum,
                    "max": f.maximum,
                    "choices": list(f.choices),
                }
            )
        return out

    # ---- 写 ----

    def patch(self, updates: dict[str, Any]) -> list[str]:
        """写一批，返回其中**确实变了且需要重启**的 key。

        两个刻意的选择：

        1. **先全部校验，再写**。一次提交里有一个非法值就整批拒绝。半套生效的
           配置（"阈值改了、后端没改"）是最难排查的一类状态。
        2. **只写与当前值不同的 key**，返回列表也只含真的变了的。否则管理台每次
           点保存都会得到一句"需要重启才能生效"，哪怕用户只是改了个标题旁边的
           开关 —— 喊了几次狼来了之后，真需要重启时也不会有人当真。
        """
        if not isinstance(updates, dict):
            raise ConfigRejected(f"要一个 key->value 的字典，收到 {type(updates).__name__}")
        unknown = sorted(set(updates) - set(_BY_KEY))
        if unknown:
            raise BadConfigKey(f"不认识这些配置项：{unknown}")

        coerced = {key: coerce(_BY_KEY[key], value) for key, value in updates.items()}
        current = self._snapshot()
        changed = {k: v for k, v in coerced.items() if v != current[k]}
        if not changed:
            return []
        self._cat.put_app_config(
            {k: json.dumps(v, ensure_ascii=False) for k, v in changed.items()},
            self._now(),
        )
        self.invalidate()
        # 按 FIELDS 的顺序返回，而不是 dict 的插入顺序（那取决于管理台提交的字段
        # 顺序）—— 同样的一次修改每次都该得到同样的答复。
        return [f.key for f in FIELDS if f.key in changed and f.needs_restart]

    def invalidate(self) -> None:
        """丢掉缓存。patch 之后自动调；另一个进程改了库时也可以手动调。"""
        with self._lock:
            self._cache = None
            self._cache_at = 0

    # ---- 内部 ----

    def _snapshot(self) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            cache = self._cache
            if cache is not None and now - self._cache_at < self._ttl_ms:
                return cache
        # 刻意**不持锁**去查库：两个线程同时发现缓存过期时会各查一次、后写的那份
        # 赢，而两份内容一样，所以没有正确性问题。持锁查 SQL 的话，所有请求线程
        # 都会排在这一次 SQL 后面，而它可能正在等入库那把写锁。
        fresh = self._load()
        with self._lock:
            self._cache = fresh
            self._cache_at = now
        return fresh

    def _load(self) -> dict[str, Any]:
        """库里存的值 + 缺的用默认值。坏值回退到默认并留一行日志。

        为什么不抛：这个函数在每个请求路径上都会被调到。一行手工改坏的 JSON
        （或者未来版本写进去、又被降级回来的枚举值）如果让它抛，表现是"整个服务
        每个接口都 500"。回退到默认值 + 日志，服务继续能用，管理台上还能看到那个
        字段显示成默认值 —— 这是一个能被发现、也能被改回来的状态。
        """
        raw = self._cat.all_app_config()
        values: dict[str, Any] = {}
        for f in FIELDS:
            text = raw.get(f.key)
            if text is None:
                values[f.key] = f.default
                continue
            try:
                # json.JSONDecodeError 是 ValueError 的子类，coerce 抛的
                # BadConfigValue 也是，所以一个 except 就够。
                values[f.key] = coerce(f, json.loads(text))
            except ValueError as exc:
                print(
                    f"[photoar] app_config 里 {f.key} 的值不可用，回退到默认值 "
                    f"{f.default!r}：{exc}",
                    flush=True,
                )
                values[f.key] = f.default
        return values
