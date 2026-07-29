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
