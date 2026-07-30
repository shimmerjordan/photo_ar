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
from pathlib import Path

MIN_QUALITY_SCORE = 75
ARCOREIMG = "arcoreimg"  # 仓库内已放置可用二进制于 tools/arcoreimg

_SCORE_RE = re.compile(r"(\d{1,3})")


class ArcoreimgMissing(RuntimeError):
    pass


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


def build_single_target_db(
    image_path: str | Path,
    name: str,
    print_width_m: float,
    out_path: str | Path,
    arcoreimg: str = ARCOREIMG,
) -> int:
    """建一个只含这一张参考图的 .imgdb，并把打印物理宽度烘进去。

    物理宽度写在清单行里，所以客户端不需要在运行时再用
    addImage(name, bitmap, widthInMeters) 传一遍——库里已经带着它了。

    实测：单目标 .imgdb 约 4.2-4.4 KB（见 phase0-results.md 里程碑 0c）。

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
    if not print_width_m > 0:
        raise ValueError(f"打印物理宽度必须为正数（米），收到 {print_width_m!r}")

    image_path = Path(image_path).resolve()  # 清单要求绝对路径
    out_path = Path(out_path)
    path_str = str(image_path)
    if "|" in path_str or "\n" in path_str:
        raise InvalidListingField(
            f"图片路径不能含 '|' 或换行（清单以 '|' 分隔），收到 {path_str!r}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 清单文件是临时产物，不留在用户目录里
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "targets.txt"
        # 显式指定 utf-8：name/image_path 现在允许非 ASCII 字符（I5），不能
        # 依赖进程 locale 的默认编码。
        listing.write_text(
            f"{name}|{image_path}|{print_width_m:.6f}\n", encoding="utf-8"
        )
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
        raise RuntimeError(f"arcoreimg build-db 未产出 {out_path}")
    return out_path.stat().st_size
