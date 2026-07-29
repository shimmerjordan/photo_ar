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

只支持 PNG/JPEG，且文件名与目标名只支持 ASCII 字符。
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


class QualityTooLow(ValueError):
    def __init__(self, path: str, score: int) -> None:
        super().__init__(
            f"{path} 的 arcoreimg 质量分为 {score}，低于阈值 {MIN_QUALITY_SCORE}。"
            f"画面纹理不足（大片天空/纯色背景/过曝），考虑换图或加细纹理边框。"
        )
        self.path = path
        self.score = score


def _run(arcoreimg: str, args: list[str]) -> str:
    if shutil.which(arcoreimg) is None and not Path(arcoreimg).is_file():
        raise ArcoreimgMissing(
            f"找不到 arcoreimg（{arcoreimg}）。从 ARCore SDK for Android 的 "
            f"tools/arcoreimg/linux/ 取，或用 arcoreimg= 参数指定路径。"
        )
    proc = subprocess.run(
        [arcoreimg, *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"arcoreimg {' '.join(args)} 退出码 {proc.returncode}：{proc.stderr.strip()}"
        )
    return proc.stdout


def eval_img(image_path: str | Path, arcoreimg: str = ARCOREIMG) -> int:
    out = _run(arcoreimg, ["eval-img", f"--input_image_path={Path(image_path)}"])
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
    """
    if not name.isascii():
        raise ValueError(f"arcoreimg 只支持 ASCII 目标名，收到 {name!r}")
    if "|" in name or "\n" in name:
        raise ValueError(f"目标名不能含 '|' 或换行（清单以 '|' 分隔），收到 {name!r}")
    if not print_width_m > 0:
        raise ValueError(f"打印物理宽度必须为正数（米），收到 {print_width_m!r}")

    image_path = Path(image_path).resolve()  # 清单要求绝对路径
    out_path = Path(out_path)
    if not image_path.name.isascii():
        raise ValueError(f"arcoreimg 只支持 ASCII 文件名，收到 {image_path.name!r}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 清单文件是临时产物，不留在用户目录里
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "targets.txt"
        listing.write_text(f"{name}|{image_path}|{print_width_m:.6f}\n")
        _run(
            arcoreimg,
            [
                "build-db",
                f"--input_image_list_path={listing}",
                f"--output_db_path={out_path}",
            ],
        )

    if not out_path.exists():
        raise RuntimeError(f"arcoreimg build-db 未产出 {out_path}")
    return out_path.stat().st_size
