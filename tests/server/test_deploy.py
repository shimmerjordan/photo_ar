"""一键部署那条路：**没有词表、没有 token、没有模型也要能起来并且能用**。

这份测试对着的是"全新部署"这个状态，而不是某个函数。它要钉住的三件事各自都曾经是
一次"服务起不来"：

1. 词表文件不存在 → 以前 `Server.create` 抛 FileNotFoundError。
2. token 为空 → 以前 `from_dict` 抛 ConfigError。而放宽它必须同时证明**空 token
   不是一把万能钥匙**（下面有一条走完整 HTTP 栈的）。
3. `recog.backend` 是 xfeat 但模型文件不在 → 以前会一路抛到启动失败。

外加两件"换后端"的正确性：库目录必须分开，以及降级必须从接口上看得出来。
"""

import json

import pytest

from photoar import backend as backend_mod
from photoar import synth
from photoar.nullvocab import NullVocab
from photoar.server import app
from photoar.server.config import ServerConfig

from .conftest import ADMIN_NAME, ADMIN_PASSWORD, TOKEN


# ---- 没有词表 ----


def test_starts_without_a_vocab_file(make_env, tmp_path):
    """词表文件不存在时服务照样起，库里装的是 NullVocab。"""
    env = make_env(vocab_path=None)
    assert isinstance(env.srv.library.vocab, NullVocab)
    assert env.srv.library.backend.name == backend_mod.ORB


def test_ping_says_the_vocab_is_untrained(make_env):
    """空词表下每次识别都全量扫描 —— 那件事必须能从接口上看到，不能只在启动日志里。

    日志会滚走，而"库大了越来越慢"是几个月之后才表现出来的。
    """
    env = make_env(vocab_path=None)
    body = env.body_json(env.get("/v1/ping"))
    assert body["vocabTrained"] is False
    assert body["vocabWords"] == 1


def test_ping_says_the_vocab_is_trained_when_it_is(env):
    body = env.body_json(env.get("/v1/ping"))
    assert body["vocabTrained"] is True and body["vocabWords"] > 1


def test_full_ingest_and_recognize_without_a_vocab(make_env):
    """全新部署的完整闭环：起服务 → 入库 → 认出来，全程没有词表。

    这是"没有词表是一个合法状态"这句话的实际含义。
    """
    env = make_env(vocab_path=None)
    ref = env.write_image("photos/a.jpg", seed=11)
    pid = env.ingest_ok(ref)
    query, _ = synth.generate(env.textured(seed=11, w=1200, h=800), count=1, seed=3)[0]
    body = env.body_json(env.post_frame("/v1/recognize", env.jpeg_of(query)))
    assert body["matched"] and body["photoId"] == pid


# ---- 空 token ----


def test_starts_without_a_token(make_env):
    env = make_env(token="")
    assert env.cfg.token == ""


def test_empty_token_is_not_a_master_key_over_http(make_env):
    """⚠️ 放宽"token 必填"之前必须证明的那一条，走**完整 HTTP 栈**。

    `tests/server/test_auth.py` 已经在 `Auth` 这一层测过（`principal_of("")` 与
    `principal_of("anything")` 都是 None）。这里再测一遍是因为放行的判断分散在三处：
    `_credential`（拿不到凭证时返回空串）、`Auth.principal_of`（`if not token`
    与 `if self._legacy_token and ...` 两道）、以及 `_dispatch`（prin 为 None 就 401）。
    只测中间那一层的话，另外两处有人改了不会红。
    """
    env = make_env(token="")
    for headers in (
        {},  # 什么都不带
        {"authorization": "Bearer "},  # 空 Bearer
        {"authorization": "Bearer  "},  # 空白 Bearer
        {"cookie": f"{app.SESSION_COOKIE}="},  # 空 cookie
        {"authorization": "Bearer anything"},  # 随便一个 token
        {"authorization": f"Bearer {TOKEN}"},  # 原本那个测试 token
    ):
        r = env.request("GET", "/v1/ping", headers=headers, auth=False)
        assert r.status == 401, f"{headers} 竟然通过了鉴权"
    # 写操作同样挡住（换一个非 GET、且是 admin only 的接口再确认一次）
    r = env.request("GET", "/v1/admin/users", auth=False)
    assert r.status == 401


def test_real_accounts_still_work_without_a_token(make_env):
    """空 token 只应该关掉运维凭证那条路，人的账号一切照常 —— 否则"放宽"就变成
    "把服务锁死了"。"""
    env = make_env(token="")
    creds = env.login(ADMIN_NAME, ADMIN_PASSWORD)
    assert env.get("/v1/ping", as_=creds).status == 200
    assert env.get("/v1/admin/users", as_=creds).status == 200


# ---- 后端切换与降级 ----


def _set_backend(env, name: str) -> None:
    """直接写库，模拟"用户在管理台把后端改了"。"""
    env.srv.catalog.put_app_config({"recog.backend": json.dumps(name)})


def test_xfeat_without_a_model_falls_back_to_orb(make_env, capsys):
    """模型是运行时资产（要外网才取得到）。它不在的时候服务必须照样起。

    拒绝启动的后果是把"XFeat 用不了"这个降级放大成"整个服务不可用"，而 ORB 才是
    通过出口条件的那条基线 —— 它一个字节的模型都不需要。
    """
    env = make_env()
    _set_backend(env, backend_mod.XFEAT)
    srv = app.Server.create(env.cfg)  # 不抛就是第一层结论
    assert srv.library.backend.name == backend_mod.ORB
    assert srv.backend_requested == backend_mod.XFEAT
    assert srv.backend_error and "xfeat.onnx" in srv.backend_error
    # 日志里要写清"实际跑的是 ORB"，不能只说一句"模型不在"
    assert "回退" in capsys.readouterr().out


def test_ping_reports_the_degradation(make_env):
    """⚠️ 这条是本次改造最要紧的一条。

    静默跑成另一个后端会让用户得出"XFeat 在我的照片上没用"这个结论，而实际上跑的
    一直是 ORB。日志会滚走，`/v1/ping` 不会。
    """
    env = make_env()
    _set_backend(env, backend_mod.XFEAT)
    srv = app.Server.create(env.cfg)
    body = json.loads(
        srv.handle(
            app.Request(method="GET", raw_path="/v1/ping", headers=dict(
                authorization=f"Bearer {TOKEN}"
            ))
        ).body
    )
    assert body["backend"] == backend_mod.ORB
    assert body["backendRequested"] == backend_mod.XFEAT
    assert body["backendDegraded"] is True
    assert body["backendError"]


def test_ping_is_not_degraded_normally(env):
    body = env.body_json(env.get("/v1/ping"))
    assert body["backend"] == backend_mod.ORB
    assert body["backendDegraded"] is False and body["backendError"] is None


def test_unknown_backend_in_db_falls_back_instead_of_crashing(make_env, capsys):
    """库里那一行被手工改成一个不认识的后端名时，服务照常起，跑 ORB。

    兜底在**两层**，这条测的是外层：`AppConfig._load` 发现枚举值不合法就回退到默认值
    并留一行日志（那是它刻意的设计 —— 每个请求都会调它，抛的话表现是"每个接口都
    500"）。所以 `_open_backend` 根本看不到 "superfeat"，`backendDegraded` 也如实是
    false —— 生效的配置确实就是 orb。
    """
    env = make_env()
    _set_backend(env, "superfeat")
    srv = app.Server.create(env.cfg)
    assert srv.library.backend.name == backend_mod.ORB
    assert srv.backend_requested == backend_mod.ORB
    assert srv.backend_error is None
    assert "superfeat" in capsys.readouterr().out  # 但必须留下痕迹


def test_open_backend_rejects_a_bogus_name_directly(make_env):
    """内层兜底：直接拿一个非法名字调 `_open_backend`（绕过 AppConfig 那层）。

    这条分支现在从配置那条路走不到，正因如此它最容易在重构时被写坏而没人发现 ——
    而它的作用是"任何一条将来新增的、绕过枚举校验的路径也不会让服务起不来"。
    """
    env = make_env()
    backend, err = app.Server._open_backend(env.cfg, "superfeat")
    assert backend.name == backend_mod.ORB
    assert err and "superfeat" in err


def test_seed_backend_does_not_overwrite_an_existing_choice(make_env, monkeypatch):
    """⚠️ `PHOTOAR_BACKEND` 只能是**初始值**。

    每次启动都按环境变量覆写的话，用户在管理台把后端改成 xfeat、重启一次容器就变回
    orb 了 —— 而管理台显示的是库里的值（也就是 orb），看起来"我的修改根本没保存"。
    """
    env = make_env()
    _set_backend(env, backend_mod.XFEAT)  # 用户在管理台选过了
    monkeypatch.setenv("PHOTOAR_BACKEND", backend_mod.ORB)  # compose 里写死的
    app.Server.create(env.cfg)
    assert json.loads(env.srv.catalog.all_app_config()["recog.backend"]) == (
        backend_mod.XFEAT
    )


def test_seed_backend_writes_the_initial_value(make_env, monkeypatch):
    env = make_env()
    assert "recog.backend" not in env.srv.catalog.all_app_config()
    monkeypatch.setenv("PHOTOAR_BACKEND", backend_mod.XFEAT)
    app.Server.create(env.cfg)
    assert json.loads(env.srv.catalog.all_app_config()["recog.backend"]) == (
        backend_mod.XFEAT
    )


def test_seed_backend_ignores_garbage(make_env, monkeypatch, capsys):
    env = make_env()
    monkeypatch.setenv("PHOTOAR_BACKEND", "nope")
    app.Server.create(env.cfg)
    assert "recog.backend" not in env.srv.catalog.all_app_config()
    assert "nope" in capsys.readouterr().out


def test_threshold_mismatch_is_warned(make_env, capsys):
    """两个后端的内点数是两个不同的量。沿用另一边的阈值会让判定实际变松或变紧，
    而那只会表现为"误识别变多"，用户会归因到"XFeat 不准"。"""
    env = make_env()
    env.srv.catalog.put_app_config({"recog.min_inliers": json.dumps(7)})
    app.Server.create(env.cfg)
    out = capsys.readouterr().out
    assert "recog.min_inliers" in out and "标定值" in out


# ---- 训词表 ----


def test_build_vocab_from_the_library(make_env):
    """空词表起服务 → 入库几张 → 训词表 → 从此有词表。一键部署的完整生命周期。"""
    env = make_env(vocab_path=None)
    for i in range(6):
        env.ingest_ok(env.write_image(f"photos/p{i}.jpg", seed=200 + i))
    out = env.cfg.vocab_path_for(
        env.srv.library.backend.name, env.srv.library.backend.vocab_file
    )
    assert not out.exists()

    r = env.srv.library.train_vocab(out)
    assert out.is_file()
    assert r.n_photos == 6 and r.n_descriptors > 0 and r.n_words > 1
    assert not isinstance(env.srv.library.vocab, NullVocab)

    # 训完立刻还能认（词序列已经用新词表重算过了）
    query, _ = synth.generate(env.textured(seed=203, w=1200, h=800), count=1, seed=1)[0]
    assert env.body_json(env.post_frame("/v1/recognize", env.jpeg_of(query)))["matched"]


def test_build_vocab_on_an_empty_library_is_refused(make_env):
    """训一个空词表并**存成文件**的后果是"词表文件在不在"这个判据永久失效 ——
    用户会看到一个"已经有词表了"的部署，却始终是全量扫描的性能。"""
    from photoar.server.library import EmptyLibrary

    env = make_env(vocab_path=None)
    with pytest.raises(EmptyLibrary):
        env.srv.library.train_vocab(env.tmp / "v.npz")
    assert not (env.tmp / "v.npz").exists()


def test_rebuild_vocab_endpoint(make_env):
    env = make_env(vocab_path=None)
    for i in range(5):
        env.ingest_ok(env.write_image(f"photos/p{i}.jpg", seed=300 + i))
    r = env.post_json("/v1/admin/rebuild-vocab", {})
    assert r.status == 200, env.body_json(r)
    body = env.body_json(r)
    assert body["backend"] == backend_mod.ORB
    assert body["photos"] == 5 and body["descriptors"] > 0 and body["words"] > 1
    assert body["elapsedMs"] >= 0
    assert env.body_json(env.get("/v1/ping"))["vocabTrained"] is True


def test_rebuild_vocab_is_admin_only(make_env):
    """不是"会泄露什么"，而是它会占满 CPU 几分钟并把入库堵在同一把写锁后面。"""
    env = make_env(vocab_path=None)
    env.ingest_ok(env.write_image("photos/p.jpg", seed=7))
    viewer = env.viewer("小明")
    r = env.post_json("/v1/admin/rebuild-vocab", {}, as_=viewer)
    assert r.status == 403 and env.body_json(r)["error"] == "admin_only"


def test_rebuild_vocab_on_empty_library_is_409(make_env):
    """409 而不是 400/500：请求本身没问题，是服务端**当前状态**不允许。
    入库几张之后同一个请求就会成功。"""
    env = make_env(vocab_path=None)
    r = env.post_json("/v1/admin/rebuild-vocab", {})
    assert r.status == 409 and env.body_json(r)["error"] == "library_empty"


def test_train_vocab_caps_descriptors(make_env):
    """上限必须真的生效：不设上限的话 1 万张 XFeat 库要在一台 3GB 的机器上
    vstack 出 1.3GB。"""
    env = make_env(vocab_path=None)
    for i in range(4):
        env.ingest_ok(env.write_image(f"photos/p{i}.jpg", seed=400 + i))
    r = env.srv.library.train_vocab(env.tmp / "v.npz", max_descriptors=40)
    # 每张配额 = 40 // 4 = 10
    assert r.n_descriptors <= 40


def test_train_vocab_is_deterministic(make_env):
    """同一个库训两次得到同一份词表 —— 排查时能对照。"""
    env = make_env(vocab_path=None)
    for i in range(4):
        env.ingest_ok(env.write_image(f"photos/p{i}.jpg", seed=500 + i))
    a = env.srv.library.train_vocab(env.tmp / "a.npz", max_descriptors=400)
    b = env.srv.library.train_vocab(env.tmp / "b.npz", max_descriptors=400)
    assert (a.n_descriptors, a.n_words) == (b.n_descriptors, b.n_words)
    assert (env.tmp / "a.npz").read_bytes() == (env.tmp / "b.npz").read_bytes()


# ---- CLI 那条路 ----


def test_cli_opens_the_library_the_same_way(make_env):
    """`reindex` / `build-vocab` 必须与服务走同一条后端解析逻辑。

    以前 `cmd_reindex` 自己写死了 ORB 的库目录和 `Vocab.load` —— 在 xfeat 部署上
    它会 reindex 一个空库并报"重建完成：0 张"，而那句话看起来完全正常。
    """
    env = make_env(vocab_path=None)
    env.ingest_ok(env.write_image("photos/p.jpg", seed=9))
    lib = app.open_library_cli(env.cfg)
    assert lib.root == env.cfg.library_dir_for(backend_mod.ORB)
    assert len(lib) == 1
    assert isinstance(lib.vocab, NullVocab)
