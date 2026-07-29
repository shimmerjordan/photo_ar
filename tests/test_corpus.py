import json

import cv2
import pytest

from photoar import synth
from photoar import vocab as V
from photoar.corpus import CorpusIntegrityError, CorpusPaths, build_corpus, load_corpus
from photoar.descstore import DescStore
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


def test_quality_gate_is_skipped_when_arcoreimg_is_none(tmp_path, photo_dir):
    """arcoreimg=None 时质量分记 -1，表示未评估；不应因缺少二进制而失败。"""
    _, paths = photo_dir
    entries = build_corpus(paths, tmp_path / "c", seed=0, arcoreimg=None)
    assert all(e.quality_score == -1 for e in entries)
    assert all(e.imgdb_bytes == 0 for e in entries)


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
