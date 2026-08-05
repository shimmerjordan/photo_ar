# `deploy/` 里有什么，以及命令速查

这个目录只有两个文件：

| 文件 | 干什么 |
|---|---|
| `config.example.json` | 可选的配置文件模板。**不需要它** —— 全部配置都能从 `.env` 的环境变量来。想用更细的参数（media 策略、`self_score_samples`、ffprobe 路径…）就 `cp` 成 `deploy/config.json`，entrypoint 检测到它存在就用它 |
| `compose.local.yml` | 在**开发机**上起服务的覆盖层。用法与「与 NAS 到底哪里不一样」写在那个文件的头部 |

> **第一次部署不看这份。** 走 [docs/deploy.md](../docs/deploy.md) —— 那份是带
> 「看到什么算成」的完整流程。取舍、实测数字与排障在
> [docs/deploy-details.md](../docs/deploy-details.md)。
>
> 这份只留**不在那两份里**的东西：例行维护的命令、`data/` 里每个文件丢了会怎样、
> 以及几条只有运维会撞上的坑。
>
> （2026-08-05 把这份文件里重复的那半删了。它当时还写着"词汇树必须预先训好拷进
> `data/`"——那条**早就不成立**了，服务没词表也能起。一份过时的速查比没有速查更坏。）

## 例行维护

```bash
# 素材完整性（mtime + bytes，只在不一致时才哈希）。不自动改绑，只报告。
docker compose exec photo-ar-server photoar-server verify

# catalog 与识别库是否一致（有不一致时退出码 1）
docker compose exec photo-ar-server photoar-server check

# 重建倒排索引（换了 vocab 加 --rebuild-words）
docker compose exec photo-ar-server photoar-server reindex

# 训词表（入完库再训，用的是你这批照片自己的描述子）
docker compose exec photo-ar-server photoar-server build-vocab
```

> `docker compose exec` **不走 ENTRYPOINT**，所以这些命令不会顺带起一个网页版进程。
> 换成 `docker run` 的话 entrypoint 会认出这几个子命令、同样只跑它们
> （见 `docker/entrypoint.py` 的 `_is_server_invocation`）。

## 入库会被拒的几种情况

都带明确原因，而且每一种的下一步动作不同：

| 状态码 | 原因 | 说明 |
|---|---|---|
| 422 `quality_too_low` | 纹理质量分 < 75（`arcoreimg eval-img` 打的） | 大片天空、纯色背景、过曝、严重模糊。**实测真实家庭照片约 65% 属于这类**。换图，或给照片留一圈有纹理的边 |
| 409 `already_ingested` | 同一张照片已入库 | photoId 是内容哈希，同内容必然同 id。响应里带着那张的 photoId |
| 409 `near_duplicate` | 与库中某张过于相似 | 会列出冲突对象。**两张都留着的后果是两张都永远认不出来**（实测） |
| 403 `path_denied` | 路径在白名单外 | 响应体不回显被拒的路径 |
| 503 `arcoreimg_missing` | 那个二进制没送进容器 | 见 [docs/deploy.md](../docs/deploy.md) 的「先准备两样东西」 |

## `/data` 里各文件的作用

| 文件 | 作用 | 丢了会怎样 |
|---|---|---|
| `catalog.db` | 照片、素材、识别历史、用户与授权 | 全部元数据丢失，要重新入库 |
| `library/desc.bin` | 每张照片的 ORB 描述子 | 同上。**网页版发下去的就是它** |
| `library/words.bin` | 每张照片的词序列 | 可用 `reindex --rebuild-words` 从 desc.bin 重算 |
| `library/index.npz` | 倒排索引 | 可用 `reindex` 重建（秒级） |
| `library/slots.json` | slot ↔ photoId 对照 | **最要紧的一个**。丢了 desc.bin 里的特征就对不上 id 了 |
| `thumb/` | 缩略图 | 要重新入库才能再生成 |
| `imgdb/` | 单张的 ARCore `.imgdb` | **现在没有消费者**（安卓客户端 2026-08-05 下线）。删了不影响网页版，但入库仍然会生成 —— 拆掉它要动数据库那两列，见 decisions.md §36.1 |
| `playable/` | 转码后的分片 mp4 | 会重新转码。**必须是分片的**（`moof` 在头部）—— 网页版靠 MediaSource 播，老的 faststart 格式播不了 |
| `models/` | `xfeat.onnx` 与词表 | 启动时会重新取（`PHOTOAR_FETCH_MODELS=1`）；词表要重训 |

**`library/` 里三份记录（`slots.json` / `desc.bin` / `words.bin`）的条数必须相等。**
入库中途断电会留下条数不齐的目录，服务启动时会直接拒绝并让你跑 `reindex` —— 这是
故意的：错位一位的后果是「识别命中后播的是别人的视频」，宁可不启动。

## 两条只有运维会撞上的

**`/data` 里的产物属主是 root**（容器以 root 跑，QTS 上的容器惯例如此）。想在宿主机上
直接删会 Permission denied，用容器自己删：

```bash
docker compose run --rm --entrypoint sh photo-ar-server -c 'rm -rf /data/*'
```

**换了 `vocab.npz` 必须 `reindex --rebuild-words`。** 不做的话库里存的词序列还是旧树
量化出来的，倒排索引指向错误的桶 —— 表现是**识别率突然掉到底，而日志里一切正常**。
