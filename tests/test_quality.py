import os
import stat
import textwrap

import cv2
import numpy as np
import pytest

from photoar import quality as Q


@pytest.fixture
def fake_arcoreimg(tmp_path):
    """造一个假的 arcoreimg，让测试不依赖真实二进制。

    行为：eval-img 打印固定分数；build-db 写出一个固定大小的文件。
    """

    def _make(score: int = 85, db_bytes: int = 4_300, exit_code: int = 0):
        script = tmp_path / "arcoreimg"
        script.write_text(
            textwrap.dedent(f"""\
            #!/usr/bin/env python3
            # 模拟真实 arcoreimg 的接口（已实测，见计划 Task 9 Step 1）：
            #   eval-img --input_image_path=<path>        -> 打印裸数字
            #   build-db --input_image_list_path=<file> --output_db_path=<file>
            # 清单文件每行: 名称|绝对路径|物理宽度(米)
            import sys, pathlib
            argv = sys.argv[1:]
            if {exit_code} != 0:
                sys.stderr.write("boom\\n"); sys.exit({exit_code})

            def opt(prefix):
                for i, a in enumerate(argv):
                    if a.startswith(prefix):
                        return a.split("=", 1)[1] if "=" in a else argv[i + 1]
                return None

            if argv and argv[0] == "eval-img":
                if not opt("--input_image_path"):
                    sys.stderr.write("missing --input_image_path\\n"); sys.exit(2)
                print({score})
                sys.exit(0)

            if argv and argv[0] == "build-db":
                listing = opt("--input_image_list_path")
                out = opt("--output_db_path")
                if not listing or not out:
                    sys.stderr.write("missing required option\\n"); sys.exit(2)
                # 真实工具会因清单格式错误而失败；这里也校验，否则测试测不到格式
                for line in pathlib.Path(listing).read_text().splitlines():
                    if not line.strip():
                        continue
                    parts = line.split("|")
                    if len(parts) not in (2, 3):
                        sys.stderr.write(f"bad list line: {{line}}\\n"); sys.exit(2)
                    if not pathlib.Path(parts[1]).is_absolute():
                        sys.stderr.write(f"path not absolute: {{parts[1]}}\\n"); sys.exit(2)
                pathlib.Path(out).write_bytes(b"X" * {db_bytes})
                sys.exit(0)

            sys.exit(2)
            """)
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    return _make


@pytest.fixture
def image_file(tmp_path, textured_image):
    path = tmp_path / "ref.jpg"
    cv2.imwrite(str(path), textured_image(seed=1))
    return path


def test_eval_img_parses_score(image_file, fake_arcoreimg):
    assert Q.eval_img(image_file, arcoreimg=fake_arcoreimg(score=88)) == 88


def test_eval_img_raises_when_binary_missing(image_file):
    with pytest.raises(Q.ArcoreimgMissing):
        Q.eval_img(image_file, arcoreimg="definitely-not-a-real-binary-xyz")


def test_eval_img_raises_on_nonzero_exit(image_file, fake_arcoreimg):
    with pytest.raises(RuntimeError):
        Q.eval_img(image_file, arcoreimg=fake_arcoreimg(exit_code=3))


def test_assert_quality_accepts_good_image(image_file, fake_arcoreimg):
    assert Q.assert_quality(image_file, arcoreimg=fake_arcoreimg(score=80)) == 80


def test_assert_quality_rejects_low_score(image_file, fake_arcoreimg):
    with pytest.raises(Q.QualityTooLow) as exc:
        Q.assert_quality(image_file, arcoreimg=fake_arcoreimg(score=40))
    assert exc.value.score == 40
    assert str(Q.MIN_QUALITY_SCORE) in str(exc.value)


def test_build_single_target_db_returns_size(tmp_path, image_file, fake_arcoreimg):
    out = tmp_path / "p1.imgdb"
    size = Q.build_single_target_db(
        image_file, name="p1", print_width_m=0.152, out_path=out,
        arcoreimg=fake_arcoreimg(db_bytes=4_312),
    )
    assert out.exists()
    assert size == 4_312


def test_build_single_target_db_rejects_nonascii_name(tmp_path, image_file, fake_arcoreimg):
    """arcoreimg 只支持 ASCII 文件名/目标名，提前拦住而不是让它神秘失败。"""
    with pytest.raises(ValueError):
        Q.build_single_target_db(
            image_file, name="外婆生日", print_width_m=0.152,
            out_path=tmp_path / "x.imgdb", arcoreimg=fake_arcoreimg(),
        )


def test_build_single_target_db_rejects_name_with_pipe(tmp_path, image_file, fake_arcoreimg):
    """清单文件用 '|' 分隔，名称里带 '|' 会把行结构破坏掉。"""
    with pytest.raises(ValueError):
        Q.build_single_target_db(
            image_file, name="a|b", print_width_m=0.152,
            out_path=tmp_path / "x.imgdb", arcoreimg=fake_arcoreimg(),
        )


def test_build_single_target_db_rejects_nonpositive_width(tmp_path, image_file, fake_arcoreimg):
    for bad in (0.0, -0.1):
        with pytest.raises(ValueError):
            Q.build_single_target_db(
                image_file, name="p1", print_width_m=bad,
                out_path=tmp_path / "x.imgdb", arcoreimg=fake_arcoreimg(),
            )


def test_build_single_target_db_writes_absolute_path_in_list(tmp_path, fake_arcoreimg, textured_image):
    """物理宽度是建库时烘进 .imgdb 的，清单行必须是 名称|绝对路径|宽度。

    fake 脚本会校验行格式与路径是否为绝对路径并在不合规时退出码非 0，
    所以这个测试真的能测到清单的写法，而不是只测到"没抛异常"。
    """
    import os

    sub = tmp_path / "photos"
    sub.mkdir()
    img_path = sub / "rel.jpg"
    cv2.imwrite(str(img_path), textured_image(seed=3))

    cwd = os.getcwd()
    os.chdir(tmp_path)  # 用相对路径调用，验证实现会自己转成绝对路径
    try:
        size = Q.build_single_target_db(
            "photos/rel.jpg", name="rel", print_width_m=0.089,
            out_path=tmp_path / "rel.imgdb", arcoreimg=fake_arcoreimg(),
        )
    finally:
        os.chdir(cwd)
    assert size > 0
