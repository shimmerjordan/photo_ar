import stat
import textwrap

import cv2
import numpy as np
import pytest


@pytest.fixture
def fake_arcoreimg(tmp_path):
    """造一个假的 arcoreimg，让测试不依赖真实二进制。

    行为：eval-img 打印固定分数；build-db 写出一个固定大小的文件，并校验
    清单行格式（列数、路径是否绝对）。传 expected_width_m 时还会校验第三列
    （物理宽度）是否与期望值一致——I6（"第六个假检查"）：原来这个 fake 只
    校验列数和路径是否绝对，从不看 parts[2]（宽度），print_width_m 这个
    参数存在的唯一理由就是要把物理宽度正确地烘进清单，却完全没有测试端到
    端验证过。expected_width_m 缺省为 None 时不做这项校验，维持旧测试不
    关心宽度值的行为不变。

    从 tests/test_quality.py 挪到这里（conftest.py），因为 tests/test_cli.py
    的 I6 覆盖（CLI 的 --print-width-mm 转换）也需要用同一个 fixture。
    """

    def _make(
        score: int = 85,
        db_bytes: int = 4_300,
        exit_code: int = 0,
        expected_width_m: float | None = None,
        stderr: str = "boom",
        expected_targets: int | None = None,
        expect_no_width: bool = False,
    ):
        # expect_no_width：清单里**每一行都不许有第三列**。
        #
        # 需要它的理由和上面 expected_width_m 那段（I6）一样：宽度未知时的正确行为是
        # 省略宽度列，而这个 fake 对 2 列和 3 列一律放行，所以"以为省略了、其实写了个
        # 0.000000"的实现能让测试通过。而那个 bug 的后果很重：ARCore 会把 0 当真，
        # 按 0 米宽算位姿，端上视频彻底贴不上，而服务端一切正常。
        assert not (expect_no_width and expected_width_m is not None), (
            "expect_no_width 与 expected_width_m 互斥：同时给等于要求"
            "同一列既不存在又等于某个值"
        )
        # expected_targets：清单里必须正好有这么多行。多目标建库
        # （quality.build_multi_target_db，整库预建那条路）唯一能被端到端验证的
        # 就是"一行一个目标真的写进去了"—— 少了这项检查，把一串目标误写成一行
        # （比如忘了换行、或者只写了最后一个）的实现也能让测试通过，而那个 .imgdb
        # 里只有一个目标，表现是"大部分照片在端上扫不出来"。
        # stderr 可指定：真 arcoreimg 用退出码 1 同时表示「这张图不行」和
        # 「工具自己坏了」，只能靠文案分（见 quality.NotEnoughKeypoints），
        # 所以测试必须能造出那句话。
        script = tmp_path / "arcoreimg"
        script.write_text(
            textwrap.dedent(f"""\
            #!/usr/bin/env python3
            # 模拟真实 arcoreimg 的接口（已实测，见计划 Task 9 Step 1）：
            #   eval-img --input_image_path=<path>        -> 打印裸数字
            #   build-db --input_image_list_path=<file> --output_db_path=<file>
            # 清单文件每行: 名称|绝对路径|物理宽度(米)
            import sys, pathlib
            argv = sys.argv[1:]
            if {exit_code} != 0:
                sys.stderr.write({stderr!r} + "\\n"); sys.exit({exit_code})

            EXPECTED_WIDTH_M = {expected_width_m!r}
            EXPECTED_TARGETS = {expected_targets!r}
            EXPECT_NO_WIDTH = {expect_no_width!r}

            def opt(prefix):
                for i, a in enumerate(argv):
                    if a.startswith(prefix):
                        return a.split("=", 1)[1] if "=" in a else argv[i + 1]
                return None

            if argv and argv[0] == "eval-img":
                if not opt("--input_image_path"):
                    sys.stderr.write("missing --input_image_path\\n"); sys.exit(2)
                print({score})
                sys.exit(0)

            if argv and argv[0] == "build-db":
                listing = opt("--input_image_list_path")
                out = opt("--output_db_path")
                if not listing or not out:
                    sys.stderr.write("missing required option\\n"); sys.exit(2)
                # 真实工具会因清单格式错误而失败；这里也校验，否则测试测不到格式
                seen = 0
                for line in pathlib.Path(listing).read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    seen += 1
                    parts = line.split("|")
                    if len(parts) not in (2, 3):
                        sys.stderr.write(f"bad list line: {{line}}\\n"); sys.exit(2)
                    if not pathlib.Path(parts[1]).is_absolute():
                        sys.stderr.write(f"path not absolute: {{parts[1]}}\\n"); sys.exit(2)
                    if EXPECT_NO_WIDTH and len(parts) != 2:
                        sys.stderr.write(
                            f"width column should be absent: {{line}}\\n"
                        ); sys.exit(2)
                    if EXPECTED_WIDTH_M is not None:
                        if len(parts) != 3:
                            sys.stderr.write("missing width field\\n"); sys.exit(2)
                        try:
                            w = float(parts[2])
                        except ValueError:
                            sys.stderr.write(f"bad width field: {{parts[2]}}\\n"); sys.exit(2)
                        if abs(w - EXPECTED_WIDTH_M) > 1e-6:
                            sys.stderr.write(
                                f"width mismatch: expected {{EXPECTED_WIDTH_M}}, got {{w}}\\n"
                            ); sys.exit(2)
                if EXPECTED_TARGETS is not None and seen != EXPECTED_TARGETS:
                    sys.stderr.write(
                        f"target count mismatch: expected {{EXPECTED_TARGETS}}, got {{seen}}\\n"
                    ); sys.exit(2)
                # 真 arcoreimg（1.2 实测）在 --output_db_path 不以 .imgdb 结尾时**自己补
                # 后缀**，写到 `<给的路径>.imgdb`，退出码仍然是 0。这个 fake 原来老老实实
                # 写到给的路径上，于是和真件在这一点上分叉 —— 而正是这个分叉让
                # `server.targets` 那条「整库临时文件名不以 .imgdb 结尾」的 bug 活了下来：
                # 每次整库构建都失败，离线识别整条路一直是坏的，全套测试却是绿的。
                real_out = out if out.endswith(".imgdb") else out + ".imgdb"
                pathlib.Path(real_out).write_bytes(b"X" * {db_bytes})
                # 把每次 build-db 的清单原文追加进日志。
                #
                # 需要它是因为这个 fake 产出的 .imgdb 内容与**输入图无关**（固定
                # {db_bytes} 个 X），所以「换参考图之后 imgdb 变了没有」这种断言在这里
                # 根本验不出来 —— 前后字节必然相同。而唯一的真实证据就是「这次 build-db
                # 拿到的清单里写的是哪张图」，所以把它记下来。
                #
                # 日志放在脚本旁边（tmp_path 下），一个测试一份，不会互相污染。
                log = pathlib.Path(__file__).with_name("arcoreimg-calls.log")
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(pathlib.Path(listing).read_text(encoding="utf-8"))
                    fh.write("\\n--\\n")
                print(f"Image database generated at: {{real_out}}")
                sys.exit(0)

            sys.exit(2)
            """),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    return _make


@pytest.fixture
def textured_image():
    """程序化生成高纹理图，保证 ORB 能稳定找到角点；不同 seed 产出可区分的图。

    不使用真实照片，测试才能确定性、可提交、不依赖用户隐私数据。
    """

    def _make(seed: int = 0, w: int = 1200, h: int = 800) -> np.ndarray:
        rng = np.random.default_rng(seed)
        # 低分辨率噪声上采样 -> 大尺度纹理
        base = rng.integers(0, 256, (max(2, h // 8), max(2, w // 8), 3), dtype=np.uint8)
        img = cv2.resize(base, (w, h), interpolation=cv2.INTER_LINEAR)
        # 叠加高对比度矩形 -> 稳定角点
        for _ in range(40):
            x1 = int(rng.integers(0, w))
            y1 = int(rng.integers(0, h))
            x2 = min(w - 1, x1 + int(rng.integers(20, 120)))
            y2 = min(h - 1, y1 + int(rng.integers(20, 120)))
            color = tuple(int(c) for c in rng.integers(0, 256, 3))
            cv2.rectangle(img, (x1, y1), (x2, y2), color, -1)
        return img

    return _make
