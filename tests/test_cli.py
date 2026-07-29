import cv2
import pytest

from photoar.cli import main


@pytest.fixture
def photo_dir(tmp_path, textured_image):
    d = tmp_path / "photos"
    d.mkdir()
    for i in range(10):
        cv2.imwrite(str(d / f"img{i:03d}.jpg"), textured_image(seed=i, w=900, h=650))
    return d


def test_build_then_eval_prints_report(tmp_path, photo_dir, capsys):
    corpus = tmp_path / "corpus"
    assert main(["build", "--photos", str(photo_dir), "--out", str(corpus)]) == 0
    capsys.readouterr()

    rc = main(["eval", "--corpus", str(corpus), "--samples", "3", "--limit", "5"])
    out = capsys.readouterr().out
    assert "正确命中" in out and "误识别" in out and "结论" in out
    assert rc in (0, 1)  # 0 = 达标, 1 = 未达标；两者都算命令成功执行


def test_eval_exit_code_signals_baseline(tmp_path, photo_dir, capsys):
    """eval 的退出码必须能被 CI 用：达标 0、未达标 1。"""
    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
    capsys.readouterr()
    rc = main(["eval", "--corpus", str(corpus), "--samples", "3", "--limit", "5"])
    out = capsys.readouterr().out
    assert (rc == 0) == ("达标" in out and "未达标" not in out)


def test_build_on_empty_directory_errors(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["build", "--photos", str(empty), "--out", str(tmp_path / "c")]) == 2
    assert "没有找到" in capsys.readouterr().err


def test_eval_on_missing_corpus_errors(tmp_path, capsys):
    assert main(["eval", "--corpus", str(tmp_path / "nope")]) == 2
    assert capsys.readouterr().err


def test_no_subcommand_shows_usage(capsys):
    assert main([]) == 2


# ---------------------------------------------------------------------------
# 人工审阅追加：_cmd_build 原本没有包住 build_corpus 的异常处理，两条现实
# 路径会变成未捕获的 traceback，进程以 Python 默认的退出码 1 结束——这违反
# 了退出码约定（1 应该只表示"未达标"，这里其实是用法/环境错误，该是 2）：
#   1. 全部照片都没入库成功（不可读/零特征/质量分不达标）
#      -> corpus.build_corpus 抛 ValueError("没有任何图片通过入库...")
#   2. --arcoreimg 指向不存在的路径 -> quality.ArcoreimgMissing
# test_build_on_empty_directory_errors 只覆盖了更早的"目录里根本没有图片
# 文件"分支（在调用 build_corpus 之前就返回 2 了），不覆盖这两条。
# ---------------------------------------------------------------------------


def test_build_reports_error_when_every_photo_is_unreadable(tmp_path, capsys):
    """目录里确实有 .jpg 文件（不是空目录，走得到 build_corpus），但内容
    全是垃圾字节、一张都读不出来——build_corpus 会抛 ValueError，_cmd_build
    必须把它映射成退出码 2 并在 stderr 给出可操作的说明，而不是崩出
    traceback（对应 Python 默认的退出码 1，违反退出码约定）。"""
    junk_dir = tmp_path / "junk_photos"
    junk_dir.mkdir()
    for i in range(3):
        (junk_dir / f"img{i}.jpg").write_bytes(b"not an image, just junk bytes")

    rc = main(["build", "--photos", str(junk_dir), "--out", str(tmp_path / "corpus")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "入库失败" in err


def test_build_reports_error_when_arcoreimg_missing(tmp_path, photo_dir, capsys):
    """--arcoreimg 指向一个不存在的可执行文件时，quality.assert_quality 会
    抛 ArcoreimgMissing；_cmd_build 必须映射成退出码 2，而不是让它变成
    未捕获的 traceback。"""
    rc = main([
        "build", "--photos", str(photo_dir), "--out", str(tmp_path / "corpus"),
        "--arcoreimg", str(tmp_path / "definitely-not-a-real-binary-xyz"),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "入库失败" in err


# ---------------------------------------------------------------------------
# 最终整体审阅追加：C1（eval 内存）、M12（负数 --limit）、I4（损坏语料的
# 退出码）。
# ---------------------------------------------------------------------------


def test_eval_streams_ref_images_one_at_a_time(tmp_path, photo_dir, capsys, monkeypatch):
    """C1：_cmd_eval 原来先把 --limit 选中的每一张参考图都 cv2.imread 解码进
    一个大 dict，再一次性调用 evaluate()——一张 12MP 手机照解码后约 36.6MB，
    1 万张图库就是约 366GB 常驻内存，0d 第一次真实 eval 会直接 OOM。
    修复后必须一次只解码一张参考图、调一次 evaluate()（流式），用完立刻可
    以被回收，不管图库多大，同时活着的解码后参考图最多一张。用 monkeypatch
    换掉 cli 模块引用的 evaluate 来窥探每次调用收到的 refs 字典大小——
    这测的是"确实在流式"，而不是"eval 命令没崩溃"（后者旧实现也能通过）。
    """
    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
    capsys.readouterr()

    import photoar.cli as cli_mod

    real_evaluate = cli_mod.evaluate
    batch_sizes: list[int] = []

    def spy_evaluate(rec, refs, **kwargs):
        batch_sizes.append(len(refs))
        return real_evaluate(rec, refs, **kwargs)

    monkeypatch.setattr(cli_mod, "evaluate", spy_evaluate)

    rc = main(["eval", "--corpus", str(corpus), "--samples", "2"])
    assert batch_sizes, "evaluate() 从未被调用"
    assert all(n == 1 for n in batch_sizes), (
        f"C1：必须一次只把一张参考图喂给 evaluate()，实际批次大小 {batch_sizes}"
    )
    assert rc in (0, 1)


def test_eval_rejects_negative_limit(tmp_path, photo_dir, capsys):
    """M12：--limit -5 目前会被 Python 切片语义悄悄解释成 entries[:-5]
    （从末尾截断），而不是"负数是非法输入"。这在 --limit 打字打错负号时会
    悄悄评估一个跟用户预期完全不同、更小的子集，而不是报错。"""
    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
    capsys.readouterr()

    rc = main(["eval", "--corpus", str(corpus), "--limit", "-5"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "-5" in err


def test_build_converts_print_width_mm_to_metres_for_arcoreimg(
    tmp_path, photo_dir, capsys, fake_arcoreimg
):
    """I6：cli.py 的 `args.print_width_mm / 1000.0` 转换此前完全没有测试
    覆盖——fake arcoreimg 原来从不校验清单第三列（宽度），这条转换写错了
    （比如漏掉 /1000、或者不小心传成别的单位）也不会有任何测试失败。这里
    传 --print-width-mm 200（对应 0.2 米），配 expected_width_m=0.2 的假
    arcoreimg，端到端验证这个值真的被正确换算并烘进了 arcoreimg 的清单。
    """
    corpus = tmp_path / "corpus"
    fake = fake_arcoreimg(expected_width_m=0.2)
    rc = main([
        "build", "--photos", str(photo_dir), "--out", str(corpus),
        "--arcoreimg", fake, "--print-width-mm", "200",
    ])
    err = capsys.readouterr().err
    assert rc == 0, f"单位换算错误会让 arcoreimg 因清单宽度不匹配而报错：{err}"


def test_eval_on_truncated_desc_store_exits_2_not_1(tmp_path, photo_dir, capsys):
    """I4：desc.bin 被截断一个 slot 后，manifest/index 仍记录原有数量，三者
    对不上。旧实现里 _verify_desc_fingerprints 会对着一个少了一个 slot 的
    store 调 store.read(最后一个下标)，抛出未捕获的 IndexError，进程以
    Python 默认退出码 1 结束——这违反了退出码约定（1 应该只表示"未达标"，
    这里其实是语料产物损坏，该是 2）。TwoStageRecognizer.__init__ 本来能
    干净地捕获这种数量不一致，但它在 load_corpus 里排在指纹校验循环
    **之后**才构造，救不了。断言退出码恰好是 2，而不只是"非零"——旧的 bug
    行为下退出码是 1，也是非零，用 assert rc != 0 测不出问题。"""
    from photoar.corpus import CorpusPaths
    from photoar.descstore import SLOT_STRIDE

    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
    capsys.readouterr()

    paths = CorpusPaths.at(corpus)
    data = paths.desc.read_bytes()
    assert len(data) % SLOT_STRIDE == 0 and len(data) > SLOT_STRIDE
    paths.desc.write_bytes(data[:-SLOT_STRIDE])  # 去掉最后一个 slot

    rc = main(["eval", "--corpus", str(corpus)])
    err = capsys.readouterr().err
    assert rc == 2, f"损坏的语料必须映射到退出码 2（环境/产物错误），实际是 {rc}"
    assert err
