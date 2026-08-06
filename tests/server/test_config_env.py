"""纯环境变量配置（`ServerConfig.from_env`）与后端相关的路径派生。

这条路径是 `docker compose up -d` 实际走的那条，所以它值得与 JSON 那条一样密的测试。
重点不在"字段读对了"，而在几个**静默失败**的地方：空串环境变量会不会盖掉默认值、
根目录重名会不会悄悄丢一个、两个后端的库目录会不会撞在一起。
"""

import json

import pytest

from photoar import backend as backend_mod
from photoar.server.config import ConfigError, ServerConfig, parse_roots


@pytest.fixture
def clean_env(monkeypatch):
    """把全部 PHOTOAR_* 清掉再测。

    不清的话，开发机上恰好导出过的一个 PHOTOAR_TOKEN 会让"没配 token 也能起"这条
    测试变成在测"配了 token 能起" —— 而它照样绿。
    """
    for name in list(__import__("os").environ):
        if name.startswith("PHOTOAR_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---- parse_roots ----


def test_named_roots():
    assert parse_roots("photos=/share/Photo,video=/share/Video") == {
        "photos": "/share/Photo",
        "video": "/share/Video",
    }


def test_bare_paths_take_the_directory_name():
    """`.env` 里最自然的那个写法。不支持它的话，用户明明配了却得到"必须配置 roots"。"""
    assert parse_roots("/share/Photo,/share/Video") == {
        "Photo": "/share/Photo",
        "Video": "/share/Video",
    }


def test_trailing_slash_and_spaces_are_tolerated():
    assert parse_roots(" /share/Photo/ , 视频=/share/Video ") == {
        "Photo": "/share/Photo/",
        "视频": "/share/Video",
    }


def test_duplicate_names_are_rejected_not_silently_merged():
    """后者覆盖前者的话，其中一个目录整体访问不到，而界面上只是少了一项。"""
    with pytest.raises(ConfigError, match="都叫"):
        parse_roots("/a/Photo,/b/Photo")


def test_same_name_same_path_is_fine():
    """重复写同一条不算冲突（compose 里两处都写了同一个目录是常见的）。"""
    assert parse_roots("/share/Photo,/share/Photo") == {"Photo": "/share/Photo"}


def test_empty_string_gives_no_roots():
    assert parse_roots("") == {}
    assert parse_roots(" , , ") == {}


# ---- from_env ----


def test_from_env_minimal(clean_env, tmp_path):
    """**只**设 PHOTOAR_ROOTS 就要能构造出配置来（这是一键部署的下限）。"""
    clean_env.setenv("PHOTOAR_ROOTS", f"nas={tmp_path}")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path / "data"))
    cfg = ServerConfig.from_env()
    assert cfg.roots == {"nas": str(tmp_path)}
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.token == ""  # 没配 token 也能起
    assert cfg.vocab_path is None  # 没配词表也能起
    assert cfg.bind == "0.0.0.0"
    assert cfg.port == 8964


def test_upload_dir_outside_roots_gets_enrolled(clean_env, tmp_path, capsys):
    """上传目录是白名单根的**兄弟目录**时，自动收编成一个根，并大声说出来。

    真实部署踩过的原样：roots 配了 photos/videos，PHOTOAR_UPLOAD_DIR 指了它们的
    兄弟 inbox —— 不收编的话每一次上传都 403 path_denied，而 403 的响应体刻意不回显
    路径，用户看到的只有"传不上去"。
    """
    clean_env.setenv(
        "PHOTOAR_ROOTS",
        f"photos={tmp_path}/photos,videos={tmp_path}/videos",
    )
    clean_env.setenv("PHOTOAR_UPLOAD_DIR", f"{tmp_path}/inbox")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path / "data"))
    cfg = ServerConfig.from_env()
    assert cfg.roots.get("upload") == f"{tmp_path}/inbox"
    assert cfg.upload_dir_root == f"{tmp_path}/inbox"
    out = capsys.readouterr().out
    assert "自动收编" in out and f"{tmp_path}/inbox" in out


def test_upload_dir_inside_a_root_is_left_alone(clean_env, tmp_path, capsys):
    """在某个根**里面**的上传目录不该多出一个根，也不该有警告 —— 那是配对了的样子。"""
    clean_env.setenv("PHOTOAR_ROOTS", f"nas={tmp_path}")
    clean_env.setenv("PHOTOAR_UPLOAD_DIR", f"{tmp_path}/inbox")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path / "data"))
    cfg = ServerConfig.from_env()
    assert set(cfg.roots) == {"nas"}
    assert "自动收编" not in capsys.readouterr().out


def test_upload_dir_equal_to_a_root_is_left_alone(clean_env, tmp_path):
    """上传目录**就是**某个根（边界情形）：算被覆盖，不收编。"""
    clean_env.setenv("PHOTOAR_ROOTS", f"nas={tmp_path}")
    clean_env.setenv("PHOTOAR_UPLOAD_DIR", str(tmp_path))
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path / "data"))
    assert set(ServerConfig.from_env().roots) == {"nas"}


def test_enrolled_root_name_avoids_collision(clean_env, tmp_path):
    """用户已经有一个叫 upload 的根时，收编的名字要让开，不能悄悄顶掉他的。"""
    clean_env.setenv(
        "PHOTOAR_ROOTS", f"upload={tmp_path}/elsewhere,nas={tmp_path}/nas"
    )
    clean_env.setenv("PHOTOAR_UPLOAD_DIR", f"{tmp_path}/inbox")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path / "data"))
    cfg = ServerConfig.from_env()
    assert cfg.roots["upload"] == f"{tmp_path}/elsewhere"  # 用户的没被动
    assert cfg.roots["upload2"] == f"{tmp_path}/inbox"


def test_enrolled_upload_dir_actually_accepts_uploads(clean_env, tmp_path):
    """收编的终点不是"配置里多了一个根"，是**上传真的能落地** —— 用 SafeRoots
    验到底，别让"收编了但校验还是不过"这种半吊子状态活下来。"""
    from photoar.server.safepath import Roots

    clean_env.setenv(
        "PHOTOAR_ROOTS",
        f"photos={tmp_path}/photos,videos={tmp_path}/videos",
    )
    clean_env.setenv("PHOTOAR_UPLOAD_DIR", f"{tmp_path}/inbox")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path / "data"))
    cfg = ServerConfig.from_env()
    (tmp_path / "inbox").mkdir()
    roots = Roots(cfg.roots)
    # _upload 落地前做的就是这一步；收编之前这里抛 PathDenied → 403。
    dst = roots.resolve(f"{tmp_path}/inbox/婚礼 照片.jpg")
    assert dst.name == "婚礼 照片.jpg"


def test_from_env_requires_roots(clean_env):
    """唯一必填的东西。信息里要写清两种写法，否则用户只知道"缺了个变量"。"""
    with pytest.raises(ConfigError, match="PHOTOAR_ROOTS"):
        ServerConfig.from_env()


def test_from_env_rejects_relative_roots(clean_env, tmp_path):
    """白名单根必须是绝对路径 —— 相对路径的解析结果取决于进程的 cwd。"""
    clean_env.setenv("PHOTOAR_ROOTS", "nas=share/Photo")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path))
    with pytest.raises(ConfigError, match="绝对路径"):
        ServerConfig.from_env()


def test_blank_env_vars_do_not_clobber_defaults(clean_env, tmp_path):
    """⚠️ 这一条是 `from_env` 里那个循环存在的全部理由。

    compose 里写 `PHOTOAR_VIDEO_ENCODER: ${X:-}` 而 X 没定义时，容器里拿到的是**空串**。
    照着 `doc[key] = os.environ.get(env)` 写的话，`video_encoder` 会从 "auto" 变成 ""，
    然后 `transcode.resolve_encoder` 抱怨一个用户从没配过的编码器。
    """
    clean_env.setenv("PHOTOAR_ROOTS", f"nas={tmp_path}")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path))
    for name in (
        "PHOTOAR_VIDEO_ENCODER",
        "PHOTOAR_UPLOAD_DIR",
        "PHOTOAR_VOCAB",
        "PHOTOAR_MODELS",
        "PHOTOAR_BIND",
    ):
        clean_env.setenv(name, "")
    cfg = ServerConfig.from_env()
    assert cfg.video_encoder == "auto"
    assert cfg.upload_dir_root is None
    assert cfg.vocab_path is None
    assert cfg.models_dir is None
    assert cfg.bind == "0.0.0.0"


def test_from_env_reads_the_optional_ones(clean_env, tmp_path):
    clean_env.setenv("PHOTOAR_ROOTS", f"nas={tmp_path}")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path / "d"))
    clean_env.setenv("PHOTOAR_MODELS", str(tmp_path / "m"))
    clean_env.setenv("PHOTOAR_VOCAB", str(tmp_path / "v.npz"))
    clean_env.setenv("PHOTOAR_TOKEN", "tok-1")
    clean_env.setenv("PHOTOAR_PORT", "9001")
    clean_env.setenv("PHOTOAR_BIND", "127.0.0.1")
    clean_env.setenv("PHOTOAR_ADMIN_NAME", "老板")
    clean_env.setenv("PHOTOAR_ADMIN_PASSWORD", "pw")
    clean_env.setenv("PHOTOAR_COOKIE_SECURE", "1")
    clean_env.setenv("PHOTOAR_VIDEO_ENCODER", "h264_vaapi")
    clean_env.setenv("PHOTOAR_UPLOAD_DIR", str(tmp_path / "inbox"))
    cfg = ServerConfig.from_env()
    assert (cfg.token, cfg.port, cfg.bind) == ("tok-1", 9001, "127.0.0.1")
    assert (cfg.admin_name, cfg.admin_password) == ("老板", "pw")
    assert cfg.cookie_secure is True
    assert cfg.video_encoder == "h264_vaapi"
    assert cfg.model_dir == tmp_path / "m"
    assert cfg.vocab_path == tmp_path / "v.npz"
    assert cfg.upload_dir_root == str(tmp_path / "inbox")


def test_admin_name_of_only_spaces_falls_back(clean_env, tmp_path):
    """`PHOTOAR_ADMIN_NAME=" "` 是 truthy 的，会一路走到 check_name 抛 InvalidName ——
    于是服务起不来，原因是一个看不见的空格。（既有行为，这里补一条 from_env 上的。）"""
    clean_env.setenv("PHOTOAR_ROOTS", f"nas={tmp_path}")
    clean_env.setenv("PHOTOAR_DATA", str(tmp_path))
    clean_env.setenv("PHOTOAR_ADMIN_NAME", "   ")
    assert ServerConfig.from_env().admin_name == "admin"


# ---- 词表与库目录怎么按后端派生 ----


def _cfg(tmp_path, **kw) -> ServerConfig:
    return ServerConfig.from_dict(
        {"roots": {"nas": "/tmp"}, "data_dir": str(tmp_path), **kw}
    )


def test_orb_library_dir_is_unchanged(tmp_path):
    """已有部署的库在 `data/library`。换个名字等于让它们全部"照片不见了"。"""
    cfg = _cfg(tmp_path)
    assert cfg.library_dir == tmp_path / "library"
    assert cfg.library_dir_for(backend_mod.ORB) == tmp_path / "library"


def test_xfeat_library_dir_is_separate(tmp_path):
    """两个后端的 slot 布局不兼容，混用会读出错位的 slot 而**不报错**。"""
    cfg = _cfg(tmp_path)
    assert cfg.library_dir_for(backend_mod.XFEAT) == tmp_path / "library_xfeat"
    assert cfg.library_dir_for(backend_mod.XFEAT) != cfg.library_dir


def test_vocab_defaults_to_per_backend_file_under_models(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.vocab_path_for(backend_mod.ORB, "vocab.npz") == (
        tmp_path / "models" / "vocab.npz"
    )
    assert cfg.vocab_path_for(backend_mod.XFEAT, "vocab_xfeat.npz") == (
        tmp_path / "models" / "vocab_xfeat.npz"
    )


def test_explicit_vocab_path_applies_to_orb_only(tmp_path):
    """显式 `vocab_path` 是"只有 ORB 的年代"留下的字段，它指向的一定是二进制词表。

    把它当成 XFeat 的词表去 load 最好的结果是 KeyError；要防的是两种 npz 的键名将来
    恰好对得上 —— 那时读出来的是一棵毫无意义的树，粗排召回静默崩塌。
    """
    cfg = _cfg(tmp_path, vocab_path="/data/legacy-vocab.npz")
    from pathlib import Path

    assert cfg.vocab_path_for(backend_mod.ORB, "vocab.npz") == Path(
        "/data/legacy-vocab.npz"
    )
    assert cfg.vocab_path_for(backend_mod.XFEAT, "vocab_xfeat.npz") == (
        tmp_path / "models" / "vocab_xfeat.npz"
    )


def test_from_dict_no_longer_requires_token_or_vocab(tmp_path):
    """全新部署那一刻两者都不可能有：词表要用库里的描述子训，而库是空的。"""
    cfg = ServerConfig.from_dict({"roots": {"nas": "/tmp"}, "data_dir": str(tmp_path)})
    assert cfg.token == "" and cfg.vocab_path is None


def test_ensure_dirs_creates_the_model_dir(tmp_path):
    """`build-vocab` 要往 model_dir 里写。不建的话那次训练会在最后一步 save 失败，
    而训练本身可能已经跑了几分钟。"""
    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    assert cfg.model_dir.is_dir()


def test_unknown_keys_still_land_in_extra(tmp_path):
    """models_dir 加进了"已知字段"名单，别把别人的自定义键也一起吃掉。"""
    cfg = _cfg(tmp_path, models_dir=str(tmp_path / "m"), my_own_thing=1)
    assert cfg.extra == {"my_own_thing": 1}
    assert json.dumps(cfg.extra)  # 可序列化，没混进 Path
