#!/usr/bin/env sh
# 等一个容器变 healthy。用法：wait-healthy.sh <容器名> [最多等几轮，每轮 2 秒，默认 60]
set -eu
name="$1"; n="${2:-60}"
i=0
while [ "$i" -lt "$n" ]; do
  i=$((i + 1))
  st=$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo starting)
  echo "[$i] $st"
  [ "$st" = "healthy" ] && exit 0
  [ "$st" = "unhealthy" ] && { echo "::error::$name unhealthy"; exit 1; }
  sleep 2
done
echo "::error::$name 在 $((n * 2)) 秒内没能变 healthy"; exit 1
