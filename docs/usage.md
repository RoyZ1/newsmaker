# 微信公众号 AI 信息采集 Agent

本项目是一个本地运行的微信公众号科技内容 Agent。根目录只保留 `run_server.py` 作为主入口；采集、写作、图片和接口测试脚本放在 `scripts/` 目录中。

## 当前能力

- 支持 `rss`、`web`、`json`、`changelog` 四类来源。
- 默认只保留有明确发布时间戳的内容。
- 默认采集最近 72 小时内容，可在 `config/sources.yml` 的 `app.lookback_hours` 调整。
- 默认保留评分最高的 50 条，可在 `app.target_items` 调整。
- 采集结果保存到 `data/items.json`，并按北京时间归档到 `data/daily/YYYY-MM-DD.json`。
- 图片缓存到 `data/images`。
- 图片策略：官方来源图片可作为发布候选；媒体/自媒体来源图片仅保留到 `raw.preview_images`，不会作为页面主图或公众号素材候选。
- 支持关键词过滤，综合媒体源可设置 `keyword_scope: title`，避免正文误命中。
- 每条内容会计算 `score`，排序优先看热度分，再看发布时间。

## 目录结构

```text
app/                 核心业务代码
config/              信息源、prompt 和模型相关配置
data/                本地数据、图片、SQLite、导出和同步记录
docs/                接口配置和操作说明
scripts/collect/     采集和选题脚本
scripts/write/       写作脚本
scripts/images/      配图脚本
scripts/tests/       LLM / 图片接口连通性测试
static/              工作台样式
templates/           工作台页面
run_server.py        主入口
```

## 运行工作台

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
python run_server.py
```

打开：

```text
http://127.0.0.1:5050
```

工作台页面：

- `http://127.0.0.1:5050`：采集列表
- 工作流分页中完成新闻采集、生成选题、生成草稿、配图、舆论素材、公众号导出和草稿箱同步。

## 可选脚本

通常直接用工作台按钮即可。需要命令行调试时，可在项目根目录运行：

```powershell
python scripts/collect/run_collector.py
python scripts/collect/run_topics.py
python scripts/write/run_writer.py
python scripts/images/run_cover_images.py
python scripts/tests/run_llm_test.py
python scripts/tests/run_image_test.py
```

## LLM 配置

在 `.env` 中配置 OpenAI-compatible 接口：

```text
LLM_API_KEY=REDACTED
LLM_MODEL_ID=...
LLM_BASE_URL=...
LLM_TIMEOUT=120
```

草稿生成使用 `LLM_BASE_URL/chat/completions`。系统不会打印密钥。

## 修改写作 Prompt

文章写作提示词在：

```text
config/prompts/article_writer.json
```

其中：

- `system` 控制作者身份和整体文风。
- `user` 控制结构、长度、配图、输出 JSON 格式。
- `{{SOURCE_BRIEF_JSON}}` 是素材占位符，不要删除。

在 PyCharm / IDEA 里建议运行根目录入口：

- `run_server.py`

其它功能脚本在 `scripts/` 下，仅用于调试或批处理。

## 草稿编辑与公众号导出

在工作台的“草稿与配图”页：

- 打开某篇文章的“编辑草稿”，可以修改标题、副标题和正文 Markdown。
- 点击“保存修改”后，会覆盖 `data/drafts.json`，下次加载页面就是保存后的内容。
- 每次保存都会在 `data/draft_versions/YYYY-MM-DD/` 写入编辑前和保存后的版本快照。
- 点击“导出公众号格式”会生成：
  - `data/exports/wechat/YYYY-MM-DD/*.html`：可预览、可复制的公众号正文 HTML。
  - `data/exports/wechat/YYYY-MM-DD/*.json`：公众号草稿箱/图文素材接口 payload 骨架。

注意：未接入公众号接口前，本地图片不能直接成为微信后台可用图片。JSON 导出文件会保留 `WECHAT_IMAGE_URL_*` 和 `WECHAT_THUMB_MEDIA_ID` 占位符，后续需要用公众号图片上传接口替换。

## SQLite 数据库存储

系统会把关键结构化数据同步到：

```text
data/agent.db
```

当前仍保留 JSON 文件作为兼容备份，SQLite 用于后续查询、备案和扩展接口。已接入的数据表包括：

- `news_items`：采集到的新闻和评分。
- `topics`：生成的选题。
- `drafts`：当前文章草稿、正文和配图状态。
- `draft_versions`：每次手动保存草稿的版本快照。
- `publications`：已发布备案。
- `wechat_exports`：公众号格式导出记录。

可以在工作台“发布记录”页点击“同步现有数据到 SQLite”，把已有 JSON 文件回填进数据库。

## 配置信息来源

编辑 `config/sources.yml`。

评分目前是规则制：官方源、顶级公司/模型、模型发布、论文、融资、合作、CEO/高管、图片、时效性都会加分；GitHub 项目会降权并受 `source_limits` 限制，避免占据多数。

RSS 示例：

```yaml
- id: qbitai_feed
  name: QbitAI
  type: rss
  enabled: true
  url: https://www.qbitai.com/feed
  tags: [media, china, ai]
  include_keywords: [AI, 大模型, 智能体, DeepSeek, 腾讯, 字节, 阿里]
  keyword_scope: title
  max_items: 10
```

网页示例：

```yaml
- id: zhipu_news
  name: Zhipu AI News
  type: web
  enabled: true
  url: https://www.zhipuai.cn/zh/news
  item_selector: "a[href^='/zh/news/']"
  title_selector: "h1"
  summary_selector: "meta[name='description']"
  image_selector: "main img, img"
  tags: [zhipu, official, china]
```

JSON API 示例：

```yaml
- id: github_ai_recent
  name: GitHub Recent AI Repos
  type: json
  enabled: true
  url: "https://api.github.com/search/repositories?q=(ai%20OR%20llm%20OR%20agent)%20created:%3E={since_date}&sort=stars&order=desc&per_page=20"
  list_path: items
  title_path: full_name
  link_path: html_url
  summary_path: description
  image_path: owner.avatar_url
  published_path: created_at
```

## 后续步骤

- 加入大模型摘要、话题聚类、热度评分。
- 增加“候选选题”页面，从当天新闻中选出 5-10 个最值得写的主题。
- 增加人工审核和公众号草稿生成。

## 小黑盒导出

工作台草稿卡片新增“小黑盒复制/导出”。当前公开渠道未发现小黑盒文章草稿箱创建 API，因此先提供富文本复制、HTML/Markdown 导出和 PNG 图片下载包，不使用模拟登录或未公开接口自动写入。详细说明见 `docs/heybox_integration.md`。
