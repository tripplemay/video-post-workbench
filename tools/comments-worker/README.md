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

## 删除意见（管理）

页面上普通访客没有删除按钮。在页面 URL 后加 `?review-admin=<ADMIN_TOKEN>` 访问一次，
浏览器会记住令牌并显示「删除」按钮（删除请求带 `X-Admin-Token` 头，Worker 校验）。
再访问 `?review-admin=` （空值）可清除本机令牌。
