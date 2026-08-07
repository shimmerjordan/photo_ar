# 文档索引

**按你现在要干的事挑一份，不用都读。**

| 我想… | 看这份 |
|---|---|
| 从零把它部署起来 | [deploy.md](deploy.md) —— 每步都带「看到什么算成」 |
| 用管理台：建账号、授权、配视频、批量导入 | [usage.md](usage.md) |
| 出问题了 | [faq.md](faq.md) —— 症状对照表 |
| 我的照片不在 `/share/Photo`，compose 怎么改 | [../docker-compose.yml](../docker-compose.yml) 顶部「改成你自己的路径」 |
| 知道某个数字/限制是怎么来的 | [deploy-details.md](deploy-details.md) —— 取舍与实测基线 |
| 搞清楚"为什么是这样设计的" | [decisions.md](decisions.md) —— 按时间排的决策日志 |
| 手上有一次 CI 构建，想知道**这一版**怎么拉怎么起 | 那次 run 的页面（Actions → server），顶部就是 |
| 改网页版的代码 | [../web-front/README.md](../web-front/README.md) |
| 复现 README 里引用的某个数字 | [../bench/README.md](../bench/README.md) |
| 例行维护命令 / `data/` 里每个文件丢了会怎样 | [../deploy/README.md](../deploy/README.md) |

## 读 `decisions.md` 之前

它是**按轮次追加的日志**，不是整理过的设计文档：

- **条目编号（`§N`）永不重排**，代码注释里到处引用。某个决定后来被推翻了，那一条也
  留在原地 —— 推翻它的那一条写在后面并指回去（如 §0.1 推翻 §0、§49.3 推翻 §49.2）。
- **早期有一批条目讲的是已经不存在的原生客户端**（2026-08-05 下线，见 §36.1、§46）。
  它们对现在的代码没有约束力，只在追溯"当初为什么那样做"时有用。涉及识别特征、阈值
  怎么量的、用户与权限、部署、入库闸门、去重、管理台的那些条目**全部仍然有效**。

## 不在这里的

- `§N` 引用的那份内部设计文档没有随仓库发布 —— 每处注释都把理由写在了旁边。
- `docs/superpowers/` 是过程产物（spec / plan），在 `.gitignore` 里。
