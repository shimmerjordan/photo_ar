import numpy as np
import pytest

from photoar import features as F
from photoar import vocab as V


def _random_desc(n, seed=0):
    return np.random.default_rng(seed).integers(0, 256, (n, 32), dtype=np.uint8)


def test_hamming_matrix_shape_and_known_values():
    a = np.zeros((2, 32), np.uint8)
    b = np.zeros((3, 32), np.uint8)
    b[1, 0] = 0b00000111  # 3 bit 不同
    b[2, :] = 0xFF        # 256 bit 全不同
    d = V.hamming_matrix(a, b)
    assert d.shape == (2, 3)
    assert d[0, 0] == 0
    assert d[0, 1] == 3
    assert d[0, 2] == 256


def test_hamming_matrix_is_symmetric_in_argument_order():
    a, b = _random_desc(4, 1), _random_desc(5, 2)
    assert np.array_equal(V.hamming_matrix(a, b), V.hamming_matrix(b, a).T)


def test_train_produces_words_within_range():
    voc = V.train(_random_desc(3000, 3), branching=4, depth=3, seed=0)
    words = voc.words_of(_random_desc(200, 4))
    assert words.shape == (200,)
    assert words.dtype == np.int32
    assert words.min() >= 0
    assert words.max() < voc.n_words


def test_train_is_deterministic_given_seed():
    d = _random_desc(2000, 5)
    a = V.train(d, branching=4, depth=3, seed=7)
    b = V.train(d, branching=4, depth=3, seed=7)
    q = _random_desc(100, 6)
    assert np.array_equal(a.words_of(q), b.words_of(q))


def test_identical_descriptors_map_to_same_word():
    voc = V.train(_random_desc(2000, 8), branching=4, depth=3, seed=0)
    d = _random_desc(1, 9)
    assert voc.words_of(np.repeat(d, 5, axis=0)).tolist() == [voc.words_of(d)[0]] * 5


def test_near_duplicate_descriptors_usually_share_word(textured_image):
    """翻转 2 bit 的描述子应大多落在同一个词——这是粗排召回率的前提。

    必须用真实 ORB 描述子训练和查询，不能用均匀随机描述子：均匀随机 256bit 向量的
    成对 Hamming 距离集中在 128 附近（std 仅 8），每一层的 argmin 几乎是巧合胜出，
    翻转 2 bit 就足以掀翻结果。真实 ORB 描述子有明显的聚类结构（std 约 18~21，最小
    距离可以低到 12~26），翻转 2 bit 才不容易越过分界面。在 branching=6, depth=3 下
    实测该比率约 0.90~0.91，比 0.8 的门槛有明显余量（详见
    test_uniform_random_descriptors_underestimate_near_duplicate_rate 的对照测试）。
    """
    descs = np.vstack([F.extract(textured_image(seed=s)).desc for s in range(30)])
    voc = V.train(descs, branching=6, depth=3, seed=0)
    base = descs[:300]
    noisy = base.copy()
    noisy[:, 0] ^= 0b00000011
    same = (voc.words_of(base) == voc.words_of(noisy)).mean()
    assert same >= 0.8


def test_uniform_random_descriptors_underestimate_near_duplicate_rate(textured_image):
    """记录一个真实踩过的坑：均匀随机描述子不是测量近重复共词率的合适素材。

    实测成对 Hamming 距离分布：
      均匀随机描述子: mean=128.0  std= 8.0  min= 95  p1=110
      真实 ORB 描述子: mean=126.1  std=18.4  min= 26  p1= 82
    均匀随机向量没有聚类结构，距离几乎全部堆在 128 附近，翻转 2 bit（期望改变 ±2
    距离）就足以让某一层的 argmin 换人；真实 ORB 描述子的最小距离远低于 128，翻转
    2 bit 更难越过分界面。用同样的 branching=6, depth=3 配置各自测一次“翻转 2 bit
    后共词的比例”，真实 ORB 应明显高于均匀随机——这里钉住这个差距本身（而不是给
    随机描述子的比例设一个绝对及格线），防止以后有人为了“简化测试”把真实 ORB 换回
    随机描述子，从而悄悄地把粗排召回率的验证基础换成一个错误的分布。
    """
    descs = np.vstack([F.extract(textured_image(seed=s)).desc for s in range(30)])
    voc_real = V.train(descs, branching=6, depth=3, seed=0)
    base_real = descs[:300]
    noisy_real = base_real.copy()
    noisy_real[:, 0] ^= 0b00000011
    real_rate = (voc_real.words_of(base_real) == voc_real.words_of(noisy_real)).mean()

    voc_rand = V.train(_random_desc(6000, 10), branching=6, depth=3, seed=0)
    base_rand = _random_desc(300, 11)
    noisy_rand = base_rand.copy()
    noisy_rand[:, 0] ^= 0b00000011
    random_rate = (voc_rand.words_of(base_rand) == voc_rand.words_of(noisy_rand)).mean()

    assert real_rate - random_rate >= 0.15


def test_words_of_empty_input():
    voc = V.train(_random_desc(1000, 12), branching=4, depth=2, seed=0)
    assert voc.words_of(np.zeros((0, 32), np.uint8)).shape == (0,)


def test_save_load_roundtrip(tmp_path):
    voc = V.train(_random_desc(2000, 13), branching=4, depth=3, seed=0)
    path = tmp_path / "voc.npz"
    voc.save(path)
    loaded = V.Vocab.load(path)
    assert loaded.n_words == voc.n_words
    q = _random_desc(150, 14)
    assert np.array_equal(loaded.words_of(q), voc.words_of(q))


def test_train_rejects_empty_descriptors():
    with pytest.raises(ValueError):
        V.train(np.zeros((0, 32), np.uint8))


def test_train_on_real_orb_descriptors(textured_image):
    descs = np.vstack([F.extract(textured_image(seed=s)).desc for s in range(30)])
    voc = V.train(descs, branching=6, depth=3, seed=0)
    words = voc.words_of(descs)
    # 真实 ORB 描述子应铺开到多个词上，而不是全挤进一个
    assert len(np.unique(words)) >= 20
