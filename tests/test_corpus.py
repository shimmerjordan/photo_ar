import json

import cv2
import numpy as np
import pytest

from photoar import corpus as C
from photoar import features as F
from photoar import synth
from photoar import vocab as V
from photoar.corpus import (
    CorpusIntegrityError,
    CorpusPaths,
    build_corpus,
    load_corpus,
    load_holdout,
    select_holdout,
    write_holdout,
)
from photoar.descstore import DescStore, DescStoreWriter
from photoar.index import InvertedIndexBuilder


@pytest.fixture
def photo_dir(tmp_path, textured_image):
    d = tmp_path / "photos"
    d.mkdir()
    paths = []
    for i in range(12):
        p = d / f"img{i:03d}.jpg"
        cv2.imwrite(str(p), textured_image(seed=i, w=900, h=650))
        paths.append(p)
    return d, paths


def test_build_corpus_writes_all_artifacts(tmp_path, photo_dir):
    _, paths = photo_dir
    out = tmp_path / "corpus"
    entries = build_corpus(paths, out, seed=0, arcoreimg=None)

    p = CorpusPaths.at(out)
    assert p.desc.exists() and p.vocab.exists() and p.index.exists() and p.manifest.exists()
    assert len(entries) == len(paths)
    assert len({e.photo_id for e in entries}) == len(paths)


def test_manifest_is_valid_json_with_stable_order(tmp_path, photo_dir):
    _, paths = photo_dir
    out = tmp_path / "corpus"
    entries = build_corpus(paths, out, seed=0, arcoreimg=None)
    data = json.loads(CorpusPaths.at(out).manifest.read_text())
    assert [e["photo_id"] for e in data["photos"]] == [e.photo_id for e in entries]


def test_loaded_corpus_recognizes_its_own_photos(tmp_path, photo_dir):
    d, paths = photo_dir
    out = tmp_path / "corpus"
    build_corpus(paths, out, seed=0, arcoreimg=None)

    rec, entries = load_corpus(out)
    target = entries[5]
    img = cv2.imread(target.ref_path)
    query, _ = synth.generate(img, count=1, seed=3)[0]
    d_ = rec.recognize(query)
    assert d_.matched
    assert d_.photo_id == target.photo_id


def test_build_corpus_skips_unreadable_files(tmp_path, photo_dir):
    d, paths = photo_dir
    bad = d / "broken.jpg"
    bad.write_bytes(b"not an image")
    out = tmp_path / "corpus"
    entries = build_corpus(paths + [bad], out, seed=0, arcoreimg=None)
    assert len(entries) == len(paths)


def test_build_corpus_rejects_empty_input(tmp_path):
    with pytest.raises(ValueError):
        build_corpus([], tmp_path / "corpus", seed=0, arcoreimg=None)


# ---------------------------------------------------------------------------
# I7 + I9（最终审阅追加）：跳过原因原来完全静默——build_corpus 只在
# `except QualityTooLow: continue` 里悄悄丢一张，CLI 只打印"入库 N 张"，
# 用户没法知道 1 万张变成 9800 张是因为质量分不够、图片本身读不出来、还是
# 重复照片；0d 的 correct_rate 也会被这种"分母悄悄变小"污染。同时
# build_corpus 明明已经算出内容哈希（_photo_id）却从不去重：字节完全相同
# 的两张照片都会入库，两个 slot 内容一模一样，互为最近邻，RATIO 判定检验
# 时谁都赢不了谁，两份都被判 ambiguous（安全方向，但等于两张都永久漏检），
# 还会让 manifest 出现重复 id、.imgdb 被写两次到同一路径。
# ---------------------------------------------------------------------------


def test_build_corpus_dedupes_byte_identical_duplicate(tmp_path, photo_dir, capsys):
    """I9：字节完全相同的重复照片必须被去重（按 _photo_id 内容哈希判重），
    而不是两份都入库。"""
    d, paths = photo_dir
    dup = d / "duplicate_of_first.jpg"
    dup.write_bytes(paths[0].read_bytes())  # 与 paths[0] 字节完全相同

    out = tmp_path / "corpus"
    capsys.readouterr()
    entries = build_corpus(paths + [dup], out, seed=0, arcoreimg=None)

    assert len(entries) == len(paths)  # 重复的那张没有多算一条
    assert len({e.photo_id for e in entries}) == len(paths)
    err = capsys.readouterr().err
    assert "1" in err  # 报出了"丢弃了 1 张重复照片"，而不是悄悄丢掉


def test_build_corpus_skips_arcoreimg_path_format_violation_per_photo(
    tmp_path, photo_dir, capsys, fake_arcoreimg
):
    """I5：文件名/路径含清单分隔符 '|' 时，build_single_target_db 抛
    InvalidListingField——build_corpus 必须像对待 QualityTooLow 一样单张
    跳过、记录原因，而不是让异常一路冒泡出 build_corpus，中止整个入库
    （此前 build_corpus 只捕获 QualityTooLow，这类异常会中止半途，还留下
    已经写出的 .imgdb 文件）。"""
    d, paths = photo_dir
    bad_dir = d / "a|b"
    bad_dir.mkdir()
    bad_path = bad_dir / "x.jpg"
    bad_path.write_bytes(paths[0].read_bytes())

    out = tmp_path / "corpus"
    capsys.readouterr()
    entries = build_corpus(
        paths + [bad_path], out, seed=0, arcoreimg=fake_arcoreimg()
    )
    assert len(entries) == len(paths)  # 违规的那张被跳过，其余正常入库
    err = capsys.readouterr().err
    assert "清单分隔符" in err


def test_build_corpus_reports_every_skip_reason(tmp_path, photo_dir, capsys):
    """I7：unreadable、zero-feature、duplicate 三种跳过原因都必须被计数并
    体现在 stderr 的摘要里——断言的是"确实按各自原因报出计数"，而不是只测
    "命令没崩溃"（后者旧实现也能通过）。"""
    d, paths = photo_dir

    unreadable = d / "broken.jpg"
    unreadable.write_bytes(b"not an image, just junk bytes")

    blank = d / "blank.jpg"
    cv2.imwrite(str(blank), np.full((400, 600, 3), 128, np.uint8))  # 纯色，零特征点

    dup = d / "dup.jpg"
    dup.write_bytes(paths[0].read_bytes())

    out = tmp_path / "corpus"
    capsys.readouterr()
    entries = build_corpus(paths + [unreadable, blank, dup], out, seed=0, arcoreimg=None)
    assert len(entries) == len(paths)

    err = capsys.readouterr().err
    assert err, "跳过原因必须被报出来，不能是空的 stderr"
    for keyword in ("读不出来", "特征点", "重复"):
        assert keyword in err, f"stderr 里缺少 {keyword!r} 这条跳过原因：{err!r}"


def test_quality_gate_is_skipped_when_arcoreimg_is_none(tmp_path, photo_dir):
    """arcoreimg=None 时质量分记 -1，表示未评估；不应因缺少二进制而失败。"""
    _, paths = photo_dir
    entries = build_corpus(paths, tmp_path / "c", seed=0, arcoreimg=None)
    assert all(e.quality_score == -1 for e in entries)
    assert all(e.imgdb_bytes == 0 for e in entries)


# ---------------------------------------------------------------------------
# C2（最终审阅追加）：词汇树训练在全量描述子上做 k-majority，根层每次迭代都
# 会物化一个 (N, BRANCHING, 32) 的中间数组，实测约 1.66 KB/训练描述子、
# 随 N 线性增长（50k -> 142.6MB, 150k -> 308.7MB, 300k -> 554.9MB）。1 万张
# 照片 x N_FEATURES(300) = 300 万描述子会把这一步撑到 ~5GB，是 0d 第一次真实
# 入库的 OOM 元凶之一。build_corpus 必须像 measure-0b.py 的 TRAIN_DESC_CAP
# 一样，在喂给 vocab.train 之前对训练描述子做确定性抽样上限。
# ---------------------------------------------------------------------------


def test_build_corpus_caps_vocab_training_descriptors(tmp_path, photo_dir, monkeypatch):
    """把 TRAIN_DESC_CAP 临时调小到 100，确认 build_corpus 真的把喂给
    vocab.train 的描述子数量截到这个上限，而不是把全部描述子（这里 12 张
    图，每张最多 N_FEATURES=300，最多 3600 个）都传进去。用 monkeypatch 换掉
    vocab.train 本身来窥探它实际收到的行数，这样测的是"确实被截断"而不是
    "训练没崩溃"（后者即使没有上限逻辑也会通过，测不出问题）。
    """
    _, paths = photo_dir
    monkeypatch.setattr(C, "TRAIN_DESC_CAP", 100)

    captured: dict[str, int] = {}
    real_train = V.train

    def spy_train(descriptors, *args, **kwargs):
        captured["n"] = descriptors.shape[0]
        return real_train(descriptors, *args, **kwargs)

    monkeypatch.setattr(V, "train", spy_train)

    build_corpus(paths, tmp_path / "c", seed=0, arcoreimg=None)
    assert captured["n"] == 100


def test_build_corpus_vocab_cap_sampling_is_deterministic(tmp_path, photo_dir, monkeypatch):
    """同一个 seed 两次 build，被抽中送去训练的描述子子集必须完全一致——
    C2 的修复要求"确定性子采样"，不能每次 build 都抽到不同子集（那样两次
    对同一批照片 build 出来的词汇树会不可复现，破坏"随机性只能来自
    default_rng(seed)"这条项目规则）。"""
    _, paths = photo_dir
    monkeypatch.setattr(C, "TRAIN_DESC_CAP", 100)

    captured: list[int] = []
    real_train = V.train

    def spy_train(descriptors, *args, **kwargs):
        captured.append(int(descriptors[:, 0].sum()))  # 内容摘要，足以区分子集
        return real_train(descriptors, *args, **kwargs)

    monkeypatch.setattr(V, "train", spy_train)

    build_corpus(paths, tmp_path / "c1", seed=5, arcoreimg=None)
    build_corpus(paths, tmp_path / "c2", seed=5, arcoreimg=None)
    assert len(captured) == 2
    assert captured[0] == captured[1]


# ---------------------------------------------------------------------------
# 顺序完整性校验（人工审阅追加需求）：TwoStageRecognizer.__init__ 只检查三者
# 的长度相等，防不住"描述子库、倒排索引用不同顺序构建，但长度凑巧一致"这种
# 错位——那种情况下 recognize() 会自信地返回一张别的、错误的照片，正是本
# 项目权重最高的误识别类别。下面两个测试分别针对性验证 load_corpus 新增的
# 两道校验：描述子指纹（能查出 desc.bin/manifest 顺序错位）与倒排索引自查
# （能查出 index.npz 的 doc 顺序错位，指纹查不出这种错位，因为索引不存
# 任何可以拿来 hash 的原始内容）。
# ---------------------------------------------------------------------------


def test_load_corpus_rejects_swapped_desc_fingerprints(tmp_path, photo_dir):
    """把 manifest 里两条 desc_sha256 互换，模拟"描述子库 slot 顺序与
    manifest 顺序错位"（例如两者是各自独立构建、顺序凑巧不一致）。
    这条测试如果没有指纹校验就不会失败——build_corpus 本身单循环
    追加+索引，天然保持顺序一致，不会产生这种错位；只有伪造它才能验证
    校验本身真的在起作用。
    """
    _, paths = photo_dir
    out = tmp_path / "corpus"
    build_corpus(paths, out, seed=0, arcoreimg=None)

    manifest_path = CorpusPaths.at(out).manifest
    data = json.loads(manifest_path.read_text())
    photos = data["photos"]
    photos[0]["desc_sha256"], photos[1]["desc_sha256"] = (
        photos[1]["desc_sha256"],
        photos[0]["desc_sha256"],
    )
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    with pytest.raises(CorpusIntegrityError):
        load_corpus(out)


def test_load_corpus_rejects_permuted_index_via_self_query(tmp_path, textured_image):
    """针对性验证自查（self-query）检测倒排索引 doc 顺序错位的能力，且
    与指纹校验隔离：desc.bin 与 manifest.json 完全不动（指纹必然全部通过），
    只重建 index.npz，让 doc j 的内容变成原 slot (n-1-j) 的描述子（整体
    倒序，n 为偶数保证没有不动点）。

    这里覆盖"大语料"这个 regime：100 张、远大于 TOP_K(20)，实际用的 k
    就是 top_k 本身（k = max(1, min(top_k, n_docs // 2)) = min(20, 50) = 20），
    不受收缩影响。与下面的
    test_load_corpus_rejects_permuted_index_small_corpus（12 张、k 被
    收缩到 6）成对，分别覆盖 _verify_self_query 里 k 公式的两条分支。
    """
    d = tmp_path / "photos"
    d.mkdir()
    img_paths = []
    for i in range(100):
        p = d / f"img{i:03d}.jpg"
        cv2.imwrite(str(p), textured_image(seed=i, w=900, h=650))
        img_paths.append(p)

    out = tmp_path / "corpus"
    build_corpus(img_paths, out, seed=0, arcoreimg=None)

    cp = CorpusPaths.at(out)
    voc = V.Vocab.load(cp.vocab)
    store = DescStore(cp.desc)
    n = len(store)
    assert n % 2 == 0  # 保证倒序没有不动点

    builder = InvertedIndexBuilder(voc.n_words)
    for j in range(n):
        original_slot = n - 1 - j
        builder.add(voc.words_of(store.read(original_slot).desc))
    builder.build().save(cp.index)

    with pytest.raises(CorpusIntegrityError):
        load_corpus(out)


def test_load_corpus_rejects_truncated_desc_store_before_fingerprint_loop(tmp_path, photo_dir):
    """I4：desc.bin 被截断一个 slot 后，manifest/index 仍记录原有数量，三者
    对不上。旧实现里 _verify_desc_fingerprints 会直接对着一个少了一个 slot
    的 store 用越界下标去读，抛出未捕获的 IndexError，而不是
    CorpusIntegrityError——cli.py 的 except 分支和这个函数的文档都假设
    "语料损坏"统一映射到 CorpusIntegrityError。必须在指纹校验循环之前，
    先做一次 len(store) == len(entries) == index.n_docs 的前置校验。"""
    _, paths = photo_dir
    out = tmp_path / "corpus"
    build_corpus(paths, out, seed=0, arcoreimg=None)

    from photoar.descstore import SLOT_STRIDE

    cp = CorpusPaths.at(out)
    data = cp.desc.read_bytes()
    assert len(data) % SLOT_STRIDE == 0 and len(data) > SLOT_STRIDE
    cp.desc.write_bytes(data[:-SLOT_STRIDE])  # 去掉最后一个 slot，数量对不上

    with pytest.raises(CorpusIntegrityError):
        load_corpus(out)


# ---------------------------------------------------------------------------
# finding I8（最终整体审阅追加）：select_holdout 从 build_corpus 的输入照片
# 里确定性地切出一部分，这部分从此彻底不参与 extract/词汇树训练/倒排索引/
# manifest——是"库外查询"这个概念在代码里的真正落地：不是"库内某张没被
# 抽到当 ref"，是识别器的候选库里根本不存在这张照片的任何痕迹。
# ---------------------------------------------------------------------------


def test_select_holdout_excludes_holdout_photos_from_library(photo_dir):
    _, paths = photo_dir
    library, holdout = select_holdout(paths, frac=0.25, seed=0)
    assert len(holdout) == 3  # round(12 * 0.25) = 3
    assert len(library) == 9
    assert set(library).isdisjoint(set(holdout))
    assert set(library) | set(holdout) == set(sorted(paths))


def test_select_holdout_is_deterministic_for_the_same_seed(photo_dir):
    """spec 要求：同一个 seed 两次 build 必须留出同一批照片。"""
    _, paths = photo_dir
    lib1, hold1 = select_holdout(paths, frac=0.25, seed=7)
    lib2, hold2 = select_holdout(paths, frac=0.25, seed=7)
    assert hold1 == hold2
    assert lib1 == lib2


def test_select_holdout_differs_across_seeds_on_a_large_enough_pool(photo_dir):
    _, paths = photo_dir
    _, hold_a = select_holdout(paths, frac=0.25, seed=1)
    _, hold_b = select_holdout(paths, frac=0.25, seed=2)
    assert hold_a != hold_b


def test_select_holdout_zero_frac_holds_out_nothing(photo_dir):
    _, paths = photo_dir
    library, holdout = select_holdout(paths, frac=0.0, seed=0)
    assert holdout == []
    assert library == sorted(paths)


def test_select_holdout_rejects_frac_that_would_empty_the_library(photo_dir):
    _, paths = photo_dir
    with pytest.raises(ValueError):
        select_holdout(paths, frac=1.0, seed=0)


def test_select_holdout_rejects_negative_frac(photo_dir):
    """本轮修复追加：frac<=0 原本被一刀切地当成"不留出"，负数因此会被
    静默接受、悄悄退化成"不留出任何图"——跟 --limit 打错负号被显式拒绝
    （M12，exit 2）的处理方式不一致，用户很可能是打错了负号却完全得不到
    任何提示。0 仍然合法（默认值，语义明确是"不留出"），只有负数被拒绝。
    """
    _, paths = photo_dir
    with pytest.raises(ValueError):
        select_holdout(paths, frac=-0.1, seed=0)


# ---------------------------------------------------------------------------
# 本轮修复（最终整体审阅追加的 gap）：select_holdout 原来对每张照片独立
# 抽样，完全不知道 build_corpus 会按内容哈希（_photo_id）去重。真实照片
# 目录里字节完全相同的重复（同一张图被复制、多次下载）近乎必然出现——
# 一旦一份被抽进 holdout、另一份留在 library，evaluate_out_of_library 会
# 把"库外查询其实是库内某张的重复、被正确认出"错记成 false_positive，
# meets_baseline 会因为纯粹的数据卫生问题在 0.1% 阈值上翻盘，而不是识别器
# 真的变差了。修复：按 _photo_id 分组，整组一起决定去留。
# ---------------------------------------------------------------------------


def test_select_holdout_never_splits_byte_identical_duplicates_across_boundary(photo_dir):
    d, paths = photo_dir
    dup = d / "dup_of_first.jpg"
    dup.write_bytes(paths[0].read_bytes())  # 与 paths[0] 字节完全相同
    all_paths = paths + [dup]

    for seed in range(30):
        library, holdout = select_holdout(all_paths, frac=0.25, seed=seed)
        first_in_holdout = paths[0] in holdout
        dup_in_holdout = dup in holdout
        assert first_in_holdout == dup_in_holdout, (
            f"seed={seed}：字节完全相同的一对重复被分到了两边"
            f"（paths[0] in holdout={first_in_holdout}, dup in holdout={dup_in_holdout}）"
        )
        # 反过来在 library 里也必须同进同出，不能其中一份在切分中丢失。
        assert (paths[0] in library) == (dup in library)
        assert set(library) | set(holdout) == set(all_paths)
        assert set(library).isdisjoint(set(holdout))


def test_build_corpus_never_ingests_holdout_photos(tmp_path, photo_dir):
    """端到端确认：select_holdout 切出的留出图确实一张都没有进 build_corpus
    的产物——manifest 里不会出现它们的 photo_id，desc.bin 的 slot 数也要
    对得上"库内"那部分的数量，不是全部 12 张。"""
    d, paths = photo_dir
    library, holdout = select_holdout(paths, frac=0.25, seed=0)
    assert len(holdout) == 3

    out = tmp_path / "corpus"
    entries = build_corpus(library, out, seed=0, arcoreimg=None)
    assert len(entries) == len(library)

    holdout_ids = {C._photo_id(p) for p in holdout}
    manifest_ids = {e.photo_id for e in entries}
    assert holdout_ids.isdisjoint(manifest_ids)


def test_write_and_load_holdout_roundtrips(tmp_path, photo_dir):
    _, paths = photo_dir
    out = tmp_path / "corpus"
    out.mkdir()
    _, holdout = select_holdout(paths, frac=0.25, seed=0)
    write_holdout(out, holdout)

    loaded = load_holdout(out)
    assert loaded == holdout


def test_load_holdout_returns_empty_list_when_no_holdout_file(tmp_path):
    """build 时没给 --holdout-frac 的语料没有 holdout.json——这是默认
    情况，load_holdout 必须返回空列表而不是报错，eval 据此判断"这次没有
    库外测量"，行为与这个特性存在之前完全一致。"""
    out = tmp_path / "corpus"
    out.mkdir()
    assert load_holdout(out) == []


# ---------------------------------------------------------------------------
# I3（最终审阅追加）：index.py 的 idf = log(n_docs/df)，df == n_docs（词在
# 全部文档里都出现）时 idf == 0。如果一篇文档的全部词都是这种"全局共享
# 词"，它的 tf-idf 范数就是 0，build() 会把它整体排除在倒排表之外——
# n_docs=1 是最极端的情形：唯一文档的每个词 df 必然等于 n_docs，所以它必然
# 触发这个情况。旧的 _verify_self_query 对这类"合法地检索不到"的文档做自查
# 探测，index.query 因为 qnorm=0 直接返回空列表，"自己的下标不在候选里"就
# 被误判成"索引顺序被打乱"，报出一个文不对题的 CorpusIntegrityError。
# ---------------------------------------------------------------------------


def test_load_corpus_succeeds_for_single_photo_corpus(tmp_path, textured_image, capsys):
    """n_docs=1 时唯一那篇文档的词必然全部满足 df==n_docs（idf 恒为 0）——
    之前 load_corpus ALWAYS 对这种语料抛 CorpusIntegrityError，还诊断成
    "顺序错位"，而实际上语料完好。这条测试是 I3 描述的最简复现：load_corpus
    必须成功返回，并把"这张照片检索不到"报出来（不是报错，是退化信号）。

    注意：粗排的 tf-idf 打分完全依赖"这个词是否只在部分文档里出现"来
    产生区分度；只有一篇文档时，它自己每个词的 df 都等于 n_docs=1，idf
    恒为 0，粗排候选恒为空——这是 BoW 检索在极小语料下的固有限制，不是
    这次修复要解决的目标（改 idf 公式会牵动 phase0-results.md 里全部已测
    数字，是本次审阅明确排除的更大改动）。这里只断言 recognize 不崩溃、
    给出确定性的"漏检"而不是误识别——漏检是这个项目校准原则里的安全方向。
    """
    d = tmp_path / "photos"
    d.mkdir()
    p = d / "only.jpg"
    cv2.imwrite(str(p), textured_image(seed=0, w=900, h=650))

    out = tmp_path / "corpus"
    build_corpus([p], out, seed=0, arcoreimg=None)
    capsys.readouterr()

    rec, entries = load_corpus(out)  # 不应该抛 CorpusIntegrityError
    assert len(entries) == 1
    err = capsys.readouterr().err
    assert "1" in err  # 报出了"1 张照片检索不到"，而不是悄悄放过

    img = cv2.imread(str(p))
    query, _ = synth.generate(img, count=1, seed=1)[0]
    d_ = rec.recognize(query)
    assert not d_.matched  # 结构性漏检（安全方向），不是这次修复的目标
    assert d_.reason == "empty"


def test_load_corpus_succeeds_for_all_ubiquitous_corpus(tmp_path, photo_dir, capsys):
    """更一般的退化情形：不止 n_docs=1，任何"每篇文档的词都是全局共享词"的
    语料都会触发同一个 bug（旧代码的注释错误地断言"n_docs=1 时恒过是对
    的"，实测是恒炸；这条测试覆盖 n_docs>1 但词汇树粗到只有 1 个词、所有
    文档共享唯一词表的情形，是同一个缺陷的更一般形式）。

    用 12 张内容不同的真实照片（不会被 I9 的去重误伤），先走一次正常
    build_corpus 拿到 desc.bin + manifest（描述子指纹与它们绑定），再手工
    用 branching=1, depth=1 重新训练一个只有 1 个词的词汇树、重建索引并
    覆盖 vocab.npz / index.npz——这样每篇文档都只含这唯一的词，df==n_docs
    对每篇文档都成立，全部文档的 tf-idf 范数都是 0，全部被踢出倒排表。
    load_corpus 必须能正常加载（不误报错位），并把这种退化情况报出来，而
    不是让这些"进了库却永远搜不到"的照片悄无声息。
    """
    _, paths = photo_dir
    out = tmp_path / "corpus"
    build_corpus(paths, out, seed=0, arcoreimg=None)

    cp = CorpusPaths.at(out)
    store = DescStore(cp.desc)
    all_feats = [store.read(slot) for slot in range(len(store))]

    coarse_voc = V.train(
        np.vstack([f.desc for f in all_feats]), branching=1, depth=1, seed=0
    )
    assert coarse_voc.n_words == 1  # 确认真的只有 1 个词，构造条件成立
    coarse_voc.save(cp.vocab)

    builder = InvertedIndexBuilder(coarse_voc.n_words)
    for f in all_feats:
        builder.add(coarse_voc.words_of(f.desc))
    idx = builder.build()
    assert len(idx.unretrievable_docs()) == len(paths)  # 全部文档都该被判定
    idx.save(cp.index)

    capsys.readouterr()
    rec, entries = load_corpus(out)  # 不应该抛 CorpusIntegrityError
    assert len(entries) == len(paths)
    err = capsys.readouterr().err
    assert str(len(paths)) in err  # 报出了不可检索的照片数量，而不是悄悄丢掉


def test_load_corpus_rejects_permuted_index_small_corpus(tmp_path, photo_dir):
    """同上，但专门覆盖"小语料"这个 regime：12 张照片，严格小于
    recognizer.TOP_K(20)。

    在给 _verify_self_query 加上 k = max(1, min(top_k, n_docs // 2)) 这个
    收缩之前，这条测试对这种规模的语料完全没有检测力——index.query 会
    把全部 12 篇文档都当 top-K 候选返回，"自己的下标在不在候选里"这条
    断言无论顺序有没有被打乱都恒为真（检测概率恒为 0%）。收缩后
    k = max(1, min(20, 6)) = 6，单次采样检测出错位的概率约
    1 - 6/12 = 50%，5 个采样点合起来约 1 - 0.5^5 ≈ 97%。

    这是概率性检测，不是恒定必然：可靠性已经用连续独立跑 20 次、全部
    通过验证过（跑法与结果记在 task-11-report.md 里，不在这里重复跑，
    避免拖慢日常测试）；也验证过把 _verify_self_query 的 k 临时改回恒定
    的 top_k 后，这条测试会稳定 FAIL（同样记在报告里）。
    """
    _, paths = photo_dir
    out = tmp_path / "corpus"
    build_corpus(paths, out, seed=0, arcoreimg=None)

    cp = CorpusPaths.at(out)
    voc = V.Vocab.load(cp.vocab)
    store = DescStore(cp.desc)
    n = len(store)
    assert n == 12 and n % 2 == 0  # 保证倒序没有不动点，且确实是小语料 regime

    builder = InvertedIndexBuilder(voc.n_words)
    for j in range(n):
        original_slot = n - 1 - j
        builder.add(voc.words_of(store.read(original_slot).desc))
    builder.build().save(cp.index)

    with pytest.raises(CorpusIntegrityError):
        load_corpus(out)
