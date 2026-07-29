"""photo-ar-server —— 跑在 NAS 上的 catalog + 识别 + 文件浏览 + 吐流服务。

spec §15 的 Phase 1。零新增依赖：HTTP 用标准库 `http.server`，库用标准库
`sqlite3`，识别直接 import 同仓的 `photoar`。理由是本服务的技术难点全在
"识别"和"路径安全"上，而不在 HTTP 框架上；引入 web 框架换不到任何东西，
却要在 QNAP 上多维护一套依赖树。

模块划分（与 spec §5 的组件划分对齐）：
  safepath     白名单根目录下的路径解析（唯一暴露文件系统的地方）
  db           SQLite catalog（spec §6 的表结构）
  integrity    asset 引用完整性校验（spec §6.1）
  library      可增量入库的识别库（固定 vocab + 定长 slot + 重建式倒排）
  fsbrowser    /v1/fs/list 与缩略图
  mediaresolve 媒体 URL 策略链（spec §10）
  ranges       Range 请求解析（spec §14.3 点名要测）
  multipart    multipart/form-data 解析（只为 /v1/recognize 的 frame 字段）
  ingest       入库流水线（spec §7 的 POST /v1/photo）
  app          路由 + 鉴权 + 全部接口
  httpd        ThreadingHTTPServer 装配与 __main__ 入口
  config       配置加载
"""
