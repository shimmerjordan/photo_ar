"""表格的读写：真 .xlsx 与 CSV，零第三方依赖。

## 为什么不用 openpyxl

这个项目对每一个依赖都写过论证（见 pyproject 里 onnxruntime 那 15 行）。而这里要做的
事只有两件：**写**一个只有文字的单页表、**读**回一个只有文字的单页表。xlsx 本质上就是
一包 XML 塞进 zip，stdlib 的 `zipfile` + `xml.etree` 正好够。加一个依赖去换 300 行
是划不来的 —— 尤其是这 300 行里有一半是注释和「Excel 的坑」。

## 只支持一页、只支持文字

写出去的每个格子都是字符串（`t="inlineStr"`）。不写数字类型是刻意的：这张表里唯一像
数字的东西是「打印宽度（毫米）」和用户名，而**用户名恰恰是最容易被 Excel 当成数字的**
—— 一个叫「007」的宾客，存成数字之后读回来是 `7`，而那时人已经登录不上了。全字符串
让这类静默变换不可能发生。

读的时候相反：Excel 存的数字**一定**是数字类型，我们要把它变回字符串（见 `_cell_text`
里对 `1.0` 的处理）。这两个方向不对称，是因为「我们写的」和「人在 Excel 里改过的」
不是同一份文件。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from xml.etree import ElementTree

__all__ = [
    "Sheet",
    "SheetError",
    "detect_format",
    "read_table",
    "read_csv_bytes",
    "read_xlsx",
    "write_csv_bytes",
    "write_xlsx",
]

# xlsx 里 worksheet / workbook 那套 XML 的命名空间。ElementTree 解析出来的 tag 是
# `{uri}local` 的形式，所以匹配时得带上它。
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# 一次最多读多少行。不是性能考虑 —— 是**解压炸弹**：一个几 KB 的 zip 可以解出几 GB
# 的 sheet1.xml，而这个接口是登录后的管理员才能调，但「管理员手滑上传了一个错文件」
# 和「有人故意传炸弹」在服务端看起来一模一样。批量导入的现实规模是几十行到几百行。
MAX_ROWS = 5000

# 单个 XML part 解压后的上限（10 MiB）。同上，防解压炸弹。5000 行 × 10 列的中文表
# 大约 1 MB，10 MiB 有 10 倍余量。
MAX_PART_BYTES = 10 * 1024 * 1024

# 一行最多多少列。Excel 允许 16384 列，而一个格子的 `r="XFD1"` 就能让我们建出一个
# 16384 长的列表 —— 一行一个，5000 行就是 8000 万个空字符串。
MAX_COLS = 64


class SheetError(Exception):
    """表格读不动。`code` 会原样进 HTTP 响应的 `error` 字段。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Sheet:
    """一页表：一行表头 + 若干数据行。

    行长度**不保证一致** —— Excel 里尾部的空格子不会出现在 XML 里，所以最后一列
    留空的那些行会短一截。用 [get] 取值而不是直接下标。
    """

    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows

    @property
    def header(self) -> list[str]:
        return self.rows[0] if self.rows else []

    @property
    def body(self) -> list[list[str]]:
        return self.rows[1:] if self.rows else []

    def column_index(self, *names: str) -> int | None:
        """按表头找列号，返回第一个匹配到的。

        接受多个别名，因为表头是**人可以改的**：模板里写「用户名」，而有人会写成
        「姓名」或者从别处导出来的表写的是英文。找不到返回 None，由调用方决定这
        一列是必需的还是可选的。

        匹配前先规范化（去空白、去全角括号里的说明、casefold）：模板的表头是
        「打印宽度(毫米，可留空)」这种带说明的写法，而人另存一次很可能把它简化成
        「打印宽度」。
        """
        want = {_norm_header(n) for n in names}
        for i, cell in enumerate(self.header):
            if _norm_header(cell) in want:
                return i
        return None

    @staticmethod
    def get(row: list[str], index: int | None) -> str:
        """取一格。列不存在、或这一行短到没有这一格，都返回空串。

        「这一列不存在」和「这一格是空的」故意合并成同一个结果：对导入逻辑来说，
        「表里没有『打印宽度』这一列」和「这一行的打印宽度没填」应该走同一条路
        （都当没填），分开处理只会多出一条没人测过的分支。
        """
        if index is None or index < 0 or index >= len(row):
            return ""
        return row[index].strip()


def _norm_header(s: str) -> str:
    """表头规范化：去掉括号里的说明、压掉所有空白、casefold。"""
    # 全角与半角括号都要去。模板里写的是全角。
    s = re.sub(r"[（(].*?[）)]", "", s)
    s = re.sub(r"\s+", "", s)
    return s.casefold()


# ---------------------------------------------------------------- 格式识别


def detect_format(data: bytes) -> str:
    """按**内容**判断是 xlsx 还是 csv，返回 `"xlsx"` / `"csv"`。

    不看文件名。理由是文件名在这条链路上最不可信：浏览器的 `filename` 来自用户的
    磁盘，而「在 Excel 里另存为 CSV，但文件名还叫 .xlsx」是真实会发生的事（Excel
    自己会警告，人会点确定）。zip 的魔数是四个字节，看它更省事也更准。
    """
    return "xlsx" if data[:4] == b"PK\x03\x04" else "csv"


def read_table(data: bytes) -> Sheet:
    """读一份表格，自动认 xlsx 还是 csv。"""
    if detect_format(data) == "xlsx":
        return read_xlsx(data)
    return read_csv_bytes(data)


# ---------------------------------------------------------------- CSV


# Excel 打开 UTF-8 的 CSV 时，只有带 BOM 才会认出编码；不带的话中文全是乱码。
# 这是 Excel 独有的行为（LibreOffice、numbers、pandas 都不需要），但我们的用户
# 用的就是 Excel。
_BOM = "﻿"


def write_csv_bytes(rows: list[list[str]]) -> bytes:
    """写 CSV。带 UTF-8 BOM，`\\r\\n` 换行。"""
    buf = io.StringIO(newline="")
    # QUOTE_MINIMAL + \r\n 是 Excel 的方言。csv 模块的 `excel` dialect 就是它，
    # 显式写出来是为了让「为什么是 \r\n」有个落点。
    writer = csv.writer(buf, dialect="excel", lineterminator="\r\n")
    for row in rows:
        writer.writerow(row)
    return (_BOM + buf.getvalue()).encode("utf-8")


def read_csv_bytes(data: bytes) -> Sheet:
    """读 CSV。认 UTF-8（带不带 BOM 都行）与 GB18030。"""
    text = _decode_csv(data)
    if text.startswith(_BOM):
        text = text[1:]
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text, newline="")):
        rows.append([c.strip() for c in row[:MAX_COLS]])
        if len(rows) > MAX_ROWS:
            raise SheetError("sheet_too_big", f"行数超过 {MAX_ROWS}")
    # 全空的行删掉。Excel 存 CSV 时常在末尾留一行 `,,,`，那不是数据。
    rows = [r for r in rows if any(c for c in r)]
    return Sheet(rows)


def _decode_csv(data: bytes) -> str:
    """UTF-8 优先，失败退 GB18030。

    退 GB18030 而不是 GBK：GB18030 是 GBK 的超集，能多认一批生僻字，而对 GBK
    能解的内容两者结果一致 —— 也就是说这个选择只会多认不会少认。

    Windows 上的 Excel「另存为 CSV」默认写的是**本地代码页**（简体中文机器上就是
    GBK），不是 UTF-8。不认它的话，最常见的那条路径（模板下载 → Excel 编辑 →
    另存 → 上传）会直接乱码。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030")
        except UnicodeDecodeError as e:
            raise SheetError(
                "bad_encoding",
                "这个 CSV 既不是 UTF-8 也不是 GB18030，认不出编码。"
                "在 Excel 里另存为「CSV UTF-8」再传一次。",
            ) from e


# ---------------------------------------------------------------- xlsx 写


def write_xlsx(rows: list[list[str]], sheet_name: str = "Sheet1") -> bytes:
    """写一个单页 xlsx。所有格子都是 inline string。

    产出的 zip 只有 5 个 part，是 Excel / WPS / LibreOffice 能打开的最小集合：

        [Content_Types].xml     每个 part 是什么类型
        _rels/.rels             包 → workbook
        xl/workbook.xml         有哪些 sheet
        xl/_rels/workbook.xml.rels   sheet 名 → sheet 文件
        xl/worksheets/sheet1.xml     格子

    没有 sharedStrings.xml。它是 xlsx 用来给重复字符串去重的，**可选**；用
    inlineStr 把文字直接写在格子里，少一个 part 也少一处能对不上的索引。代价是
    重复内容不压缩，而 zip 自己的 deflate 已经把这件事做掉了。

    没有 styles.xml。也可选。没有它 Excel 用默认样式，表头不加粗 —— 这个取舍是
    刻意的：加粗需要 styles.xml + cellXfs 索引，而这张表是拿去改的，不是拿去看的。
    """
    if not rows:
        rows = [[]]
    n_cols = max(len(r) for r in rows)
    if n_cols > MAX_COLS:
        raise SheetError("too_many_columns", f"列数超过 {MAX_COLS}")

    sheet_xml = _sheet_xml(rows)
    buf = io.BytesIO()
    # ZIP_DEFLATED：xlsx 的读取方都支持它，而中文 XML 压缩比很高（实测约 8:1）。
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _workbook_xml(sheet_name))
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels"'
    ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/worksheet"'
    ' Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def _workbook_xml(sheet_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="' + _NS_MAIN + '"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships">'
        "<sheets>"
        f'<sheet name="{_xml_attr(_safe_sheet_name(sheet_name))}" sheetId="1"'
        ' r:id="rId1"/>'
        "</sheets></workbook>"
    )


def _safe_sheet_name(name: str) -> str:
    """sheet 名的合法化。

    Excel 对 sheet 名有一套硬规则：不能含 `[]:*?/\\`、不能超过 31 个字符、不能为空。
    违反了不是「显示得难看」，是**整个文件打不开**（Excel 报「找到不可读取的内容」
    然后修复掉整张表）。这里的 sheet 名来自导出类型（「模板」「用户」「映射」），
    本来都合法 —— 但它是个参数，所以挡一道。
    """
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", name).strip()[:31]
    return cleaned or "Sheet1"


def _sheet_xml(rows: list[list[str]]) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="' + _NS_MAIN + '"><sheetData>',
    ]
    for r, row in enumerate(rows, start=1):
        out.append(f'<row r="{r}">')
        for c, value in enumerate(row, start=1):
            # 空格子干脆不写。这是 Excel 自己的做法，也是读的时候必须靠 `r` 属性
            # 定位而不能靠出现顺序的原因（见 `_row_values`）。
            if value == "":
                continue
            ref = f"{_col_letters(c)}{r}"
            out.append(
                f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                f"{_xml_text(value)}</t></is></c>"
            )
        out.append("</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def _col_letters(n: int) -> str:
    """1 → A、26 → Z、27 → AA。

    是 26 进制但**没有 0**（A 既是 1 也是「个位」），所以每一步要先减 1 再取余，
    不能直接 divmod —— 直接 divmod 的话 26 会算成 "A@" 或 "AZ"（取决于怎么写错的）。
    """
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(ord("A") + rem) + s
    return s


def _xml_text(s: str) -> str:
    """XML 文本转义 + 剔掉 XML 1.0 不允许的控制字符。

    剔控制字符是必须的：`&#x0;` 这种转义在 XML 1.0 里**根本不存在**（不是「要转义」
    而是「不能出现」），写进去的文件 Excel 会判定损坏。而这些字符会从哪来？导出的是
    照片标题和用户名，都是人输入的，从别处粘贴时带一个 \\x0b 完全正常。
    """
    s = "".join(
        ch
        for ch in s
        if ch in "\t\n\r" or (0x20 <= ord(ch) <= 0xD7FF) or (0xE000 <= ord(ch) <= 0xFFFD)
    )
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _xml_attr(s: str) -> str:
    return _xml_text(s).replace('"', "&quot;")


# ---------------------------------------------------------------- xlsx 读


def read_xlsx(data: bytes) -> Sheet:
    """读一个 xlsx 的第一页。

    「第一页」按 workbook.xml 里的顺序定，而不是按 `xl/worksheets/sheet1.xml` 这个
    文件名 —— Excel 删过页之后剩下的那一页可能叫 sheet2.xml，而按文件名找会 404 或者
    读到错的一页。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            sheet_path = _first_sheet_path(z, names)
            shared = _shared_strings(z, names)
            sheet_bytes = _read_part(z, sheet_path)
    except zipfile.BadZipFile as e:
        raise SheetError(
            "bad_xlsx", "这不是一个能打开的 xlsx（zip 结构就已经坏了）"
        ) from e
    except KeyError as e:
        raise SheetError("bad_xlsx", f"xlsx 里缺少必需的部件：{e}") from e

    try:
        root = ElementTree.fromstring(sheet_bytes)
    except ElementTree.ParseError as e:
        raise SheetError("bad_xlsx", f"xlsx 的表格 XML 解析失败：{e}") from e

    rows: list[list[str]] = []
    data_node = root.find(f"{{{_NS_MAIN}}}sheetData")
    if data_node is not None:
        for row_node in data_node.findall(f"{{{_NS_MAIN}}}row"):
            values = _row_values(row_node, shared)
            if any(v for v in values):
                rows.append(values)
            if len(rows) > MAX_ROWS:
                raise SheetError("sheet_too_big", f"行数超过 {MAX_ROWS}")
    return Sheet(rows)


def _first_sheet_path(z: zipfile.ZipFile, names: set[str]) -> str:
    """workbook.xml → 第一个 sheet 的 rId → workbook.xml.rels → 部件路径。"""
    if "xl/workbook.xml" not in names:
        raise SheetError("bad_xlsx", "xlsx 里没有 xl/workbook.xml，不是表格文件")
    wb = ElementTree.fromstring(_read_part(z, "xl/workbook.xml"))
    sheets = wb.find(f"{{{_NS_MAIN}}}sheets")
    rid = None
    if sheets is not None:
        first = sheets.find(f"{{{_NS_MAIN}}}sheet")
        if first is not None:
            ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            rid = first.get(f"{{{ns_r}}}id")
    if not rid:
        raise SheetError("bad_xlsx", "这个 xlsx 里一页都没有")

    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in names:
        # 没有 rels 就只能猜。猜 sheet1.xml 是对的概率很高（我们自己写的就是它），
        # 而这一步失败的话下面 `_read_part` 会给出「缺少必需的部件」。
        return "xl/worksheets/sheet1.xml"
    rels = ElementTree.fromstring(_read_part(z, rels_path))
    ns_pr = "http://schemas.openxmlformats.org/package/2006/relationships"
    for rel in rels.findall(f"{{{ns_pr}}}Relationship"):
        if rel.get("Id") == rid:
            target = rel.get("Target") or ""
            # Target 可以是相对 xl/ 的（"worksheets/sheet1.xml"），也可以是绝对的
            # （"/xl/worksheets/sheet1.xml"）。两种都见过。
            if target.startswith("/"):
                return target.lstrip("/")
            return "xl/" + target.lstrip("./")
    raise SheetError("bad_xlsx", f"xlsx 里找不到 {rid} 指向的那一页")


def _read_part(z: zipfile.ZipFile, path: str) -> bytes:
    """读一个 part，带解压上限。

    用 `open()` + 限量 `read` 而不是 `z.read(path)`：后者会把整个 part 解到内存里，
    上限检查在那之后就太晚了。多读 1 个字节是为了能区分「正好到上限」和「超了」。
    """
    try:
        info = z.getinfo(path)
    except KeyError:
        raise SheetError("bad_xlsx", f"xlsx 里缺少 {path}") from None
    with z.open(info) as f:
        blob = f.read(MAX_PART_BYTES + 1)
    if len(blob) > MAX_PART_BYTES:
        raise SheetError(
            "sheet_too_big",
            f"{path} 解压后超过 {MAX_PART_BYTES // (1024 * 1024)} MiB",
        )
    return blob


def _shared_strings(z: zipfile.ZipFile, names: set[str]) -> list[str]:
    """读 sharedStrings.xml。没有这个 part 是**合法**的（我们自己写的就没有）。

    一个 `<si>` 里可能有多个 `<r>`（富文本：同一个格子里一半加粗一半不加粗），
    那时文字分散在多个 `<t>` 里，得全部拼起来 —— 只取第一个 `<t>` 的话，一个
    在 Excel 里手动加粗过半截的名字会被截断，而这在人编辑过的表里很常见。
    """
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ElementTree.fromstring(_read_part(z, "xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall(f"{{{_NS_MAIN}}}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{{{_NS_MAIN}}}t")))
    return out


def _row_values(row_node: ElementTree.Element, shared: list[str]) -> list[str]:
    """一行的格子 → 定长列表，按 `r` 属性放到正确的列上。

    **必须**按 `r`（如 `"C7"`）定位而不是按出现顺序：Excel 不写空格子，所以
    「张三 | (空) | video.mp4」在 XML 里只有两个 `<c>`，按顺序读会把 video.mp4
    读成第二列 —— 也就是当成了「照片路径」。这个错不会报任何异常，只会让导入
    把视频路径当照片去入库，然后报一句「这不是图片」。
    """
    values: list[str] = []
    for c in row_node.findall(f"{{{_NS_MAIN}}}c"):
        idx = _col_index(c.get("r"))
        if idx is None:
            # 没有 `r` 属性的格子（规范允许省略，含义是「接着上一个」）。
            idx = len(values)
        if idx >= MAX_COLS:
            continue
        while len(values) <= idx:
            values.append("")
        values[idx] = _cell_text(c, shared)
    return values


def _col_index(ref: str | None) -> int | None:
    """`"C7"` → 2（0 基）。认不出返回 None。"""
    if not ref:
        return None
    m = re.match(r"([A-Za-z]+)", ref)
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _cell_text(c: ElementTree.Element, shared: list[str]) -> str:
    """一个格子 → 字符串。"""
    t = c.get("t") or "n"
    if t == "s":
        # 共享字符串：`<v>` 里是索引。
        v = c.find(f"{{{_NS_MAIN}}}v")
        try:
            return shared[int((v.text or "").strip())].strip()
        except (TypeError, ValueError, IndexError):
            # 索引对不上就当空。抛异常是错的：一个坏索引不该让整份表读不了，
            # 而空值会在上层的逐行校验里变成「这一行少了必填项」——那条错误信息
            # 带行号，比「sharedStrings 索引 173 越界」对使用者有用得多。
            return ""
    if t == "inlineStr":
        is_node = c.find(f"{{{_NS_MAIN}}}is")
        if is_node is None:
            return ""
        return "".join(x.text or "" for x in is_node.iter(f"{{{_NS_MAIN}}}t")).strip()
    if t == "str":
        # 公式的计算结果。
        v = c.find(f"{{{_NS_MAIN}}}v")
        return (v.text or "").strip() if v is not None else ""
    if t == "b":
        v = c.find(f"{{{_NS_MAIN}}}v")
        return "TRUE" if (v is not None and (v.text or "").strip() == "1") else "FALSE"
    if t == "e":
        # 错误值（#REF! 之类）。当空，理由同 `t == "s"` 的越界分支。
        return ""
    # 数字（默认类型）。
    v = c.find(f"{{{_NS_MAIN}}}v")
    return _num_text((v.text or "").strip() if v is not None else "")


def _num_text(raw: str) -> str:
    """数字格子的文本化。

    Excel 把整数存成 `"1"`，但**也**会存成 `"1.0"`（取决于它内部怎么算的），而
    `float` 一进一出还会给出 `"400.00000000000006"` 这种。直接 str() 的后果是
    「打印宽度 400」变成「400.0」，而下游 `int()` 会拒绝它 —— 一个只在某些
    Excel 版本上出现的导入失败，查起来极其费劲。

    做法：能无损转成整数的就给整数形式，否则去掉浮点尾巴上的零。
    """
    if not raw:
        return ""
    try:
        f = float(raw)
    except ValueError:
        return raw
    if f.is_integer():
        return str(int(f))
    # `repr` 对 float 给的是最短往返表示，比 f-string 的定点格式更接近人在
    # Excel 里看到的那个数。
    return repr(f)
