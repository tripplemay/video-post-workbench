#!/usr/bin/env bash
# 上传网页预览视频到 GitHub Release（tag: media）。视频不入 git 仓库：
# GitHub 单文件限 100MB，Release 资产限 2GB，直链支持 Range 拖动播放。
# 用法: tools/upload_video.sh <本地视频路径> [Release 上的文件名，默认同本地名]
# 例:   tools/upload_video.sh p/zouzhipeng-packaging/assets/film_web.mp4
# 凭据: 复用 git 凭据管理器里 github.com 的 token（gh auth login 或 Git 登录过即可）。
set -euo pipefail

FILE="${1:?用法: tools/upload_video.sh <视频文件> [文件名]}"
NAME="${2:-$(basename "$FILE")}"
REPO="tripplemay/video-post-workbench"
TAG="media"

TOKEN="$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill | sed -n 's/^password=//p')"
[ -n "$TOKEN" ] || { echo "找不到 github.com 凭据"; exit 1; }
AUTH="Authorization: Bearer $TOKEN"

json_id() { python -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true; }

# 1) 确保 release 存在
RID="$(curl -s -H "$AUTH" "https://api.github.com/repos/$REPO/releases/tags/$TAG" | json_id)"
if [ -z "$RID" ]; then
  echo "创建 release: $TAG"
  RID="$(curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"tag_name\":\"$TAG\",\"name\":\"Media assets\",\"body\":\"网页预览视频等大文件资产，不入 git 历史。\"}" \
    "https://api.github.com/repos/$REPO/releases" | json_id)"
fi
[ -n "$RID" ] || { echo "release 创建失败"; exit 1; }

# 2) 同名资产已存在则先删（实现覆盖上传）
OLD="$(curl -s -H "$AUTH" "https://api.github.com/repos/$REPO/releases/$RID/assets" \
  | python -c "import sys,json;print(next((a['id'] for a in json.load(sys.stdin) if a['name']=='$NAME'),''))" 2>/dev/null || true)"
[ -n "$OLD" ] && curl -s -X DELETE -H "$AUTH" "https://api.github.com/repos/$REPO/releases/assets/$OLD" >/dev/null

# 3) 上传（校验 HTTP 状态；157MB 约需几分钟，视网络而定）
echo "上传 $FILE -> $NAME"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "$AUTH" -H "Content-Type: video/mp4" --data-binary "@$FILE" \
  "https://uploads.github.com/repos/$REPO/releases/$RID/assets?name=$NAME")"
[ "$CODE" = "201" ] || { echo "上传失败 HTTP $CODE（网络不稳定时重试本命令即可）"; exit 1; }

echo "完成。引用地址："
echo "https://github.com/$REPO/releases/download/$TAG/$NAME"
