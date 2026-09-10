# 校招岗位检索器 · 物流 / 供应链专版

自动同步 [27届校招汇总站](http://101.132.173.68/campus/campus_recruit.html) 的岗位数据，
每天重新抓取、重新生成静态页面，发布到 GitHub Pages。**完全免费，公开仓库零成本。**

部署后会得到**两个页面**，按需选用：

| 地址 | 体积 | 特点 |
|---|---|---|
| `https://<用户名>.github.io/campus-jobs/app.html` | 45 KB | **接口版**（推荐）。页面不含数据，运行时向 `api/` 拉，改界面不用重跑数据 |
| `https://<用户名>.github.io/campus-jobs/` | 4.2 MB | 离线版。数据烧进 HTML，双击本地文件、断网也能用 |

---

## 工作原理

```
GitHub Actions（每天 2 次定时触发）
   └─ 抓取原站 HTML（5MB 静态页，数据内联在 RAW_DATA 里）
        └─ 解析 7000+ 条岗位 + 城市省份映射
             └─ 物流/供应链关键词打分
                  ├─ dist/index.html      离线版：数据内嵌
                  ├─ dist/app.html        接口版：只含界面
                  └─ dist/api/*.json      接口数据
                       └─ force push 到 gh-pages 分支（孤立提交，历史不累积）
```

关键点：**gh-pages 每次都是全新的孤立提交**（`git init` + `--force`），
所以 4MB 的文件每天覆盖，仓库体积恒定，不会撞上 GitHub 1GB 软限制。

---

## 部署步骤（约 5 分钟）

### 1. 建仓库

在 GitHub 新建仓库，名字随意（下面以 `campus-jobs` 为例）。
**Visibility 必须选 Public** —— 免费账户的私有仓库虽然也能开 Pages，
但公开仓库的 Actions 额度才是真正无限的，而且你本来就是要分享给别人。

### 2. 上传代码（两种方式，选一个）

**方式 A：命令行**（推荐，隐藏目录不会丢）

```bash
cd campus-jobs
git init
git config user.name  "你的名字"
git config user.email "你的邮箱"      # 换成你 GitHub 注册邮箱
git add -A
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<你的用户名>/campus-jobs.git
git push -u origin main
```

推送时选 `Browser` 登录，或提前在 GitHub Settings → Developer settings →
Personal access tokens 生成 token 当密码用（注意：密码方式已被 GitHub 停用，只能用 token）。

**方式 B：网页上传**（不用装 git）

新建仓库后点 **uploading an existing file**，把 `campus-jobs` 文件夹里的内容
（含 `.github` 文件夹）整个拖进去，然后 Commit。
> 若 `.github` 没传上去，Actions 不会触发——检查一下仓库里有没有 `.github/workflows/sync.yml`。

### 3. 等第一次跑完

推送后去仓库的 **Actions** 标签页，会看到 `每日同步校招岗位数据` 正在跑，约 1 分钟。

跑完后脚本会自动尝试开启 Pages。如果没成功，手动开一次：
**Settings → Pages → Source 选 `Deploy from a branch` → 分支 `gh-pages` → 目录 `/ (root)` → Save**。

### 4. 拿到链接

`https://<你的用户名>.github.io/campus-jobs/`

把它发给同学即可。以后每天自动更新，链接永远不变。

---

## 手动触发

想立刻刷新，不用等定时：**Actions → 每日同步校招岗位数据 → Run workflow**。

---

## 定时任务说明

| cron (UTC) | 北京时间 | 说明 |
|---|---|---|
| `0 4 * * *` | 12:00 | 主任务，原站通常上午 11 点前后更新 |
| `0 10 * * *` | 18:00 | 兜底，当天首次失败时补跑 |

**注意**：GitHub 的定时触发在高峰期可能延迟 15~40 分钟，这是正常的，不是脚本挂了。

**另一个坑**：GitHub 会禁用 60 天无活动的仓库的定时 workflow。
放寒假、招聘季结束之后如果连续两个月没动静，去 Actions 页面看一眼是否被自动停用。

---

## 本地运行

不想等 Actions，本地也能生成（需要 Python 3.8+，无第三方依赖）：

```bash
python fetch_build.py
```

产物在 `dist/`，`index.html` 双击即可打开（数据已内嵌，离线可用）。
失败会返回非 0 退出码，不会覆盖已有产物。

自定义数据源：`SRC_URL=http://... python fetch_build.py`

---

## 数据接口

接口版页面从 `api/` 目录取数——这一层就是你的"后端"。四个端点：

| 端点 | 内容 | 体积 |
|---|---|---|
| `api/meta.json` | 更新时间、总条数、分片清单、近 7 天新增 | ~1 KB |
| `api/logistics.json` | 物流 / 供应链命中分包 | 1.3 MB（gzip **0.32 MB**） |
| `api/all.json` | 全量岗位 | 4.4 MB（gzip 1.1 MB） |
| `api/cities.json` | 城市 → 省份映射 | 15 KB |

**加载流程**：先取 `meta.json` 看版本 → 命中 localStorage 缓存就直接渲染（约 0.2 秒，不产生流量）
→ 否则拉 `logistics.json`。用户点「加载全部」才去取 `all.json`。
所以首屏只传 0.32 MB，而不是 1.1 MB。

`meta.json` 结构：

```json
{
  "updated": "2026-09-10 21:46",
  "updatedAt": "2026-09-10T21:46:17+08:00",
  "total": 7564, "hit": 1860, "strong": 1342, "cities": 780, "today": 0,
  "recent7": [{ "date": "2026-09-09", "count": 108 }],
  "topIndustry": [{ "name": "制造业", "count": 1656 }],
  "shards": {
    "all":       { "file": "all.json",       "count": 7564, "bytes": 4383838, "gzip": 1163739 },
    "logistics": { "file": "logistics.json", "count": 1860, "bytes": 1293731, "gzip": 334938 }
  }
}
```

### 让前端和接口分开部署

接口版默认用相对路径 `./api/`，即前后端同域。想让前端跑在别处，加 `?api=` 参数指定接口地址：

```
https://你的前端地址/app.html?api=https://<用户名>.github.io/campus-jobs/api/
```

这样界面与数据彻底解耦：改界面只动前端，改数据只跑 Actions。

> **为什么不用 jsDelivr 加速**：实测它对分支引用返回 `Cache-Control: max-age=604800`，
> 浏览器会缓存 7 天，数据更新了用户也看不到。GitHub Pages 自带
> `Access-Control-Allow-Origin: *` 且缓存只有 10 分钟，更适合每日更新的场景。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `fetch_build.py` | 一体化：抓取 → 解析 → 打分 → 生成页面与 API 分片。改关键词就编辑顶部的 `CORE` / `RELA` 列表 |
| `template.html` | 页面模板，占位符 `__DATA__` / `__CITYPROV__` / `__API__` / `__TOTAL__` / `__UPDATED__`。`__DATA__` 为空数组且 `__API__` 非空时自动走接口模式 |
| `.github/workflows/sync.yml` | 定时任务 |
| `test_api.js` | 端到端联调：起本地服务器，用真实 HTTP 请求验证接口版加载流程 |
| `dist/` | 产物（不提交到 main）。`index.html` 离线版、`app.html` 接口版、`api/` 接口数据 |

想换专业方向（比如改成"财务""机械"），只需改 `fetch_build.py` 里的 `CORE`、`RELA`、
`COMPANY_HINT` 三个列表，以及 `template.html` 里的 `PRESET_CORE`、`PRESET_BROAD`。

---

## 数据说明

- 来源为第三方聚合站，岗位信息以原站及企业官方公告为准
- 页面里的「投递链接」指向原站或企业招聘系统，部分链接包含渠道追踪参数
- 岗位均带原始来源链接，可回溯核实
