# `deploy/`：运维命令速查

另两个文件：`config.example.json` 是可选的配置模板（**不需要它** —— 全部配置都能从 `.env`
来；`cp` 成 `deploy/config.json` 才生效），`compose.local.yml` 是开发机上的覆盖层（用法写在
它自己的头部）。第一次部署走 [docs/deploy.md](../docs/deploy.md)，出问题查
[docs/faq.md](../docs/faq.md)，某个数字为什么是这个值看
[docs/deploy-details.md](../docs/deploy-details.md)。

## 常用维护

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

`docker compose exec` **不走 ENTRYPOINT**，所以这些命令不会顺带起一个网页版进程。
升级、回滚、备份与恢复的命令在 [docs/deploy.md 第 8 节](../docs/deploy.md#8-升级与回滚)。

## `/data` 里各文件的作用

| 文件 | 作用 | 丢了会怎样 |
|---|---|---|
| `catalog.db` | 照片、素材、识别历史、用户与授权 | 全部元数据丢失，要重新入库 |
| `library/desc.bin` | 每张照片的 ORB 描述子 | 同上。**网页版发下去的就是它** |
| `library/words.bin` | 每张照片的词序列 | 可用 `reindex --rebuild-words` 从 desc.bin 重算 |
| `library/index.npz` | 倒排索引 | 可用 `reindex` 重建（秒级） |
| `library/slots.json` | slot ↔ photoId 对照 | **最要紧的一个**。丢了 desc.bin 里的特征就对不上 id 了 |
| `thumb/` | 缩略图 | 要重新入库才能再生成 |
| `imgdb/` | 老版本产物 | 现在不再生成，可整目录删除 |
| `playable/` | 转码后的分片 mp4 | 会重新转码。**必须是分片的**（`moof` 在头部）—— 网页版靠 MediaSource 播，老的 faststart 格式播不了 |
| `models/` | `xfeat.onnx` 与词表 | `xfeat.onnx` 随镜像分发，启动时从镜像内拷贝并校验 sha256；`vocab.npz` 是你训的，删了要重训 |

**`library/` 里三份记录（`slots.json` / `desc.bin` / `words.bin`）的条数必须相等。**
入库中途断电会留下条数不齐的目录，服务启动时会直接拒绝并让你跑 `reindex` —— 这是
故意的：错位一位的后果是「识别命中后播的是别人的视频」，宁可不启动。

## 两条只有运维会撞上的

- **`/data` 里的产物属主是 root**（容器以 root 跑，QTS 上的容器惯例如此），想在宿主机上
  直接删会 Permission denied。用容器自己删：
  `docker compose run --rm --entrypoint sh photo-ar-server -c 'rm -rf /data/*'`
- **换了 `vocab.npz` 必须 `reindex --rebuild-words`**，不做的表现是**识别率突然掉到底，
  而日志里一切正常**（还有哪些改动要动库，见
  [docs/deploy.md 第 8 节](../docs/deploy.md#8-升级与回滚)）
