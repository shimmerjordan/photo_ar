"""批量导入：一张表 → 一份**没有副作用**的执行计划。

## 为什么是「计划」而不是「直接导入」

一份表有几十行，每一行都要建用户、入库照片（几秒 CPU：特征提取 + 自匹配分）、转码视频
（可能几十秒）、然后授权。做成一个同步接口的话：

- 一次请求要几分钟，反向代理与 Cloudflare 隧道那 125 秒超时都会先掐断它；
- 第 37 行才发现路径写错，前 36 行已经落库了，而调用方只看到一个 500；
- 想知道进度只能等它结束。

所以拆成两步：**这里**只解析和校验，一行都不写库；浏览器拿到计划、把错行显示出来、
人确认之后再逐行调既有的 `/v1/admin/users`、`/v1/photo`、`/v1/photo/*/video`、
`/v1/admin/users/*/grants`。好处不只是不超时：

- 预演（dry-run）是免费的 —— 一份写错的表在**动手之前**就全暴露了；
- 每一行的成败单独可见，失败的那几行可以改完再跑一遍；
- 不需要在服务端引入任何任务队列/作业状态，也就没有「作业表和真实状态不一致」这类问题。

重跑同一份表是安全的，这依赖两条既有行为而不是新写的逻辑：`user.name` 有 UNIQUE
约束（重复建会 409），`ingest_photo` 对同一张参考图抛 409 `already_ingested` **并带上
photoId**（所以浏览器能接着用那个 id 去关联视频、授权）。

## errors 与 warnings 是两回事

`errors` 会阻止那一行执行（浏览器把它标红并跳过），`warnings` 不会。分开是因为有一类
情况「合法但几乎肯定不是本意」—— 最典型的是给访客填了口令：schema 允许，登录时
**也确实会验**（见 `auth.Auth.login` 的 viewer 分支），于是那位宾客拿到的是一个需要
口令才能进的账号，而发表格的人以为宾客只要输名字。当成错误拦掉是越权（人家可能真想
这样），不提醒则是眼看着人踩坑。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..sheet import Sheet

__all__ = [
    "PlanRow",
    "Plan",
    "TEMPLATE_HEADER",
    "TEMPLATE_EXAMPLES",
    "MAPPING_HEADER",
    "build_plan",
    "mapping_rows",
    "template_rows",
    "users_rows",
]

# 模板的表头。**顺序就是模板里的顺序**，而导入时靠的是名字（[Sheet.column_index]），
# 所以人在 Excel 里调换列的顺序、或者删掉不用的列，导入照样能认。
TEMPLATE_HEADER = [
    "用户名",
    "角色（viewer/admin，留空=viewer）",
    "口令（仅管理员必填）",
    "照片路径",
    "视频路径",
    "标题",
    "打印宽度（毫米，可留空）",
]

# 模板里带两行例子。空模板会让人不确定「照片路径」到底是 NAS 路径还是本地路径 ——
# 而这是导入失败最常见的原因。第二行故意只填用户名，用来示意「一行可以只做一件事」。
TEMPLATE_EXAMPLES = [
    ["张三", "viewer", "", "/media/photos/zhangsan.jpg", "/media/videos/zhangsan.mp4", "张三的合照", ""],
    ["李四", "", "", "", "", "", ""],
]

ROLE_VIEWER = "viewer"
ROLE_ADMIN = "admin"

# 角色列的宽容写法。人会写中文，也会写大写。
_ROLE_ALIASES = {
    "": ROLE_VIEWER,
    "viewer": ROLE_VIEWER,
    "访客": ROLE_VIEWER,
    "宾客": ROLE_VIEWER,
    "guest": ROLE_VIEWER,
    "admin": ROLE_ADMIN,
    "管理员": ROLE_ADMIN,
    "administrator": ROLE_ADMIN,
}


@dataclass
class PlanRow:
    """计划里的一行。`line` 是**表里的行号**（含表头，所以数据从 2 开始）。

    行号必须是表里的真实行号，不能是「第几条数据」：人拿到「第 5 行有问题」是要回
    Excel 里去找那一行的，而 Excel 左边那一列显示的就是含表头的行号。
    """

    line: int
    user_name: str = ""
    # 规范化后的名字（`auth.normalize_name` 的结果）。执行者（浏览器）靠它把这一行
    # 对上库里已有的用户 —— 理由见 `app.Server._user_json` 里 `nameKey` 那段注释。
    name_key: str = ""
    role: str = ROLE_VIEWER
    password: str = ""
    photo_path: str = ""
    video_path: str = ""
    title: str = ""
    width_mm: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def actions(self) -> list[str]:
        """这一行要做哪几件事。浏览器按它决定调哪几个接口。"""
        out = []
        if self.user_name:
            out.append("user")
        if self.photo_path:
            out.append("photo")
        if self.video_path:
            out.append("video")
        if self.user_name and self.photo_path:
            out.append("grant")
        return out

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "userName": self.user_name,
            "nameKey": self.name_key,
            "role": self.role,
            # 口令**要回显**。
            #
            # 第一版刻意不回显，理由是「服务端没有理由把它再发一遍」。那个理由是错的：
            # 执行者是浏览器（见模块 docstring），它建管理员账号时必须把口令放进
            # `POST /v1/admin/users` 的请求体里 —— 不回显就等于这套批量导入建不出
            # 管理员，而模板里那一列会变成一个填了也没用的摆设。
            #
            # 回显不多泄露任何东西：这个口令几秒钟前就在**这位管理员自己上传的文件**
            # 里，走的是同一个连接、同一个会话。真正需要防的是它被缓存下来，所以
            # `_admin_import_parse` 的响应带 `Cache-Control: no-store`。
            "password": self.password,
            # 界面用它显示「已填口令」，不必去看 password 是否为空串。
            "hasPassword": bool(self.password),
            "photoPath": self.photo_path,
            "videoPath": self.video_path,
            "title": self.title,
            "printWidthMm": self.width_mm,
            "actions": self.actions,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class Plan:
    rows: list[PlanRow] = field(default_factory=list)
    #  整张表层面的错误（缺必需的列之类）。有它时所有行都不该执行。
    errors: list[str] = field(default_factory=list)

    @property
    def ok_rows(self) -> list[PlanRow]:
        return [] if self.errors else [r for r in self.rows if r.ok]

    def summary(self) -> dict:
        """给界面顶部那一行统计用。

        用户与照片按**去重后**计数：同一张照片授权给三个人是三行，但只入库一次，
        而「要入库 3 张照片」会让人以为表写错了。
        """
        ok = self.ok_rows
        return {
            "rows": len(self.rows),
            "okRows": len(ok),
            "badRows": sum(1 for r in self.rows if not r.ok),
            "warnRows": sum(1 for r in self.rows if r.warnings),
            "users": len({r.user_name for r in ok if r.user_name}),
            "photos": len({r.photo_path for r in ok if r.photo_path}),
            "videos": len({r.video_path for r in ok if r.video_path}),
            "grants": sum(1 for r in ok if r.user_name and r.photo_path),
        }

    def to_json(self) -> dict:
        return {
            "errors": self.errors,
            "summary": self.summary(),
            "rows": [r.to_json() for r in self.rows],
        }


def template_rows() -> list[list[str]]:
    return [TEMPLATE_HEADER, *TEMPLATE_EXAMPLES]


def users_rows(
    users: list[dict],
    photo_of: dict[str, dict],
    grants: dict[str, list[str]],
) -> list[list[str]]:
    """导出「用户 + 他被授权的照片」。

    **表头刻意与 [TEMPLATE_HEADER] 完全一致**，因为这份导出的主要用途是「导出 → 在
    Excel 里改 → 导回去」。表头一旦不同，那条路就断了，而人会以为是自己表格改坏了。
    `tests/server/test_batch.py` 里有一条用例专门盯这个往返。

    一个用户被授权 3 张照片就是 3 行（和导入侧的语义一致）；一张都没有的用户仍然占
    一行，只填名字 —— 少了那一行的话，「导出 → 改 → 导入」会把没授权的用户从表里
    弄丢，而人不会注意到。

    口令列**永远是空的**。库里只有散列，导不出原文；留空的语义正好是「不改口令」。
    """
    out: list[list[str]] = []
    for u in users:
        name = str(u.get("name") or "")
        role = str(u.get("role") or ROLE_VIEWER)
        photo_ids = grants.get(str(u.get("id") or ""), [])
        if not photo_ids:
            out.append([name, role, "", "", "", "", ""])
            continue
        for pid in photo_ids:
            p = photo_of.get(pid) or {}
            out.append(
                [
                    name,
                    role,
                    "",
                    str(p.get("refPath") or ""),
                    str(p.get("videoPath") or ""),
                    str(p.get("title") or ""),
                    _width_text(p.get("printWidthM")),
                ]
            )
    return out


# 映射表的表头。和模板**不一样**是刻意的：这张表是「照片 ↔ 视频」的现状快照，
# 第一列是 photoId（照片的身份），而模板的第一列是用户名。硬凑成同一套表头会让
# 「导出映射 → 直接导入」看起来可行，实际上会把 photoId 当成用户名去建用户。
MAPPING_HEADER = [
    "photoId",
    "标题",
    "照片路径",
    "视频路径",
    "打印宽度（毫米）",
    "质量分",
    "被授权人数",
]


def mapping_rows(photos: list[dict]) -> list[list[str]]:
    """导出照片 ↔ 视频的现状。一行一张照片，没配视频的那一列留空。

    这份表是**只读的快照**，不设计成可导入的。想批量改映射就用模板那张表（照片路径
    + 视频路径两列），那条路已经存在且经过校验；再造一条「按 photoId 改映射」的导入
    路径，等于让同一件事有两套语义。
    """
    out: list[list[str]] = []
    for p in photos:
        out.append(
            [
                str(p.get("photoId") or ""),
                str(p.get("title") or ""),
                str(p.get("refPath") or ""),
                str(p.get("videoPath") or ""),
                _width_text(p.get("printWidthM")),
                str(p.get("qualityScore") if p.get("qualityScore") is not None else ""),
                str(p.get("grantCount") if p.get("grantCount") is not None else ""),
            ]
        )
    return out


def _width_text(print_width_m: object) -> str:
    """库里的米 → 表里的毫米文本。

    0 与 None 都是「未知」，导出成**空**而不是 "0"：这份表要能导回去，而空格子的
    语义是「不知道，交给 ARCore 量」—— 导成 "0" 虽然导入侧也当未知处理（还会多一条
    警告），但让人以为库里真的记着一个 0。
    """
    try:
        m = float(print_width_m)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return ""
    return str(int(round(m * 1000)))


def build_plan(
    table: Sheet,
    *,
    normalize_name: Callable[[str], str],
    check_path: Callable[[str, str], str | None] | None = None,
) -> Plan:
    """表 → 计划。不碰数据库、不写任何东西。

    @param normalize_name 必须传 `auth.normalize_name` **本身**，不能在这里自己写一个
        「转小写去空格」。登录时认的是规范化后的名字，两处规则只要差一点点，表里
        「张三」和「张三 」就会在这里算成两个人、在库里合成一个 —— 于是第二行报
        409，而人完全看不出为什么。
    @param check_path 校验一个路径（`(raw, kind)` → 错误信息或 None），kind 是
        `"image"` / `"video"`。可以为 None（那时只做结构校验）。分离出来是因为
        「路径在不在白名单、文件存不存在」需要 roots 与文件系统，而这个模块要能在
        没有它们的情况下测。
    """
    plan = Plan()
    if not table.rows:
        plan.errors.append("表是空的，一行都没有。")
        return plan

    col_user = table.column_index("用户名", "姓名", "username", "name")
    col_role = table.column_index("角色", "role")
    col_pwd = table.column_index("口令", "密码", "password")
    col_photo = table.column_index("照片路径", "照片", "photopath", "photo")
    col_video = table.column_index("视频路径", "视频", "videopath", "video")
    col_title = table.column_index("标题", "title")
    col_width = table.column_index("打印宽度", "printwidthmm", "width")

    # 用户名和照片路径这两列，至少要有一列在。两列都没有意味着这份表里没有任何
    # 可执行的东西 —— 最常见的原因是表头行被删掉了（于是第一行数据被当成了表头）。
    if col_user is None and col_photo is None:
        plan.errors.append(
            "表头里既没有「用户名」也没有「照片路径」这两列。"
            "最常见的原因是第一行表头被删了 —— 导入靠表头名字认列，第一行必须是表头。"
            f"当前读到的表头是：{table.header[:8]}"
        )
        return plan

    if not table.body:
        plan.errors.append("表里只有表头，没有数据行。")
        return plan

    # 表内自查用的账本。键是**规范化后**的名字/路径。
    role_of: dict[str, tuple[str, int]] = {}
    video_of_photo: dict[str, tuple[str, int]] = {}
    seen_pairs: dict[tuple[str, str], int] = {}

    for offset, raw_row in enumerate(table.body):
        row = PlanRow(line=offset + 2)
        row.user_name = Sheet.get(raw_row, col_user)
        row.name_key = normalize_name(row.user_name) if row.user_name else ""
        row.password = Sheet.get(raw_row, col_pwd)
        row.photo_path = Sheet.get(raw_row, col_photo)
        row.video_path = Sheet.get(raw_row, col_video)
        row.title = Sheet.get(raw_row, col_title)

        _parse_role(row, Sheet.get(raw_row, col_role))
        _parse_width(row, Sheet.get(raw_row, col_width))
        _check_shape(row)
        _check_paths(row, check_path)
        _check_sheet_conflicts(
            row, normalize_name, role_of, video_of_photo, seen_pairs
        )
        plan.rows.append(row)

    if not plan.rows:
        plan.errors.append("表里没有非空的数据行。")
    return plan


def _parse_role(row: PlanRow, raw: str) -> None:
    key = raw.strip().casefold()
    if key in _ROLE_ALIASES:
        row.role = _ROLE_ALIASES[key]
        return
    row.role = ROLE_VIEWER
    row.errors.append(
        f"角色「{raw}」认不出。填 viewer（或「访客」）、admin（或「管理员」），"
        "留空就是 viewer。"
    )


# 打印宽度的可接受区间（毫米）。
#
# 这两个数**必须**与端上 `Geometry.MIN_WIDTH_M` / `MAX_WIDTH_M`（0.02 / 2.0 米）一致。
# 服务端收下而端上拒掉的值，后果是入库时一切正常、扫的时候四边形算不出来 —— 一个
# 只在真机上才现形的失败，而那时人已经不在电脑前了。
WIDTH_MIN_MM = 20
WIDTH_MAX_MM = 2000


def _parse_width(row: PlanRow, raw: str) -> None:
    """解析打印宽度。留空与 0 都是「未知」。

    「未知」是一等公民而不是缺失值：实际照片尺寸经常就是不知道的，而一个**猜的**
    宽度比不填更糟（理由见 `app.Server._create_photo` 那段注释）。所以这里不为
    「没填」报错，只为「填了但显然不对」报错。
    """
    if not raw:
        row.width_mm = None
        return
    try:
        # 先过 float 再取整：Excel 会把 400 存成 "400.0"（见 sheet._num_text），
        # 直接 int() 会拒绝它。
        value = float(raw)
    except ValueError:
        row.errors.append(
            f"打印宽度「{raw}」不是数字。不知道实际尺寸就**留空** —— "
            "留空时 ARCore 自己量，比填一个猜的数更准。"
        )
        return

    if value < 0:
        row.errors.append(
            f"打印宽度 {raw} 是负数。这不是「未知」，是算错了或者单位搞反了 —— "
            "未知就留空。"
        )
        return
    if value == 0:
        # 0 在库里就是「未知」（`print_width_m` 那一列以 0 表示未知），所以这不是
        # 错误。但留空更能表达意图，提一句。
        row.width_mm = None
        row.warnings.append("打印宽度填了 0，按「未知」处理（留空是更清楚的写法）。")
        return

    # 区间检查放在取整**之前**：0.15（把米当成了单位）取整之后是 0，那时再报错就
    # 只能说「0 不合法」，而真正的原因是单位错了。
    if value < WIDTH_MIN_MM:
        row.errors.append(
            f"打印宽度 {raw} 太小了（下限 {WIDTH_MIN_MM} 毫米）。单位是**毫米**："
            "一张 6 寸照片约 152，0.15 是米、15 是厘米。不知道就留空。"
        )
        return
    if value > WIDTH_MAX_MM:
        row.errors.append(
            f"打印宽度 {raw} 太大了（上限 {WIDTH_MAX_MM} 毫米 = 2 米）。"
            "单位是**毫米**，不是像素 —— 一张 6 寸照片约 152。不知道就留空。"
        )
        return

    if value != int(value):
        # 毫米级的小数没有意义（ARCore 自己的测量误差远大于它）。152.4 这种数字
        # 通常是从英寸换算来的，静默取整不如说一句。
        row.warnings.append(f"打印宽度 {raw} 有小数，按 {int(value)} 毫米算。")
    row.width_mm = int(value)


def _check_shape(row: PlanRow) -> None:
    """行内部的自洽性。"""
    if not row.user_name and not row.photo_path:
        # 读表时已经丢掉了**全空**的行，但这一条管的是另一种情况：只填了标题、
        # 或者只填了打印宽度的行。那在读表那边算非空（确实有内容），到这里则无事
        # 可做 —— 真实来源是人删掉了一行的前几格却没删整行。
        row.errors.append("这一行既没有用户名也没有照片路径，没有可做的事。")
        return

    if row.role == ROLE_ADMIN and not row.password:
        row.errors.append("管理员必须填口令（服务端会拒绝没有口令的管理员）。")

    if row.role == ROLE_VIEWER and row.password:
        row.warnings.append(
            "给访客填了口令。这是合法的，但那位访客从此**必须**输这个口令才能登录"
            "（不是「可以不输」）—— 如果本意是让他只输名字就能进，把这一格清空。"
        )

    if row.video_path and not row.photo_path:
        row.errors.append(
            "填了视频路径但没有照片路径。视频是挂在照片上的，"
            "没有照片就没有地方挂 —— 如果是想给一张已入库的照片换视频，"
            "把那张照片的路径也填上（重复的照片不会被入库两次）。"
        )

    if row.title and not row.photo_path:
        row.warnings.append("填了标题但没有照片路径，这个标题不会被用到。")

    if row.width_mm is not None and not row.photo_path:
        row.warnings.append("填了打印宽度但没有照片路径，这个宽度不会被用到。")

    if row.password and not row.user_name:
        row.warnings.append("填了口令但没有用户名，这个口令不会被用到。")


def _check_paths(
    row: PlanRow, check_path: Callable[[str, str], str | None] | None
) -> None:
    if check_path is None:
        return
    if row.photo_path:
        err = check_path(row.photo_path, "image")
        if err:
            row.errors.append(f"照片路径：{err}")
    if row.video_path:
        err = check_path(row.video_path, "video")
        if err:
            row.errors.append(f"视频路径：{err}")


def _check_sheet_conflicts(
    row: PlanRow,
    normalize_name: Callable[[str], str],
    role_of: dict[str, tuple[str, int]],
    video_of_photo: dict[str, tuple[str, int]],
    seen_pairs: dict[tuple[str, str], int],
) -> None:
    """表内部前后行之间的冲突。

    这些冲突服务端逐行执行时**也会**发现（第二行会 409 或者静默覆盖），但那时已经
    做了一半。在预览里就说出来，才是「预演」的意义。
    """
    if row.user_name:
        key = normalize_name(row.user_name)
        prev = role_of.get(key)
        if prev is None:
            role_of[key] = (row.role, row.line)
        elif prev[0] != row.role:
            row.errors.append(
                f"同一个用户「{row.user_name}」在第 {prev[1]} 行是 {prev[0]}、"
                f"这一行是 {row.role}。同一个人只能有一个角色。"
            )

    if row.photo_path and row.video_path:
        prev = video_of_photo.get(row.photo_path)
        if prev is None:
            video_of_photo[row.photo_path] = (row.video_path, row.line)
        elif prev[0] != row.video_path:
            row.errors.append(
                f"同一张照片在第 {prev[1]} 行配的是「{prev[0]}」、这一行配的是"
                f"「{row.video_path}」。一张照片只能配一段视频，"
                "后执行的那一行会把前一行覆盖掉。"
            )

    if row.user_name and row.photo_path:
        pair = (normalize_name(row.user_name), row.photo_path)
        prev_line = seen_pairs.get(pair)
        if prev_line is None:
            seen_pairs[pair] = row.line
        else:
            # 重复不是错（授权是幂等的），但几乎总是复制粘贴留下的。
            row.warnings.append(
                f"这一行和第 {prev_line} 行是同一对（用户 + 照片），重复了。"
            )
