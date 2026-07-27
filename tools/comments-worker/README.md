# 审片意见 API（Cloudflare Worker + KV）

为纯静态站提供共享的审片意见存储：`GET /<slug>` 列出、`POST /<slug>` 提交、
`DELETE /<slug>?id=...` 删除（需管理令牌）。每个项目 slug 对应 KV 里一个 JSON 数组。

免费额度（Workers 10 万次请求/天、KV 10 万次读/天）对审片场景绰绰有余。

## 一次性部署（约 5 分钟）

```bash
cd tools/comments-worker

# 1. 登录 Cloudflare 账号（会打开浏览器授权）
npx wrangler login

# 2. 创建 KV 命名空间，把输出里的 id 填进 wrangler.toml 的 id 字段
npx wrangler kv namespace create COMMENTS

# 3. 设置管理令牌（用于删除意见，自己编一个长随机串）
npx wrangler secret put ADMIN_TOKEN

# 4. 部署
npx wrangler deploy
```

部署完成后 API 地址为 `https://review-comments.tripple.workers.dev/<slug>`（workers.dev 免费子域），例如：

```bash
curl https://review-comments.tripple.workers.dev/zouzhipeng-packaging
```

> 自定义域 `comments.guangai.ai` 需要 token 具备 Zone 级「Workers Routes:Edit」权限，
> 当前部署 token 没有，故先用 workers.dev 子域。之后可在 Cloudflare 后台
> Workers → review-comments → Settings → Domains & Routes 手动添加路由
> `comments.guangai.ai/*`，再把前端 `REVIEW_API` 与 wrangler.toml 的 routes 切回。

## 前端对接

项目页 `p/<slug>/index.html` 里的 `REVIEW_API` 常量指向 Worker 地址，意见全区共享可见。

## 删除意见

- **用户删自己的**：提交时浏览器生成随机删除令牌，服务端只存其 SHA-256 哈希（评论的 `h` 字段，
  GET 不返回）。本机浏览器持有令牌的评论会显示「删除」按钮，删除请求带 `X-Delete-Token`，
  Worker 验哈希通过才删。用户清除浏览器数据后失去删除权。
- **管理员删任意**：在页面 URL 后加 `?review-admin=<ADMIN_TOKEN>` 访问一次，浏览器会记住令牌
  并对所有评论显示「删除」按钮（删除请求带 `X-Admin-Token` 头，Worker 校验）。
  再访问 `?review-admin=` （空值）可清除本机令牌。

## 管理员上传预览视频（浏览器分片上传）

`/upload/<slug>` 一组接口把 `<slug>/film_web.mp4` 分片写入 R2（binding `MEDIA` → guangai-media），
全部需要 `X-Admin-Token`，用于在网页上直接更新预览视频（替代 tools/upload_video.py，网络不稳时可断点续传）：

- `POST /upload/<slug>?op=start` → `{ uploadId }` 创建分片任务
- `PUT  /upload/<slug>?op=part&uploadId=..&n=N`（body 为分片内容）→ `{ etag }`
- `POST /upload/<slug>?op=complete`，body `{ uploadId, parts:[{PartNumber,ETag}] }` → 合并完成
- `POST /upload/<slug>?op=abort`，body `{ uploadId }` → 放弃任务

项目页在持有管理令牌时会显示「Admin · 上传预览视频」面板：选择本地转码好的 720p
`film_web.mp4`，按 16MB 分片上传，断点状态存浏览器 localStorage，中断后重选同一文件即可续传。
