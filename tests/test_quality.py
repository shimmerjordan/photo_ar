import cv2
import numpy as np
import pytest

from photoar import quality as Q

# fake_arcoreimg 这个 fixture 挪到了 tests/conftest.py（I6 最终审阅追加）：
# tests/test_cli.py 覆盖 --print-width-mm 转换时也需要用同一份假 arcoreimg，
# 并且新加了 expected_width_m 参数用来校验清单第三列（物理宽度），不再只是
# 校验列数和路径是否绝对。


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


# ---------------------------------------------------------------------------
# I5（最终审阅追加）：这里原来断言"非 ASCII 目标名会被拒绝"，理由是"arcoreimg
# 只支持 ASCII"。这个假设从未被真正验证过，用仓库里实际的 tools/arcoreimg
# （版本 1.2）实测后被推翻：中文目标名、中文文件名、中文父目录，build-db 和
# eval-img 都正常返回码 0、产出有效结果；--help 里也从未提过字符集限制。
# 真正会破坏清单格式的只有字面 '|' 或换行——因为清单以 '|' 分隔列，这与
# ASCII 无关。旧的 ASCII 拒绝逻辑如果不改，会让 0d 对着几乎必然出现中文
# 文件名的真实照片目录整批 build 失败。
# ---------------------------------------------------------------------------


def test_build_single_target_db_accepts_nonascii_name(tmp_path, image_file, fake_arcoreimg):
    """中文目标名必须被接受——实测 arcoreimg 本身不拒绝它。"""
    size = Q.build_single_target_db(
        image_file, name="外婆生日", print_width_m=0.152,
        out_path=tmp_path / "x.imgdb", arcoreimg=fake_arcoreimg(),
    )
    assert size > 0


def test_build_single_target_db_accepts_nonascii_parent_directory(tmp_path, fake_arcoreimg, textured_image):
    """中文父目录必须被接受，不能只检查 basename 而漏掉父目录——写进清单
    的是解析后的完整绝对路径，父目录同样是这条路径的一部分。"""
    sub = tmp_path / "中文目录"
    sub.mkdir()
    img_path = sub / "photo.jpg"
    cv2.imwrite(str(img_path), textured_image(seed=5))

    size = Q.build_single_target_db(
        img_path, name="p1", print_width_m=0.152,
        out_path=tmp_path / "cn.imgdb", arcoreimg=fake_arcoreimg(),
    )
    assert size > 0


def test_build_single_target_db_rejects_name_with_pipe(tmp_path, image_file, fake_arcoreimg):
    """清单文件用 '|' 分隔，名称里带 '|' 会把行结构破坏掉——这与字符集无关，
    是清单格式本身的约束（真实 arcoreimg 对此返回非零退出码，见模块
    docstring 的实测记录）。"""
    with pytest.raises(Q.InvalidListingField):
        Q.build_single_target_db(
            image_file, name="a|b", print_width_m=0.152,
            out_path=tmp_path / "x.imgdb", arcoreimg=fake_arcoreimg(),
        )


def test_build_single_target_db_rejects_path_with_pipe(tmp_path, fake_arcoreimg, textured_image):
    """I5 的"过紧过松"里"过松"的那一半：旧代码只检查 image_path.name（不含
    父目录）是否 ASCII，完全没检查过路径里是否含 '|'——如果父目录名字面带
    '|'（Linux 文件名合法字符），清单行同样会被破坏，旧代码却完全没有拦。"""
    sub = tmp_path / "a|b"
    sub.mkdir()
    img_path = sub / "photo.jpg"
    cv2.imwrite(str(img_path), textured_image(seed=6))

    with pytest.raises(Q.InvalidListingField):
        Q.build_single_target_db(
            img_path, name="p1", print_width_m=0.152,
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

    fake 脚本会校验行格式、路径是否为绝对路径、以及第三列的宽度是否与
    期望值一致（expected_width_m，I6），在不合规时退出码非 0——所以这个
    测试真的能测到清单的写法（包括宽度这个值本身），而不是只测到"没抛
    异常"。I6 之前：fake 从不看 parts[2]，即使把 0.089 写成 89.0（1000 倍
    的 mm/m 单位错误）这条测试也会通过；见
    final-fix-wave1-report.md 记录的注入验证。
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
            out_path=tmp_path / "rel.imgdb",
            arcoreimg=fake_arcoreimg(expected_width_m=0.089),
        )
    finally:
        os.chdir(cwd)
    assert size > 0


def test_build_single_target_db_rejects_width_unit_mismatch(tmp_path, image_file, fake_arcoreimg):
    """I6 的直接回归：如果调用方不小心把毫米当米传进来（1000 倍单位错误），
    fake arcoreimg 现在会真的校验清单第三列，退出非零，_run 把它包成
    RuntimeError——这条测试锁死"宽度确实被端到端验证过"，不是只测格式。"""
    with pytest.raises(RuntimeError):
        Q.build_single_target_db(
            image_file, name="p1", print_width_m=0.152,
            out_path=tmp_path / "x.imgdb",
            arcoreimg=fake_arcoreimg(expected_width_m=152.0),  # 期望值故意错 1000 倍
        )
