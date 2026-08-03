"""管理台静态页的路由，重点是**每个分区一个自己的 URI**。

`/admin/users`、`/admin/photos` 这些路径要返回首页，由前端按 `location.pathname` 决定打开
哪个分区。不加这条的话它们会走文件查找（`users` 恰好符合文件名白名单）→ 找不到 → 404，
也就是**刷新页面就白屏**，而「刷新」正是独立 URI 最主要的用途。

另一半是别把这条做成「什么都回首页」的兜底：`/admin/app.js` 拼错时必须还是 404，否则
浏览器会拿一份 HTML 当 JS 解，报出来的是一句莫名其妙的语法错误。
"""

from __future__ import annotations

import re

import pytest

from photoar.server import app


def html_of(env, resp) -> str:
    # 静态文件走 `Response.file`（由 httpd 分块发送），不进 `body` —— 直接读 `.body`
    # 会得到空串，而那会让「两个地址给的是同一份内容」这类断言恒真。
    return env.body_bytes(resp).decode("utf-8")


# ---------------------------------------------------------------- 首页


def test_admin_与带斜杠的都给首页(env):
    for path in ("/admin", "/admin/"):
        r = env.get(path, auth=False)
        assert r.status == 200, path
        assert r.headers["Content-Type"].startswith("text/html"), path


def test_首页免鉴权(env):
    # 页面本身就是那个输口令的界面。要求先鉴权才能拿到它等于要求先登录才能看到登录框。
    assert env.get("/admin", auth=False).status == 200


def test_静态资源能取到(env):
    for name, ctype in (("app.js", "text/javascript"), ("app.css", "text/css")):
        r = env.get(f"/admin/{name}", auth=False)
        assert r.status == 200, name
        assert r.headers["Content-Type"].startswith(ctype), name


# ---------------------------------------------------------------- 每个分区一个 URI


def test_每个分区的_URI_都返回首页(env):
    # 这些是要发给别人、要收藏、要刷新的地址。
    for tab in sorted(app._WEBUI_TABS):
        r = env.get(f"/admin/{tab}", auth=False)
        assert r.status == 200, tab
        assert r.headers["Content-Type"].startswith("text/html"), tab


def test_分区_URI_给的就是首页本身_不是别的文件(env):
    home = html_of(env, env.get("/admin", auth=False))
    assert "photoar 管理台" in home
    for tab in sorted(app._WEBUI_TABS):
        assert html_of(env, env.get(f"/admin/{tab}", auth=False)) == home, tab


def test_带尾随斜杠的分区_URI_也认(env):
    # 浏览器地址栏、以及从别处跳过来时可能补上斜杠。
    for tab in sorted(app._WEBUI_TABS):
        assert env.get(f"/admin/{tab}/", auth=False).status == 200, tab


def test_服务端的分区清单与前端的_TABS_一致(env):
    """两边对不上的后果各有一种，都很难看出来：

    - 服务端多一项：那个地址打得开，但前端不认，回落到默认分区（地址栏和内容对不上）。
    - 服务端少一项：那个地址刷新时 404，而它在页面里点得到（前端 pushState 写得进去）。

    所以直接从 `app.js` 里把 TABS 抠出来比。硬编一份清单在这里的话，这条测试就只是在
    重复服务端那份常量。
    """
    js = (env.srv.webui_dir / "app.js").read_text("utf-8")
    m = re.search(r"const TABS = \[([^\]]*)\]", js)
    assert m, "app.js 里找不到 TABS"
    front = set(re.findall(r"'([\w-]+)'", m.group(1)))
    assert front == set(app._WEBUI_TABS), (
        f"前端 {sorted(front)} 与服务端 {sorted(app._WEBUI_TABS)} 对不上"
    )


def test_前端的每个分区在_HTML_里都有按钮和面板(env):
    html = (env.srv.webui_dir / "index.html").read_text("utf-8")
    for tab in sorted(app._WEBUI_TABS):
        assert f'data-tab="{tab}"' in html, f"{tab} 少了页签按钮"
        assert f'id="p-{tab}"' in html, f"{tab} 少了面板"


# ---------------------------------------------------------------- 不能变成兜底


def test_拼错的静态文件名仍然是_404(env):
    # 做成「什么都回首页」的话，浏览器会拿一份 HTML 当 JS 解，报出来的是一句
    # 莫名其妙的语法错误，而真因是文件名拼错了。
    for name in ("app.jss", "apps.js", "style.css", "index.htm"):
        r = env.get(f"/admin/{name}", auth=False)
        assert r.status == 404, name


def test_不在清单里的分区名是_404(env):
    for name in ("mapping", "dashboard", "settings"):
        assert name not in app._WEBUI_TABS
        assert env.get(f"/admin/{name}", auth=False).status == 404, name


def test_路径穿越还是被挡住(env):
    # 分区清单是在名字白名单**之前**匹配的，所以要确认它没有把穿越放进来。
    for bad in ("/admin/../config.py", "/admin/..%2Fapp.py", "/admin/.env"):
        r = env.get(bad, auth=False)
        assert r.status in (403, 404), f"{bad} -> {r.status}"


def test_只支持_GET(env):
    r = env.request("POST", "/admin/users", body=b"{}", auth=False)
    assert r.status == 405


def test_分区_URI_不许被缓存住(env):
    # 换镜像就换了页面。`max-age` 的后果是升级完之后有一段时间里用户看到的是旧页面
    # 配新接口，而「清一下缓存就好了」是最不该出现在家用部署里的指示。
    r = env.get("/admin/photos", auth=False)
    assert "no-cache" in r.headers.get("Cache-Control", "")
