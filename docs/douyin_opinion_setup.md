# 抖音评论采集配置

本项目只接入授权 API，不做绕登录、绕签名或绕风控的页面爬取。

## 推荐流程：本地 OAuth 授权

先在 `.env` 填应用配置：

```env
DOUYIN_CLIENT_KEY=你的client_key
DOUYIN_CLIENT_SECRET=你的client_secret
DOUYIN_REDIRECT_URI=http://127.0.0.1:5050/api/douyin/auth/callback
DOUYIN_OAUTH_SCOPE=user_info,item.comment
```

注意：`DOUYIN_REDIRECT_URI` 必须和抖音开放平台应用后台登记的回调地址完全一致。
如果不确定当前本地服务端口，可以在工作台「发布记录」里点击「检测端口」，复制显示的抖音回调地址。

然后重启本地服务，在工作台「舆论素材」里：

1. 点击「授权状态」，确认 client 配置已读取。
2. 点击「生成授权链接」，在新窗口完成抖音授权。
3. 如果回调地址可访问，系统会自动保存授权；如果没有自动保存，把回调 URL 里的 `code` 粘贴到输入框，再点「保存授权Code」。
4. 点击「检测配置」，看到 `official_video` 配置完整后即可采集。

授权 token 会保存到 `data/secrets/douyin_oauth.json`，系统后续会自动读取，不需要把 `access_token` 再复制到 `.env`。

完成 OAuth 授权后，指定视频评论采集不要求你再手动填写 `DOUYIN_OPINION_ENABLED`。如果你更喜欢纯 `.env` 手动配置，也可以按下面的模式一填写。

## 模式一：官方视频评论

适合采集某条公开视频下面的评论。需要在抖音开放平台申请互动管理/评论相关权限，并拿到授权用户的 `open_id` 和 `access_token`。

如果你不使用上面的 OAuth 辅助，也可以直接在 `.env` 手动填 token：

```env
DOUYIN_OPINION_ENABLED=true
DOUYIN_OPINION_MODE=official_video
DOUYIN_OPEN_ACCESS_TOKEN=你的用户授权access_token
DOUYIN_OPEN_ID=授权用户open_id
DOUYIN_COMMENT_LIST_URL=https://open.douyin.com/item/comment/list/
```

在工作台「舆论素材」里选择抖音，输入抖音视频 `item_id` 或包含 `/video/数字ID` 的链接，再点击「采集评论」。

如果你的开放平台文档显示为新版视频评论路径，可以把地址改成：

```env
DOUYIN_COMMENT_LIST_URL=https://open.douyin.com/video/comment/list/
```

## 模式二：关键词评论

适合先按关键词搜索视频，再采集这些视频的评论。这个能力通常需要搜索权限和 `client_token`。

`.env` 示例：

```env
DOUYIN_OPINION_ENABLED=true
DOUYIN_OPINION_MODE=official_keyword
DOUYIN_OPEN_ACCESS_TOKEN=你的用户授权access_token
DOUYIN_OPEN_ID=授权用户open_id
DOUYIN_CLIENT_TOKEN=你的client_token
DOUYIN_KEYWORD_VIDEO_SEARCH_URL=https://open.douyin.com/video/search/
DOUYIN_KEYWORD_COMMENT_LIST_URL=https://open.douyin.com/video/search/comment/list/
```

如果关键词视频搜索接口要求单独 token，可额外配置：

```env
DOUYIN_KEYWORD_SEARCH_TOKEN=关键词搜索接口token
```

## 模式三：中转接口

如果你已有自己的授权采集服务，可以继续使用中转模式。服务需要接收 `query` 和 `limit` 参数，并返回 `comments`、`data`、`items` 或 `list` 数组。

```env
DOUYIN_OPINION_ENABLED=true
DOUYIN_OPINION_MODE=proxy
DOUYIN_OPINION_ENDPOINT=https://你的服务/comments
DOUYIN_OPINION_ACCESS_TOKEN=你的服务token
```

返回字段建议：

```json
{
  "comments": [
    {
      "text": "评论内容",
      "published_at": "2026-06-20T10:00:00+08:00",
      "like_count": 12,
      "source_url": "https://www.douyin.com/video/..."
    }
  ]
}
```

系统会自动匿名化评论文本，只保存评论内容、时间、点赞数和来源链接，不保存昵称、头像、用户 ID。
