# 小黑盒接入说明

当前实现的是小黑盒格式导出和富文本复制，不是自动写入小黑盒草稿箱。

原因是公开入口里可以看到小黑盒开放平台和创作者内容管理页，但没有发现类似微信公众号 `draft/add` 的文章草稿箱创建 API。为了避免模拟登录、抓包逆向或绕过平台权限，系统先提供安全的人工粘贴流程。

## 已支持

- 在草稿卡片点击“复制到小黑盒”，生成适合粘贴到小黑盒编辑器的富文本，并自动打开小黑盒创作者后台。
- 在草稿卡片点击“半自动导入小黑盒”，会打开可见浏览器，尝试进入发文页、填写标题/正文、上传图片，最后停在页面等待人工审核，不会自动发布。
- 在草稿卡片点击“导出小黑盒”，生成：
  - `data/exports/heybox/YYYY-MM-DD/*.html`
  - `data/exports/heybox/YYYY-MM-DD/*.md`
  - `data/exports/heybox/YYYY-MM-DD/*.json`
  - `data/exports/heybox/YYYY-MM-DD/*-images/*.png`
- 导出记录会同步到 SQLite 的 `platform_exports` 表，平台名为 `heybox`。

## 半自动导入依赖

半自动导入使用 Playwright 控制本机可见浏览器。首次使用前需要在运行服务的同一个 Python 环境中执行：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

如果小黑盒未登录、遇到验证码、找不到发文入口或找不到编辑器，agent 会暂停并提示你处理，然后在工作台里的“小黑盒浏览器小窗”中点击、输入或刷新页面。系统不会自动点击最终发布。

如果自动识别不到标题栏或正文栏，小黑盒页面顶部会出现黑色提示条。此时请在工作台的小窗截图里点击对应输入区域，看到蓝色边框后，点击“继续自动导入”。系统会使用页面内记录的字段选择器继续写入，不依赖浏览器焦点。

## 后续真正草稿箱同步需要

如果小黑盒提供官方内容发布/草稿接口，需要补齐这些配置：

```text
HEYBOX_API_BASE_URL=...
HEYBOX_CLIENT_ID=...
HEYBOX_CLIENT_SECRET=...
HEYBOX_ACCESS_TOKEN=...
HEYBOX_DRAFT_ENDPOINT=...
```

然后在 `app/heybox_export.py` 当前的 payload 基础上，新增官方接口客户端即可。
