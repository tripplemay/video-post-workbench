# 影像后期工作台

多项目影像后期工艺展示站。每一个项目，一页工艺拆解：怎么做的、为什么这么做、还缺什么。

- 线上地址：https://video.guangai.ai/
- 纯静态：无框架、无构建、无外部依赖，全部相对路径。

## 架构

```
GitHub 仓库（代码 + 图片资产）
   │  push 到 main
   ▼
GitHub Actions（.github/workflows/deploy.yml）
   │  rsync 到 VPS（自有服务器，secrets 注入部署密钥）
   ▼
VPS nginx（video.guangai.ai → /var/www/workbench，Let's Encrypt 证书）

网页预览视频 → 阿里云 OSS（guanghe-ai，成都；对象级公共读）
   引用地址 https://guanghe-ai.oss-cn-chengdu.aliyuncs.com/<key>（国内骨干直连，支持 Range 拖动）
   （2026-08 起从 Cloudflare R2 迁入：本机到 R2 S3 端点被运营商重置、境外上行被时变 QoS 掐流；
   R2 桶保留为冷备份）
```

## 目录结构

```
index.html                  工作台首页（项目卡入口）
p/<项目slug>/
  index.html                单项目工艺拆解页
  assets/                   图片、poster、OG 图等（视频除外）
tools/upload_video.py       预览视频上传到阿里云 OSS 的 CLI（分片断点续传，可选 VPS 拉取副本）
.github/workflows/deploy.yml CI/CD：push 到 main 自动 rsync 到 VPS
```

## 部署（CI/CD）

push 到 `main` 分支，GitHub Actions 自动 rsync 到 VPS 的 `/var/www/workbench/`。
部署状态在仓库 Actions 页查看。需要的仓库 Secrets：

- `DEPOYSVR_HOST` / `DEPOYSVR_USER`：VPS 地址与登录用户
- `DEPOYSVR_SSH_KEY`：部署私钥（对应公钥在 VPS 的 authorized_keys）

## 视频工作流（重要）

GitHub 单文件限 100MB，**网页预览视频一律不入仓库**（`.gitignore` 已忽略 `film_web.mp4`）：

1. 预览视频统一命名 `film_web.mp4`，放对应项目的 `assets/`。
2. 上传到阿里云 OSS（国内骨干直连，稳定）：
   ```bash
   python tools/upload_video.py p/<slug>/assets/film_web.mp4 <slug>
   ```
   CLI 为纯标准库实现（OSS REST + V1 签名）：并行分片（默认 8MB×4）、断点续传
  （状态绑定文件+key+分片大小指纹，中断后重跑同一命令即可）、指数退避重试、
   uploadId 失效自动重建、完成后 PutObjectACL 公共读 + 按字节数校验。
   可选 `--rate <KB/s>` 限速（令牌桶+AIMD 自动降速，网络差的日子用）。
   凭据读本机 `~/.deploy/oss.env`（OSS_ACCESS_KEY_ID/SECRET/BUCKET/ENDPOINT）。
   常用选项：`--key <自定义key>`、`--chunk 16M`、`--jobs 4`、`--delete <key>` 删除对象。
3. 项目页 `<source>` 引用 OSS 地址：
   ```html
   <source src="https://guanghe-ai.oss-cn-chengdu.aliyuncs.com/<slug>/film_web.mp4" type="video/mp4">
   ```
   同名覆盖即时生效，无需版本号（无 CDN 缓存层）。

### VPS 副本（可选）

视频无需直传 VPS（本地→VPS 单流国际链路极易断）。改为 VPS 自己从 OSS 公共地址拉取：

```bash
python tools/upload_video.py p/<slug>/assets/film_web.mp4 <slug> --target all   # 传 OSS 后 VPS 拉副本
python tools/upload_video.py x <slug> --target vps                              # 只对已有 key 拉副本
```

VPS 侧用 `curl -C -` 断点续传拉取（先落 `.part` 再原子改名），存到 `/var/www/media/<key>`
（可用 `--vps-dir` 改）。凭据读 `~/.deploy/depoysvr.env`（HOST/USER/PORT）+ 私钥。

`/var/www/media/` 同时是 **FTP 上传空间**（vsftpd，用户 `mediaftp` chroot 于此，凭据在
`~/.deploy/ftp.env`），手工 FTP 客户端上传的文件直接经 nginx 出链：
`https://video.guangai.ai/media/<相对路径>`（支持 Range 拖动，缓存 5 分钟）。

### 历史备注

2026-08 前预览视频存 Cloudflare R2（cdn.guangai.ai）：本机到 R2 S3 端点 TLS 被 100% 重置、
境外上行被时变 QoS 掐流（突发即断），故迁入阿里云 OSS。R2 桶保留为冷备份；
其生命周期规则建议保留「Abort incomplete multipart uploads after 7 days」。

## 新增一个项目页

1. 复制 `p/zouzhipeng-packaging/` 为 `p/<新slug>/`，替换页面内容与 `assets/`。
2. 项目页顶栏面包屑里改当前项目名。
3. 首页 `index.html` 的项目网格里加一张 `.pcard`，链接到 `p/<新slug>/`。
4. 预览视频按上面的视频工作流上传到 OSS 并引用。
5. push 到 `main`，自动部署到 https://video.guangai.ai/p/<新slug>/。

## 项目

| 项目 | 页面 | 状态 |
| --- | --- | --- |
| 福哥和他的朋友们 · 邹志鹏期 — 后期包装工艺 | [p/zouzhipeng-packaging](https://video.guangai.ai/p/zouzhipeng-packaging/) | 已定稿 |
