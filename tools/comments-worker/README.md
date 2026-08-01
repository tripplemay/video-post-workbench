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

> 若 wrangler 报「Failed to automatically retrieve account IDs」（CF_API_TOKEN 无 User 级权限），
> 改用 REST API 直部署（R2_API_TOKEN 有 Workers Scripts 写权限）：
> ```bash
> set -a; source ~/.deploy/r2.env; set +a
> curl -X PUT "https://api.cloudflare.com/client/v4/accounts/$R2_ACCOUNT_ID/workers/scripts/review-comments" \
>   -H "Authorization: Bearer $R2_API_TOKEN" \
>   -F 'metadata={"main_module":"worker.js","compatibility_date":"2026-07-26","bindings":[{"name":"COMMENTS","type":"kv_namespace","namespace_id":"<KV_ID>"},{"name":"OSS_BUCKET","type":"plain_text","text":"guanghe-ai"},{"name":"OSS_ENDPOINT","type":"plain_text","text":"oss-cn-chengdu.aliyuncs.com"}],"keep_bindings":["secret_text"]};type=application/json' \
>   -F 'worker.js=@worker.js;type=application/javascript+module'
> ```
> 写/轮换 secret（ADMIN_TOKEN、OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET；body 为单个对象，不是数组）：
> `PUT .../workers/scripts/review-comments/secrets`，body `{"name":"<名称>","text":"<值>","type":"secret_text"}`。

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

## 管理员上传预览视频（浏览器 OSS 直传）

`/upload/<slug>` 是阿里云 OSS（guanghe-ai，成都）分片上传的**控制面**：Worker 只做签名
（代签 Init/Complete/Abort/Delete 与分片预签名 URL），浏览器数据面 PUT 直连 OSS，
不经过 Worker。全部接口需要 `X-Admin-Token`。默认对象 `<slug>/film_web.mp4`，
可加 `?key=` 指定其他 `.mp4` 对象（如测试 key）：

- `POST /upload/<slug>?op=start` → `{ uploadId }` 创建分片任务
- `GET  /upload/<slug>?op=sign&uploadId=..&from=1&count=20` → `{ urls:{n:url}, expires }`
  批量分片预签名 URL（V1 查询串签名，有效期 1h）
- `POST /upload/<slug>?op=complete`，body `{ uploadId, parts:[{PartNumber,ETag}] }` →
  合并完成：自动设对象 ACL 公共读，并返回 `{ ok, key, url, size }`（size 供客户端校验）
- `POST /upload/<slug>?op=abort`，body `{ uploadId }` → 放弃任务
- `POST /upload/<slug>?op=delete&key=..` → 删除对象

配置：`wrangler.toml [vars]` 存 `OSS_BUCKET` / `OSS_ENDPOINT`；
`OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` 用 secrets API 写入（body 为单对象）；
桶需配 CORS 允许 `https://video.guangai.ai` 等的 PUT/GET 并 Expose ETag（已配置）。

两个客户端：
- **CLI（主力）**：`tools/upload_video.py`，自签直传 OSS，不经 Worker，见仓库 README「视频工作流」。
- **浏览器面板（应急）**：项目页持有管理令牌（`?review-admin=`）时显示「Admin · 上传预览视频」，
  8MB 分片按批取预签名后直连 OSS（2 并发），断点存浏览器 localStorage，签名过期自动重取。
  注意：直传 body 必须是无 MIME type 的 Blob（预签名不含 Content-Type，带了会签名不符）。

> 历史：2026-08 前经 Worker 中转写 R2（本机到 R2 S3 端点被重置、境外上行被 QoS 掐流），
> 已迁入阿里云 OSS；R2 binding 与旧 `op=part` 中转接口随之移除。
