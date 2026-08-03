"""批量导入的计划构建。

这一层的价值全在「**动手之前**就把错说清楚」，所以下面大半用例验的是错误信息本身
（有没有带行号、有没有说该怎么改），而不只是「有没有报错」。一条说不清怎么改的错误
等于让人一行一行试。
"""

from __future__ import annotations

from photoar.server import batch
from photoar.server.auth import normalize_name
from photoar.sheet import Sheet

H = batch.TEMPLATE_HEADER


def plan(rows: list[list[str]], **kw) -> batch.Plan:
    return batch.build_plan(Sheet(rows), normalize_name=normalize_name, **kw)


# ---------------------------------------------------------------- 正常路径


def test_一行建用户_入库照片_配视频_并授权():
    p = plan([H, ["张三", "", "", "/p/a.jpg", "/v/a.mp4", "合照", "152"]])
    assert p.errors == []
    (r,) = p.rows
    assert r.ok and r.warnings == []
    assert r.line == 2
    assert r.user_name == "张三"
    assert r.role == "viewer"
    assert r.photo_path == "/p/a.jpg"
    assert r.video_path == "/v/a.mp4"
    assert r.title == "合照"
    assert r.width_mm == 152
    assert r.actions == ["user", "photo", "video", "grant"]


def test_行号是表里的真实行号_含表头():
    # 人拿到「第 5 行有问题」是要回 Excel 里找的，而 Excel 左边显示的就是含表头的行号。
    p = plan([H, ["a", "", "", "", "", "", ""], ["b", "", "", "", "", "", ""]])
    assert [r.line for r in p.rows] == [2, 3]


def test_只填用户名_就只建用户():
    p = plan([H, ["李四", "", "", "", "", "", ""]])
    (r,) = p.rows
    assert r.ok and r.actions == ["user"]


def test_只填照片路径_就只入库_不授权给谁():
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", ""]])
    (r,) = p.rows
    assert r.ok and r.actions == ["photo"]


def test_管理员带口令():
    p = plan([H, ["老板", "admin", "hunter2", "", "", "", ""]])
    (r,) = p.rows
    assert r.ok and r.role == "admin"


def test_角色的中文与大小写写法都认():
    for raw, want in [
        ("", "viewer"),
        ("viewer", "viewer"),
        ("VIEWER", "viewer"),
        ("访客", "viewer"),
        ("宾客", "viewer"),
        ("admin", "admin"),
        ("Admin", "admin"),
        ("管理员", "admin"),
    ]:
        pwd = "x" if want == "admin" else ""
        p = plan([H, ["某人", raw, pwd, "", "", "", ""]])
        assert p.rows[0].role == want, raw
        assert p.rows[0].ok, raw


def test_口令回显到_json_因为执行者是浏览器():
    # 第一版刻意不回显，理由是「服务端没有理由把它再发一遍」—— 那个理由是错的：
    # 浏览器建管理员时必须把口令放进 POST /v1/admin/users 的请求体，不回显等于
    # 这套批量导入建不出管理员。防护改在传输层（响应带 Cache-Control: no-store）。
    p = plan([H, ["老板", "admin", "hunter2", "", "", "", ""]])
    j = p.rows[0].to_json()
    assert j["password"] == "hunter2"
    assert j["hasPassword"] is True


def test_没填口令时回显空串而不是_null():
    # 界面拿它直接当 POST 的字段用，null 与 "" 在 JSON 里是两种东西。
    p = plan([H, ["张三", "", "", "", "", "", ""]])
    j = p.rows[0].to_json()
    assert j["password"] == ""
    assert j["hasPassword"] is False


# ---------------------------------------------------------------- 表头


def test_表头被删掉时说清原因():
    # 第一行数据被当成了表头，这是导入失败最常见的原因之一。
    p = plan([["张三", "viewer", "", "/p/a.jpg", "", "", ""]])
    assert len(p.errors) == 1
    assert "表头" in p.errors[0]
    assert "第一行" in p.errors[0]
    # 把读到的表头回显出来，人才能看出「哦这是我的数据」
    assert "张三" in p.errors[0]
    assert p.ok_rows == []


def test_列顺序可以随便调_靠名字认():
    header = ["照片路径", "用户名"]
    p = plan([header, ["/p/a.jpg", "张三"]])
    (r,) = p.rows
    assert r.user_name == "张三" and r.photo_path == "/p/a.jpg"


def test_用不到的列可以删掉():
    p = plan([["用户名", "照片路径"], ["张三", "/p/a.jpg"]])
    (r,) = p.rows
    assert r.ok and r.actions == ["user", "photo", "grant"]


def test_表头带括号说明也能匹配_人简化过表头的情况():
    header = ["用户名", "角色", "口令", "照片路径", "视频路径", "标题", "打印宽度"]
    p = plan([header, ["张三", "admin", "pw", "/p/a.jpg", "/v/a.mp4", "t", "152"]])
    (r,) = p.rows
    assert r.ok and r.role == "admin" and r.width_mm == 152


def test_空表与只有表头():
    assert "空" in plan([]).errors[0]
    assert "没有数据行" in plan([H]).errors[0]


# ---------------------------------------------------------------- 逐行校验


def test_管理员没填口令是错误():
    p = plan([H, ["老板", "admin", "", "", "", "", ""]])
    (r,) = p.rows
    assert not r.ok
    assert "口令" in r.errors[0]


def test_访客填了口令是警告而不是错误():
    # 合法（schema 允许，登录时也确实会验），但发表格的人通常以为宾客只输名字。
    # 拦掉是越权，不提醒是眼看着人踩坑。
    p = plan([H, ["张三", "viewer", "pw", "", "", "", ""]])
    (r,) = p.rows
    assert r.ok
    assert len(r.warnings) == 1
    assert "必须" in r.warnings[0]


def test_有视频没照片是错误_并说明怎么改():
    p = plan([H, ["张三", "", "", "", "/v/a.mp4", "", ""]])
    (r,) = p.rows
    assert not r.ok
    assert "视频是挂在照片上的" in r.errors[0]
    # 得告诉人「重复的照片不会被入库两次」，否则他不敢把已入库的照片路径填上
    assert "不会被入库两次" in r.errors[0]


def test_既没用户名也没照片路径():
    # 只填了标题的行 —— 人删掉了前几格却没删整行。
    p = plan([H, ["", "", "", "", "", "只有标题", ""]])
    (r,) = p.rows
    assert not r.ok


def test_填了标题却没照片_只是警告():
    p = plan([H, ["张三", "", "", "", "", "没用的标题", ""]])
    (r,) = p.rows
    assert r.ok and any("标题" in w for w in r.warnings)


def test_角色写错时报错并给出可用的写法():
    p = plan([H, ["张三", "超级管理员", "", "", "", "", ""]])
    (r,) = p.rows
    assert not r.ok
    assert "viewer" in r.errors[0] and "admin" in r.errors[0]


# ---------------------------------------------------------------- 打印宽度


def test_宽度留空就是未知():
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", ""]])
    assert p.rows[0].width_mm is None
    assert p.rows[0].ok


def test_Excel_存成_400_点_0_也认():
    # sheet 层会把数字格子给成 "400"，但 CSV 里人手打 "400.0" 是可能的。
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", "400.0"]])
    assert p.rows[0].width_mm == 400
    assert p.rows[0].ok


def test_宽度不是数字():
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", "六寸"]])
    (r,) = p.rows
    assert not r.ok and "留空" in r.errors[0]


def test_宽度填_0_当作未知_不是错误():
    # 库里 0 就是「未知」，所以拦掉它是自相矛盾的。只提醒留空更清楚。
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", "0"]])
    (r,) = p.rows
    assert r.ok
    assert r.width_mm is None
    assert any("未知" in w for w in r.warnings)


def test_宽度是负数():
    # 负数不是「未知」，是算错了或单位搞反了 —— 静默当未知会把真 bug 藏起来。
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", "-152"]])
    (r,) = p.rows
    assert not r.ok and "负数" in r.errors[0]


def test_把米当成单位填_0_点_15_时说清是单位错了():
    # 取整之后是 0，那时只能说「0 不合法」，而真因是单位。所以区间检查必须在取整前。
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", "0.15"]])
    (r,) = p.rows
    assert not r.ok
    assert "毫米" in r.errors[0] and "米" in r.errors[0]
    assert not r.warnings, "不该先弹一句「按 0 毫米算」再报错"


def test_把像素填进来时提醒单位():
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", "3024"]])
    (r,) = p.rows
    assert not r.ok and "像素" in r.errors[0]


def test_一米九的巨幅打印是合法的():
    # 1920 毫米 = 1.92 米，落在与端上 Geometry 对齐的 20–2000 带内。
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", "1920"]])
    assert p.rows[0].ok and p.rows[0].width_mm == 1920


def test_宽度上下限与端上_Geometry_一致():
    # 服务端收而端上拒 = 入库正常、扫的时候画不出四边形，只在真机上现形。
    assert batch.WIDTH_MIN_MM == 20, "对应 Geometry.MIN_WIDTH_M = 0.02"
    assert batch.WIDTH_MAX_MM == 2000, "对应 Geometry.MAX_WIDTH_M = 2.0"
    assert plan([H, ["", "", "", "/p/a.jpg", "", "", "20"]]).rows[0].ok
    assert plan([H, ["", "", "", "/p/a.jpg", "", "", "2000"]]).rows[0].ok
    assert not plan([H, ["", "", "", "/p/a.jpg", "", "", "19"]]).rows[0].ok
    assert not plan([H, ["", "", "", "/p/a.jpg", "", "", "2001"]]).rows[0].ok


def test_宽度有小数时取整并提醒():
    p = plan([H, ["", "", "", "/p/a.jpg", "", "", "152.4"]])
    (r,) = p.rows
    assert r.ok and r.width_mm == 152
    assert any("小数" in w for w in r.warnings)


# ---------------------------------------------------------------- 表内冲突


def test_同一个人在两行有不同角色是错误():
    p = plan(
        [
            H,
            ["张三", "viewer", "", "/p/a.jpg", "", "", ""],
            ["张三", "admin", "pw", "/p/b.jpg", "", "", ""],
        ]
    )
    assert p.rows[0].ok
    assert not p.rows[1].ok
    # 必须指出**前一行**的行号，否则人不知道去哪儿改
    assert "第 2 行" in p.rows[1].errors[0]


def test_名字规范化必须与登录一致():
    # 「张三」和「张三 」在登录时是同一个人（auth.normalize_name 压空白）。这里
    # 要是自己写一套「转小写」，两行会被算成两个人 —— 然后第二行在执行时 409，
    # 而人完全看不出为什么。
    p = plan(
        [
            H,
            ["Alice", "viewer", "", "", "", "", ""],
            [" alice ", "admin", "pw", "", "", "", ""],
        ]
    )
    assert not p.rows[1].ok, "Alice 与 alice 必须被认成同一个人"


def test_同一个人同一个角色出现多次是正常的():
    # 一位宾客出现在三张照片里就是三行。
    rows = [H] + [["张三", "", "", f"/p/{i}.jpg", "", "", ""] for i in "abc"]
    p = plan(rows)
    assert all(r.ok for r in p.rows)
    assert p.summary()["users"] == 1
    assert p.summary()["photos"] == 3


def test_同一张照片配了两段不同视频是错误():
    p = plan(
        [
            H,
            ["张三", "", "", "/p/a.jpg", "/v/1.mp4", "", ""],
            ["李四", "", "", "/p/a.jpg", "/v/2.mp4", "", ""],
        ]
    )
    assert p.rows[0].ok
    assert not p.rows[1].ok
    assert "覆盖" in p.rows[1].errors[0]


def test_同一张照片配同一段视频给两个人是正常的():
    p = plan(
        [
            H,
            ["张三", "", "", "/p/a.jpg", "/v/1.mp4", "", ""],
            ["李四", "", "", "/p/a.jpg", "/v/1.mp4", "", ""],
        ]
    )
    assert all(r.ok for r in p.rows)
    assert p.summary()["grants"] == 2
    assert p.summary()["photos"] == 1


def test_完全重复的一对是警告不是错误():
    p = plan(
        [
            H,
            ["张三", "", "", "/p/a.jpg", "", "", ""],
            ["张三", "", "", "/p/a.jpg", "", "", ""],
        ]
    )
    assert all(r.ok for r in p.rows)
    assert any("重复" in w for w in p.rows[1].warnings)


# ---------------------------------------------------------------- 路径校验


def test_路径校验被调用_错误带上是哪一列():
    def check(raw: str, kind: str) -> str | None:
        return f"不在白名单里（{kind}）" if raw.startswith("/etc") else None

    p = plan(
        [H, ["张三", "", "", "/etc/passwd", "/v/a.mp4", "", ""]], check_path=check
    )
    (r,) = p.rows
    assert not r.ok
    assert r.errors[0].startswith("照片路径：")
    assert "image" in r.errors[0]


def test_视频路径的校验用_video_这个_kind():
    seen = []

    def check(raw: str, kind: str) -> str | None:
        seen.append((raw, kind))
        return None

    plan([H, ["", "", "", "/p/a.jpg", "/v/a.mp4", "", ""]], check_path=check)
    assert seen == [("/p/a.jpg", "image"), ("/v/a.mp4", "video")]


def test_没传校验器时只做结构校验():
    # 这个模块要能在没有 roots 和文件系统的情况下测。
    p = plan([H, ["张三", "", "", "/随便什么/a.jpg", "", "", ""]])
    assert p.rows[0].ok


# ---------------------------------------------------------------- 汇总


def test_汇总里用户和照片是去重后的数():
    # 同一张照片授权给三个人是三行，但只入库一次。写「要入库 3 张」会让人以为表错了。
    p = plan(
        [
            H,
            ["张三", "", "", "/p/a.jpg", "/v/a.mp4", "", ""],
            ["李四", "", "", "/p/a.jpg", "/v/a.mp4", "", ""],
            ["王五", "", "", "/p/a.jpg", "/v/a.mp4", "", ""],
        ]
    )
    s = p.summary()
    assert s == {
        "rows": 3,
        "okRows": 3,
        "badRows": 0,
        "warnRows": 0,
        "users": 3,
        "photos": 1,
        "videos": 1,
        "grants": 3,
    }


def test_汇总只统计可执行的行():
    p = plan(
        [
            H,
            ["张三", "", "", "/p/a.jpg", "", "", ""],
            ["老板", "admin", "", "", "", "", ""],  # 缺口令，不可执行
        ]
    )
    s = p.summary()
    assert s["okRows"] == 1 and s["badRows"] == 1
    assert s["users"] == 1, "不可执行的那一行不该被算进去"


def test_整表级错误会让所有行都不执行():
    p = plan([["张三", "viewer", "", "/p/a.jpg", "", "", ""]])
    assert p.errors
    assert p.ok_rows == []
    assert p.summary()["okRows"] == 0


# ---------------------------------------------------------------- 模板


def test_模板自己能被解析_而且没有错误行():
    # 模板里的例子如果自己都过不了校验，那它就是在教人写错。
    from photoar import sheet as sheet_mod

    data = sheet_mod.write_xlsx(batch.template_rows())
    p = batch.build_plan(sheet_mod.read_xlsx(data), normalize_name=normalize_name)
    assert p.errors == []
    assert len(p.rows) == len(batch.TEMPLATE_EXAMPLES)
    for r in p.rows:
        assert r.ok, (r.line, r.errors)
        assert r.warnings == [], (r.line, r.warnings)


def test_模板的表头能被自己的列匹配认出来():
    s = Sheet(batch.template_rows())
    assert s.column_index("用户名") == 0
    assert s.column_index("角色") == 1
    assert s.column_index("口令") == 2
    assert s.column_index("照片路径") == 3
    assert s.column_index("视频路径") == 4
    assert s.column_index("标题") == 5
    assert s.column_index("打印宽度") == 6


def test_模板走_csv_也能往返():
    from photoar import sheet as sheet_mod

    data = sheet_mod.write_csv_bytes(batch.template_rows())
    p = batch.build_plan(sheet_mod.read_csv_bytes(data), normalize_name=normalize_name)
    assert p.errors == []
    assert all(r.ok for r in p.rows)
