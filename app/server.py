from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1]))

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

from app.collector import collect_once_report
from app.changelog import load_changelog
from app.config import ROOT_DIR
from app.douyin_auth import (
    DouyinAuthError,
    build_douyin_authorize_url,
    douyin_auth_status,
    exchange_douyin_code,
    refresh_douyin_access_token,
)
from app.env_loader import load_dotenv
from app.database import backfill_from_json, database_summary
from app.draft_store import ensure_draft_identity, save_draft_dicts
from app.heybox_automation import (
    HeyboxAutomationError,
    click_heybox_automation,
    continue_heybox_automation,
    heybox_automation_status,
    heybox_automation_screenshot,
    press_heybox_automation,
    start_heybox_automation,
    stop_heybox_automation,
    type_heybox_automation,
)
from app.network_status import detect_public_ip
from app.opinion_materials import (
    OpinionMaterialError,
    collect_opinions_auto,
    import_opinion_screenshot,
    import_opinion_texts,
    load_opinion_items,
    opinion_config_status,
)
from app.opinion_draft_linker import apply_opinion_screenshot_to_draft
from app.publication import publication_summary
from app.storage import load_items
from app.title_format import title_prefix_context
from app.topics import load_topics
from app.writer import load_drafts
from app.workflow import (
    WorkflowError,
    add_topic_checked,
    apply_draft_title_choice_checked,
    build_draft_heybox_clipboard,
    build_draft_wechat_clipboard,
    capture_draft_slot_official_screenshot,
    clear_draft_slot_image_selection,
    dashboard_context,
    delete_draft,
    delete_draft_cover,
    delete_draft_slot_image_candidate,
    delete_news_image,
    delete_news_item,
    delete_topic_checked,
    draft_variant_previews_checked,
    export_draft_heybox_format,
    export_draft_wechat_format,
    format_exception,
    generate_covers_checked,
    generate_drafts_checked,
    generate_topics_checked,
    import_draft_slot_image,
    import_draft_slot_images,
    mark_draft_as_published,
    regenerate_draft_heybox_copy,
    regenerate_single_draft_checked,
    regenerate_draft_image_candidate,
    regenerate_draft_slot_image_candidate,
    rewrite_draft_title_checked,
    save_draft_edits,
    select_draft_image_candidate,
    select_draft_platform_variant,
    select_draft_slot_image_candidate,
    sync_draft_to_wechat_checked,
    update_draft_slot_image_caption,
    update_topic_checked,
    wechat_status_checked,
)


app = Flask(
    __name__,
    template_folder=str(ROOT_DIR / "templates"),
    static_folder=str(ROOT_DIR / "static"),
)


@app.get("/")
def index():
    return render_template("index.html", **dashboard_context(), generated_at=datetime.now(timezone.utc).isoformat())


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/api/items")
def api_items():
    return jsonify(ok_response({"items": load_items()}))


@app.get("/api/changelog")
def api_changelog():
    return jsonify(ok_response({"changelog": load_changelog()}))


@app.get("/topics")
def topics_page():
    return render_template("topics.html", topics=load_topics())


@app.get("/api/topics")
def api_topics():
    return jsonify(ok_response({"topics": load_topics()}))


@app.post("/api/topics/generate")
def api_generate_topics():
    topics = generate_topics_checked()
    return jsonify(ok_response({"count": len(topics), "topics": [topic.to_dict() for topic in topics]}))


@app.post("/api/topics")
def api_add_topic():
    payload = request.get_json(silent=True) or {}
    topic = add_topic_checked(payload)
    return jsonify(ok_response({"topic": topic}, message="选题已加入，旧草稿已清空，请按确认后的选题重新生成草稿。"))


@app.put("/api/topics/<topic_id>")
def api_update_topic(topic_id: str):
    payload = request.get_json(silent=True) or {}
    topic = update_topic_checked(topic_id, payload)
    return jsonify(ok_response({"topic": topic}, message="选题已保存，旧草稿已清空，请按确认后的选题重新生成草稿。"))


@app.delete("/api/topics/<topic_id>")
def api_delete_topic(topic_id: str):
    delete_topic_checked(topic_id)
    return jsonify(ok_response(message="选题已删除，旧草稿已清空，请按确认后的选题重新生成草稿。"))


@app.get("/drafts")
def drafts_page():
    return render_template("drafts.html", drafts=load_drafts(), title_prefix=title_prefix_context())


@app.get("/api/drafts")
def api_drafts():
    return jsonify(ok_response({"drafts": load_drafts()}))


@app.put("/api/drafts/<int:draft_index>")
def api_update_draft(draft_index: int):
    payload = request.get_json(silent=True) or {}
    result = save_draft_edits(draft_index, payload)
    return jsonify(ok_response(result, message="草稿已保存，下次加载会读取保存后的版本。"))


@app.post("/api/drafts/generate")
def api_generate_drafts():
    payload = request.get_json(silent=True) or {}
    topic_ids = payload.get("topic_ids")
    if topic_ids is not None and not isinstance(topic_ids, list):
        raise WorkflowError("选题列表格式不正确。")
    drafts = generate_drafts_checked(topic_ids=topic_ids)
    return jsonify(ok_response({"count": len(drafts), "drafts": drafts}, message=f"已按 {len(drafts)} 个选题生成文本草稿，配图可在确认文章后单独补齐。"))


@app.post("/api/drafts/<int:draft_index>/regenerate")
def api_regenerate_one_draft(draft_index: int):
    result = regenerate_single_draft_checked(draft_index)
    return jsonify(ok_response(result, message="这篇文章文本已重新生成，当前配图候选和已选图片会尽量保留。"))


@app.post("/api/drafts/<int:draft_index>/rewrite-title")
def api_rewrite_draft_title(draft_index: int):
    result = rewrite_draft_title_checked(draft_index)
    return jsonify(ok_response(result, message="标题和副标题已重写，正文与配图未改动。"))


@app.post("/api/drafts/<int:draft_index>/title-choice")
def api_apply_draft_title_choice(draft_index: int):
    payload = request.get_json(silent=True) or {}
    result = apply_draft_title_choice_checked(draft_index, payload)
    return jsonify(ok_response(result, message="标题已应用，发布版本预览会按新标题刷新。"))


@app.post("/api/drafts/<int:draft_index>/regenerate-heybox")
def api_regenerate_one_heybox_copy(draft_index: int):
    result = regenerate_draft_heybox_copy(draft_index)
    return jsonify(ok_response({"short_copy": result, "heybox_copy": result}, message="这篇草稿的短文版已重新生成，长文版正文未改动。"))


@app.get("/api/drafts/<int:draft_index>/variants")
def api_draft_variants(draft_index: int):
    result = draft_variant_previews_checked(draft_index, public_base_url=request.host_url.rstrip("/"))
    return jsonify(ok_response({"variant_preview": result}))


@app.post("/api/drafts/<int:draft_index>/platform-choice")
def api_select_draft_platform_choice(draft_index: int):
    payload = request.get_json(silent=True) or {}
    platform = str(payload.get("platform") or "article")
    variant = str(payload.get("variant") or "")
    choice = select_draft_platform_variant(draft_index, platform, variant)
    return jsonify(ok_response({"choice": choice}, message=f"已选择{choice.get('label', '当前版本')}作为当前文章输出版本。"))


@app.post("/api/drafts/generate-covers")
def api_generate_draft_covers():
    covers = generate_covers_checked(force=False)
    message = "当前草稿标题配图已补齐。" if not covers else f"已补齐 {len(covers)} 张标题配图。"
    return jsonify(ok_response({"count": len(covers), "covers": covers}, message=message))


@app.post("/api/drafts/<int:draft_index>/images/generate")
def api_generate_one_draft_image(draft_index: int):
    result = regenerate_draft_image_candidate(draft_index)
    return jsonify(ok_response({"cover": result}, message="已为这篇草稿重新生成一张候选图。"))


@app.post("/api/drafts/<int:draft_index>/images/select")
def api_select_draft_image(draft_index: int):
    payload = request.get_json(silent=True) or {}
    image_url = str(payload.get("url") or "").strip()
    record = select_draft_image_candidate(draft_index, image_url)
    return jsonify(ok_response({"cover_image": record}, message="已切换当前配图。"))


@app.post("/api/drafts/<int:draft_index>/image-slots/<slot_id>/generate")
def api_generate_draft_slot_image(draft_index: int, slot_id: str):
    result = regenerate_draft_slot_image_candidate(draft_index, slot_id)
    return jsonify(ok_response({"image": result}, message="已为这个位置重新生成一张候选图。"))


@app.post("/api/drafts/<int:draft_index>/image-slots/<slot_id>/select")
def api_select_draft_slot_image(draft_index: int, slot_id: str):
    payload = request.get_json(silent=True) or {}
    image_url = str(payload.get("url") or "").strip()
    record = select_draft_slot_image_candidate(draft_index, slot_id, image_url)
    return jsonify(ok_response({"selected_image": record}, message="已切换这个位置的配图。"))


@app.put("/api/drafts/<int:draft_index>/image-slots/<slot_id>/caption")
def api_update_draft_slot_image_caption(draft_index: int, slot_id: str):
    payload = request.get_json(silent=True) or {}
    result = update_draft_slot_image_caption(draft_index, slot_id, str(payload.get("caption") or ""))
    return jsonify(ok_response(result, message="图解已保存。"))


@app.delete("/api/drafts/<int:draft_index>/image-slots/<slot_id>/candidate")
def api_delete_draft_slot_image_candidate(draft_index: int, slot_id: str):
    payload = request.get_json(silent=True) or {}
    image_url = str(payload.get("url") or "").strip()
    result = delete_draft_slot_image_candidate(draft_index, slot_id, image_url)
    return jsonify(ok_response(result, message="配图库图片已删除。"))


@app.delete("/api/drafts/<int:draft_index>/image-slots/<slot_id>/selection")
def api_clear_draft_slot_image_selection(draft_index: int, slot_id: str):
    result = clear_draft_slot_image_selection(draft_index, slot_id)
    return jsonify(ok_response(result, message="已清空这个位置的配图，导出和同步时不会在这里插图。"))


@app.post("/api/drafts/<int:draft_index>/image-slots/<slot_id>/import")
def api_import_draft_slot_image(draft_index: int, slot_id: str):
    files = [file for file in request.files.getlist("image") if file and file.filename]
    if not files:
        raise WorkflowError("请选择要导入的图片。")
    if len(files) == 1:
        record = import_draft_slot_image(draft_index, slot_id, files[0].stream, files[0].filename or "upload.png")
        return jsonify(ok_response({"selected_image": record, "images": [record], "count": 1}, message="图片已导入，并已设为这个位置的配图。"))
    uploads = [(file.stream, file.filename or f"upload-{index + 1}.png") for index, file in enumerate(files)]
    result = import_draft_slot_images(draft_index, slot_id, uploads)
    return jsonify(ok_response(result, message=f"已导入 {result['count']} 张图片，并默认使用第一张。"))


@app.post("/api/drafts/<int:draft_index>/image-slots/<slot_id>/official-screenshot")
def api_capture_draft_slot_official_screenshot(draft_index: int, slot_id: str):
    result = capture_draft_slot_official_screenshot(draft_index, slot_id)
    count = len(result.get("candidates") or [])
    return jsonify(ok_response(result, message=f"已从官方页面截取 {count} 张候选图，并默认使用第一张。"))


@app.get("/api/publications")
def api_publications():
    return jsonify(ok_response({"publication": publication_summary()}))


@app.get("/api/database")
def api_database():
    return jsonify(ok_response({"database": database_summary()}))


@app.get("/api/server/status")
def api_server_status():
    host_header = request.host or ""
    hostname = request.host.split(":", 1)[0] if request.host else request.host_url.split("//", 1)[-1].strip("/")
    port = request.environ.get("SERVER_PORT") or ""
    if ":" in host_header.rsplit("]", 1)[-1]:
        port = host_header.rsplit(":", 1)[-1]
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    forwarded_host = request.headers.get("X-Forwarded-Host", "")
    public_host = forwarded_host or host_header
    base_url = f"{scheme}://{public_host}".rstrip("/")
    callback_url = f"{base_url}/api/douyin/auth/callback"
    public_ip = detect_public_ip()
    return jsonify(
        ok_response(
            {
                "server": {
                    "host": hostname,
                    "port": str(port),
                    "host_header": host_header,
                    "public_ip": public_ip.get("ip", ""),
                    "public_ip_source": public_ip.get("source", ""),
                    "public_ip_errors": public_ip.get("errors", []),
                    "scheme": scheme,
                    "base_url": base_url,
                    "douyin_callback_url": callback_url,
                    "note": "公众号 IP 白名单通常填写公网 IP；端口用于本地回调、云服务器安全组或反向代理配置。",
                }
            },
            message=f"当前访问端口是 {port}，当前公网 IP 是 {public_ip.get('ip') or '检测失败'}。",
        )
    )


@app.get("/api/opinions")
def api_opinions():
    return jsonify(ok_response({"opinions": load_opinion_items()}))


@app.get("/api/opinions/config")
def api_opinion_config():
    platform = str(request.args.get("platform") or "douyin").strip()
    try:
        status = opinion_config_status(platform)
    except OpinionMaterialError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    message = status.get("hint") or "舆论素材配置检测完成。"
    return jsonify(ok_response({"opinion_config": status}, message=message))


@app.get("/api/douyin/auth/status")
def api_douyin_auth_status():
    return jsonify(ok_response({"douyin_auth": douyin_auth_status()}, message="抖音授权状态检测完成。"))


@app.get("/api/douyin/auth/url")
def api_douyin_auth_url():
    try:
        result = build_douyin_authorize_url(state=str(request.args.get("state") or ""))
    except DouyinAuthError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    return jsonify(ok_response({"douyin_auth": result}, message="已生成抖音授权链接，请在新窗口完成授权。"))


@app.post("/api/douyin/auth/exchange")
def api_douyin_auth_exchange():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code") or "").strip()
    try:
        result = exchange_douyin_code(code)
    except DouyinAuthError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    return jsonify(ok_response({"douyin_auth": result}, message="抖音授权已保存，后续采集会自动使用本地 token。"))


@app.post("/api/douyin/auth/refresh")
def api_douyin_auth_refresh():
    try:
        result = refresh_douyin_access_token()
    except DouyinAuthError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    return jsonify(ok_response({"douyin_auth": result}, message="抖音 access_token 已刷新。"))


@app.get("/api/douyin/auth/callback")
def api_douyin_auth_callback():
    code = str(request.args.get("code") or "").strip()
    if not code:
        raise WorkflowError("抖音授权回调缺少 code。", status_code=400)
    try:
        exchange_douyin_code(code)
    except DouyinAuthError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    return """
    <!doctype html>
    <meta charset="utf-8">
    <title>抖音授权完成</title>
    <body style="font-family: sans-serif; padding: 32px;">
      <h2>抖音授权已保存</h2>
      <p>可以关闭这个页面，回到本地工作台点击“检测配置”或“采集评论”。</p>
    </body>
    """


@app.post("/api/opinions/collect")
def api_collect_opinions():
    payload = request.get_json(silent=True) or {}
    platform = str(payload.get("platform") or "douyin").strip()
    query = str(payload.get("query") or "").strip()
    limit = int(payload.get("limit") or 10)
    if not query:
        raise WorkflowError("缺少要采集评论的话题关键词。")
    try:
        result = collect_opinions_auto(platform, query, limit=limit)
    except OpinionMaterialError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    return jsonify(ok_response(result, message=f"已采集 {result.get('count', 0)} 条舆论评论素材。"))


@app.post("/api/opinions/import-text")
def api_import_opinion_text():
    payload = request.get_json(silent=True) or {}
    platform = str(payload.get("platform") or "manual").strip()
    topic = str(payload.get("topic") or "").strip()
    raw_text = str(payload.get("text") or "")
    texts = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not topic:
        raise WorkflowError("缺少评论对应的话题。")
    try:
        result = import_opinion_texts(platform, topic, texts, source_url=str(payload.get("source_url") or ""))
    except OpinionMaterialError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    return jsonify(ok_response(result, message=f"已导入 {result.get('count', 0)} 条评论，并生成匿名评论卡片。"))


@app.post("/api/opinions/import-screenshot")
def api_import_opinion_screenshot():
    file = request.files.get("image")
    if not file:
        raise WorkflowError("请选择要导入的评论截图。")
    platform = str(request.form.get("platform") or "manual").strip()
    topic = str(request.form.get("topic") or "").strip()
    note = str(request.form.get("note") or "").strip()
    draft_ref = resolve_opinion_draft_ref(request.form)
    if not draft_ref:
        raise WorkflowError("请先选择这张评论截图对应的草稿。")
    if not topic:
        topic = str(draft_ref.get("draft_title") or "").strip()
    if not topic:
        raise WorkflowError("缺少评论截图对应的话题。")
    try:
        result = import_opinion_screenshot(platform, topic, file.stream, file.filename or "comment.png", note=note, draft_ref=draft_ref)
        draft_update = apply_opinion_screenshot_to_draft(draft_ref, result["item"])
        result["draft_update"] = draft_update
    except OpinionMaterialError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    except (IndexError, ValueError) as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    return jsonify(ok_response(result, message="评论截图已导入，并已生成舆论点评段落和对应插图。"))


def resolve_opinion_draft_ref(form: Any) -> dict[str, Any]:
    raw_index = str(form.get("draft_index") or "").strip()
    if raw_index == "":
        return {}
    try:
        draft_index = int(raw_index)
    except ValueError as exc:
        raise WorkflowError("评论截图关联的草稿序号无效。", status_code=400) from exc

    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这张截图关联的草稿，请刷新页面后重试。", status_code=404)
    draft = drafts[draft_index]
    had_draft_id = bool(str(draft.get("draft_id") or "").strip())
    draft_id = ensure_draft_identity(draft, draft_index)
    if not had_draft_id:
        save_draft_dicts(drafts)
    return {
        "draft_index": draft_index,
        "draft_id": draft_id,
        "topic_id": str(draft.get("topic_id") or form.get("topic_id") or "").strip(),
        "draft_title": str(draft.get("title") or form.get("draft_title") or "").strip(),
    }


@app.post("/api/database/backfill")
def api_database_backfill():
    result = backfill_from_json()
    summary = database_summary()
    return jsonify(ok_response({"backfill": result, "database": summary}, message="已从现有 JSON 文件同步到 SQLite。"))


@app.post("/api/drafts/<int:draft_index>/publish-record")
def api_mark_draft_published(draft_index: int):
    payload = request.get_json(silent=True) or {}
    channel = str(payload.get("channel") or "manual").strip()
    note = str(payload.get("note") or "").strip()
    record = mark_draft_as_published(draft_index, channel=channel, note=note)
    return jsonify(ok_response({"publication_record": record}, message="已备案为已发布，后续会阻止重复发布。"))


@app.post("/api/drafts/<int:draft_index>/export-wechat")
def api_export_draft_wechat(draft_index: int):
    export = export_draft_wechat_format(draft_index, public_base_url=request.host_url.rstrip("/"))
    image_count = len(export.get("image_downloads") or [])
    return jsonify(ok_response({"export": export}, message=f"已导出公众号 HTML、接口 JSON，并生成 {image_count} 张 PNG 下载图。"))


@app.post("/api/drafts/<int:draft_index>/copy-wechat")
def api_copy_draft_wechat(draft_index: int):
    clipboard = build_draft_wechat_clipboard(draft_index, public_base_url=request.host_url.rstrip("/"))
    return jsonify(ok_response({"clipboard": clipboard}, message="已生成公众号富文本，可直接复制到公众号后台。"))


@app.post("/api/drafts/<int:draft_index>/export-heybox")
def api_export_draft_heybox(draft_index: int):
    export = export_draft_heybox_format(draft_index, public_base_url=request.host_url.rstrip("/"))
    image_count = len(export.get("image_downloads") or [])
    return jsonify(
        ok_response(
            {"export": export},
            message=f"已导出小黑盒 HTML、Markdown 和 {image_count} 张 PNG 下载图。小黑盒暂未接入公开草稿箱 API，请复制后粘贴到创作者后台。",
        )
    )


@app.post("/api/drafts/<int:draft_index>/copy-heybox")
def api_copy_draft_heybox(draft_index: int):
    clipboard = build_draft_heybox_clipboard(draft_index, public_base_url=request.host_url.rstrip("/"))
    return jsonify(
        ok_response(
            {"clipboard": clipboard},
            message="已生成小黑盒富文本，可复制到小黑盒创作者后台；当前不使用模拟登录或未公开接口自动写入。",
        )
    )


@app.post("/api/drafts/<int:draft_index>/automate-heybox")
def api_automate_draft_heybox(draft_index: int):
    try:
        session = start_heybox_automation(draft_index, public_base_url=request.host_url.rstrip("/"))
    except HeyboxAutomationError as exc:
        raise WorkflowError(str(exc), status_code=409) from exc
    return jsonify(ok_response({"heybox_automation": session}, message="已启动小黑盒半自动导入。浏览器会打开，请按提示登录或审核。"))


@app.get("/api/heybox/automation/status")
def api_heybox_automation_status():
    return jsonify(ok_response({"heybox_automation": heybox_automation_status()}))


@app.post("/api/heybox/automation/continue")
def api_heybox_automation_continue():
    try:
        session = continue_heybox_automation()
    except HeyboxAutomationError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    return jsonify(ok_response({"heybox_automation": session}, message="已继续小黑盒半自动导入。"))


@app.post("/api/heybox/automation/stop")
def api_heybox_automation_stop():
    session = stop_heybox_automation()
    return jsonify(ok_response({"heybox_automation": session}, message="已请求关闭小黑盒自动化浏览器。"))


@app.get("/api/heybox/automation/screenshot")
def api_heybox_automation_screenshot():
    try:
        result = heybox_automation_screenshot()
    except HeyboxAutomationError as exc:
        raise WorkflowError(str(exc), status_code=409) from exc
    return jsonify(ok_response(result))


@app.post("/api/heybox/automation/click")
def api_heybox_automation_click():
    payload = request.get_json(silent=True) or {}
    try:
        session = click_heybox_automation(float(payload.get("x") or 0), float(payload.get("y") or 0))
    except HeyboxAutomationError as exc:
        raise WorkflowError(str(exc), status_code=409) from exc
    return jsonify(ok_response({"heybox_automation": session}, message="已发送小窗点击。"))


@app.post("/api/heybox/automation/type")
def api_heybox_automation_type():
    payload = request.get_json(silent=True) or {}
    try:
        session = type_heybox_automation(str(payload.get("text") or ""))
    except HeyboxAutomationError as exc:
        raise WorkflowError(str(exc), status_code=409) from exc
    return jsonify(ok_response({"heybox_automation": session}, message="已发送小窗输入。"))


@app.post("/api/heybox/automation/press")
def api_heybox_automation_press():
    payload = request.get_json(silent=True) or {}
    try:
        session = press_heybox_automation(str(payload.get("key") or "Enter"))
    except HeyboxAutomationError as exc:
        raise WorkflowError(str(exc), status_code=409) from exc
    return jsonify(ok_response({"heybox_automation": session}, message="已发送小窗按键。"))


@app.get("/api/wechat/status")
def api_wechat_status():
    check = str(request.args.get("check") or "").lower() in {"1", "true", "yes", "on"}
    return jsonify(ok_response({"wechat": wechat_status_checked(check_token=check)}))


@app.post("/api/drafts/<int:draft_index>/sync-wechat")
def api_sync_draft_wechat(draft_index: int):
    result = sync_draft_to_wechat_checked(draft_index, public_base_url=request.host_url.rstrip("/"))
    return jsonify(ok_response({"wechat_draft": result}, message="已同步到公众号草稿箱，请到公众号后台审核后再发布。"))


@app.post("/api/collect")
def api_collect():
    payload = request.get_json(silent=True) or {}
    report = collect_once_report(collection_options=payload or None)
    items = report["items"]
    warnings = report["errors"]
    message = f"采集完成，共 {len(items)} 条。"
    if warnings:
        message += f" 其中 {len(warnings)} 个来源失败，可在错误详情中查看。"
    return jsonify(ok_response({"count": len(items), "items": [item.to_dict() for item in items], "warnings": warnings, "collection_profile": report.get("collection_profile", {})}, message))


@app.delete("/api/items/<item_id>")
def api_delete_item(item_id: str):
    delete_news_item(item_id)
    return jsonify(ok_response(message="新闻已删除，选题已同步更新；旧草稿已清空，请重新生成。"))


@app.delete("/api/items/<item_id>/images")
def api_delete_item_image(item_id: str):
    payload = request.get_json(silent=True) or {}
    image = str(payload.get("image") or "").strip()
    if not image:
        raise WorkflowError("缺少要删除的图片地址。")
    delete_news_image(item_id, image)
    return jsonify(ok_response(message="图片已从这条新闻中移除，选题已同步更新；旧草稿已清空，请重新生成。"))


@app.delete("/api/drafts/<int:draft_index>")
def api_delete_draft(draft_index: int):
    delete_draft(draft_index)
    return jsonify(ok_response(message="草稿已删除。"))


@app.delete("/api/drafts/<int:draft_index>/cover")
def api_delete_draft_cover(draft_index: int):
    delete_draft_cover(draft_index)
    return jsonify(ok_response(message="封面图已删除。"))


@app.get("/static-data/images/<path:file_name>")
def data_images(file_name: str):
    return send_from_directory(ROOT_DIR / "data" / "images", file_name)


@app.get("/static-data/generated-images/<path:file_name>")
def generated_images(file_name: str):
    return send_from_directory(ROOT_DIR / "data" / "generated_images", file_name)


@app.get("/static-data/imported-images/<path:file_name>")
def imported_images(file_name: str):
    return send_from_directory(ROOT_DIR / "data" / "imported_images", file_name)


@app.get("/static-data/official-screenshots/<path:file_name>")
def official_screenshots(file_name: str):
    return send_from_directory(ROOT_DIR / "data" / "official_screenshots", file_name)


@app.get("/static-data/automation/<path:file_name>")
def automation_static(file_name: str):
    return send_from_directory(ROOT_DIR / "data" / "automation", file_name)


@app.get("/static-data/opinion-cards/<path:file_name>")
def opinion_cards(file_name: str):
    return send_from_directory(ROOT_DIR / "data" / "opinion_cards", file_name)


@app.get("/static-data/opinion-imports/<path:file_name>")
def opinion_imports(file_name: str):
    return send_from_directory(ROOT_DIR / "data" / "opinion_imports", file_name)


@app.get("/exports/<path:file_name>")
def exported_file(file_name: str):
    return send_from_directory(ROOT_DIR / "data" / "exports", file_name)


@app.errorhandler(WorkflowError)
def handle_workflow_error(exc: WorkflowError):
    return jsonify(error_response(exc.message)), exc.status_code


@app.errorhandler(HTTPException)
def handle_http_error(exc: HTTPException):
    if request.path.startswith("/api/"):
        return jsonify(error_response(exc.description or exc.name)), exc.code or 500
    return exc


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    if request.path.startswith("/api/"):
        return jsonify(error_response(format_exception(exc))), 500
    raise exc


def ok_response(data: dict | None = None, message: str = "OK") -> dict:
    payload = {"ok": True, "message": message}
    if data:
        payload.update(data)
    return payload


def error_response(message: str) -> dict:
    return {"ok": False, "message": message}


def main() -> None:
    load_dotenv()
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "5050"))
    debug = os.getenv("APP_DEBUG", "true").lower() in {"1", "true", "yes", "on"}
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
