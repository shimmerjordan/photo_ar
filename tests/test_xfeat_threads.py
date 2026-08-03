"""`xfeat.cpu_quota()` / `_default_threads()`：cgroup v1 与 v2 都要读得到配额。

这一整份测试来自一次实测：本机 Docker 是 cgroup **v1**，而 `_default_threads` 原来
只看 v2 的 `/sys/fs/cgroup/cpu.max`。于是在 `--cpus=3.0` 的容器里它返回了宿主机的
16，ORT 按 16 开 intra-op 线程去抢 3 个核的配额 —— 正是那个函数存在的目的所要避免
的事，而它不报错、不打日志，只表现为"推理比预期慢"。

QNAP QTS 的 Container Station 也是 cgroup v1，也就是说**目标机器正好落在那条读不到
配额的路径上**。

不 mock `Path`，而是把 `xfeat.Path` 换成一个只认我们给的那几个文件的假实现：要测的
恰恰是"这个函数去读哪几个路径、怎么解析"，mock 掉解析就什么都没测。
"""

from pathlib import Path

import pytest

from photoar import xfeat


class _FakePath:
    """一个只认 `files` 里那些路径的极简 Path 替身。"""

    def __init__(self, p, files=None):
        self._p = str(p)
        self._files = files

    def read_text(self, *a, **k):
        if self._p not in self._files:
            raise FileNotFoundError(self._p)
        return self._files[self._p]


@pytest.fixture
def fs(monkeypatch):
    """`fs({"路径": "内容"})` 之后，xfeat 里的 Path 只看得见这些文件。"""

    def _install(files: dict[str, str]):
        monkeypatch.setattr(
            xfeat, "Path", lambda p: _FakePath(p, files), raising=True
        )

    yield _install


V2 = "/sys/fs/cgroup/cpu.max"
V1Q = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
V1P = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
V1Q_ALT = "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us"
V1P_ALT = "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us"


def test_reads_cgroup_v2(fs):
    fs({V2: "300000 100000\n"})
    assert xfeat.cpu_quota() == 3


def test_reads_cgroup_v1(fs):
    """⚠️ 回归：这条以前是空的，于是配额被静默忽略。"""
    fs({V1Q: "300000\n", V1P: "100000\n"})
    assert xfeat.cpu_quota() == 3


def test_reads_cgroup_v1_alternate_mount(fs):
    """有些运行时把 cpu 控制器挂在 `cpu,cpuacct/` 下面。"""
    fs({V1Q_ALT: "300000", V1P_ALT: "100000"})
    assert xfeat.cpu_quota() == 3


def test_v2_unlimited_means_unlimited(fs):
    """v2 明确说了 "max" 就是不限制 —— 此时**不能**再去猜 v1（那两个文件在一台
    v2 的机器上不存在，但万一存在，读到的会是另一个 cgroup 层级的陈旧值）。"""
    fs({V2: "max 100000\n"})
    assert xfeat.cpu_quota() is None


def test_v1_unlimited_is_minus_one(fs):
    """v1 用 -1 表示不限制。不判这个的话会算出 `int(-1/100000)` = 0，
    再被 `max(1, ...)` 兜成 1 —— 一个**单线程**的推理，慢得莫名其妙。"""
    fs({V1Q: "-1\n", V1P: "100000\n"})
    assert xfeat.cpu_quota() is None


def test_no_cgroup_at_all(fs):
    fs({})
    assert xfeat.cpu_quota() is None


def test_garbage_does_not_raise(fs):
    """cgroup 文件长得不对时不能抛：这个函数在构造 XFeatExtractor 的路径上，
    抛了就等于"服务起不来"，而原因是一个跟识别毫无关系的伪文件系统。"""
    fs({V2: "not-a-number\n"})
    assert xfeat.cpu_quota() is None
    fs({V1Q: "", V1P: ""})
    assert xfeat.cpu_quota() is None


def test_fractional_quota_rounds_down(fs):
    """`--cpus=3.5` → 3 个线程。向上取整会让线程数超过配额，正是要避免的事。"""
    fs({V2: "350000 100000\n"})
    assert xfeat.cpu_quota() == 3


def test_tiny_quota_still_gives_one_thread(fs):
    fs({V2: "50000 100000\n"})
    assert xfeat.cpu_quota() == 1


def test_default_threads_prefers_the_quota(fs):
    fs({V1Q: "200000", V1P: "100000"})
    assert xfeat._default_threads() == 2


def test_default_threads_falls_back_to_affinity(fs):
    """读不到配额时退到亲和性 —— 那是裸机上正确的答案。"""
    import os

    fs({})
    assert xfeat._default_threads() == len(os.sched_getaffinity(0))


def test_the_real_machine_answer_is_sane():
    """不 mock，直接在跑测试的这台机器上要一个数：必须是正整数。

    钉住的是"这个函数在任何真实环境里都给得出一个能用的答案"——它的返回值会直接
    变成 ORT 的 `intra_op_num_threads`，0 或负数会让会话构造失败。
    """
    n = xfeat._default_threads()
    assert isinstance(n, int) and n >= 1
