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
