"""表格读写。

**这个文件里最重要的不是往返测试。** 自己写自己读一定过 —— 我们写的 xlsx 用 inlineStr、
不写 sharedStrings、不省略格子，正好把 Excel 特有的三个坑全绕开了。往返绿灯 = 什么都
没验到，而真实的导入文件全都来自 Excel。

所以下面有一组 `_excel_style_xlsx` 的用例：手工拼出 Excel 真实会产出的结构
（sharedStrings + 省略空格子 + 数字类型 + 富文本），拿它们当输入。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from photoar import sheet
from photoar.sheet import Sheet, SheetError


# ---------------------------------------------------------------- 自家往返


def test_写出去的_xlsx_能读回来():
    rows = [["用户名", "照片路径", "标题"], ["张三", "/media/photos/a.jpg", "合照"]]
    out = sheet.read_xlsx(sheet.write_xlsx(rows))
    assert out.rows == rows


def test_写出去的确实是个_zip_而且_detect_认得出来():
    data = sheet.write_xlsx([["a"]])
    assert data[:4] == b"PK\x03\x04"
    assert sheet.detect_format(data) == "xlsx"
    # 必需的 5 个 part 一个都不能少 —— 少任何一个 Excel 都会报「找到不可读取的内容」
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert set(z.namelist()) == {
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/sheet1.xml",
        }


def test_空表也要产出一个能打开的文件():
    # 导出「用户」时库里一个用户都没有是合法状态（新装机）。那时不该给一个 0 字节
    # 的文件 —— 用户会以为是下载失败。
    data = sheet.write_xlsx([])
    assert sheet.read_xlsx(data).rows == []


def test_中间列留空时_列位置不能错位():
    # 我们写的时候会**跳过**空格子（和 Excel 一样），所以这条是在验自己的读回路径
    # 也依赖 `r` 属性而不是出现顺序。
    rows = [["张三", "", "/v/a.mp4"]]
    out = sheet.read_xlsx(sheet.write_xlsx(rows))
    assert out.rows == [["张三", "", "/v/a.mp4"]]


def test_超过列上限直接拒绝():
    with pytest.raises(SheetError) as e:
        sheet.write_xlsx([["x"] * (sheet.MAX_COLS + 1)])
    assert e.value.code == "too_many_columns"


# ---------------------------------------------------------------- XML 安全


def test_标题里的尖括号和与号不会写坏文件():
    rows = [["<script>&amp;", 'a"b']]
    out = sheet.read_xlsx(sheet.write_xlsx(rows))
    assert out.rows == [["<script>&amp;", 'a"b']]


def test_控制字符被剔掉而不是写成非法转义():
    # XML 1.0 里 \x0b 根本不能出现（连 &#xB; 都不合法），写进去 Excel 判定文件损坏。
    # 这个字符会从「人从别处粘贴过来的标题」里进来。
    data = sheet.write_xlsx([["标\x0b题"]])
    assert sheet.read_xlsx(data).rows == [["标题"]]


def test_换行和制表符要保留():
    # 这两个是合法的 XML 字符，而标题里换行是真实存在的。
    out = sheet.read_xlsx(sheet.write_xlsx([["上\n下"]]))
    assert out.rows == [["上\n下"]]


def test_sheet_名里的非法字符被换掉():
    # 含 : * ? / \ [ ] 的 sheet 名会让 Excel 整张表打不开，不是显示难看。
    data = sheet.write_xlsx([["a"]], sheet_name="用户/授权:2026")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        wb = z.read("xl/workbook.xml").decode()
    assert "用户_授权_2026" in wb


def test_sheet_名超过_31_字符被截断():
    data = sheet.write_xlsx([["a"]], sheet_name="很" * 40)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        wb = z.read("xl/workbook.xml").decode()
    assert "很" * 31 in wb
    assert "很" * 32 not in wb


def test_列号进位():
    assert sheet._col_letters(1) == "A"
    assert sheet._col_letters(26) == "Z"
    # 26 进制但没有 0 —— 直接 divmod 会在这里算错
    assert sheet._col_letters(27) == "AA"
    assert sheet._col_letters(52) == "AZ"
    assert sheet._col_letters(53) == "BA"


def test_列号反解():
    assert sheet._col_index("A1") == 0
    assert sheet._col_index("C7") == 2
    assert sheet._col_index("AA1") == 26
    assert sheet._col_index("") is None
    assert sheet._col_index(None) is None


# ------------------------------------------------------- Excel 真实产出的结构


def _excel_style_xlsx(
    sheet_xml: str,
    shared: list[str] | None = None,
    *,
    sheet_path: str = "xl/worksheets/sheet1.xml",
    rel_target: str = "worksheets/sheet1.xml",
) -> bytes:
    """拼一个 Excel 风格的 xlsx：共享字符串 + 完整的 rels 链。

    存在的意义就是**不复用 `write_xlsx`**。复用它的话这些用例验的还是我们自己的
    格式选择，而不是 Excel 的。
    """
    ns = sheet._NS_MAIN
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument"'
            ' Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            f'<?xml version="1.0"?><workbook xmlns="{ns}"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships"><sheets>'
            '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/worksheet"'
            f' Target="{rel_target}"/></Relationships>'
        ),
        sheet_path: f'<?xml version="1.0"?><worksheet xmlns="{ns}">{sheet_xml}</worksheet>',
    }
    if shared is not None:
        items = "".join(f"<si><t>{s}</t></si>" for s in shared)
        parts["xl/sharedStrings.xml"] = (
            f'<?xml version="1.0"?><sst xmlns="{ns}" count="{len(shared)}"'
            f' uniqueCount="{len(shared)}">{items}</sst>'
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return buf.getvalue()


def test_读_Excel_的共享字符串():
    data = _excel_style_xlsx(
        "<sheetData>"
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>1</v></c></row>'
        "</sheetData>",
        shared=["用户名", "照片路径", "张三"],
    )
    assert sheet.read_xlsx(data).rows == [
        ["用户名", "照片路径"],
        ["张三", "照片路径"],
    ]


def test_Excel_省略空格子时靠_r_属性定位_不能按顺序读():
    # 这是整个文件里最要紧的一条。「张三 | (空) | /v/a.mp4」在 Excel 存出来的 XML
    # 里只有两个 <c>，第二个的 r 是 "C2"。按出现顺序读会把 /v/a.mp4 放到第 2 列，
    # 也就是当成**照片路径**去入库 —— 不报任何异常，只会在入库时说「这不是图片」，
    # 而人会以为是自己的视频文件有问题。
    data = _excel_style_xlsx(
        "<sheetData>"
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="C2" t="s"><v>4</v></c></row>'
        "</sheetData>",
        shared=["用户名", "照片路径", "视频路径", "张三", "/v/a.mp4"],
    )
    rows = sheet.read_xlsx(data).rows
    assert rows[1] == ["张三", "", "/v/a.mp4"]


def test_数字格子读成整数字符串_而不是_400_点_0():
    # Excel 把「400」存成数字，float 一进一出就是 "400.0"，而下游 int() 会拒绝它。
    data = _excel_style_xlsx(
        '<sheetData><row r="1">'
        '<c r="A1"><v>400</v></c><c r="B1"><v>400.0</v></c>'
        '<c r="C1"><v>152.5</v></c>'
        "</row></sheetData>"
    )
    assert sheet.read_xlsx(data).rows == [["400", "400", "152.5"]]


def test_用户名_007_不会被读成_7():
    # 一个叫「007」的宾客。Excel 里如果那一格被存成了数字，读回来必须还是能登录的
    # 那个名字。我们自己**写**的时候全用字符串正是为了避免这一步，但人在 Excel 里
    # 重新输入过之后就由 Excel 决定了。
    data = _excel_style_xlsx(
        '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>',
        shared=["007"],
    )
    assert sheet.read_xlsx(data).rows == [["007"]]


def test_富文本格子的多段文字要拼起来():
    # 人在 Excel 里把名字的一半加粗过，那一格的 <si> 就会分成多个 <r>。只取第一个
    # <t> 会把名字截断。
    ns = sheet._NS_MAIN
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships"><sheets>'
            '<sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/worksheet"'
            ' Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{ns}"><si><r><t>张</t></r><r><t>三</t></r></si></sst>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{ns}"><sheetData><row r="1">'
            '<c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>',
        )
    assert sheet.read_xlsx(buf.getvalue()).rows == [["张三"]]


def test_第一页按_workbook_顺序找_不按文件名():
    # Excel 删过页之后剩下的那一页可能叫 sheet2.xml。按文件名找会读不到。
    data = _excel_style_xlsx(
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>在这</t></is></c>'
        "</row></sheetData>",
        sheet_path="xl/worksheets/sheet2.xml",
        rel_target="worksheets/sheet2.xml",
    )
    assert sheet.read_xlsx(data).rows == [["在这"]]


def test_rels_里的绝对_Target_也要认():
    data = _excel_style_xlsx(
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c>'
        "</row></sheetData>",
        rel_target="/xl/worksheets/sheet1.xml",
    )
    assert sheet.read_xlsx(data).rows == [["x"]]


def test_布尔与错误值():
    data = _excel_style_xlsx(
        '<sheetData><row r="1">'
        '<c r="A1" t="b"><v>1</v></c><c r="B1" t="b"><v>0</v></c>'
        '<c r="C1" t="e"><v>#REF!</v></c>'
        '<c r="D1" t="str"><v>公式结果</v></c>'
        "</row></sheetData>"
    )
    assert sheet.read_xlsx(data).rows == [["TRUE", "FALSE", "", "公式结果"]]


def test_共享字符串索引越界当空_不炸整份表():
    # 一个坏索引不该让整份表读不了 —— 空值会在逐行校验里变成「这一行少了必填项」，
    # 那条错误带行号，比「sharedStrings 索引越界」有用得多。
    data = _excel_style_xlsx(
        '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>99</v></c></row></sheetData>',
        shared=["有"],
    )
    assert sheet.read_xlsx(data).rows == [["有", ""]]


def test_全空的行被丢掉():
    data = _excel_style_xlsx(
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>a</t></is></c></row>'
        '<row r="2"/>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>b</t></is></c></row>'
        "</sheetData>"
    )
    assert sheet.read_xlsx(data).rows == [["a"], ["b"]]


# ---------------------------------------------------------------- 坏输入


def test_不是_zip():
    with pytest.raises(SheetError) as e:
        sheet.read_xlsx(b"this is not a zip at all")
    assert e.value.code == "bad_xlsx"


def test_是_zip_但没有_workbook():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(SheetError) as e:
        sheet.read_xlsx(buf.getvalue())
    assert e.value.code == "bad_xlsx"


def test_一页都没有的_workbook():
    ns = sheet._NS_MAIN
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}"><sheets/></workbook>')
    with pytest.raises(SheetError) as e:
        sheet.read_xlsx(buf.getvalue())
    assert e.value.code == "bad_xlsx"


def test_解压炸弹被挡住():
    # 几十 KB 的 zip 解出 200 MiB 的 sheet1.xml。这个接口只有管理员能调，但
    # 「管理员手滑传了个错文件」和「有人故意传炸弹」在服务端看起来一样。
    ns = sheet._NS_MAIN
    bomb = f'<worksheet xmlns="{ns}"><sheetData>' + "<!--" + "A" * (12 * 1024 * 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships"><sheets>'
            '<sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/worksheet"'
            ' Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr("xl/worksheets/sheet1.xml", bomb)
    # 压缩后应该很小，解压后超限
    assert len(buf.getvalue()) < 200 * 1024
    with pytest.raises(SheetError) as e:
        sheet.read_xlsx(buf.getvalue())
    assert e.value.code == "sheet_too_big"


def test_行数超上限():
    rows = "".join(
        f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>x</t></is></c></row>'
        for i in range(1, sheet.MAX_ROWS + 3)
    )
    data = _excel_style_xlsx(f"<sheetData>{rows}</sheetData>")
    with pytest.raises(SheetError) as e:
        sheet.read_xlsx(data)
    assert e.value.code == "sheet_too_big"


# ---------------------------------------------------------------- CSV


def test_csv_带_BOM_否则_Excel_中文乱码():
    data = sheet.write_csv_bytes([["用户名"], ["张三"]])
    assert data.startswith(b"\xef\xbb\xbf")
    assert sheet.detect_format(data) == "csv"
    assert sheet.read_csv_bytes(data).rows == [["用户名"], ["张三"]]


def test_csv_用_CRLF():
    data = sheet.write_csv_bytes([["a", "b"]])
    assert data.endswith(b"a,b\r\n")


def test_csv_里的逗号和引号被正确括起来():
    rows = [['他说"好"', "a,b"]]
    assert sheet.read_csv_bytes(sheet.write_csv_bytes(rows)).rows == rows


def test_读_Windows_Excel_另存的_GBK_csv():
    # 简体中文 Windows 上的 Excel「另存为 CSV」默认写本地代码页，不是 UTF-8。
    # 不认它的话，最常见那条路径（下模板 → 编辑 → 另存 → 上传）直接乱码。
    data = "用户名,照片路径\r\n张三,/a.jpg\r\n".encode("gb18030")
    assert sheet.read_csv_bytes(data).rows == [
        ["用户名", "照片路径"],
        ["张三", "/a.jpg"],
    ]


def test_既不是_utf8_也不是_gb18030():
    with pytest.raises(SheetError) as e:
        sheet.read_csv_bytes(b"\xff\xfe\x00\x01\x82\x30\xff")
    assert e.value.code == "bad_encoding"


def test_csv_末尾那行全是逗号的被丢掉():
    # Excel 存 CSV 时常留一行 ",,,"
    data = "a,b\r\n1,2\r\n,,\r\n".encode()
    assert sheet.read_csv_bytes(data).rows == [["a", "b"], ["1", "2"]]


def test_read_table_按内容分派_不看文件名():
    x = sheet.write_xlsx([["从 xlsx 来"]])
    c = sheet.write_csv_bytes([["从 csv 来"]])
    assert sheet.read_table(x).rows == [["从 xlsx 来"]]
    assert sheet.read_table(c).rows == [["从 csv 来"]]


# ---------------------------------------------------------------- Sheet 取值


def test_按表头找列_支持别名():
    s = Sheet([["用户名", "照片路径", "视频路径"], ["张三", "/a.jpg", "/a.mp4"]])
    assert s.column_index("用户名", "姓名") == 0
    assert s.column_index("姓名", "用户名") == 0
    assert s.column_index("视频路径") == 2
    assert s.column_index("不存在的列") is None


def test_表头带括号说明也能匹配():
    # 模板里写的是「打印宽度（毫米，可留空）」，而人另存一次很可能简化成「打印宽度」。
    s = Sheet([["打印宽度（毫米，可留空）", "角色 (admin/viewer)"]])
    assert s.column_index("打印宽度") == 0
    assert s.column_index("角色") == 1


def test_表头大小写与空白无关():
    s = Sheet([[" User Name ", "PhotoPath"]])
    assert s.column_index("username") == 0
    assert s.column_index("photopath") == 1


def test_取值时列不存在与格子为空同样返回空串():
    # 两者故意合并：对导入逻辑来说「表里没这一列」和「这一行没填」该走同一条路。
    row = ["张三", "/a.jpg"]
    assert Sheet.get(row, 0) == "张三"
    assert Sheet.get(row, None) == ""
    assert Sheet.get(row, 5) == ""
    assert Sheet.get(row, -1) == ""


def test_取值会去掉首尾空白():
    assert Sheet.get(["  张三  "], 0) == "张三"


def test_header_与_body():
    s = Sheet([["h1", "h2"], ["a", "b"], ["c", "d"]])
    assert s.header == ["h1", "h2"]
    assert s.body == [["a", "b"], ["c", "d"]]
    empty = Sheet([])
    assert empty.header == []
    assert empty.body == []
