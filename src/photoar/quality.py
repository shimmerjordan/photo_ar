"""arcoreimg 封装：参考图质量评分与单目标 .imgdb 生成。

外部工具契约（由 `arcoreimg --help` 实测，Step 1 记录）：

    $ arcoreimg
    Available actions: help, version, build-db, eval-db, eval-img

    $ arcoreimg eval-img --help
    Usage: arcoreimg eval-image --input_image_path=<some_file_path>
      --input_image_path:  Path of image to be evaluated. Only *.png, *.jpg, *.jpeg.

    $ arcoreimg build-db --help
    Usage: arcoreimg build-db --input_images_directory=<dir>|--input_image_list_path=<file> --output_db_path=<file>
      --input_image_list_path:
        Path of a text file where every line consists of the name, the absolute path and the
        width in meters (optional) of an image, separated by a '|'. e.g.:
            cat|path/to/cat_image.png|0.1
            little dog|/path/to/dog_image.jpg
      --input_images_directory:  all images under it are used
      --output_db_path:          output database file path

只支持 PNG/JPEG。

清单是**一行一个目标**，所以"一次建一个含多个目标的库"是这个工具本来就有的能力
（`build_multi_target_db`），不需要任何外部支持；单目标（`build_single_target_db`）
只是它 n=1 的特例。

清单行格式是"名称|绝对路径|宽度"，用 '|' 分隔，所以名称与路径都不能含
'|' 或换行，否则会把一行拆成错误的列数。这是清单格式本身的约束，与字符
集无关——之前这里错误地记录过"文件名与目标名只支持 ASCII 字符"，已用
tools/arcoreimg（版本 1.2）实测推翻（final-fix-wave1-report.md 的 I5
测量）：中文目标名、中文文件名、中文父目录，build-db 与 eval-img 都正常
返回 0 并产出有效结果；只有路径或名称本身含字面 '|' 才会让 arcoreimg 报
"Invalid line format"。真实照片目录里中文文件名近乎必然出现，这条错误
记录曾经让 build_corpus 对着一张中文文件名照片直接判错误诊断并中止整个
入库（I5）。

质量分低于 MIN_QUALITY_SCORE 的照片在入库阶段就拒绝——留到扫不出来
才发现的代价高得多。
"""

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from collections.abc import Iterable
from pathlib import Path

MIN_QUALITY_SCORE = 75
ARCOREIMG = "arcoreimg"  # 仓库内已放置可用二进制于 tools/arcoreimg

# 一个 ARCore `AugmentedImageDatabase` 能装的目标数上限（官方文档：1000 张，
# 每条约 6KB，反序列化 5MB 约 10-20ms，同时最多跟踪 20 张，一个 session 同时
# 只能装一个库）。
#
# 这个数在这里而不是在服务端：它是 **arcoreimg 产物的约束**，不是某个部署的策略。
# 放在服务端的话，`quality` 就可以被别的调用方（CLI、批量脚本）拿去建一个 1500
# 张的库 —— 那个文件建得出来、也传得下去，只有在手机上 `setAugmentedImageDatabase`
# 的那一刻才炸，而那时候已经没有任何上下文能指回"是谁建的这个库"。
MAX_TARGETS_PER_DB = 1000

_SCORE_RE = re.compile(r"(\d{1,3})")

# `photo.quality_score` 的哨兵：**没测过**（arcoreimg 不在，测不了）。
#
# 不是 0 —— 0 是一个真实的测量结果（"连关键点都提不够"那一档记的就是 0）。
# 列是 NOT NULL（那是对的：有工具时必须写真数字，见 ingest 里的注释），所以
# "没测过"需要一个列内哨兵而不是 NULL。API 层把它转成 null（`app._score_out`），
# 界面显示"未测"。选 -1 是因为它在"分数"这个值域（0..100）之外，一眼假。
UNMEASURED = -1


class ArcoreimgMissing(RuntimeError):
    pass


class TooManyTargets(ValueError):
    """目标数超过 `MAX_TARGETS_PER_DB`。

    **必须拒绝，不能静默截断**。截断的后果是"有一部分照片在端上永远扫不出来"，
    而这件事没有任何地方会报错：那些照片仍然在 catalog 里、仍然有自己的单目标
    `.imgdb`、服务端识别也仍然命中 —— 只是离线那条路上不存在。用户能观察到的
    只有"有几张照片在没网的时候扫不出来"，而这与网络本身的问题完全无法区分。
    """

    def __init__(self, count: int, limit: int = MAX_TARGETS_PER_DB) -> None:
        super().__init__(
            f"一个 .imgdb 最多装 {limit} 个目标（ARCore 官方上限），收到 {count} 个。"
            f"截断会让多出来的照片在端上永远扫不出来且不报错，所以这里拒绝 —— "
            f"调用方要自己决定留哪些（并把没留下的那些算进 overflow 报出来）。"
        )
        self.count = count
        self.limit = limit


class InvalidListingField(ValueError):
    """目标名或图片路径含清单分隔符 '|' 或换行，会把清单行拆成错误的列数。

    与字符集无关（非 ASCII 字符本身没问题，见模块 docstring 的实测记录），
    只有字面的 '|' 或 '\\n' 会破坏"名称|绝对路径|宽度"这个格式。build_corpus
    像对待 QualityTooLow 一样对待这个异常：跳过这一张、记录原因，不中止
    整个入库（见 I5/I7）。
    """


# arcoreimg 对纹理太少的图不给分，直接以这句话 + 退出码 1 结束。
#
# 靠文案匹配是没办法的事：arcoreimg 是闭源二进制，退出码只有 0/1，没有别的
# 通道能区分「这张图不行」和「工具自己坏了」。文案哪天变了就退回今天的行为
# （当成 RuntimeError → 500），不会更糟。
_NO_KEYPOINTS_MARK = "Failed to get enough keypoints"


class NotEnoughKeypoints(ValueError):
    """arcoreimg 连关键点都提不够 —— 比 [QualityTooLow] 更靠下的同一件事。

    **必须和工具故障分开**：这是输入的问题，重试一万次结果一样。`clean` 数据集上
    实测约 **2.1%** 的照片是这样（3030 张里 65 张）。分不开的后果是它以 500 +
    整个 traceback 的形式出现 —— 批量入库一万张就是 200 多个栈刷进日志，把真正
    的服务端故障淹掉，而调用方看到 5xx 会当成「服务端挂了，值得重试」。
    """

    def __init__(self, path: str, stderr: str) -> None:
        super().__init__(
            f"{path}：arcoreimg 连足够的关键点都提不出来（{stderr}）。"
            f"这是纹理不足的极端情况，比低分更严重。"
        )
        self.path = path
        # 原始 stderr 单独留一份：多目标建库要从里面挑出**是哪几张**不行。
        # 让调用方去正则上面那句拼好的消息是错的 —— 中文标点不是空白字符，
        # `\S+?` 会把「…提不出来（」和路径吞成一个 token（实测踩过）。
        self.stderr = stderr


class QualityTooLow(ValueError):
    def __init__(self, path: str, score: int) -> None:
        super().__init__(
            f"{path} 的 arcoreimg 质量分为 {score}，低于阈值 {MIN_QUALITY_SCORE}。"
            f"画面纹理不足（大片天空/纯色背景/过曝），考虑换图或加细纹理边框。"
        )
        self.path = path
        self.score = score


def _run(
    arcoreimg: str, args: list[str], *, image_path: str | Path | None = None
) -> str:
    if shutil.which(arcoreimg) is None and not Path(arcoreimg).is_file():
        raise ArcoreimgMissing(
            f"找不到 arcoreimg（{arcoreimg}）。从 ARCore SDK for Android 的 "
            f"tools/arcoreimg/linux/ 取，或用 arcoreimg= 参数指定路径。"
        )
    proc = subprocess.run(
        [arcoreimg, *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        # 「这张图不行」和「工具坏了」共用退出码 1，只能靠文案分。见
        # [NotEnoughKeypoints]：不分开的话前者会以 500 + traceback 的形式出现。
        if _NO_KEYPOINTS_MARK in err:
            raise NotEnoughKeypoints(str(image_path or " ".join(args)), err)
        raise RuntimeError(f"arcoreimg {' '.join(args)} 退出码 {proc.returncode}：{err}")
    return proc.stdout


def eval_img(image_path: str | Path, arcoreimg: str = ARCOREIMG) -> int:
    out = _run(
        arcoreimg,
        ["eval-img", f"--input_image_path={Path(image_path)}"],
        image_path=image_path,
    )
    scores = _SCORE_RE.findall(out)
    if not scores:
        raise RuntimeError(f"无法从 arcoreimg 输出中解析质量分：{out!r}")
    return int(scores[-1])


def assert_quality(image_path: str | Path, arcoreimg: str = ARCOREIMG) -> int:
    score = eval_img(image_path, arcoreimg)
    if score < MIN_QUALITY_SCORE:
        raise QualityTooLow(str(image_path), score)
    return score


def _listing_line(
    name: str, image_path: str | Path, print_width_m: float | None
) -> tuple[str, Path]:
    """一行清单（`名称|绝对路径|宽度`）+ 解析后的绝对路径。

    `print_width_m` 为 None（或非正数）= **物理宽度未知**，此时省略第三列，清单行变成
    `名称|绝对路径`。这是 arcoreimg 清单格式本来就支持的写法（`--help` 里明确写了宽度
    可选，示例第二行 `little dog|/path/to/dog_image.jpg` 就没有宽度）。

    什么时候该省略：**不知道真实尺寸的时候**。烘一个猜的宽度进去比不烘更糟 —— ARCore
    会当真并照它回显 `getExtentX`，于是端上按这个错数字画四边形，而位姿来自 SLAM、
    量纲是真的，两个尺度一错位，视频就比照片大一圈或小一圈。省略之后 ARCore 自己从
    SLAM 量物理尺寸，`getExtentX` 返回的是**测量值**，与位姿自洽。
    代价是检测要靠视差收敛，需要用户稍微动一下手机。

    ## 实测：省略宽度写进 .imgdb 的是 -1.0

    拿同一张照片建两次库（tools/arcoreimg 1.2，708×468 的 JPEG），两个产物都是 6406
    字节，`cmp -l` 显示**只差 4 个字节**，偏移 0x9DC-0x9DF，解成小端 float32：

        带 0.30 → 9a 99 99 3e = 0.30000001
        省略    → 00 00 80 bf = **-1.0**

    也就是说宽度确实是烘进库文件里的（不是被丢掉），而"未知"在这个格式里有一个明确的
    哨兵值 -1.0。这条实测同时排掉了两个猜测：省略不是"写 0"（那会让 ARCore 按 0 米宽
    算位姿，端上彻底贴不上），也不是"这个字段根本没用"。

    单目标与多目标共用这一处校验，**不是为了少写几行**：这三条检查里任何一条只在
    一边生效，都会让另一条路把一个格式坏掉的清单交给 arcoreimg。那时的失败形态是
    "arcoreimg 退出码 1 + Invalid line format"，被 `_run` 包成 RuntimeError → 500，
    而真正的原因（某张照片的父目录名里有个 '|'）要从一整批照片里翻出来。

    I5：目标名与图片路径都不要求 ASCII——实测 tools/arcoreimg（版本 1.2）
    对中文目标名、中文文件名、中文父目录均正常返回 0 并产出有效 .imgdb，
    真正会破坏清单格式的只有字面 '|' 或换行（清单以 '|' 分隔列）。这里对
    "名称"和"绝对路径整体"（不只是 basename——路径写进清单的是完整绝对
    路径，父目录里如果含 '|' 同样会破坏格式）都做这个检查。
    """
    if "|" in name or "\n" in name:
        raise InvalidListingField(
            f"目标名不能含 '|' 或换行（清单以 '|' 分隔），收到 {name!r}"
        )
    known_width = print_width_m is not None and print_width_m > 0

    resolved = Path(image_path).resolve()  # 清单要求绝对路径
    path_str = str(resolved)
    if "|" in path_str or "\n" in path_str:
        raise InvalidListingField(
            f"图片路径不能含 '|' 或换行（清单以 '|' 分隔），收到 {path_str!r}"
        )
    if known_width:
        return f"{name}|{resolved}|{print_width_m:.6f}", resolved
    return f"{name}|{resolved}", resolved


def _build_db(
    lines: list[str],
    out_path: str | Path,
    arcoreimg: str,
    *,
    image_path: str | Path | None = None,
) -> int:
    """把清单行喂给 `arcoreimg build-db`，返回产物字节数。

    清单文件写在临时目录里（不留在用户目录），且显式 utf-8：name/image_path 允许
    非 ASCII 字符（I5），不能依赖进程 locale 的默认编码 —— 在 `LANG=C` 的容器里
    那个默认是 ascii，一张中文文件名的照片会让整次建库以 UnicodeEncodeError 结束。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "targets.txt"
        listing.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        _run(
            arcoreimg,
            [
                "build-db",
                f"--input_image_list_path={listing}",
                f"--output_db_path={out_path}",
            ],
            image_path=image_path,
        )
    if not out_path.exists():
        # arcoreimg **会自己补 `.imgdb` 后缀**：`--output_db_path` 不以 `.imgdb` 结尾时，
        # 它写到 `<给的路径>.imgdb`，而且退出码仍然是 0、stdout 里还如实打了真实路径
        # （"Image database generated at: …"）。
        #
        # 这个行为咬过一次，而且咬得很深：`server.targets` 建整库时按
        # `<版本>.imgdb.tmp-<pid>-<tid>` 命名临时文件（不以 .imgdb 结尾），于是每一次
        # 整库构建都以 "未产出" 失败 —— 而**离线识别整条路就是靠整库**，所以那条路
        # 一直是坏的，表现只是「离线命中从来不发生」，没有任何报错指向这里。
        #
        # 修在这里而不是去改调用方的命名：这个函数是全工程唯一直接对接 arcoreimg
        # build-db 的地方，契约的怪癖就该收在契约的边界上。否则下一个想用临时文件名的
        # 调用方会再踩一遍同一个坑。
        appended = out_path.with_name(out_path.name + ".imgdb")
        if appended.exists():
            appended.replace(out_path)
        else:
            raise RuntimeError(f"arcoreimg build-db 未产出 {out_path}")
    return out_path.stat().st_size


def build_single_target_db(
    image_path: str | Path,
    name: str,
    print_width_m: float | None,
    out_path: str | Path,
    arcoreimg: str = ARCOREIMG,
) -> int:
    """建一个只含这一张参考图的 .imgdb，知道打印物理宽度时把它烘进去。

    物理宽度写在清单行里，所以客户端不需要在运行时再用
    addImage(name, bitmap, widthInMeters) 传一遍——库里已经带着它了。

    `print_width_m=None`（或非正）= 不知道真实尺寸，不烘。见 [_listing_line] 里那段
    「什么时候该省略」—— 简短说：烘一个猜的数比不烘更糟。

    实测：单目标 .imgdb 约 4.2-4.4 KB（见 phase0-results.md 里程碑 0c）。

    这个函数仍然存在（没有被 `build_multi_target_db` 取代）：单目标库是"扫到这张
    之后只跟这一张"那条路用的（见 Android 侧 `TargetLoader`），它与整库多目标库
    是两件事 —— 一个 session 同时只能装一个库，所以两者都要有。
    """
    line, resolved = _listing_line(name, image_path, print_width_m)
    return _build_db([line], out_path, arcoreimg, image_path=resolved)


@dataclass(frozen=True)
class MultiDbResult:
    """一次多目标建库的结果。

    `names` 是**真正进了库**的目标名，`dropped` 是被剔掉的 (目标名, 原因)。
    调用方必须按 `names` 而不是按自己给的输入去生成 manifest —— 否则端上会拿到
    "manifest 说有、库里其实没有"的照片，表现为这几张永远扫不出来而没有任何提示。
    """

    bytes: int
    names: tuple[str, ...]
    dropped: tuple[tuple[str, str], ...]


class AllTargetsUnusable(ValueError):
    """全部目标都提不出足够关键点。

    与"空列表"分开：那是调用方给错了，这是这批照片全都不合格 —— 用户该做的事
    完全不同（前者是 bug，后者要换照片或放开质量门槛）。
    """

    def __init__(self, count: int) -> None:
        super().__init__(
            f"{count} 张目标全部提不出足够关键点，建不出库。"
            f"这批照片纹理都太弱；换纹理更丰富的照片，或检查是不是把质量门槛关掉后"
            f"入了一批不合格的照片。"
        )


def build_multi_target_db(
    # 第三项是打印物理宽度；None 或非正 = 未知，那一行就不写宽度列（见 _listing_line）
    targets: Iterable[tuple[str, str | Path, float | None]],
    out_path: str | Path,
    arcoreimg: str = ARCOREIMG,
    *,
    drop_unusable: bool = True,
) -> MultiDbResult:
    """一次 `build-db` 建一个含**多个**目标的 .imgdb，返回产物字节数。

    清单格式本来就是"一行一个目标"（见模块 docstring 里 `--help` 的原文），所以
    多目标不需要任何新的外部能力 —— 只是以前没用过这个能力。

    ## 为什么服务端要预建整库，而不是让端上自己 addImage

    端上现建（Android 侧 `LocalTargetDb`）用的是 640px 缩略图，代价有三条：
    `addImage` 每张约 30ms（ARCore 官方数字，200 张约 6 秒）、特征来自缩略图所以
    跟踪质量比原图预建的低一档（`NoticeKind.LOCAL_HIT` 提示的正是这件事）、还受
    端上缓存条数上限约束。服务端预建把这三条一次去掉：手机拿到的是一个反序列化
    只要 10-20ms 的文件，特征提自原图。

    ## 建库耗时与产物大小（**实测**，本机 16 核 x86-64）

        输入张数   保留   剔掉   总耗时    产物      每目标
            10      10      0     0.1s    55.2 KB   5650 B
            50      50      0     0.6s   283.3 KB   5802 B
           200     196      4     4.4s  1049.2 KB   5482 B
           500     483     17    10.8s  2405.7 KB   5100 B
          1000     983     17    22.3s  5235.0 KB   5453 B

    也就是约 **22ms/目标**、约 **5.4KB/目标**（与官方"每条约 6KB"吻合）。1000 张的
    库是 5.2MB，反序列化按官方数字约 10-20ms。

    ⚠️ N5095 上会明显更慢（没有 AVX/AVX2），按单线程性能比外推是 50-90 秒量级。
    这个耗时落在**请求路径上**（`GET /v1/targets/db` 会触发构建），所以
    `server/targets.py` 把构建放到后台线程、让请求立刻拿到 503 + Retry-After。

    ## 一张坏照片不能毁掉整个库

    `build-db` 的行为是：**只要有一张图提不出足够关键点，整次构建就整体失败**
    （实测 Oxford5k 1000 张里有 17 张这样）。照搬这个行为的后果是一张照片让**所有
    用户的离线识别一起消失**，而且是静默的 —— 服务端只会反复建库反复失败。

    所以默认 `drop_unusable=True`：把 arcoreimg 报出来的坏图剔掉再重试。实测
    arcoreimg 会在一次 stderr 里把**所有**坏图一次报全，所以一轮重试就够；仍然留了
    `_MAX_BUILD_PASSES` 这个上限，避免某个版本改成"一次只报一个"时变成死循环。

    被剔掉的目标名从 `MultiDbResult.dropped` 返回，调用方**必须**把它们从 manifest
    里排除 —— 否则端上会拿到"manifest 说有、库里其实没有"的照片，表现为这几张永远
    扫不出来而没有任何提示。

    上限（`MAX_TARGETS_PER_DB`）超了直接 `TooManyTargets`，不截断，理由写在那个
    异常类里。
    """
    lines: list[str] = []
    path_to_name: dict[str, str] = {}
    for name, image_path, print_width_m in targets:
        line, resolved = _listing_line(name, image_path, print_width_m)
        lines.append(line)
        path_to_name[str(resolved)] = name
        if len(lines) > MAX_TARGETS_PER_DB:
            # 边收边判而不是先全收完再判：调用方给的可能是一个生成器，而"先物化
            # 一百万行再说不行"没有任何意义。多算一个 len() 是免费的。
            raise TooManyTargets(len(lines))
    if not lines:
        # 空库不是"一个装了 0 张的库"，而是一个**没有意义**的文件：装进 session
        # 之后每一帧都不可能命中，而客户端拿到 200 + 一个文件会认为离线识别已经
        # 就绪。宁可让调用方显式处理"这个人一张照片都没有"这个状态。
        raise ValueError("目标列表是空的：建一个 0 目标的 .imgdb 没有意义")

    dropped: list[tuple[str, str]] = []
    for _ in range(_MAX_BUILD_PASSES):
        try:
            size = _build_db(lines, out_path, arcoreimg)
        except NotEnoughKeypoints as exc:
            if not drop_unusable:
                raise
            bad = _unusable_paths(exc.stderr)
            if not bad:
                # 报了"关键点不够"却没报是哪张 —— 不能猜，也不能把整个库丢掉一半
                # 来试，直接抛给调用方。
                raise
            keep, removed = [], 0
            for line in lines:
                # 清单行是 `名称|绝对路径|宽度`，中间那一段就是路径
                path = line.split("|")[1]
                if path in bad:
                    dropped.append((path_to_name.get(path, path), "关键点不足"))
                    removed += 1
                else:
                    keep.append(line)
            if not removed:
                # arcoreimg 报的路径不在我们给的清单里（版本改了输出格式？）——
                # 继续重试只会原地打转。
                raise
            lines = keep
            if not lines:
                raise AllTargetsUnusable(len(dropped)) from exc
            continue
        names = tuple(line.split("|")[0] for line in lines)
        return MultiDbResult(
            bytes=size, names=names, dropped=tuple(dropped)
        )
    raise RuntimeError(
        f"建库重试 {_MAX_BUILD_PASSES} 轮仍未成功（已剔掉 {len(dropped)} 张）"
    )


# 剔坏图的重试上限。实测 arcoreimg 一次 stderr 就把所有坏图报全，所以 2 轮足够；
# 留这个上限是为了让"某个版本改成一次只报一个"变成一个报错而不是死循环。
_MAX_BUILD_PASSES = 4


def _unusable_paths(stderr: str) -> set[str]:
    """从**原始 stderr** 里挑出"关键点不够"的那些图片路径。

    格式是每行 `<绝对路径>: Failed to get enough keypoints from target image.`。

    逐行处理而不是对整段做正则：路径里允许有空格（"我的 照片.jpg"），所以任何
    基于 `\S+` 的模式都会在第一个空格处截断，得到一个不存在的路径 —— 于是"剔掉坏图"
    悄悄变成"什么也没剔掉"，然后重试上限耗尽、整库建不出来。
    按标记切、取它**左边整段**再去掉结尾的冒号，才对所有路径都成立。
    """
    out: set[str] = set()
    for line in stderr.splitlines():
        head, sep, _ = line.partition(_NO_KEYPOINTS_MARK)
        if not sep:
            continue
        path = head.rstrip().rstrip(":").strip()
        if path:
            out.add(path)
    return out
