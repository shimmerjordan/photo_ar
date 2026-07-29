"""白名单根目录下的路径解析。**全服务唯一暴露文件系统的地方。**

spec §14.3 把这里单列为"必须专门测"的接口，§13 要求越界一律 403 并记日志
（"正常客户端不会产生，出现即为异常"）。所以本模块的设计原则是：

1. **只信任解析后的真实路径。** 唯一的放行条件是 `Path.resolve()` 之后仍
   落在某个已解析根目录之下。`..`、URL 编码的 `..`（HTTP 层解码后就是普通
   的 `..`）、多重斜杠、`.`、指向白名单外的符号链接，全部由这一条挡住 ——
   不靠逐个枚举攻击串。
2. **在此之上再显式拒绝几类明显不该出现的输入**（相对路径、NUL、反斜杠、
   字面 `..`）。这些本来就会被第 1 条挡住，写出来是为了让 403 的原因可读、
   并且在有人把根目录换成符号链接时也不依赖 resolve 的具体语义。

根目录在构造时就 `resolve()`：如果 `/share` 本身是符号链接（QNAP 上很常见，
容器里挂载点更常见），构造时不解析、检查时解析，会让每一次比较都拿"未解析
的根"去比"已解析的路径"，从而把合法路径全判成越界。
"""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class PathDenied(Exception):
    """路径不被允许。调用方一律映射成 403 并记日志。"""

    def __init__(self, raw: str, reason: str) -> None:
        super().__init__(f"路径被拒绝：{reason}（raw={raw!r}）")
        self.raw = raw
        self.reason = reason


@dataclass(frozen=True)
class Root:
    name: str
    path: Path  # 已 resolve


class Roots:
    """白名单根目录集合。"""

    def __init__(self, roots: dict[str, str | Path]) -> None:
        if not roots:
            raise ValueError("白名单根目录不能为空：那样整个 fs 接口都无法使用")
        resolved: list[Root] = []
        for name, raw in roots.items():
            p = Path(raw).expanduser().resolve()
            resolved.append(Root(name=name, path=p))
        # 按路径长度降序，让最深的根先匹配（嵌套根目录时 name 才是确定的那个）
        self._roots = sorted(resolved, key=lambda r: (-len(str(r.path)), r.name))

    @property
    def roots(self) -> list[Root]:
        return list(self._roots)

    def root_of(self, resolved: Path) -> Root | None:
        for r in self._roots:
            if resolved == r.path or r.path in resolved.parents:
                return r
        return None

    def resolve(self, raw: str) -> Path:
        """把客户端给的路径字符串解析成白名单内的真实绝对路径。

        不要求路径存在（入库时要能解析尚未创建的转码产物路径由别处负责，
        这里的"不存在"更多是用户把文件删了 —— 那应该由调用方报 404 而不是
        403，两者含义不同）。
        """
        if not raw:
            raise PathDenied(raw, "空路径")
        if "\x00" in raw:
            raise PathDenied(raw, "含 NUL 字节")
        if "\\" in raw:
            # POSIX 上反斜杠是合法文件名字符，所以它不会被 resolve 当作分隔符。
            # 但没有任何正常客户端会发它，出现即为探测（Windows 风格分隔符是
            # spec §14.3 点名要测的一类）。当成数据静默放行会让日志里看不见
            # 这次探测，所以显式拒绝。
            raise PathDenied(raw, "含反斜杠（Windows 风格分隔符）")
        if not raw.startswith("/"):
            raise PathDenied(raw, "必须是绝对路径")
        if ".." in PurePosixPath(raw).parts:
            raise PathDenied(raw, "含 .. 路径段")

        resolved = Path(raw).resolve()
        if self.root_of(resolved) is None:
            # 不回显 resolved：符号链接指向哪里属于服务端信息，不该泄给客户端。
            # reason 只进服务端日志，响应体里另给一句通用文案。
            raise PathDenied(raw, f"解析后不在任何白名单根目录下（-> {resolved}）")
        return resolved
