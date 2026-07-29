#!/usr/bin/env bash
# Phase 1 出口条件：用 curl 完成 入库 → 识别 → 取流，且路径穿越测试通过。
#
# 为什么要有这个脚本而不只靠 pytest：tests/server 里的"真 socket"那组用 stdlib
# urllib 做客户端，而 urllib 会替你把很多事做对（Host 头、编码、Content-Length）。
# curl 是另一个实现，且这里跑的是真配置文件、真 vocab（5000 张真实照片训出来
# 的那棵）、真 ffmpeg 转码、真 arcoreimg —— 全部 mock 都撤掉之后是否还成立，
# 只有这么跑一遍才知道。
#
# 用法：
#   bash bench/e2e_curl.sh
# 可覆盖的环境变量：
#   VOCAB   词汇树 .npz（默认 ~/photoar-data/corpus/vocab.npz，由 photoar build 产出）
#   PHOTOS  真实照片目录（默认 ~/photoar-data/photos）
#   KEEP=1  结束后保留工作目录，便于事后翻日志

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOCAB="${VOCAB:-$HOME/photoar-data/corpus/vocab.npz}"
PHOTOS="${PHOTOS:-$HOME/photoar-data/photos}"
ARCOREIMG="${ARCOREIMG:-$REPO/tools/arcoreimg}"
TOKEN="e2e-token-$RANDOM$RANDOM"

pass=0
fail=0
ok()   { pass=$((pass+1)); printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { fail=$((fail+1)); printf '  \033[31m✗\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
# eq <期望> <实际> <说明>
eq()   { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3（期望 $1，实得 $2）"; fi; }

for need in "$VOCAB" "$PHOTOS" "$ARCOREIMG"; do
  [ -e "$need" ] || { echo "缺少：$need" >&2; exit 2; }
done
for bin in curl ffmpeg ffprobe python3; do
  command -v "$bin" >/dev/null || { echo "缺少命令：$bin" >&2; exit 2; }
done

WORK="$(mktemp -d /tmp/photoar-e2e-XXXXXX)"
NAS="$WORK/nas"
SRV_LOG="$WORK/server.log"
SRV_PID=""
cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null && wait "$SRV_PID" 2>/dev/null
  if [ "${KEEP:-0}" = "1" ]; then
    echo "工作目录保留在 $WORK"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

mkdir -p "$NAS/photos" "$NAS/videos" "$NAS/inbox" "$WORK/data" "$WORK/outside"
echo "secret-not-yours" > "$WORK/outside/secret.txt"

step "0. 准备素材（$WORK）"
# 取三张真实照片：两张入库，一张只用来当"库里没有的照片"。必须先过 arcoreimg
# 质量分 —— 数据集里不少照片只有 50 多分，服务端会正确地用 422 拒掉，那会把
# 后面所有步骤连带失败。第三张也挑高分的：用一张本来就没特征的照片去证明
# "不误识别"是没有说服力的。
mapfile -t picks < <(PYTHONPATH="$REPO/src" python3 bench/e2e_pick_photos.py \
  "$PHOTOS" 3 85 "$ARCOREIMG" | cut -f2)
[ "${#picks[@]}" -eq 3 ] || { echo "挑不出 3 张质量分够的照片：$PHOTOS" >&2; exit 2; }
cp "${picks[0]}" "$NAS/photos/a.jpg"
cp "${picks[1]}" "$NAS/photos/b.jpg"
cp "${picks[2]}" "$NAS/photos/stranger.jpg"
ok "参考图 a.jpg / b.jpg / stranger.jpg（分别来自 $(basename "${picks[0]}") / $(basename "${picks[1]}") / $(basename "${picks[2]}")）"

# 真视频。故意造一个 moov 在尾部的（-movflags 默认），逼服务端走转码分支。
ffmpeg -nostdin -loglevel error -y -f lavfi -i "testsrc=size=640x360:rate=15:duration=3" \
  -c:v libx264 -pix_fmt yuv420p "$NAS/videos/a.mp4" \
  || { echo "ffmpeg 造视频失败" >&2; exit 2; }
ok "视频 a.mp4（$(stat -c%s "$NAS/videos/a.mp4") 字节）"

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
cat > "$WORK/config.json" <<JSON
{
  "token": "$TOKEN",
  "bind": "127.0.0.1",
  "port": $PORT,
  "roots": {"nas": "$NAS"},
  "data_dir": "$WORK/data",
  "vocab_path": "$VOCAB",
  "arcoreimg": "$ARCOREIMG",
  "upload_dir_root": "$NAS/inbox",
  "self_score_samples": 6,
  "version": "e2e"
}
JSON
ok "config.json（端口 $PORT，白名单根 nas=$NAS）"

step "1. 启动服务"
cd "$REPO"
PYTHONPATH="$REPO/src" python3 -m photoar.server.httpd -c "$WORK/config.json" serve \
  > "$SRV_LOG" 2>&1 &
SRV_PID=$!
BASE="http://127.0.0.1:$PORT"
AUTH=(-H "Authorization: Bearer $TOKEN")
for _ in $(seq 1 100); do
  curl -fsS "${AUTH[@]}" "$BASE/v1/ping" -o /dev/null 2>/dev/null && break
  kill -0 "$SRV_PID" 2>/dev/null || { echo "服务进程已退出："; cat "$SRV_LOG"; exit 1; }
  sleep 0.3
done
curl -fsS "${AUTH[@]}" "$BASE/v1/ping" | grep -q '"ok": *true' \
  && ok "GET /v1/ping" || { bad "GET /v1/ping"; cat "$SRV_LOG"; exit 1; }
eq 401 "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/ping")" "无 token → 401"
eq 401 "$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer wrong' "$BASE/v1/ping")" \
  "错 token → 401"
curl -s -D- -o /dev/null "$BASE/v1/ping" | grep -qi '^WWW-Authenticate: Bearer' \
  && ok "401 带 WWW-Authenticate" || bad "401 缺 WWW-Authenticate"

step "2. 入库（POST /v1/photo）"
t0=$(date +%s%N)
ING="$(curl -s -w '\n%{http_code}' "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"refPath\":\"$NAS/photos/a.jpg\",\"videoPath\":\"$NAS/videos/a.mp4\",\"printWidthMm\":152,\"title\":\"E2E A\"}" \
  "$BASE/v1/photo")"
code="$(tail -n1 <<<"$ING")"; body="$(sed '$d' <<<"$ING")"
eq 201 "$code" "入库返回 201"
echo "     $body"
PID_A="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("photoId",""))' <<<"$body")"
if [ ${#PID_A} -eq 32 ]; then
  ok "photoId 是 32 位内容哈希：$PID_A"
else
  bad "photoId 不对：$PID_A"
  # 入库没成的话后面每一步都会失败，那些失败没有诊断价值，直接停在这里
  echo "  入库失败，后续步骤无意义。服务端日志："; sed 's/^/    /' "$SRV_LOG" | tail -20
  exit 1
fi
python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d["transcoded"] else 1)' <<<"$body" \
  && ok "moov 在尾部的视频被转码成 faststart" || bad "该转码却没转"
echo "     入库耗时 $(( ($(date +%s%N)-t0)/1000000 ))ms（含 arcoreimg + 6 次自匹配 + ffmpeg）"

ING2="$(curl -s -w '\n%{http_code}' "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"refPath\":\"$NAS/photos/b.jpg\",\"printWidthMm\":102}" "$BASE/v1/photo")"
eq 201 "$(tail -n1 <<<"$ING2")" "第二张入库（无视频）"
PID_B="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("photoId",""))' <<<"$(sed '$d' <<<"$ING2")")"

eq 409 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"refPath\":\"$NAS/photos/a.jpg\",\"printWidthMm\":152}" "$BASE/v1/photo")" \
  "同一张再入库 → 409"
eq 400 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"refPath\":\"$NAS/photos/b.jpg\"}" "$BASE/v1/photo")" \
  "缺 printWidthMm → 400"

step "3. 识别（POST /v1/recognize）"
PYTHONPATH="$REPO/src" python3 bench/e2e_make_query.py "$NAS/photos/a.jpg" "$WORK/query_a.jpg" 1 \
  | sed 's/^/     帧：/'
REC="$(curl -s -w '\n%{http_code}' "${AUTH[@]}" -H 'X-PhotoAR-Endpoint: lan' \
  -F "frame=@$WORK/query_a.jpg;type=image/jpeg" "$BASE/v1/recognize")"
eq 200 "$(tail -n1 <<<"$REC")" "识别返回 200"
body="$(sed '$d' <<<"$REC")"
echo "     $body"
got="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("photoId"))' <<<"$body")"
eq "$PID_A" "$got" "命中的是 a.jpg"
python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["inliers"] >= 40, d["inliers"]
assert abs(d["printWidthM"]-0.152) < 1e-9, d["printWidthM"]
assert d["imgdbUrl"].endswith("/imgdb") and d["mediaUrl"].endswith("/media")
assert d["refAspect"] > 0
print("     inliers=%d latency=%dms refAspect=%.4f" % (d["inliers"], d["latencyMs"], d["refAspect"]))
' <<<"$body" && ok "inliers ≥ 40（MIN_INLIERS）且字段齐全" || bad "识别响应字段不达标"

MISS_Q="$WORK/query_stranger.jpg"
PYTHONPATH="$REPO/src" python3 bench/e2e_make_query.py "$NAS/photos/stranger.jpg" "$MISS_Q" 2 >/dev/null
MISS="$(curl -s -w '\n%{http_code}' "${AUTH[@]}" -F "frame=@$MISS_Q;type=image/jpeg" "$BASE/v1/recognize")"
eq 200 "$(tail -n1 <<<"$MISS")" "库外照片也返回 200（未命中不是错误）"
python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if not d["matched"] else 1)' \
  <<<"$(sed '$d' <<<"$MISS")" && ok "matched=false（无误识别）" || bad "把库外照片认成了库内照片"

eq 400 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" \
  -F "frame=@$WORK/config.json;type=image/jpeg" "$BASE/v1/recognize")" "帧不是图片 → 400"

step "4. 取流（media → asset/stream）"
MEDIA="$(curl -s "${AUTH[@]}" "$BASE/v1/photo/$PID_A/media")"
echo "     $MEDIA"
URL="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["url"])' <<<"$MEDIA")"
python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d["via"]=="nas_serve" and not d["absolute"] and d["integrity"]=="ok" and not d["missing"] else 1)' \
  <<<"$MEDIA" && ok "via=nas_serve、相对 URL、完整性 ok" || bad "media 响应不对"
curl -s -D- -o /dev/null "${AUTH[@]}" "$BASE/v1/photo/$PID_A/media" | grep -qi '^Cache-Control: no-store' \
  && ok "media 响应 no-store（直链会过期）" || bad "media 缺 no-store"

curl -s "${AUTH[@]}" "$BASE$URL" -o "$WORK/dl_full.mp4" -D "$WORK/h_full.txt"
PLAY="$(find "$WORK/data/playable" -name '*.mp4' | head -1)"
[ -n "$PLAY" ] || PLAY="$NAS/videos/a.mp4"
if cmp -s "$WORK/dl_full.mp4" "$PLAY"; then ok "全量取流字节完全一致（$(stat -c%s "$PLAY") 字节）"
else bad "取到的字节与磁盘上的不一致"; fi
grep -qi '^Accept-Ranges: bytes' "$WORK/h_full.txt" && ok "Accept-Ranges: bytes" || bad "缺 Accept-Ranges"
eq "$(stat -c%s "$PLAY")" "$(grep -i '^Content-Length:' "$WORK/h_full.txt" | tr -dc 0-9)" "Content-Length 正确"

TOTAL=$(stat -c%s "$PLAY")
code="$(curl -s -o "$WORK/dl_part.bin" -w '%{http_code}' -D "$WORK/h_part.txt" \
  "${AUTH[@]}" -H 'Range: bytes=100-199' "$BASE$URL")"
eq 206 "$code" "Range 请求 → 206"
eq "bytes 100-199/$TOTAL" "$(grep -i '^Content-Range:' "$WORK/h_part.txt" | sed 's/^[^ ]* //' | tr -d '\r')" \
  "Content-Range 正确"
dd if="$PLAY" bs=1 skip=100 count=100 of="$WORK/expect_part.bin" status=none
cmp -s "$WORK/dl_part.bin" "$WORK/expect_part.bin" \
  && ok "206 的体真的从偏移 100 开始" || bad "206 的体偏移不对（会花屏）"
eq 416 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" \
  -H "Range: bytes=$((TOTAL+10))-" "$BASE$URL")" "越界 Range → 416"
eq 200 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" \
  -H 'Range: bytes=0-9,20-29' "$BASE$URL")" "多段 Range → 忽略并返回 200 全量（RFC 7233）"
eq 200 "$(curl -s -o /dev/null -w '%{http_code}' -I "${AUTH[@]}" "$BASE$URL")" "HEAD 探能力 → 200"

eq 200 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/v1/photo/$PID_B/media")" \
  "无视频的照片 media → 200"
python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d["url"] is None and d["reason"]=="no_video" else 1)' \
  <<<"$(curl -s "${AUTH[@]}" "$BASE/v1/photo/$PID_B/media")" \
  && ok "无视频时 url=null reason=no_video" || bad "无视频的响应不对"

step "5. imgdb / thumb（ARCore 增强图像数据库）"
code="$(curl -s -D "$WORK/h_imgdb.txt" -o "$WORK/a.imgdb" -w '%{http_code}' \
  "${AUTH[@]}" "$BASE/v1/photo/$PID_A/imgdb")"
eq 200 "$code" "GET imgdb → 200"
imgdb_bytes="$(stat -c%s "$WORK/a.imgdb")"
if [ "$imgdb_bytes" -gt 1000 ]; then ok ".imgdb 有 $imgdb_bytes 字节"
else bad ".imgdb 只有 $imgdb_bytes 字节，像是错误响应而不是真数据库"; fi
eq "$imgdb_bytes" "$(python3 -c 'import json,sys;print(json.load(sys.stdin)["imgdbBytes"])' \
  <<<"$(curl -s "${AUTH[@]}" "$BASE/v1/photo/$PID_A")")" "detail 里的 imgdbBytes 与实际一致"
grep -qi 'immutable' "$WORK/h_imgdb.txt" && ok "Cache-Control immutable" || bad "缺 immutable"
ETAG="$(grep -i '^ETag:' "$WORK/h_imgdb.txt" | sed 's/^[^ ]* //' | tr -d '\r')"
eq 304 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -H "If-None-Match: $ETAG" \
  "$BASE/v1/photo/$PID_A/imgdb")" "带 If-None-Match → 304"
code="$(curl -s -o "$WORK/a_thumb.jpg" -w '%{http_code}' "${AUTH[@]}" "$BASE/v1/photo/$PID_A/thumb")"
eq 200 "$code" "GET thumb → 200"
python3 -c "
import cv2,sys
img=cv2.imread('$WORK/a_thumb.jpg')
assert img is not None, '缩略图解不开'
print('     缩略图 %dx%d' % (img.shape[1], img.shape[0]))
" && ok "缩略图是能解开的 JPEG" || bad "缩略图坏了"

step "6. 路径穿越（白名单外一律 403，且不回显路径）"
travs=(
  "/etc/passwd|绝对路径"
  "$NAS/../outside/secret.txt|.. 逃出根"
  "%2Fetc%2Fpasswd|URL 编码的绝对路径"
  "%252Fetc%252Fpasswd|双重编码"
  "..%2F..%2Fetc%2Fpasswd|编码的 .."
  "$NAS/photos/../../outside/secret.txt|根内绕出去"
  "photos/a.jpg|相对路径（必须是绝对）"
  "$WORK/outside/secret.txt|同一父目录下的邻居"
)
for t in "${travs[@]}"; do
  p="${t%%|*}"; desc="${t##*|}"
  c="$(curl -s -o "$WORK/trav.json" -w '%{http_code}' -G "${AUTH[@]}" \
    --data-urlencode "path=$p" "$BASE/v1/fs/list")"
  if [ "$c" = "403" ]; then ok "fs/list $desc → 403"; else bad "fs/list $desc → $c（应 403）"; fi
  if grep -q 'secret\|passwd' "$WORK/trav.json" 2>/dev/null; then
    bad "403 响应体回显了被拒的路径（信息泄露）"
  fi
done
# 已编码的穿越串直接拼进 query（不经 --data-urlencode），走真实解码路径
eq 403 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/v1/fs/list?path=%2Fetc%2Fpasswd")" \
  "裸 query 里的编码穿越串 → 403"
# 入库接口也走同一套校验
eq 403 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"refPath\":\"$WORK/outside/secret.txt\",\"printWidthMm\":100}" "$BASE/v1/photo")" \
  "入库白名单外的文件 → 403"
eq 403 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"videoPath\":\"/etc/hosts\"}" "$BASE/v1/photo/$PID_B/video")" \
  "关联白名单外的视频 → 403"
# 符号链接逃逸：根内放一个指向外面的链接
ln -sfn "$WORK/outside" "$NAS/escape"
eq 403 "$(curl -s -o /dev/null -w '%{http_code}' -G "${AUTH[@]}" \
  --data-urlencode "path=$NAS/escape/secret.txt" "$BASE/v1/fs/list")" "符号链接逃逸 → 403"
rm -f "$NAS/escape"
# 白名单内的正常浏览必须还能用
eq 200 "$(curl -s -o /dev/null -w '%{http_code}' -G "${AUTH[@]}" \
  --data-urlencode "path=$NAS/photos" "$BASE/v1/fs/list")" "白名单内目录 → 200"
eq 200 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/v1/fs/list")" "不带 path 列出所有根 → 200"

step "7. 上传 / 列表 / 历史 / 一致性"
head -c 200000 /dev/urandom > "$WORK/up.bin"
eq 201 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X POST --data-binary "@$WORK/up.bin" \
  "$BASE/v1/upload?name=up.bin")" "上传落地到 inbox → 201"
cmp -s "$WORK/up.bin" "$NAS/inbox/up.bin" && ok "上传字节一致" || bad "上传字节不一致"
eq 409 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X POST --data-binary "@$WORK/up.bin" \
  "$BASE/v1/upload?name=up.bin")" "重名上传 → 409（不覆盖）"
eq 400 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X POST --data-binary "@$WORK/up.bin" \
  "$BASE/v1/upload?name=../escape.bin")" "name 带路径 → 400"
eq 413 "$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -H 'CF-Ray: fake-ray' \
  -X POST --data-binary "@$WORK/up.bin" "$BASE/v1/upload?name=via-tunnel.bin")" \
  "带 Cloudflare 头的上传 → 413（隧道 100MB 上限）"

python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if len(d["photos"])==2 else 1)' \
  <<<"$(curl -s "${AUTH[@]}" "$BASE/v1/photos")" && ok "GET /v1/photos 两张" || bad "照片列表数量不对"
# 到这里只发生过两次识别：命中 a、未命中 stranger（第三次是 400，不进日志）
python3 -c '
import json,sys
d=json.load(sys.stdin)["entries"]
assert len(d) == 2, "历史条数 %d（命中 1 + 未命中 1）" % len(d)
hit = [e for e in d if e["photoId"]]
miss = [e for e in d if not e["photoId"]]
assert len(hit) == 1 and len(miss) == 1, d
assert hit[0]["inliers"] >= 40, hit[0]
assert hit[0]["via"] == "lan", "X-PhotoAR-Endpoint 没落到 via：%r" % hit[0]["via"]
assert hit[0]["refThumbUrl"] and hit[0]["title"] == "E2E A", hit[0]
print("     历史 %d 条：命中 inliers=%d via=%s；未命中 1 条" % (len(d), hit[0]["inliers"], hit[0]["via"]))
' <<<"$(curl -s "${AUTH[@]}" "$BASE/v1/history?limit=50")" \
  && ok "识别历史落库（命中/未命中都记，via 来自请求头）" || bad "历史不对"

PYTHONPATH="$REPO/src" python3 -m photoar.server.httpd -c "$WORK/config.json" check > "$WORK/check.json"
rc=$?
eq 0 "$rc" "photoar-server check：catalog 与识别库一致"
python3 -c 'import json,sys;d=json.load(sys.stdin);print("     catalog %d 张｜library %d 张｜问题 %d 处"%(d["photosInCatalog"],d["photosInLibrary"],len(d["problems"])))' \
  < "$WORK/check.json"
PYTHONPATH="$REPO/src" python3 -m photoar.server.httpd -c "$WORK/config.json" verify \
  | sed 's/^/     /'

step "8. 重启后仍能识别（索引真的落盘了）"
kill "$SRV_PID"; wait "$SRV_PID" 2>/dev/null; SRV_PID=""
PYTHONPATH="$REPO/src" python3 -m photoar.server.httpd -c "$WORK/config.json" serve \
  >> "$SRV_LOG" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 100); do
  curl -fsS "${AUTH[@]}" "$BASE/v1/ping" -o /dev/null 2>/dev/null && break; sleep 0.3
done
got="$(curl -s "${AUTH[@]}" -F "frame=@$WORK/query_a.jpg;type=image/jpeg" "$BASE/v1/recognize" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin).get("photoId"))')"
eq "$PID_A" "$got" "重启后同一帧仍命中同一张"

step "结果"
printf '  通过 \033[32m%d\033[0m  失败 \033[31m%d\033[0m\n' "$pass" "$fail"
if [ "$fail" -ne 0 ]; then
  echo "  服务端日志："; sed 's/^/    /' "$SRV_LOG" | tail -40
fi
exit $(( fail > 0 ? 1 : 0 ))
