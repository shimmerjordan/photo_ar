"""NAS 文件浏览与缩略图。spec §7 的 `/v1/fs/list` 与 `/v1/fs/thumb`。

路径安全全部委托给 `safepath.Roots`，这里一行手写的路径拼接都没有 —— 唯一
的入口是 `roots.resolve()`。这不是"顺便"，而是 §14.3 把路径穿越单列为必测项
的直接后果：只要有第二处地方自己拼路径，那处就会成为绕过白名单的那条路。
"""

import hashlib
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .safepath import PathDenied, Roots

IMAGE_EXT = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
)
VIDEO_EXT = frozenset(
    {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mts", ".m2ts"}
)

THUMB_LONG_EDGE = 320  # spec §7：文件选择器用
REF_THUMB_LONG_EDGE = 640  # spec §7：.imgdb 下载失败时的兜底路径用
JPEG_QUALITY = 82


class ThumbFailed(RuntimeError):
    """解不出这个文件的缩略图。调用方映射成 415，不是 500。"""


def kind_of(path: str | Path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


def list_dir(roots: Roots, raw_path: str | None) -> dict[str, Any]:
    """`path` 省略时返回白名单根目录列表（spec §7）。"""
    if not raw_path:
        return {
            "path": None,
            "parent": None,
            "entries": [
                {"name": r.name, "path": str(r.path), "isDir": True, "isRoot": True}
                for r in sorted(roots.roots, key=lambda r: r.name)
            ],
        }

    target = roots.resolve(raw_path)
    if not target.is_dir():
        raise NotADirectoryError(str(target))

    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        try:
            st = child.stat()
        except OSError:
            # 断掉的符号链接 / 权限不足：跳过而不是让整个目录 500。用户在
            # 界面上看不到它，与看到一个点不开的条目相比是更好的结果。
            continue
        if child.is_dir():
            entries.append({"name": child.name, "isDir": True})
            continue
        entries.append(
            {
                "name": child.name,
                "isDir": False,
                "kind": kind_of(child),
                "bytes": int(st.st_size),
                "mtime": int(st.st_mtime * 1000),
            }
        )

    # parent 也要在白名单内才给出，否则客户端点"上一级"会拿到 403。
    parent: str | None = None
    if target.parent != target:
        try:
            parent = str(roots.resolve(str(target.parent)))
        except PathDenied:
            parent = None
    return {"path": str(target), "parent": parent, "entries": entries}


def etag_for(path: Path, extra: str = "") -> str:
    st = path.stat()
    raw = f"{path}:{st.st_size}:{int(st.st_mtime * 1000)}:{extra}"
    return '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] + '"'


def _first_video_frame(path: Path) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            raise ThumbFailed(f"读不出视频首帧：{path}")
        return frame
    finally:
        cap.release()


def decode_for_thumb(path: Path, kind: str | None = None) -> np.ndarray:
    kind = kind or kind_of(path)
    if kind == "video":
        return _first_video_frame(path)
    if kind == "image":
        # cv2.imread 不认非 ASCII 路径（Windows）也不区分"文件不存在"和"解不开"，
        # 所以自己读字节再 imdecode。QNAP 上中文文件名很常见。
        buf = np.fromfile(str(path), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            raise ThumbFailed(f"解不开这张图：{path}")
        return img
    raise ThumbFailed(f"不是图片也不是视频：{path}")


def encode_thumb(img: np.ndarray, long_edge: int = THUMB_LONG_EDGE) -> bytes:
    from ..features import resize_to_long_edge

    small = resize_to_long_edge(img, long_edge)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise ThumbFailed("JPEG 编码失败")
    return bytes(buf.tobytes())


def thumb_bytes(path: Path, long_edge: int = THUMB_LONG_EDGE) -> bytes:
    return encode_thumb(decode_for_thumb(path), long_edge)
