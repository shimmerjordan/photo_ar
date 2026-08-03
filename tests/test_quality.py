import cv2
import numpy as np
import textwrap

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


# ---- 「这张图不行」与「工具坏了」必须分开 ----
#
# 真 arcoreimg 两种情况都是退出码 1，唯一的区别在 stderr 的文案。分不开的代价
# 实测过：放量入库 3030 张里 65 张（2.1%）是纹理不足的照片，全部以 500 +
# traceback 的形式出现 —— 调用方看到 5xx 会重试（结果一样），而真正的服务端
# 故障被这些栈淹掉。


def test_not_enough_keypoints_is_not_a_runtime_error(image_file, fake_arcoreimg):
    fake = fake_arcoreimg(
        exit_code=1, stderr="Failed to get enough keypoints from target image."
    )
    with pytest.raises(Q.NotEnoughKeypoints) as exc:
        Q.eval_img(image_file, arcoreimg=fake)
    # 出错信息里要有是哪张图 —— 批量入库时这是唯一能定位到照片的线索
    assert image_file.name in str(exc.value)


def test_other_nonzero_exits_stay_runtime_errors(image_file, fake_arcoreimg):
    """反向：别的失败仍是 RuntimeError（→ 500）。

    把匹配写宽（比如只看退出码）会把真正的工具故障也静默降级成「照片不合格」，
    那样一整批照片会被逐张跳过而没人发现 arcoreimg 根本没在工作。
    """
    fake = fake_arcoreimg(exit_code=1, stderr="Cannot open shared library")
    with pytest.raises(RuntimeError) as exc:
        Q.eval_img(image_file, arcoreimg=fake)
    assert not isinstance(exc.value, Q.NotEnoughKeypoints)


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


def test_build_single_target_db_omits_width_when_unknown(tmp_path, image_file, fake_arcoreimg):
    """宽度未知（None / 0 / 负）时清单行只有两列，**不写 0**。

    这三个输入原来都抛 ValueError。改了是因为照片实际尺寸经常就是不知道的，而烘一个
    猜的宽度比不烘更糟：ARCore 会当真并照它回显 getExtentX，端上按这个错数字画四边形，
    位姿却来自量纲真实的 SLAM，两个尺度错位多少，视频就比照片大/小多少。

    `expect_no_width=True` 让假 arcoreimg 在看到第三列时直接退出码 2 —— 否则"以为省略
    了、其实写了 0.000000"的实现照样能过，而 ARCore 会把 0 米宽当真，位姿彻底是废的。
    """
    for unknown in (None, 0.0, -0.1):
        out = tmp_path / f"x{unknown}.imgdb"
        n = Q.build_single_target_db(
            image_file, name="p1", print_width_m=unknown,
            out_path=out, arcoreimg=fake_arcoreimg(expect_no_width=True),
        )
        assert n > 0, f"{unknown!r} 应该正常建库"
        assert out.exists()


def test_build_single_target_db_still_bakes_known_width(tmp_path, image_file, fake_arcoreimg):
    """知道宽度时照旧烘进第三列 —— 可选不等于弃用。"""
    out = tmp_path / "known.imgdb"
    Q.build_single_target_db(
        image_file, name="p1", print_width_m=0.152,
        out_path=out, arcoreimg=fake_arcoreimg(expected_width_m=0.152),
    )
    assert out.exists()


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


# ---------------------------------------------------------------------------
# 多目标建库（整库预建那条路）。清单本来就是一行一个目标，所以这里测的全部是
# "有没有真的写成多行"以及"上限有没有被静默截断"。
# ---------------------------------------------------------------------------


@pytest.fixture
def three_images(tmp_path, textured_image):
    paths = []
    for i in range(3):
        p = tmp_path / f"m{i}.jpg"
        cv2.imwrite(str(p), textured_image(seed=100 + i))
        paths.append(p)
    return paths


def test_build_multi_target_db_writes_one_line_per_target(
    tmp_path, three_images, fake_arcoreimg
):
    """三个目标 → 清单三行，每行的宽度都得对。

    fake 会同时校验行数（expected_targets）和每行第三列（expected_width_m），所以
    "把三个目标拼成一行"或者"只写了最后一个"都会让它非零退出。
    """
    out = tmp_path / "all.imgdb"
    result = Q.build_multi_target_db(
        [(f"p{i}", p, 0.152) for i, p in enumerate(three_images)],
        out,
        arcoreimg=fake_arcoreimg(db_bytes=12_900, expected_targets=3, expected_width_m=0.152),
    )
    assert out.exists()
    assert result.bytes == 12_900
    # names 是**真正进了库**的那些。调用方要按它生成 manifest，不能按自己给的输入 ——
    # 一张提不出关键点的照片会被剔掉，manifest 若仍然声称它在库里，端上就会有几张
    # 照片永远扫不出来且没有任何提示。
    assert result.names == ("p0", "p1", "p2")
    assert result.dropped == ()


def test_build_multi_target_db_keeps_per_target_width(tmp_path, three_images, fake_arcoreimg):
    """每个目标有自己的物理宽度 —— 一张 A4、一张 6 寸是常态。

    这条与上一条的区别：上一条所有宽度相同，一个"只把第一个目标的宽度写给所有行"
    的实现也能过。这里三个宽度各不相同，然后逐行解析回来对一遍。
    """
    listing_dump = tmp_path / "seen.txt"
    # 用一个把清单原样抄出来的假二进制，才能逐行对宽度。fake_arcoreimg 的
    # expected_width_m 只能校验"所有行都是同一个值"。
    import stat as _stat
    import textwrap as _tw

    script = tmp_path / "arcoreimg-dump"
    script.write_text(
        _tw.dedent(f"""\
        #!/usr/bin/env python3
        import sys, pathlib
        argv = sys.argv[1:]
        def opt(p):
            for a in argv:
                if a.startswith(p):
                    return a.split("=", 1)[1]
            return None
        listing = pathlib.Path(opt("--input_image_list_path"))
        pathlib.Path({str(listing_dump)!r}).write_text(
            listing.read_text(encoding="utf-8"), encoding="utf-8"
        )
        pathlib.Path(opt("--output_db_path")).write_bytes(b"X" * 99)
        """),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | _stat.S_IEXEC)

    widths = [0.089, 0.152, 0.21]
    Q.build_multi_target_db(
        [(f"p{i}", p, w) for i, (p, w) in enumerate(zip(three_images, widths))],
        tmp_path / "w.imgdb",
        arcoreimg=str(script),
    )
    lines = listing_dump.read_text(encoding="utf-8").splitlines()
    assert [line.split("|")[0] for line in lines] == ["p0", "p1", "p2"]
    assert [float(line.split("|")[2]) for line in lines] == widths
    assert all(line.split("|")[1] == str(p) for line, p in zip(lines, three_images))


def test_build_multi_target_db_accepts_nonascii(tmp_path, textured_image, fake_arcoreimg):
    """中文目标名与中文父目录 —— 与单目标同一个理由（I5 实测）。"""
    sub = tmp_path / "中文目录"
    sub.mkdir()
    paths = []
    for name in ("外婆生日", "全家福"):
        p = sub / f"{name}.jpg"
        cv2.imwrite(str(p), textured_image(seed=hash(name) % 1000))
        paths.append((name, p, 0.152))
    assert Q.build_multi_target_db(
        paths, tmp_path / "cn.imgdb", arcoreimg=fake_arcoreimg(expected_targets=2)
    ).bytes > 0


@pytest.mark.parametrize("bad_name", ["a|b", "a\nb"])
def test_build_multi_target_db_rejects_bad_name(
    tmp_path, three_images, fake_arcoreimg, bad_name
):
    """'|' 与换行都会破坏清单结构，而且**每一行**都要查。

    只查第一行的实现能过一半的用例，所以坏名字放在第二个目标上。
    """
    with pytest.raises(Q.InvalidListingField):
        Q.build_multi_target_db(
            [
                ("ok", three_images[0], 0.152),
                (bad_name, three_images[1], 0.152),
            ],
            tmp_path / "x.imgdb",
            arcoreimg=fake_arcoreimg(),
        )


def test_build_multi_target_db_rejects_pipe_in_path(
    tmp_path, three_images, fake_arcoreimg, textured_image
):
    sub = tmp_path / "a|b"
    sub.mkdir()
    bad = sub / "photo.jpg"
    cv2.imwrite(str(bad), textured_image(seed=7))
    with pytest.raises(Q.InvalidListingField):
        Q.build_multi_target_db(
            [("ok", three_images[0], 0.152), ("bad", bad, 0.152)],
            tmp_path / "x.imgdb",
            arcoreimg=fake_arcoreimg(),
        )


def test_build_multi_target_db_omits_width_when_unknown(
    tmp_path, three_images, fake_arcoreimg
):
    """整库里宽度未知的那些行同样省略第三列。

    整库这条路（`GET /v1/targets/db`）是离线识别的来源，一张宽度未知的照片必须能进去 ——
    原来这里对 0 抛 ValueError，后果是**只要库里有一张没填宽度的照片，整库就建不出来**，
    于是所有人的离线识别一起消失。
    """
    res = Q.build_multi_target_db(
        [("a", three_images[0], None), ("b", three_images[1], 0.0)],
        tmp_path / "x.imgdb",
        arcoreimg=fake_arcoreimg(expect_no_width=True, expected_targets=2),
    )
    assert set(res.names) == {"a", "b"}
    assert res.dropped == ()


def test_build_multi_target_db_rejects_over_arcore_limit(tmp_path, fake_arcoreimg):
    """超过 1000 个目标必须**拒绝**，不能静默截断。

    截断的后果是"有些照片在端上永远扫不出来"而没有任何地方报错（服务端识别仍然
    命中，所以连日志里都看不出来）。这里还断言一个字节都没产出 —— 拒绝必须发生在
    调 arcoreimg 之前，否则磁盘上会留下一个"看起来是好的"截断库。
    """
    out = tmp_path / "too-many.imgdb"
    over = Q.MAX_TARGETS_PER_DB + 1
    with pytest.raises(Q.TooManyTargets) as exc:
        Q.build_multi_target_db(
            [(f"p{i}", tmp_path / f"{i}.jpg", 0.152) for i in range(over)],
            out,
            arcoreimg=fake_arcoreimg(),
        )
    assert str(Q.MAX_TARGETS_PER_DB) in str(exc.value)
    assert exc.value.count == over and exc.value.limit == Q.MAX_TARGETS_PER_DB
    assert not out.exists()


def test_build_multi_target_db_accepts_exactly_the_limit(tmp_path, fake_arcoreimg):
    """边界的另一侧：正好 1000 个是**合法**的。

    把判据写成 `>=` 的实现在上一条测试下完全正常，而它会让一个正好 1000 张的库
    建不出来 —— 那是官方文档写着允许的容量。
    """
    n = Q.MAX_TARGETS_PER_DB
    assert Q.build_multi_target_db(
        [(f"p{i}", tmp_path / f"{i}.jpg", 0.152) for i in range(n)],
        tmp_path / "full.imgdb",
        arcoreimg=fake_arcoreimg(expected_targets=n),
    ).bytes > 0


def test_build_multi_target_db_rejects_empty(tmp_path, fake_arcoreimg):
    """0 个目标不是"一个空库"，是一个没有意义的文件。

    收下的后果：客户端拿到 200 + 一个文件，认为离线识别已就绪，然后每一帧都不
    命中 —— 与"库坏了"完全无法区分。
    """
    with pytest.raises(ValueError):
        Q.build_multi_target_db([], tmp_path / "empty.imgdb", arcoreimg=fake_arcoreimg())


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


def _arcoreimg_that_rejects(tmp_path, bad_names: list[str], db_bytes: int = 8_000):
    """造一个假 arcoreimg：清单里出现 `bad_names` 里任何一个名字就整体失败。

    这是在复刻**真 arcoreimg 的实测行为**：`build-db` 只要有一张图提不出足够
    关键点，就以退出码 1 + 每张一行 "<路径>: Failed to get enough keypoints from
    target image." 结束，而且**整个库都不产出**（Oxford5k 1000 张里有 17 张这样）。
    照搬这个行为的后果是一张照片让所有用户的离线识别一起消失，所以
    `build_multi_target_db` 必须剔掉坏图重试 —— 这个 fake 就是用来钉住那件事的。
    """
    script = tmp_path / "arcoreimg-reject"
    script.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys, pathlib
        argv = sys.argv[1:]
        BAD = {bad_names!r}
        def opt(prefix):
            for a in argv:
                if a.startswith(prefix):
                    return a.split("=", 1)[1]
            return None
        listing = pathlib.Path(opt("--input_image_list_path"))
        rows = [l.split("|") for l in listing.read_text("utf-8").splitlines() if l]
        bad = [r for r in rows if r[0] in BAD]
        if bad:
            for r in bad:
                sys.stderr.write(r[1] + ": Failed to get enough keypoints from target image.\\n")
            sys.exit(1)
        out = pathlib.Path(opt("--output_db_path"))
        out.write_bytes(b"X" * {db_bytes})
        sys.exit(0)
        """),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def test_build_multi_target_db_drops_unusable_and_keeps_going(tmp_path):
    """一张提不出关键点的照片**不能**毁掉整个库。

    这是实测出来的真实失败形态，不是假想：`build-db` 整体失败，于是所有人的离线
    识别一起消失，而且服务端只会反复建库反复失败 —— 没有任何地方指向"是哪张照片"。
    """
    names = [f"p{i}" for i in range(5)]
    result = Q.build_multi_target_db(
        [(n, tmp_path / f"{n}.jpg", 0.152) for n in names],
        tmp_path / "db.imgdb",
        arcoreimg=_arcoreimg_that_rejects(tmp_path, ["p1", "p3"]),
    )
    assert result.names == ("p0", "p2", "p4")
    assert [n for n, _ in result.dropped] == ["p1", "p3"]
    assert result.bytes == 8_000


def test_build_multi_target_db_can_refuse_to_drop(tmp_path):
    """`drop_unusable=False` 时保持原样抛出。

    入库那条路要的是这个行为：单张照片入库时"这张不行"必须**当场告诉用户**，
    而不是静默剔掉 —— 用户以为入库成功了，实际上这张永远扫不出来。
    """
    with pytest.raises(Q.NotEnoughKeypoints):
        Q.build_multi_target_db(
            [("p0", tmp_path / "a.jpg", 0.152)],
            tmp_path / "db.imgdb",
            arcoreimg=_arcoreimg_that_rejects(tmp_path, ["p0"]),
            drop_unusable=False,
        )


def test_build_multi_target_db_all_unusable_is_its_own_error(tmp_path):
    """全都不行要有自己的异常。

    与"给了个空列表"分开：那是调用方的 bug，这是这批照片全都不合格 —— 用户该做的
    事完全不同（换照片 / 检查是不是把质量门槛关掉后入了一批不合格的）。
    """
    with pytest.raises(Q.AllTargetsUnusable):
        Q.build_multi_target_db(
            [("p0", tmp_path / "a.jpg", 0.152), ("p1", tmp_path / "b.jpg", 0.152)],
            tmp_path / "db.imgdb",
            arcoreimg=_arcoreimg_that_rejects(tmp_path, ["p0", "p1"]),
        )


def test_build_db_handles_arcoreimg_appending_imgdb_suffix(tmp_path, image_file, fake_arcoreimg):
    """输出路径不以 .imgdb 结尾时，产物要能被找回来。

    真 arcoreimg 在这种情况下**自己补后缀**，写到 `<给的路径>.imgdb`，退出码仍然是 0。
    `server.targets` 建整库时用的正是这种名字（`<版本>.imgdb.tmp-<pid>-<tid>`），于是
    每一次整库构建都以 "未产出" 失败 —— 而离线识别整条路就是靠整库，所以那条路一直
    坏着，表现只是「离线命中从来不发生」，没有任何报错指向 arcoreimg。

    这个测试用的就是 targets.py 真实的那种临时文件名。
    """
    out = tmp_path / "0b90638aa53a7a52.imgdb.tmp-1-140590201165504"
    n = Q.build_single_target_db(
        image_file, name="p1", print_width_m=None,
        out_path=out, arcoreimg=fake_arcoreimg(),
    )
    assert out.exists(), "产物必须落在调用方要求的那个路径上"
    assert n == out.stat().st_size
    assert not out.with_name(out.name + ".imgdb").exists(), "补了后缀的那份要挪走，不能留着"
