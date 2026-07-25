# 影像后期工作台

多项目影像后期工艺展示站。每一个项目，一页工艺拆解：怎么做的、为什么这么做、还缺什么。

- 站点入口（GitHub Pages）：https://tripplemay.github.io/video-post-workbench/
- 纯静态：无框架、无构建、无外部依赖，全部相对路径。

## 目录结构

```
index.html                  工作台首页（项目卡入口）
p/<项目slug>/
  index.html                单项目工艺拆解页
  assets/                   图片、poster、OG 图等（视频除外）
tools/upload_video.sh       预览视频发布脚本（走 Release，不入仓库）
.github/workflows/pages.yml CI/CD：push 到 main 自动部署 Pages
```

## 部署（CI/CD）

push 到 `main` 分支即自动部署到 GitHub Pages（`.github/workflows/pages.yml`），
无需手动操作。部署状态在仓库 Actions 页查看。

## 视频工作流（重要）

GitHub 单文件限 100MB，**网页预览视频一律不入仓库**，约定与流程：

1. 预览视频统一命名 `film_web.mp4`，放对应项目的 `assets/`（`.gitignore` 已忽略）。
2. 发布视频：
   ```bash
   tools/upload_video.sh p/<slug>/assets/film_web.mp4
   ```
   脚本把它传到仓库 Release（tag: `media`，重名自动覆盖）。
3. 项目页 `<source>` 引用 Release 直链：
   ```html
   <source src="https://github.com/tripplemay/video-post-workbench/releases/download/media/film_web.mp4" type="video/mp4">
   ```
   多项目时用不同文件名区分，如 `film_web_<slug>.mp4`。

## 新增一个项目页

1. 复制 `p/zouzhipeng-packaging/` 为 `p/<新slug>/`，替换页面内容与 `assets/`。
2. 项目页顶栏面包屑里改当前项目名。
3. 首页 `index.html` 的项目网格里加一张 `.pcard`，链接到 `p/<新slug>/`。
4. 预览视频按上面的视频工作流发布并引用。
5. push 到 `main`，自动上线。

## 项目

| 项目 | 页面 | 状态 |
| --- | --- | --- |
| 福哥和他的朋友们 · 邹志鹏期 — 后期包装工艺 | [p/zouzhipeng-packaging](https://tripplemay.github.io/video-post-workbench/p/zouzhipeng-packaging/) | 已定稿 |
