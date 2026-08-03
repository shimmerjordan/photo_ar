"""`tools/fetch_models.py`：幂等、校验、以及失败时不留半成品。

这个脚本在 entrypoint 里、服务起来**之前**跑，每次容器启动都跑一次。所以它的三条
性质各自都是一次真实的部署故障：

- 不幂等 → 每次重启重下一遍，没外网的那次启动直接失败。
- 不校验 → 一个截断的 onnx 在 `InferenceSession` 那里抛 protobuf 解析错误，
  读起来像"模型格式不对"，完全不像"你下载的东西不完整"。
- 校验失败不删 → 那个坏文件永久留在卷上，下次启动的幂等检查把它跳过。

用一个 localhost 上的 `http.server` 当下载源，不 mock urllib：要测的恰恰是
"真的走一遍 HTTP 然后落盘"这条路。
"""

import functools
import hashlib
import http.server
import importlib.util
import threading
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _load_fetch_models():
    """按文件路径导入 `tools/fetch_models.py`。

    `tools/` 不是一个包（里面是几个可以直接跑的脚本，而且 `tools/arcoreimg` 是个
    二进制），所以不能 `from tools import fetch_models`。
    """
    spec = importlib.util.spec_from_file_location(
        "photoar_fetch_models", _TOOLS / "fetch_models.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fm = _load_fetch_models()


@pytest.fixture
def payload():
    """一份假"模型"。内容任意 —— 这个脚本只关心字节和它们的 sha256。"""
    data = bytes(range(256)) * 40  # 10240 字节
    return data, hashlib.sha256(data).hexdigest()


@pytest.fixture
def server(tmp_path, payload):
    """在 tmp 目录上起一个 http.server，返回 `(base_url, 根目录)`。"""
    data, _ = payload
    root = tmp_path / "srv"
    root.mkdir()
    (root / "model.bin").write_bytes(data)
    (root / "truncated.bin").write_bytes(data[:100])

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(root)
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # 服务器自己的日志会把 pytest 的输出刷满，而它一条都没用
    httpd.RequestHandlerClass.log_message = lambda *a, **k: None
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", root
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_downloads_and_verifies(server, tmp_path, payload):
    base, _ = server
    data, sha = payload
    out = tmp_path / "out" / "model.bin"
    downloaded, why = fm.fetch(out, f"{base}/model.bin", expected_sha=sha)
    assert downloaded is True
    assert out.read_bytes() == data
    assert "校验通过" in why


def test_is_idempotent(server, tmp_path, payload):
    """已存在且校验通过就不重下 —— 容器每次启动都会调它，而那次启动可能没外网。"""
    base, _ = server
    data, sha = payload
    out = tmp_path / "model.bin"
    out.write_bytes(data)
    downloaded, why = fm.fetch(out, f"{base}/model.bin", expected_sha=sha)
    assert downloaded is False and "跳过下载" in why


def test_idempotent_even_when_the_url_is_dead(tmp_path, payload):
    """幂等这条性质在**没有网**的时候才真正有价值，所以单独测一次：地址完全不通，
    但文件已经在了 → 成功。"""
    data, sha = payload
    out = tmp_path / "model.bin"
    out.write_bytes(data)
    downloaded, _ = fm.fetch(
        out, "http://127.0.0.1:1/nope", expected_sha=sha  # 端口 1，必然连不上
    )
    assert downloaded is False


def test_existing_but_wrong_file_is_redownloaded(server, tmp_path, payload):
    """卷上那份是坏的时候必须重下，否则坏文件永久留着。"""
    base, _ = server
    data, sha = payload
    out = tmp_path / "model.bin"
    out.write_bytes(b"garbage")
    downloaded, _ = fm.fetch(out, f"{base}/model.bin", expected_sha=sha)
    assert downloaded is True and out.read_bytes() == data


def test_truncated_download_is_deleted_and_reported(server, tmp_path, payload):
    """校验不过 → 删掉半成品 + 明确报错。留着它下次启动会被幂等检查跳过。"""
    base, _ = server
    _, sha = payload
    # 单独一个空目录：`tmp_path` 里还有 http.server 的根目录（`srv/`），
    # 直接在 tmp_path 上断言"什么都没剩"会把它算进去。
    dest = tmp_path / "vol"
    dest.mkdir()
    out = dest / "model.bin"
    with pytest.raises(fm.FetchFailed, match="校验不过"):
        fm.fetch(out, f"{base}/truncated.bin", expected_sha=sha)
    assert not out.exists()
    # 连临时文件都不能留（`.part` 之类）—— 留着的话下一次启动的幂等检查会
    # 看到一个陌生文件，而且卷会随失败次数慢慢长大。
    assert list(dest.iterdir()) == []


def test_404_gives_actionable_advice(server, tmp_path, payload):
    """默认那个 GitHub release 目前**确实不存在**，所以 404 是预期路径 ——
    它必须给出可执行的下一步，而不是一句"下载失败"。"""
    base, _ = server
    _, sha = payload
    with pytest.raises(fm.FetchFailed) as exc:
        fm.fetch(tmp_path / "m.bin", f"{base}/no-such-file.bin", expected_sha=sha)
    msg = str(exc.value)
    assert "404" in msg
    assert "export_models.py" in msg  # 出路 1：自己导出
    assert "release" in msg  # 出路 2：发布 release
    assert "--url" in msg  # 出路 3：指别处
    assert "不影响服务启动" in msg  # 以及"这不是致命的"


def test_unreachable_host_gives_the_same_advice(tmp_path, payload):
    _, sha = payload
    with pytest.raises(fm.FetchFailed, match="连不上"):
        fm.fetch(tmp_path / "m.bin", "http://127.0.0.1:1/x", expected_sha=sha)
    assert not (tmp_path / "m.bin").exists()


def test_the_expected_sha_matches_the_real_export():
    """常量本身。改模型必须同时改这两行 —— 那正是应该被 code review 看到的改动。

    这里只钉住格式与自洽（64 位十六进制、字节数是那个 4.31MB），不去碰真实文件：
    仓库里没有 xfeat.onnx（它不进版本库也不进镜像），测试不能依赖开发机上恰好有一份。
    """
    assert len(fm.EXPECTED_SHA256) == 64
    assert set(fm.EXPECTED_SHA256) <= set("0123456789abcdef")
    assert fm.EXPECTED_BYTES == 4_313_719
    assert fm.DEFAULT_URL.startswith("https://github.com/")


def test_out_can_be_a_directory(server, tmp_path, payload):
    """compose 里那个变量是"模型目录"（同一个目录还放词表），让用户在两处写同一个
    文件名迟早写歪一处。"""
    base, _ = server
    data, sha = payload
    d = tmp_path / "models"
    d.mkdir()
    rc = fm.main(
        ["--out", str(d), "--url", f"{base}/model.bin", "--sha256", sha]
    )
    assert rc == 0
    assert (d / fm.MODEL_FILENAME).read_bytes() == data


def test_main_returns_1_on_failure(server, tmp_path, payload, capsys):
    """entrypoint 靠这个返回码决定要不要打那句"模型没取到"的警告。"""
    base, _ = server
    _, sha = payload
    rc = fm.main(
        ["--out", str(tmp_path / "m.bin"), "--url", f"{base}/nope", "--sha256", sha]
    )
    assert rc == 1
    assert "取不到" in capsys.readouterr().err


def test_verify_reports_the_size_difference_first(tmp_path):
    """先比大小：不用读完整个文件就能否掉最常见的坏法，而且"少了多少字节"比一个
    不匹配的十六进制串好读得多。

    用**默认**那个 sha（不是测试自造的），因为字节数只对那一个已知产物成立 ——
    见下面 `test_custom_sha_skips_the_size_gate`。
    """
    p = tmp_path / "m.bin"
    p.write_bytes(b"x" * 100)
    ok, why = fm.verify(p, fm.EXPECTED_SHA256)
    assert not ok and "字节" in why and "-4313619" in why


def test_custom_sha_skips_the_size_gate(tmp_path, payload):
    """⚠️ 这条是被测试挖出来的一个真 bug 的回归。

    `verify` 原来无条件拿模块常量 `EXPECTED_BYTES` 当期望大小。于是 `--sha256`
    这个 override **永远校验失败**（换了模型的字节数一定不是 4,313,719），而失败
    信息说的是"大小不对"—— 看起来像文件下坏了，跟"你覆盖了 sha"毫无关系。
    字节数是**某一个具体产物**的属性，不是一条全局规则。
    """
    data, sha = payload
    p = tmp_path / "m.bin"
    p.write_bytes(data)
    assert fm.expected_bytes_for(sha) is None
    assert fm.expected_bytes_for(fm.EXPECTED_SHA256) == fm.EXPECTED_BYTES
    ok, why = fm.verify(p, sha)
    assert ok, why
