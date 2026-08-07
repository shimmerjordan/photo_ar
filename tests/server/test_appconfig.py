"""热配置。四件必须钉住的事：

1. **默认值等于代码里的常量**。`recog.min_inliers` 的默认值必须就是
   `verify.MIN_INLIERS` 那个经过标定的 40。抄一份字面量到 appconfig 里，以后重新
   标定改了 verify.py，"我没改过配置"的用户跑的就是旧阈值 —— 而两处数字不一样这
   件事没有任何报错。
2. **非法输入必须抛**，因为管理台会把用户在输入框里打的东西直接丢过来。
3. **一批里有一个非法值就整批不写**。半套生效的配置（阈值改了、后端没改）是最难
   排查的一类状态。
4. **坏数据不能让服务起不来**。一行手工改坏的 JSON 只该让那一个字段回退到默认值。
"""

import json

import pytest

from photoar import backend as recog_backend
from photoar import recognizer, verify
from photoar.server import appconfig, auth, db


class Clock:
    def __init__(self, start: int = 1_700_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance_s(self, seconds: float) -> None:
        self.now += int(seconds * 1000)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def catalog(tmp_path):
    return db.Catalog(tmp_path / "catalog.db")


@pytest.fixture
def make_cfg(catalog, clock):
    def _make(**kw):
        kw.setdefault("ttl_s", 0)  # 默认关缓存，绝大多数测试关心的是值本身
        return appconfig.AppConfig(catalog, now_ms=clock, **kw)

    return _make


@pytest.fixture
def cfg(make_cfg):
    return make_cfg()


# ---- 字段表 ----


def test_defaults_come_from_the_code_constants(cfg):
    """这条断言的全部意义在于：它会在有人重新标定阈值却忘了这里时红。

    反过来说，如果哪天 verify.MIN_INLIERS 改了而本测试仍然绿，说明 appconfig 里
    写的是字面量而不是引用 —— 那正是要防的那件事。
    """
    assert cfg.get("recog.min_inliers") == verify.MIN_INLIERS
    assert cfg.get("recog.ratio") == verify.RATIO
    assert cfg.get("recog.top_k") == recognizer.TOP_K
    assert cfg.get("recog.backend") == recog_backend.ORB
    assert cfg.get("video.fit_mode") == db.FIT_FILL
    assert cfg.get("session.viewer_days") == auth.VIEWER_TTL_DAYS
    assert cfg.get("session.admin_hours") == auth.ADMIN_TTL_HOURS
    assert cfg.get("ingest.dedup_gate") is True


def test_field_table_is_well_formed():
    """字段声明本身的自检。管理台完全按这张表画表单，缺一项范围就变成一个没有
    校验的输入框。"""
    keys = [f.key for f in appconfig.FIELDS]
    assert len(keys) == len(set(keys)), "key 重复会让后一个静默盖掉前一个"
    for f in appconfig.FIELDS:
        assert f.label and f.help, f"{f.key} 缺中文标签或说明"
        if f.kind == appconfig.KIND_ENUM:
            assert f.choices, f"{f.key} 是枚举但没给取值集合"
        if f.kind in (appconfig.KIND_INT, appconfig.KIND_FLOAT):
            assert f.minimum is not None and f.maximum is not None, (
                f"{f.key} 是数值但没给上下界；没有界的数值输入框等于没有校验"
            )


def test_every_default_survives_its_own_validation():
    """默认值必须自己就能通过校验。

    默认值是从别的模块 import 来的常量，而范围是在这里手写的。两者对不上时的表现
    是"没人改过任何配置，但一 patch 就报越界"——而且只在那一个字段上出现。
    """
    for f in appconfig.FIELDS:
        assert appconfig.coerce(f, f.default) == f.default


def test_required_keys_exist():
    keys = {f.key for f in appconfig.FIELDS}
    assert keys >= {
        "recog.backend", "recog.min_inliers", "recog.ratio", "recog.top_k",
        "ingest.dedup_gate",
        "video.fit_mode", "session.viewer_days", "session.admin_hours",
    }


def test_backend_switch_needs_restart_but_thresholds_do_not():
    """哪些字段要重启不是随口标的：换后端会换掉描述子库的 slot 布局与 ONNX 会话，
    而阈值是每次判定时读的。标错的方向都很糟 —— 该重启的没标，用户以为换了后端；
    不该重启的标了，每次改个阈值都被要求重启一次服务。"""
    by_key = {f.key: f for f in appconfig.FIELDS}
    assert by_key["recog.backend"].needs_restart
    assert by_key["session.viewer_days"].needs_restart
    assert by_key["session.admin_hours"].needs_restart
    assert not by_key["recog.min_inliers"].needs_restart
    assert not by_key["recog.ratio"].needs_restart
    assert not by_key["ingest.dedup_gate"].needs_restart
    assert not by_key["video.fit_mode"].needs_restart


# ---- 读写 ----


def test_patch_persists_across_instances(catalog, cfg, clock):
    cfg.patch({"recog.min_inliers": 45})
    assert cfg.get("recog.min_inliers") == 45
    again = appconfig.AppConfig(catalog, ttl_s=0, now_ms=clock)
    assert again.get("recog.min_inliers") == 45


def test_patch_returns_only_changed_keys_that_need_restart(cfg):
    assert cfg.patch({"recog.min_inliers": 45}) == []
    assert cfg.patch({"recog.backend": "xfeat"}) == ["recog.backend"]
    assert cfg.patch(
        {"recog.backend": "orb", "session.admin_hours": 6, "recog.top_k": 30}
    ) == ["recog.backend", "session.admin_hours"], "顺序按 FIELDS，不按提交顺序"


def test_patching_the_same_value_reports_no_restart(cfg):
    """否则每次点保存都被告知"需要重启"，哪怕什么都没变。喊几次狼来了之后，真需要
    重启时也不会有人当真。"""
    cfg.patch({"recog.backend": "xfeat"})
    assert cfg.patch({"recog.backend": "xfeat"}) == []


def test_patch_of_default_value_is_a_noop(catalog, cfg):
    cfg.patch({"recog.min_inliers": verify.MIN_INLIERS})
    assert catalog.all_app_config() == {}, "与默认值相同就不必落一行"


def test_empty_patch(cfg):
    assert cfg.patch({}) == []


def test_all_returns_a_copy(cfg):
    """返回缓存本身的话，调用方随手改一个键就会改到所有线程共享的那份，而且在 TTL
    到期前一直有效 —— 一个没有任何人写库的"配置被改了"。"""
    snapshot = cfg.all()
    snapshot["recog.top_k"] = 999
    assert cfg.get("recog.top_k") == recognizer.TOP_K


def test_describe_covers_every_field_with_its_current_value(cfg):
    cfg.patch({"recog.top_k": 25})
    rows = {row["key"]: row for row in cfg.describe()}
    assert set(rows) == {f.key for f in appconfig.FIELDS}
    top_k = rows["recog.top_k"]
    assert top_k["value"] == 25
    assert top_k["default"] == recognizer.TOP_K
    assert top_k["min"] == 1 and top_k["max"] == 200
    assert top_k["needsRestart"] is False
    assert rows["recog.backend"]["choices"] == list(recog_backend.NAMES)
    # 整张表必须能直接 json.dumps —— 管理台就是拿它当响应体。
    assert json.loads(json.dumps(cfg.describe()))


# ---- 校验 ----


def test_unknown_key_is_rejected(cfg):
    """静默忽略未知 key 的表现是"保存成功但值没变"，而用户会以为是缓存问题反复重试。"""
    with pytest.raises(appconfig.BadConfigKey):
        cfg.patch({"recog.minInliers": 40})  # 驼峰写错
    with pytest.raises(appconfig.BadConfigKey):
        cfg.patch({"": 1})


def test_string_forms_from_html_forms_are_accepted(cfg):
    """HTML 表单提交的一律是字符串。只接受 JSON 原生类型的话，同一个界面换个提交
    方式就全线报错。"""
    assert cfg.patch({"recog.min_inliers": "45"}) == []
    assert cfg.get("recog.min_inliers") == 45
    cfg.patch({"recog.ratio": " 2.5 "})
    assert cfg.get("recog.ratio") == 2.5
    cfg.patch({"ingest.dedup_gate": "off"})
    assert cfg.get("ingest.dedup_gate") is False
    cfg.patch({"ingest.dedup_gate": "on"})
    assert cfg.get("ingest.dedup_gate") is True


def test_int_field_rejects_non_integers(cfg):
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.min_inliers": "abc"})
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.min_inliers": None})
    # 40.5 不是整数，悄悄截断成 40 等于替用户改主意
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.min_inliers": 40.5})


def test_int_field_accepts_a_float_that_is_a_whole_number(cfg):
    """有些 JSON 序列化器会把 45 写成 45.0，那是同一个整数。"""
    cfg.patch({"recog.min_inliers": 45.0})
    assert cfg.get("recog.min_inliers") == 45


def test_bool_is_not_an_integer_and_vice_versa(cfg):
    """`isinstance(True, int)` 在 Python 里是 True。判断顺序写反的话，True 会被当成
    整数 1 一路走下去 —— 于是 `recog.top_k: true` 会静默把粗排候选数设成 1。"""
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.top_k": True})
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.ratio": True})
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"ingest.dedup_gate": 2})
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"ingest.dedup_gate": "maybe"})


def test_ranges_are_enforced_at_both_ends(cfg):
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.min_inliers": 0})
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.min_inliers": 501})
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.ratio": 0.9})
    # 边界本身是合法的（闭区间）
    cfg.patch({"recog.min_inliers": 1, "recog.ratio": 1.0})


def test_non_finite_floats_are_rejected(cfg):
    """NaN 会让后面所有比较都返回 False（`inliers >= nan` 恒假 = 永远不命中），
    而它在管理台上显示出来就是个普通的 nan，没人会觉得它是原因。"""
    for bad in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
        with pytest.raises(appconfig.BadConfigValue):
            cfg.patch({"recog.ratio": bad})


def test_enum_only_accepts_declared_choices(cfg):
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.backend": "sift"})
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"video.fit_mode": "stretch"})
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.backend": 1})
    cfg.patch({"video.fit_mode": " fit "})  # 首尾空白容错
    assert cfg.get("video.fit_mode") == db.FIT_FIT


def test_patch_is_all_or_nothing(catalog, cfg):
    """一批里有一个非法值 → 整批不写。"""
    with pytest.raises(appconfig.BadConfigValue):
        cfg.patch({"recog.top_k": 30, "recog.min_inliers": 9999})
    assert catalog.all_app_config() == {}
    assert cfg.get("recog.top_k") == recognizer.TOP_K

    with pytest.raises(appconfig.BadConfigKey):
        cfg.patch({"recog.top_k": 30, "nope": 1})
    assert cfg.get("recog.top_k") == recognizer.TOP_K


def test_patch_rejects_non_dict(cfg):
    with pytest.raises(appconfig.ConfigRejected):
        cfg.patch([("recog.top_k", 30)])


def test_get_unknown_key_raises(cfg):
    with pytest.raises(appconfig.BadConfigKey):
        cfg.get("recog.nope")


def test_coerce_handles_plain_strings():
    """`KIND_STR` 目前没有字段在用（十个 key 都是 bool/int/float/enum），但 coerce
    支持它，所以直接对着一个合成 Field 测 —— 否则这段是没人跑过的代码，等第一个
    字符串字段加进来时才第一次执行。"""
    field = appconfig.Field(
        key="x.note", kind=appconfig.KIND_STR, default="", label="备注", help="随便写"
    )
    assert appconfig.coerce(field, "hi") == "hi"
    with pytest.raises(appconfig.BadConfigValue):
        appconfig.coerce(field, 1)


def test_unimplemented_kind_is_a_code_error_not_user_error():
    """FIELDS 里写了一个 coerce 不认识的 kind 是代码错误，不该伪装成"用户输入非法"
    （那会变成一个 400，而 400 会被当成用户的问题去查）。"""
    field = appconfig.Field(key="x", kind="uuid", default=None, label="l", help="h")
    with pytest.raises(AssertionError):
        appconfig.coerce(field, "whatever")


# ---- 缓存 ----


def test_cache_holds_for_its_ttl_then_refreshes(catalog, make_cfg, clock):
    """TTL 内不再查库，过了就重新读。用直接写库（绕过 patch）来制造"别人改了配置"。"""
    cfg = make_cfg(ttl_s=2.0)
    assert cfg.get("recog.top_k") == recognizer.TOP_K
    catalog.put_app_config({"recog.top_k": "30"})
    assert cfg.get("recog.top_k") == recognizer.TOP_K, "TTL 内应当还用缓存"

    clock.advance_s(2)
    assert cfg.get("recog.top_k") == 30


def test_patch_invalidates_the_cache_immediately(catalog, make_cfg):
    """自己改的必须马上看得到，不能等 TTL —— 管理台保存后立刻回读的就是这个值。"""
    cfg = make_cfg(ttl_s=3600.0)
    assert cfg.get("recog.top_k") == recognizer.TOP_K
    cfg.patch({"recog.top_k": 30})
    assert cfg.get("recog.top_k") == 30


def test_invalidate_picks_up_external_writes(catalog, make_cfg):
    cfg = make_cfg(ttl_s=3600.0)
    cfg.get("recog.top_k")
    catalog.put_app_config({"recog.top_k": "30"})
    cfg.invalidate()
    assert cfg.get("recog.top_k") == 30


# ---- 坏数据 ----


def test_unparseable_json_falls_back_to_default(catalog, cfg, capsys):
    """一行手工改坏的 JSON 不能让每个接口都 500。"""
    catalog.put_app_config({"recog.ratio": "not-json"})
    assert cfg.get("recog.ratio") == verify.RATIO
    assert "recog.ratio" in capsys.readouterr().out, "回退要留一行日志，否则无从发现"


def test_out_of_range_stored_value_falls_back_to_default(catalog, cfg):
    """库里的值也要过一遍校验：它可能是手工改的，也可能是更高版本写进去的
    （那个版本的上限更宽）。"""
    catalog.put_app_config({"recog.min_inliers": "99999"})
    assert cfg.get("recog.min_inliers") == verify.MIN_INLIERS


def test_unknown_stored_key_is_ignored(catalog, cfg):
    """库里多出来的 key 不影响读取。它只可能来自被降级回来的更高版本。"""
    catalog.put_app_config({"future.feature": "true"})
    assert "future.feature" not in cfg.all()
    assert cfg.get("recog.top_k") == recognizer.TOP_K


def test_stored_wrong_type_falls_back(catalog, cfg):
    catalog.put_app_config({"ingest.dedup_gate": json.dumps("maybe")})
    assert cfg.get("ingest.dedup_gate") is True
