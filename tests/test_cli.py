import cv2
import pytest

import photoar.cli as cli_mod
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


class TestStridedLimit:
    """`--limit` 必须**等间距抽样**，不能取前 N 张。

    旧实现是 `entries[: args.limit]`，覆盖面完全由 manifest 的路径排序决定。
    在 Oxford5k 上实测过后果：前 500 张只落在 ashmolean/balliol/all_souls/
    bodleian 四个分组，而最大也最自相似的 oxford(1502)/magdalen(685)/
    christ_church(543) 一张都没覆盖到——量出来的是"四个地标的识别率"，而
    结果文档里只会写着"评估了 500 张"。
    """

    def test_spreads_across_the_whole_list(self):
        from photoar.cli import _strided

        items = list(range(100))
        assert _strided(items, 5) == [0, 20, 40, 60, 80], (
            "必须铺满整个列表；取前 N 张会得到 [0, 1, 2, 3, 4]"
        )

    def test_returns_exactly_limit_items_without_duplicates(self):
        from photoar.cli import _strided

        for n, limit in [(100, 7), (5063, 500), (11, 10), (10, 10)]:
            got = _strided(list(range(n)), limit)
            assert len(got) == limit, f"n={n} limit={limit} 抽到 {len(got)} 个"
            assert len(set(got)) == limit, f"n={n} limit={limit} 抽到了重复元素"
            assert got == sorted(got), "必须保持原顺序"

    def test_limit_zero_or_oversized_returns_everything(self):
        from photoar.cli import _strided

        items = list(range(10))
        assert _strided(items, 0) == items
        assert _strided(items, 10) == items
        assert _strided(items, 99) == items

    def test_limit_also_bounds_the_holdout_loop(self, tmp_path, photo_dir, capsys,
                                                monkeypatch):
        """--limit 原来只截断库内参考图，留出集是无上限遍历的：1 万张语料
        + --holdout-frac 0.1 + --samples 10 就是 1 万次库外查询照样跑满，
        限幅形同失效。用 spy 数 evaluate_out_of_library() 被调了几次——每张
        留出图恰好一次（流式，见 C1），所以调用次数就是覆盖的留出图张数。"""
        import photoar.cli as cli_mod

        corpus = tmp_path / "corpus"
        main(["build", "--photos", str(photo_dir), "--out", str(corpus),
              "--holdout-frac", "0.3"])
        capsys.readouterr()

        n_holdout = len(cli_mod.load_holdout(corpus))
        assert n_holdout >= 2, f"这个前提得成立才测得出限幅：留出 {n_holdout} 张"

        real = cli_mod.evaluate_out_of_library
        calls = []

        def spy(rec, refs, **kwargs):
            calls.append(len(refs))
            return real(rec, refs, **kwargs)

        monkeypatch.setattr(cli_mod, "evaluate_out_of_library", spy)

        main(["eval", "--corpus", str(corpus), "--samples", "2", "--limit", "1"])
        assert len(calls) == 1, (
            f"--limit 1 时库外查询只该覆盖 1 张留出图，实际覆盖 {len(calls)} 张"
            f"（共 {n_holdout} 张）"
        )

    def test_report_prints_coverage_denominator(self, tmp_path, photo_dir, capsys):
        """限幅跑出来的数字只写"评估参考图 500"，读者没法分辨是全量还是
        4500 张里的 500 张，而结果文档要求写明覆盖面。"""
        corpus = tmp_path / "corpus"
        main(["build", "--photos", str(photo_dir), "--out", str(corpus),
              "--holdout-frac", "0.3"])
        capsys.readouterr()

        main(["eval", "--corpus", str(corpus), "--samples", "2", "--limit", "1"])
        out = capsys.readouterr().out
        assert "评估参考图  1/" in out, f"缺少覆盖面分母：\n{out}"
        assert "库外查询图  1/" in out, f"库外也要带分母：\n{out}"
        assert "等间距抽样" in out, "限幅时要说明抽样方式，否则读者会以为是前 N 张"


class TestEvalProgress:
    """长跑必须能被判断活性。

    上规模 0d 的一次 eval 是 29740 次查询、约 1 小时，改之前整个过程零输出，
    日志文件一小时都停在 0 字节——从外面完全分不清它是在正常跑、卡在某张图
    上、还是早就死了。
    """

    def test_progress_goes_to_stderr_not_stdout(self, tmp_path, photo_dir, capsys):
        """stdout 那份报告会被逐行引用进结果文档，掺进进度行就污染了它。"""
        corpus = tmp_path / "corpus"
        main(["build", "--photos", str(photo_dir), "--out", str(corpus),
              "--holdout-frac", "0.3"])
        capsys.readouterr()

        main(["eval", "--corpus", str(corpus), "--samples", "1"])
        cap = capsys.readouterr()
        assert "[eval] 库内参考图" in cap.err, f"stderr 里没有进度行：\n{cap.err}"
        assert "[eval] 库外查询图" in cap.err, f"库外那段也要打进度：\n{cap.err}"
        assert "[eval]" not in cap.out, f"进度行不该出现在 stdout：\n{cap.out}"

    def test_last_item_always_reported(self, tmp_path, photo_dir, capsys):
        """间隔整除不到最后一张时也必须收尾打一行，否则日志会停在
        "18/20" 上，看日志的人分不清是跑完了还是最后两张卡死了。"""
        corpus = tmp_path / "corpus"
        main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
        capsys.readouterr()

        n = len(cli_mod.load_corpus(corpus)[1])
        main(["eval", "--corpus", str(corpus), "--samples", "1"])
        err = capsys.readouterr().err
        assert f"库内参考图 {n}/{n}" in err, f"最后一张没收尾：\n{err}"

    def test_unreadable_ref_does_not_skip_a_number(self, tmp_path, photo_dir, capsys,
                                                  monkeypatch):
        """读不出来的那张也要计进进度。用 `continue` 跳过 _progress 的话
        计数会跳号，看日志的人会以为进度行漏打了。"""
        corpus = tmp_path / "corpus"
        main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
        capsys.readouterr()

        entries = cli_mod.load_corpus(corpus)[1]
        n = len(entries)
        assert n >= 2, f"这个测试需要至少 2 张参考图，实际 {n} 张"
        # 让最后一张读取失败：它恰好是"收尾那一行"必须打出来的位置。
        doomed = entries[-1].ref_path
        real_imread = cli_mod.cv2.imread
        monkeypatch.setattr(
            cli_mod.cv2, "imread",
            lambda p, *a, **k: None if str(p) == str(doomed) else real_imread(p, *a, **k),
        )

        main(["eval", "--corpus", str(corpus), "--samples", "1"])
        cap = capsys.readouterr()
        assert f"库内参考图 {n}/{n}" in cap.err, (
            f"最后一张读取失败时进度停在了前一张：\n{cap.err}"
        )
        assert "读取失败" in cap.out, f"跳过的张数要报出来：\n{cap.out}"

    def test_interval_scales_so_output_stays_bounded(self):
        """按张数触发而不是每张都打：1000 张打 1000 行会把日志淹掉，
        而 20 行足够看出速度和剩余时间。"""
        from photoar.cli import _PROGRESS_LINES, _progress_every

        assert _progress_every(1000) == 50
        assert _progress_every(29740) == 1487
        # 小语料不能算出 0 —— i % 0 会 ZeroDivisionError。
        for n in (0, 1, 5, 19):
            assert _progress_every(n) >= 1, f"n={n} 算出了非正间隔"
        assert 1000 // _progress_every(1000) == _PROGRESS_LINES


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


def test_build_rejects_negative_holdout_frac(tmp_path, photo_dir, capsys):
    """本轮修复追加：--holdout-frac -0.1 原本会被 select_holdout 的
    `frac <= 0` 分支静默当成"不留出"接受，跟 --limit 打错负号被显式拒绝
    （M12，exit 2）的处理方式不一致。这里钉死退出码必须**正好是 2**（用法
    错误），不是笼统的"非零"，也不是 0/1。"""
    rc = main([
        "build", "--photos", str(photo_dir), "--out", str(tmp_path / "corpus"),
        "--holdout-frac", "-0.1",
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "-0.1" in err


def test_eval_reports_out_of_library_false_positive_attribution_for_analysis(
    tmp_path, photo_dir, capsys, monkeypatch
):
    """本轮修复追加：select_holdout 按内容哈希整组去留只堵住了字节完全
    相同的重复跨边界，堵不住"重新编码的近似重复"（同一张照片被压缩/裁切/
    转码成不同字节，哈希本身就不相等）。CLI 必须把每次库外误识别命中的
    库内 photo_id 报出来，供验收跑之后人工/脚本核对该 photo_id 在
    manifest 里的 ref_path 是不是这张留出图的另一份编码——用 monkeypatch
    强制产生确定性的 false positive，不依赖真实 CV 恰好触发误识别。"""
    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus),
          "--holdout-frac", "0.3", "--seed", "0"])
    capsys.readouterr()

    import photoar.cli as cli_mod
    from photoar.evaluate import OutOfLibraryMetrics

    def fake_evaluate_out_of_library(rec, queries, **kwargs):
        (qid,) = queries.keys()
        return OutOfLibraryMetrics(
            total=1, false_positive=1, correct_rejection=0,
            false_positive_matches=[(qid, "some-library-photo-id")],
        )

    monkeypatch.setattr(cli_mod, "evaluate_out_of_library", fake_evaluate_out_of_library)

    main(["eval", "--corpus", str(corpus), "--samples", "2"])
    err = capsys.readouterr().err
    assert "some-library-photo-id" in err


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


# ---------------------------------------------------------------------------
# finding I8（最终整体审阅追加）：此前所有里程碑都只用库内照片当查询源，
# "误识别 0"只覆盖了"库内 A 认成库内 B"这一种混淆，没有覆盖生产环境里更
# 常见的那种——用户拍一张库里从来没有的东西。下面验证 CLI 端到端接线：
# build --holdout-frac 写出 holdout.json、eval 自动读取并测出库外误识别率，
# 与库内数字分开报告。
# ---------------------------------------------------------------------------


def test_build_with_holdout_frac_writes_holdout_json_and_shrinks_library(
    tmp_path, photo_dir, capsys
):
    corpus = tmp_path / "corpus"
    rc = main([
        "build", "--photos", str(photo_dir), "--out", str(corpus),
        "--holdout-frac", "0.3",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "留出" in out

    from photoar.corpus import CorpusPaths
    cp = CorpusPaths.at(corpus)
    assert cp.holdout.exists()

    import json
    manifest = json.loads(cp.manifest.read_text())
    holdout_data = json.loads(cp.holdout.read_text())
    # photo_dir 有 10 张，round(10*0.3)=3 留出，7 张入库
    assert len(holdout_data["paths"]) == 3
    assert len(manifest["photos"]) == 7


def test_eval_reports_out_of_library_false_positive_rate_separately(
    tmp_path, photo_dir, capsys
):
    """真正的端到端验收：build 一次（带 --holdout-frac）+ eval 一次，报告
    必须同时出现库内与库外两套数字，且明确标注、互不混淆。"""
    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus),
          "--holdout-frac", "0.3", "--seed", "0"])
    capsys.readouterr()

    rc = main(["eval", "--corpus", str(corpus), "--samples", "3"])
    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert "库外查询图" in out
    assert "库外误识别" in out
    assert "库外正确拒绝" in out
    # 库内数字仍然照常出现，两套数字共存于同一份报告
    assert "正确命中" in out and "误识别" in out


def test_eval_without_holdout_omits_out_of_library_section(tmp_path, photo_dir, capsys):
    """build 时没给 --holdout-frac（默认行为）时，报告里不应该出现库外
    相关的任何一行——这是这个特性存在之前的完全等价行为。"""
    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
    capsys.readouterr()

    rc = main(["eval", "--corpus", str(corpus), "--samples", "3"])
    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert "库外" not in out


def test_build_holdout_frac_is_deterministic_across_rebuilds(tmp_path, photo_dir):
    """同一个 seed 重新 build 两次（不同输出目录），必须留出同一批照片——
    spec 明确要求的确定性。"""
    import json
    from photoar.corpus import CorpusPaths

    corpus1 = tmp_path / "corpus1"
    corpus2 = tmp_path / "corpus2"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus1),
          "--holdout-frac", "0.3", "--seed", "9"])
    main(["build", "--photos", str(photo_dir), "--out", str(corpus2),
          "--holdout-frac", "0.3", "--seed", "9"])

    h1 = json.loads(CorpusPaths.at(corpus1).holdout.read_text())["paths"]
    h2 = json.loads(CorpusPaths.at(corpus2).holdout.read_text())["paths"]
    assert h1 == h2


def test_strict_latency_flag_can_flip_exit_code_when_p95_exceeds_target(
    tmp_path, photo_dir, capsys, monkeypatch
):
    """Minor #10：--strict-latency 必须能把一个正确率/误识别率都达标、但
    延迟超标的结果从退出码 0 翻成 1——不给这个 flag 时保持旧行为不变
    （已由 test_eval_streams_ref_images_one_at_a_time 等既有测试覆盖）。
    用 monkeypatch 整个替换掉 evaluate()，构造一份"正确率 100%、延迟明显
    超标"的确定性结果，不依赖真实 CV 在这个小语料上恰好能不能测到 95% 正确
    率这种脆弱前提——这里只测 CLI 对 latency_gate 的接线，不是重新测识别率。
    """
    import photoar.cli as cli_mod
    from photoar.evaluate import Metrics

    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
    capsys.readouterr()

    def fake_evaluate(rec, refs, **kwargs):
        n = kwargs["samples_per_ref"]
        return Metrics(total=n, correct=n, wrong=0, missed=0, latencies_ms=[900.0] * n)

    monkeypatch.setattr(cli_mod, "evaluate", fake_evaluate)

    rc_default = main(["eval", "--corpus", str(corpus), "--samples", "3", "--limit", "3"])
    out_default = capsys.readouterr().out
    assert "达标" in out_default and "未达标" not in out_default
    assert rc_default == 0  # 默认不看延迟

    rc_strict = main([
        "eval", "--corpus", str(corpus), "--samples", "3", "--limit", "3",
        "--strict-latency",
    ])
    out_strict = capsys.readouterr().out
    assert "未达标" in out_strict
    assert rc_strict == 1


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
