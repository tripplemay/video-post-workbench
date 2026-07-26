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

网页预览视频 → Cloudflare R2（guangai-media bucket）
   引用地址 https://cdn.guangai.ai/<key>（免出口流量费，支持 Range 拖动）
```

## 目录结构

```
index.html                  工作台首页（项目卡入口）
p/<项目slug>/
  index.html                单项目工艺拆解页
  assets/                   图片、poster、OG 图等（视频除外）
tools/upload_video.py       预览视频上传到 R2 的脚本
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
2. 上传到 R2：
   ```bash
   python tools/upload_video.py p/<slug>/assets/film_web.mp4 <slug>/film_web.mp4
   ```
   分片断点续传，凭据读本机 `~/.deploy/r2.env`。
3. 项目页 `<source>` 引用 CDN 地址：
   ```html
   <source src="https://cdn.guangai.ai/<slug>/film_web.mp4" type="video/mp4">
   ```

## 新增一个项目页

1. 复制 `p/zouzhipeng-packaging/` 为 `p/<新slug>/`，替换页面内容与 `assets/`。
2. 项目页顶栏面包屑里改当前项目名。
3. 首页 `index.html` 的项目网格里加一张 `.pcard`，链接到 `p/<新slug>/`。
4. 预览视频按上面的视频工作流上传到 R2 并引用。
5. push 到 `main`，自动部署到 https://video.guangai.ai/p/<新slug>/。

## 项目

| 项目 | 页面 | 状态 |
| --- | --- | --- |
| 福哥和他的朋友们 · 邹志鹏期 — 后期包装工艺 | [p/zouzhipeng-packaging](https://video.guangai.ai/p/zouzhipeng-packaging/) | 已定稿 |
