from __future__ import annotations

import json
import re
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.draft_store import ensure_draft_identity
from app.formatting import (
    HEADING_RE,
    IMAGE_MARKER_RE,
    clean_body_markdown,
    disabled_image_slots,
    is_source_note,
    render_inline_rich_text,
    strip_inline_rich_markers,
)
from app.heybox_export import HEYBOX_CREATOR_URL, build_heybox_clipboard_payload, export_draft_for_heybox
from app.heybox_writer import load_or_create_heybox_draft
from app.platform_variants import selected_article_variant
from app.title_format import format_title_with_prefix, strip_title_prefix, title_prefix_bracketed
from app.writer import load_drafts


PROFILE_DIR = ROOT_DIR / "data" / "browser_profiles" / "heybox"
AUTOMATION_VIEW_DIR = ROOT_DIR / "data" / "automation"
AUTOMATION_VIEW_URL_PREFIX = "/static-data/automation"
TERMINAL_STATUSES = {"completed", "error", "stopped"}
COMMANDABLE_STATUSES = {"running", "waiting_user", "waiting_review", "stopping"}
HEYBOX_IMAGE_TEXT_EDITOR_URL = "https://www.xiaoheihe.cn/creator/editor/draft/image_text"
HEYBOX_ARTICLE_EDITOR_URL = "https://www.xiaoheihe.cn/creator/editor/draft/article"
HEYBOX_TITLE_LIMIT = 30
BROWSER_CLOSED_MESSAGE = "检测到小黑盒浏览器已关闭，任务已结束，可以重新开始导入。"
HEYBOX_BROWSER_WIDTH = 1280
HEYBOX_BROWSER_HEIGHT = 760
HEYBOX_BROWSER_X = 40
HEYBOX_BROWSER_Y = 20
HEYBOX_BROWSER_ARGS = [
    f"--window-size={HEYBOX_BROWSER_WIDTH},{HEYBOX_BROWSER_HEIGHT}",
    f"--window-position={HEYBOX_BROWSER_X},{HEYBOX_BROWSER_Y}",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
]


class HeyboxAutomationError(RuntimeError):
    pass


class AutomationStopped(RuntimeError):
    pass


@dataclass
class HeyboxAutomationSession:
    id: str
    draft_index: int
    status: str = "starting"
    step: str = "init"
    message: str = "正在启动小黑盒半自动导入。"
    logs: list[str] = field(default_factory=list)
    export: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    continue_event: threading.Event = field(default_factory=threading.Event, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    command_queue: queue.Queue = field(default_factory=queue.Queue, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    page: Any | None = field(default=None, repr=False)
    context: Any | None = field(default=None, repr=False)
    last_screenshot_url: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, status: str | None = None, step: str | None = None, message: str | None = None, log: str | None = None) -> None:
        with self.lock:
            if status:
                self.status = status
            if step:
                self.step = step
            if message:
                self.message = message
            if log:
                self.logs.insert(0, f"{datetime.now().strftime('%H:%M:%S')} {log}")
                self.logs = self.logs[:30]
            self.updated_at = datetime.now(timezone.utc).isoformat()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "draft_index": self.draft_index,
                "status": self.status,
                "step": self.step,
                "message": self.message,
                "logs": list(self.logs),
                "export": self.export,
                "browser_view_available": self.page is not None and self.status in COMMANDABLE_STATUSES,
                "last_screenshot_url": self.last_screenshot_url,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }


_session_lock = threading.Lock()
_active_session: HeyboxAutomationSession | None = None


def start_heybox_automation(draft_index: int, public_base_url: str) -> dict[str, Any]:
    global _active_session
    with _session_lock:
        if _active_session:
            mark_session_stopped_if_thread_finished(_active_session)
            mark_session_browser_closed(_active_session)
        if _active_session and _active_session.status not in TERMINAL_STATUSES:
            force_stop_session_for_restart(_active_session)
        session = HeyboxAutomationSession(id=uuid.uuid4().hex[:12], draft_index=draft_index)
        thread = threading.Thread(target=run_automation_session, args=(session, public_base_url), daemon=True)
        session.thread = thread
        _active_session = session
        thread.start()
        return session.snapshot()


def heybox_automation_status() -> dict[str, Any]:
    with _session_lock:
        if not _active_session:
            return {"status": "idle", "message": "当前没有小黑盒半自动导入任务。"}
        mark_session_stopped_if_thread_finished(_active_session)
        mark_session_browser_closed(_active_session)
        return _active_session.snapshot()


def continue_heybox_automation() -> dict[str, Any]:
    with _session_lock:
        if not _active_session:
            raise HeyboxAutomationError("当前没有小黑盒半自动导入任务。")
        mark_session_stopped_if_thread_finished(_active_session)
        mark_session_browser_closed(_active_session)
        if _active_session.status != "waiting_user":
            return _active_session.snapshot()
        _active_session.update(status="running", message="已收到继续指令，正在继续自动导入。", log="用户点击继续。")
        _active_session.continue_event.set()
        return _active_session.snapshot()


def stop_heybox_automation() -> dict[str, Any]:
    with _session_lock:
        if not _active_session:
            return {"status": "idle", "message": "当前没有小黑盒半自动导入任务。"}
        mark_session_stopped_if_thread_finished(_active_session)
        mark_session_browser_closed(_active_session)
        if _active_session.status in TERMINAL_STATUSES:
            return _active_session.snapshot()
        _active_session.update(status="stopping", message="正在关闭小黑盒自动化浏览器。", log="用户请求关闭浏览器。")
        _active_session.stop_event.set()
        _active_session.continue_event.set()
        return _active_session.snapshot()


def force_stop_session_for_restart(session: HeyboxAutomationSession, wait_timeout: float = 3) -> None:
    message = "检测到重新发起小黑盒导入，已强制关闭上一个半自动导入任务。"
    session.update(status="stopping", step="restart", message=message, log=message)
    session.stop_event.set()
    session.continue_event.set()
    fail_pending_browser_commands(session, message)
    close_session_browser(session)
    thread = session.thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=wait_timeout)
    if session.status not in TERMINAL_STATUSES:
        session.update(status="stopped", step="restarted", message=message, log="旧任务已释放，可以启动新的导入。")


def close_session_browser(session: HeyboxAutomationSession) -> None:
    page = session.page
    context = session.context or context_from_page(page)
    if context is not None:
        try:
            context.close()
        except Exception as exc:  # noqa: BLE001
            session.update(log=f"强制关闭旧浏览器上下文失败：{exc}")
    elif page is not None:
        try:
            if not browser_page_is_closed(page):
                page.close()
        except Exception as exc:  # noqa: BLE001
            session.update(log=f"强制关闭旧浏览器页面失败：{exc}")
    session.page = None
    session.context = None


def context_from_page(page: Any | None) -> Any | None:
    if page is None:
        return None
    try:
        return page.context
    except Exception:  # noqa: BLE001
        return None


def reset_heybox_profile_window_state() -> None:
    preferences_path = PROFILE_DIR / "Default" / "Preferences"
    if not preferences_path.exists():
        return
    try:
        preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    browser = preferences.setdefault("browser", {})
    if not isinstance(browser, dict):
        return
    placement = browser.get("window_placement")
    if not isinstance(placement, dict):
        placement = {}
        browser["window_placement"] = placement
    placement.update(
        {
            "left": HEYBOX_BROWSER_X,
            "top": HEYBOX_BROWSER_Y,
            "right": HEYBOX_BROWSER_X + HEYBOX_BROWSER_WIDTH,
            "bottom": HEYBOX_BROWSER_Y + HEYBOX_BROWSER_HEIGHT,
            "maximized": False,
        }
    )
    profile = preferences.get("profile")
    if isinstance(profile, dict):
        profile["exit_type"] = "Normal"
    try:
        preferences_path.write_text(json.dumps(preferences, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except OSError:
        return


def normalize_heybox_window(context: Any, page: Any) -> None:
    try:
        cdp = context.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")
        window_id = window.get("windowId")
        if window_id is None:
            return
        cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
        cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "left": HEYBOX_BROWSER_X,
                    "top": HEYBOX_BROWSER_Y,
                    "width": HEYBOX_BROWSER_WIDTH,
                    "height": HEYBOX_BROWSER_HEIGHT,
                },
            },
        )
    except Exception:  # noqa: BLE001
        return


def heybox_automation_screenshot() -> dict[str, Any]:
    session = active_commandable_session()
    result = submit_browser_command(session, {"type": "screenshot"}, timeout=12)
    return {"screenshot_url": result.get("screenshot_url", ""), "session": session.snapshot()}


def click_heybox_automation(x: float, y: float) -> dict[str, Any]:
    session = active_commandable_session()
    submit_browser_command(session, {"type": "click", "x": float(x), "y": float(y)}, timeout=8)
    session.update(log=f"小窗点击：({int(x)}, {int(y)})")
    return session.snapshot()


def type_heybox_automation(text: str) -> dict[str, Any]:
    session = active_commandable_session()
    submit_browser_command(session, {"type": "type", "text": text}, timeout=8)
    session.update(log=f"小窗输入文本：{len(text)} 字。")
    return session.snapshot()


def press_heybox_automation(key: str) -> dict[str, Any]:
    session = active_commandable_session()
    submit_browser_command(session, {"type": "press", "key": key}, timeout=8)
    session.update(log=f"小窗按键：{key}")
    return session.snapshot()


def active_commandable_session() -> HeyboxAutomationSession:
    with _session_lock:
        if not _active_session:
            raise HeyboxAutomationError("当前没有小黑盒半自动导入任务。")
        mark_session_stopped_if_thread_finished(_active_session)
        mark_session_browser_closed(_active_session)
        if _active_session.status not in COMMANDABLE_STATUSES:
            if _active_session.message == BROWSER_CLOSED_MESSAGE:
                raise HeyboxAutomationError(BROWSER_CLOSED_MESSAGE)
            raise HeyboxAutomationError("当前小黑盒半自动导入任务不接受小窗操作。")
        return _active_session


def mark_session_stopped_if_thread_finished(session: HeyboxAutomationSession) -> bool:
    thread = session.thread
    if session.status in TERMINAL_STATUSES or thread is None or thread.is_alive():
        return False
    finish_session_after_browser_closed(session, log="自动化线程已结束，释放半自动导入任务。")
    return True


def browser_page_is_closed(page: Any | None) -> bool:
    if page is None:
        return True
    try:
        return bool(page.is_closed())
    except Exception:  # noqa: BLE001
        return True


def mark_session_browser_closed(session: HeyboxAutomationSession) -> bool:
    if session.status in TERMINAL_STATUSES:
        return False
    page = session.page
    if page is None or not browser_page_is_closed(page):
        return False
    finish_session_after_browser_closed(session, log="检测到浏览器窗口已关闭，释放半自动导入任务。")
    return True


def finish_session_after_browser_closed(session: HeyboxAutomationSession, log: str) -> None:
    session.page = None
    session.stop_event.set()
    session.continue_event.set()
    fail_pending_browser_commands(session, BROWSER_CLOSED_MESSAGE)
    session.update(status="stopped", step="closed", message=BROWSER_CLOSED_MESSAGE, log=log)


def fail_pending_browser_commands(session: HeyboxAutomationSession, error: str) -> None:
    while True:
        try:
            command = session.command_queue.get_nowait()
        except queue.Empty:
            break
        set_command_result(command, {"error": error})


def submit_browser_command(session: HeyboxAutomationSession, command: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    done = threading.Event()
    result: dict[str, Any] = {}
    command["done"] = done
    command["result"] = result
    session.command_queue.put(command)
    if not done.wait(timeout):
        raise HeyboxAutomationError("小黑盒浏览器同步画面暂时没有响应，请稍后刷新或关闭后重试。")
    if result.get("error"):
        raise HeyboxAutomationError(str(result["error"]))
    return result


def run_automation_session(session: HeyboxAutomationSession, public_base_url: str) -> None:
    context = None
    try:
        session.update(status="running", step="prepare", message="正在准备小黑盒导入内容和图片。", log="开始准备导入素材。")
        export = export_draft_for_heybox(session.draft_index, public_base_url=public_base_url)
        clipboard = build_heybox_clipboard_payload(session.draft_index, public_base_url=public_base_url)
        title = str(clipboard.get("display_title") or clipboard.get("title") or export.get("title") or "").strip()
        heybox_title = compact_heybox_title(title)
        article = build_heybox_article_import(session.draft_index)
        image_paths = exported_image_paths(export)
        session.export = {
            "title": title,
            "heybox_title": heybox_title,
            "image_count": len(image_paths),
            "body_image_count": len(article["image_paths"]),
            "html_url": export.get("html_url", ""),
            "markdown_url": export.get("markdown_url", ""),
            "image_downloads": export.get("image_downloads", []),
            "creator_url": HEYBOX_CREATOR_URL,
            "editor_url": HEYBOX_ARTICLE_EDITOR_URL,
        }

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise HeyboxAutomationError("缺少 Playwright。请执行：pip install -r requirements.txt，然后执行：python -m playwright install chromium。") from exc

        session.update(step="browser", message="正在打开小黑盒创作者后台。", log="启动可见 Chromium 浏览器。")
        with sync_playwright() as playwright:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            reset_heybox_profile_window_state()
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    headless=False,
                    no_viewport=True,
                    locale="zh-CN",
                    args=HEYBOX_BROWSER_ARGS,
                )
                session.context = context
                context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://www.xiaoheihe.cn")
            except Exception as exc:  # noqa: BLE001
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    raise HeyboxAutomationError("Playwright 已安装，但 Chromium 浏览器内核还没安装。请执行：python -m playwright install chromium。") from exc
                raise

            page = context.pages[0] if context.pages else context.new_page()
            session.page = page
            normalize_heybox_window(context, page)
            page.goto(HEYBOX_CREATOR_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            capture_session_screenshot(session)
            drain_browser_commands(session)
            check_stopped(session)

            if likely_login_required(page):
                wait_for_user(
                    session,
                    "检测到小黑盒可能未登录。请在打开的浏览器或下方同步画面里完成登录，确认进入创作者后台后点击“继续自动导入”。",
                    step="login",
                )

            ensure_editor_page(page, session)
            fill_title(page, session, heybox_title)
            fill_body(page, session, article["html"], article["plain_text"])
            upload_message = upload_article_images(page, article["image_paths"])
            reveal_heybox_review_actions(page)

            message = "已尝试填入标题、富文本正文和正文插图，并把页面滚动到保存/发布区域附近。请人工审核排版、图片和封面；确认无误后你自己保存草稿或发布。"
            if upload_message:
                message += f" {upload_message}"
            session.update(status="waiting_review", step="review", message=message, log="自动导入步骤已结束，等待人工审核。")
            capture_session_screenshot(session)

            while not session.stop_event.wait(1):
                if mark_session_browser_closed(session):
                    break
                drain_browser_commands(session)
            if session.status not in TERMINAL_STATUSES:
                session.update(status="stopped", step="closed", message="小黑盒自动化浏览器已关闭。", log="浏览器已关闭。")
                context.close()
                context = None
    except AutomationStopped:
        if session.status not in TERMINAL_STATUSES:
            session.update(status="stopped", step="stopped", message="小黑盒半自动导入已停止。", log="任务已停止。")
    except HeyboxAutomationError as exc:
        if session.status not in TERMINAL_STATUSES:
            session.update(status="error", step="error", message=str(exc), log=f"任务失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        if session.status not in TERMINAL_STATUSES:
            session.update(status="error", step="error", message=f"小黑盒半自动导入失败：{exc}", log=f"任务失败：{exc}")
    finally:
        session.page = None
        session.context = None
        if context is not None and session.status != "waiting_review":
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


def wait_for_user(session: HeyboxAutomationSession, message: str, step: str) -> None:
    session.continue_event.clear()
    session.update(status="waiting_user", step=step, message=message, log=f"暂停等待人工处理：{step}")
    while not session.continue_event.wait(0.5):
        if mark_session_browser_closed(session):
            raise AutomationStopped()
        drain_browser_commands(session)
        check_stopped(session)
    session.continue_event.clear()
    check_stopped(session)
    session.update(status="running", message="继续执行小黑盒半自动导入。")


def check_stopped(session: HeyboxAutomationSession) -> None:
    if session.stop_event.is_set():
        raise AutomationStopped()


def drain_browser_commands(session: HeyboxAutomationSession) -> None:
    page = session.page
    if page is None:
        return
    if browser_page_is_closed(page):
        mark_session_browser_closed(session)
        return
    while True:
        try:
            command = session.command_queue.get_nowait()
        except queue.Empty:
            break
        try:
            command_type = command.get("type")
            if command_type == "click":
                page.mouse.click(float(command.get("x") or 0), float(command.get("y") or 0))
                page.wait_for_timeout(250)
                set_command_result(command, {"ok": True})
            elif command_type == "type":
                page.keyboard.insert_text(str(command.get("text") or ""))
                page.wait_for_timeout(250)
                set_command_result(command, {"ok": True})
            elif command_type == "press":
                page.keyboard.press(str(command.get("key") or "Enter"))
                page.wait_for_timeout(250)
                set_command_result(command, {"ok": True})
            elif command_type == "screenshot":
                path = screenshot_path_for_session(session.id)
                path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(path), full_page=False)
                version = int(time.time() * 1000)
                session.last_screenshot_url = f"{AUTOMATION_VIEW_URL_PREFIX}/{path.name}?v={version}"
                set_command_result(command, {"ok": True, "screenshot_url": session.last_screenshot_url})
        except Exception as exc:  # noqa: BLE001
            session.update(log=f"浏览器同步操作失败：{exc}")
            set_command_result(command, {"error": str(exc)})


def set_command_result(command: dict[str, Any], payload: dict[str, Any]) -> None:
    result = command.get("result")
    if isinstance(result, dict):
        result.update(payload)
    done = command.get("done")
    if isinstance(done, threading.Event):
        done.set()


def screenshot_path_for_session(session_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", session_id).strip("-") or "session"
    return AUTOMATION_VIEW_DIR / f"heybox-{safe_id}.png"


def capture_session_screenshot(session: HeyboxAutomationSession) -> None:
    page = session.page
    if page is None:
        return
    if browser_page_is_closed(page):
        mark_session_browser_closed(session)
        return
    try:
        path = screenshot_path_for_session(session.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=False)
        version = int(time.time() * 1000)
        session.last_screenshot_url = f"{AUTOMATION_VIEW_URL_PREFIX}/{path.name}?v={version}"
    except Exception as exc:  # noqa: BLE001
        session.update(log=f"刷新浏览器同步画面失败：{exc}")


def reveal_heybox_review_actions(page: Any) -> None:
    try:
        page.keyboard.press("End")
    except Exception:  # noqa: BLE001
        pass
    try:
        page.mouse.wheel(0, 3600)
    except Exception:  # noqa: BLE001
        pass
    try:
        page.evaluate(
            """
            () => {
              window.scrollTo(0, document.body.scrollHeight);
              const selectors = [
                'main',
                '[class*="scroll"]',
                '[class*="Scroll"]',
                '[class*="editor"]',
                '[class*="Editor"]',
                '[class*="container"]',
                '[class*="Container"]'
              ];
              const nodes = new Set();
              selectors.forEach((selector) => {
                document.querySelectorAll(selector).forEach((node) => nodes.add(node));
              });
              nodes.forEach((node) => {
                if (node && node.scrollHeight > node.clientHeight) {
                  node.scrollTop = node.scrollHeight;
                }
              });
            }
            """
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass


def body_text_for_draft(draft_index: int) -> str:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise HeyboxAutomationError("没有找到这篇草稿，可能已经被删除。")
    draft = drafts[draft_index]
    heybox_copy = selected_heybox_copy(draft, draft_index)
    subtitle = strip_inline_rich_markers(str(heybox_copy.get("subtitle") or draft.get("subtitle") or "")).strip()
    body = strip_inline_rich_markers(clean_body_markdown(str(heybox_copy.get("body_markdown") or draft.get("body_markdown") or ""))).strip()
    return "\n\n".join(part for part in [subtitle, body] if part)


def build_heybox_article_import(draft_index: int) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise HeyboxAutomationError("没有找到这篇草稿，可能已经被删除。")
    draft = drafts[draft_index]
    heybox_copy = selected_heybox_copy(draft, draft_index)
    body = clean_body_markdown(str(heybox_copy.get("body_markdown") or draft.get("body_markdown") or ""))
    subtitle = strip_inline_rich_markers(str(heybox_copy.get("subtitle") or draft.get("subtitle") or "")).strip()
    slot_paths = selected_image_paths_by_slot(draft)
    extra_slot_ids = extra_image_slot_ids(slot_paths, body)

    blocks: list[str] = []
    plain_lines: list[str] = []
    image_paths: list[str] = []
    paragraph_lines: list[str] = []
    heading_index = 0
    paragraph_count = 0

    def add_image_marker(slot_id: str) -> None:
        path = slot_paths.get(slot_id)
        if not path:
            return
        marker = f"TECH_AGENT_IMAGE_{len(image_paths) + 1:02d}"
        image_paths.append(str(path))
        blocks.append(f'<p data-tech-agent-image="{len(image_paths) - 1}">{marker}</p>')
        plain_lines.append(marker)

    def flush_paragraph() -> None:
        nonlocal paragraph_count
        if not paragraph_lines:
            return
        text = " ".join(line.strip() for line in paragraph_lines if line.strip())
        paragraph_lines.clear()
        if not text or is_source_note(text):
            return
        if IMAGE_MARKER_RE.match(text):
            add_image_marker("cover")
            return
        blocks.append(f"<p>{render_inline_rich_text(text)}</p>")
        plain_lines.append(strip_inline_rich_markers(text))
        paragraph_count += 1
        if extra_slot_ids and paragraph_count % 2 == 0:
            add_image_marker(extra_slot_ids.pop(0))

    if slot_paths.get("cover"):
        add_image_marker("cover")
    if subtitle:
        blocks.append(f"<p><strong>{html_escape(subtitle)}</strong></p>")
        plain_lines.append(subtitle)

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        heading = HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            heading_index += 1
            text = heading.group(2).strip()
            level = "h2" if len(heading.group(1)) <= 2 else "h3"
            blocks.append(f"<{level}>{render_inline_rich_text(text)}</{level}>")
            plain_lines.append(strip_inline_rich_markers(text))
            add_image_marker(f"section-{heading_index}")
            continue
        paragraph_lines.append(line)

    flush_paragraph()
    for slot_id in extra_slot_ids:
        add_image_marker(slot_id)
    return {
        "html": compact_heybox_import_html("".join(blocks)),
        "plain_text": compact_heybox_import_text("\n".join(plain_lines)),
        "image_paths": image_paths,
    }


def selected_heybox_copy(draft: dict[str, Any], draft_index: int) -> dict[str, Any]:
    draft_id = ensure_draft_identity(draft, draft_index)
    if selected_article_variant(draft_id, default="long") == "long":
        return {}
    return load_or_create_heybox_draft(draft_index)


def compact_heybox_import_text(text: str) -> str:
    lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def compact_heybox_import_html(html: str) -> str:
    return re.sub(r">\s+<", "><", str(html or "").strip())


def extra_image_slot_ids(slot_paths: dict[str, Path], body: str, max_images: int = 4) -> list[str]:
    heading_count = len([line for line in clean_body_markdown(body).splitlines() if HEADING_RE.match(line.strip())])
    inline_slots = {"cover"} | {f"section-{index}" for index in range(1, heading_count + 1)}
    ordered = ["cover", *[f"section-{index}" for index in range(1, 20)]]
    selected = [slot_id for slot_id in ordered if slot_id in slot_paths]
    extras = [slot_id for slot_id in selected if slot_id not in inline_slots]
    remaining = max(max_images - len([slot_id for slot_id in selected if slot_id in inline_slots]), 0)
    return extras[:remaining]


def selected_image_paths_by_slot(draft: dict[str, Any]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    disabled_slots = disabled_image_slots(draft)
    for slot in draft.get("image_slots", []) or []:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id") or "")
        if slot_id in disabled_slots:
            continue
        selected = slot.get("selected_image")
        if not slot_id or not isinstance(selected, dict):
            continue
        path = local_image_path_from_record(selected)
        if path:
            paths[slot_id] = path
    cover = draft.get("cover_image")
    if "cover" not in disabled_slots and "cover" not in paths and isinstance(cover, dict):
        path = local_image_path_from_record(cover)
        if path:
            paths["cover"] = path
    return paths


def local_image_path_from_record(image: dict[str, Any]) -> Path | None:
    local_path = str(image.get("local_path") or "").strip()
    if local_path:
        path = Path(local_path)
        if path.exists() and path.is_file():
            return path
    url = str(image.get("url") or "").strip()
    if url.startswith("/static-data/generated-images/"):
        path = ROOT_DIR / "data" / "generated_images" / url.rsplit("/", 1)[-1]
    elif url.startswith("/static-data/imported-images/"):
        path = ROOT_DIR / "data" / "imported_images" / url.rsplit("/", 1)[-1]
    elif url.startswith("/static-data/official-screenshots/"):
        path = ROOT_DIR / "data" / "official_screenshots" / url.rsplit("/", 1)[-1]
    elif url.startswith("/static-data/images/"):
        path = ROOT_DIR / "data" / "images" / url.rsplit("/", 1)[-1]
    else:
        return None
    return path if path.exists() and path.is_file() else None


def html_escape(text: str) -> str:
    import html

    return html.escape(text)


def compact_heybox_title(title: str) -> str:
    cleaned = format_title_with_prefix(strip_inline_rich_markers(title).strip())
    if heybox_title_units(cleaned) <= HEYBOX_TITLE_LIMIT:
        return cleaned
    prefix = title_prefix_bracketed()
    clean_title = strip_title_prefix(cleaned)
    if clean_title and heybox_title_units(clean_title) <= HEYBOX_TITLE_LIMIT:
        return clean_title
    if prefix and clean_title:
        available = HEYBOX_TITLE_LIMIT - heybox_title_units(prefix)
        if available >= 8:
            compact = compact_heybox_title_without_prefix(clean_title, available)
            return f"{prefix}{compact}"
    return compact_heybox_title_without_prefix(cleaned, HEYBOX_TITLE_LIMIT)


def compact_heybox_title_without_prefix(title: str, limit: int) -> str:
    cleaned = strip_inline_rich_markers(title).strip()
    if heybox_title_units(cleaned) <= limit:
        return cleaned
    candidates = compact_heybox_title_candidates(cleaned)
    for candidate in candidates:
        if heybox_title_units(candidate) <= limit:
            return candidate
    return trim_title_at_natural_boundary(cleaned, limit)


def heybox_title_units(text: str) -> float:
    units = 0.0
    for char in text:
        units += 0.5 if char.isascii() else 1.0
    return units


def take_heybox_title_units(text: str, limit: float) -> str:
    units = 0.0
    chars: list[str] = []
    for char in text:
        next_units = units + (0.5 if char.isascii() else 1.0)
        if next_units > limit:
            break
        chars.append(char)
        units = next_units
    return "".join(chars)


def compact_heybox_title_candidates(title: str) -> list[str]:
    cleaned = strip_inline_rich_markers(title).strip()
    candidates = [cleaned]

    def add(value: str) -> None:
        value = value.strip("，。！？、；：,.!?;: …")
        if value and value not in candidates:
            candidates.append(value)

    add(re.sub(r"(价格战)比[^，。！？、；：,.!?;:]{1,12}(先来了|来了|更重要|更关键|更现实)", r"\1\2", cleaned))
    add(re.sub(r"(成本战)比[^，。！？、；：,.!?;:]{1,12}(先来了|来了|更重要|更关键|更现实)", r"\1\2", cleaned))
    add(re.sub(r"(AI价格战)比[^，。！？、；：,.!?;:]{1,12}(先来了|来了|更重要|更关键|更现实)", r"\1\2", cleaned))

    if "，" in cleaned:
        first, second = cleaned.split("，", 1)
        second_candidates = compact_heybox_title_candidates(second) if len(second) < len(cleaned) else [second]
        for second_candidate in second_candidates:
            add(f"{first}，{second_candidate}")
    if "：" in cleaned:
        first, second = cleaned.split("：", 1)
        add(f"{first}：{trim_title_at_natural_boundary(second, 12)}")

    return candidates


def trim_title_at_natural_boundary(title: str, limit: int) -> str:
    clipped = take_heybox_title_units(title, limit).rstrip("，。！？、；：,.!?;: …")
    for mark in ["，", "：", "、", " "]:
        index = clipped.rfind(mark)
        if index >= max(6, int(limit) // 2):
            return clipped[:index].rstrip("，。！？、；：,.!?;: …")
    return clipped


def exported_image_paths(export: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in export.get("image_downloads", []) or []:
        path = Path(str(item.get("download_path") or ""))
        if path.exists() and path.is_file():
            paths.append(str(path))
    return paths


def likely_login_required(page: Any) -> bool:
    url = str(page.url or "").lower()
    if "login" in url or "passport" in url:
        return True
    login_texts = ["登录", "扫码登录", "手机号登录"]
    return any(locator_visible(page, text, timeout=400) for text in login_texts) and not editor_ready(page)


def editor_ready(page: Any) -> bool:
    return bool(find_title_input(page) or visible_contenteditable_count(page) > 0)


def ensure_editor_page(page: Any, session: HeyboxAutomationSession) -> None:
    session.update(step="open-editor", message="正在进入小黑盒文章编辑页。", log="尝试进入小黑盒文章编辑页。")
    if editor_ready(page):
        return
    close_common_popups(page)
    if open_heybox_article_editor(page):
        page.wait_for_timeout(2500)
        if editor_ready(page):
            session.update(log="已进入小黑盒文章编辑页。")
            return
    for text in ["发布文章", "发布内容", "创作内容", "写文章", "发文章", "文章", "新建内容", "新建"]:
        if click_by_text(page, text):
            page.wait_for_timeout(1200)
            if not editor_ready(page) and open_heybox_article_editor(page):
                page.wait_for_timeout(1800)
            if editor_ready(page):
                return
    wait_for_user(
        session,
        "暂时没找到小黑盒文章编辑器。请在打开的浏览器里进入“发布文章”，进入编辑页后回到 agent 点击“继续自动导入”。",
        step="open-editor",
    )


def open_heybox_article_editor(page: Any) -> bool:
    try:
        if "/creator/editor/draft/article" not in str(page.url):
            page.goto(HEYBOX_ARTICLE_EDITOR_URL, wait_until="domcontentloaded", timeout=60000)
        return True
    except Exception:  # noqa: BLE001
        try:
            return click_by_href(page, "/creator/editor/draft/article")
        except Exception:  # noqa: BLE001
            return False


def fill_title(page: Any, session: HeyboxAutomationSession, title: str) -> None:
    if not title:
        return
    session.update(step="fill-title", message="正在填写标题。", log=f"填写标题：{len(title)} 字。")
    if set_heybox_prosemirror_field(page, title, field_type="title"):
        return
    if set_title_by_dom(page, title):
        return
    if fill_with_user_selected_field(
        page,
        session,
        "title",
        title,
        "没有自动找到标题输入框。请在打开的浏览器或同步画面里点击标题输入区域，看到蓝色边框后，再回到 agent 点击“继续自动导入”。",
    ):
        return
    raise HeyboxAutomationError("标题填写失败：没有拿到可用的标题输入框。")


def fill_body(page: Any, session: HeyboxAutomationSession, html: str, plain_text: str) -> None:
    if not html and not plain_text:
        return
    session.update(step="fill-body", message="正在填写富文本正文。", log="填写富文本正文。")
    if paste_heybox_rich_body(page, html, plain_text):
        return
    if set_heybox_prosemirror_field(page, plain_text, field_type="body") and body_editor_contains(page, content_probe(plain_text)):
        return
    if set_body_by_dom(page, plain_text) and body_editor_contains(page, content_probe(plain_text)):
        return
    if fill_with_user_selected_field(
        page,
        session,
        "body",
        plain_text,
        "没有自动找到正文编辑区。请在打开的浏览器或同步画面里点击正文编辑区域，看到蓝色边框后，再回到 agent 点击“继续自动导入”。",
    ):
        return
    raise HeyboxAutomationError("正文填写失败：没有拿到可用的正文编辑区。")


def upload_images(page: Any, image_paths: list[str]) -> str:
    if not image_paths:
        return "当前没有可自动上传的本地 PNG 图片。"
    if set_file_inputs(page, image_paths):
        return ""
    if upload_heybox_image_text_images(page, image_paths):
        return ""
    if upload_via_file_chooser(page, image_paths):
        return ""
    return "没有自动找到图片上传控件；图片已导出到本地，可用“导出小黑盒”里的 PNG 包手动上传。"


def upload_article_images(page: Any, image_paths: list[str]) -> str:
    if not image_paths:
        return "当前没有可自动上传的本地 PNG 图片。"
    uploaded = 0
    for index, image_path in enumerate(image_paths):
        if insert_article_image_at_marker(page, image_path, index):
            uploaded += 1
    if uploaded == len(image_paths):
        return ""
    if uploaded:
        return f"已插入 {uploaded}/{len(image_paths)} 张正文图片；剩余图片需要人工补传。"
    return "没有自动找到正文图片上传控件；图片已导出到本地，可用“导出小黑盒”里的 PNG 包手动上传。"


def fill_with_user_selected_field(page: Any, session: HeyboxAutomationSession, field_type: str, value: str, message: str) -> bool:
    inject_field_picker(page, field_type)
    wait_for_user(session, message, step=f"pick-{field_type}")
    selector = selected_field_selector(page, field_type)
    if not selector:
        return False
    return set_value_by_selector(page, selector, value)


def inject_field_picker(page: Any, field_type: str) -> None:
    label = "标题" if field_type == "title" else "正文"
    try:
        page.evaluate(
            """
            ({ fieldType, label }) => {
              window.__techAgentPickedFields = window.__techAgentPickedFields || {};
              window.__techAgentPickMode = fieldType;
              if (!document.getElementById('__tech_agent_pick_style')) {
                const style = document.createElement('style');
                style.id = '__tech_agent_pick_style';
                style.textContent = `
                  .__tech-agent-pickable { outline: 2px dashed #0f766e !important; outline-offset: 3px !important; cursor: crosshair !important; }
                  .__tech-agent-picked { outline: 3px solid #2563eb !important; outline-offset: 3px !important; }
                  #__tech_agent_pick_tip { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 2147483647; background: #0f172a; color: white; padding: 10px 14px; border-radius: 8px; font-size: 14px; box-shadow: 0 12px 28px rgba(0,0,0,.22); }
                `;
                document.head.appendChild(style);
              }
              let tip = document.getElementById('__tech_agent_pick_tip');
              if (!tip) {
                tip = document.createElement('div');
                tip.id = '__tech_agent_pick_tip';
                document.body.appendChild(tip);
              }
              tip.textContent = `请点击${label}输入区域，蓝框出现后回到 agent 点“继续自动导入”`;
              const candidates = [...document.querySelectorAll('input, textarea, [contenteditable="true"], [contenteditable="plaintext-only"]')]
                .filter((el) => {
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 20 && rect.height > 20;
                });
              candidates.forEach((el) => el.classList.add('__tech-agent-pickable'));
              if (!window.__techAgentPickHandler) {
                window.__techAgentPickHandler = (event) => {
                  const mode = window.__techAgentPickMode;
                  if (!mode) return;
                  const target = event.target.closest('input, textarea, [contenteditable="true"], [contenteditable="plaintext-only"]');
                  if (!target) return;
                  event.preventDefault();
                  event.stopPropagation();
                  document.querySelectorAll('.__tech-agent-picked').forEach((el) => el.classList.remove('__tech-agent-picked'));
                  target.classList.add('__tech-agent-picked');
                  if (!target.id) target.id = `tech-agent-picked-${mode}-${Date.now()}`;
                  window.__techAgentPickedFields[mode] = `#${CSS.escape(target.id)}`;
                  const currentTip = document.getElementById('__tech_agent_pick_tip');
                  if (currentTip) currentTip.textContent = `${mode === 'title' ? '标题' : '正文'}区域已选中，请回到 agent 点击“继续自动导入”`;
                };
                document.addEventListener('click', window.__techAgentPickHandler, true);
              }
            }
            """,
            {"fieldType": field_type, "label": label},
        )
    except Exception:  # noqa: BLE001
        return


def selected_field_selector(page: Any, field_type: str) -> str:
    try:
        return str(page.evaluate("(fieldType) => (window.__techAgentPickedFields || {})[fieldType] || ''", field_type) or "")
    except Exception:  # noqa: BLE001
        return ""


def set_heybox_prosemirror_field(page: Any, value: str, field_type: str) -> bool:
    index = 0 if field_type == "title" else 1
    try:
        editor = page.locator(".ProseMirror.hb-editor").nth(index)
        if editor.count() <= 0 or not editor.is_visible(timeout=800):
            return False
        editor.click(timeout=1500)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(value)
        page.wait_for_timeout(450)
        return True
    except Exception:  # noqa: BLE001
        return False


def paste_heybox_rich_body(page: Any, html: str, plain_text: str) -> bool:
    try:
        editor = page.locator(".ProseMirror.hb-editor").nth(1)
        if editor.count() <= 0 or not editor.is_visible(timeout=800):
            return False
        editor.click(timeout=1500)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        probe = content_probe(plain_text)
        if paste_html_clipboard(page, html, plain_text) and body_editor_contains(page, probe):
            return True
        editor.click(timeout=1500)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        if paste_html_event(page, html, plain_text) and body_editor_contains(page, probe):
            return True
        editor.click(timeout=1500)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        if insert_html_command(page, html) and body_editor_contains(page, probe):
            return True
        editor.click(timeout=1500)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(plain_text)
        page.wait_for_timeout(900)
        return body_editor_contains(page, probe)
    except Exception:  # noqa: BLE001
        return False


def paste_html_clipboard(page: Any, html: str, plain_text: str) -> bool:
    try:
        page.evaluate(
            """
            async ({ html, plainText }) => {
              const item = new ClipboardItem({
                'text/html': new Blob([html], { type: 'text/html' }),
                'text/plain': new Blob([plainText], { type: 'text/plain' })
              });
              await navigator.clipboard.write([item]);
            }
            """,
            {"html": html, "plainText": plain_text},
        )
        page.keyboard.press("Control+V")
        page.wait_for_timeout(1300)
        return True
    except Exception:  # noqa: BLE001
        return False


def paste_html_event(page: Any, html: str, plain_text: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                ({ html, plainText }) => {
                  const editor = document.querySelectorAll('.ProseMirror.hb-editor')[1];
                  if (!editor) return false;
                  editor.focus();
                  const data = new DataTransfer();
                  data.setData('text/html', html);
                  data.setData('text/plain', plainText);
                  const event = new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true });
                  editor.dispatchEvent(event);
                  return true;
                }
                """,
                {"html": html, "plainText": plain_text},
            )
        )
    except Exception:  # noqa: BLE001
        return False


def insert_html_command(page: Any, html: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (html) => {
                  const editor = document.querySelectorAll('.ProseMirror.hb-editor')[1];
                  if (!editor) return false;
                  editor.focus();
                  return document.execCommand('insertHTML', false, html);
                }
                """,
                html,
            )
        )
    except Exception:  # noqa: BLE001
        return False


def body_editor_contains(page: Any, text: str) -> bool:
    if not text:
        return True
    try:
        return bool(
            page.evaluate(
                """
                (text) => {
                  const editor = document.querySelectorAll('.ProseMirror.hb-editor')[1];
                  if (!editor) return false;
                  const content = (editor.innerText || editor.textContent || '').replace(/\\s+/g, '');
                  return content.includes(String(text).replace(/\\s+/g, ''));
                }
                """,
                text,
            )
        )
    except Exception:  # noqa: BLE001
        return False


def content_probe(plain_text: str) -> str:
    marker = re.search(r"TECH_AGENT_IMAGE_\d{2}", plain_text)
    if marker:
        return marker.group(0)
    compact = re.sub(r"\s+", "", strip_inline_rich_markers(plain_text))
    return compact[:8]


def insert_article_image_at_marker(page: Any, image_path: str, index: int) -> bool:
    marker = f"TECH_AGENT_IMAGE_{index + 1:02d}"
    try:
        if not select_marker_text(page, marker):
            return False
        page.keyboard.press("Backspace")
        page.wait_for_timeout(250)
        return upload_article_image_from_cursor(page, image_path)
    except Exception:  # noqa: BLE001
        return False


def select_marker_text(page: Any, marker: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (marker) => {
                  const editor = document.querySelectorAll('.ProseMirror.hb-editor')[1];
                  if (!editor) return false;
                  editor.focus();
                  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
                  let node;
                  while ((node = walker.nextNode())) {
                    const index = node.textContent.indexOf(marker);
                    if (index < 0) continue;
                    const range = document.createRange();
                    range.setStart(node, index);
                    range.setEnd(node, index + marker.length);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                    return true;
                  }
                  return false;
                }
                """,
                marker,
            )
        )
    except Exception:  # noqa: BLE001
        return False


def upload_article_image_from_cursor(page: Any, image_path: str) -> bool:
    selectors = [
        ".editor-menu-image__btn",
        "button[data-name='插入图片']",
    ]
    for selector in selectors:
        try:
            target = page.locator(selector).first
            if target.count() <= 0:
                continue
            target.evaluate("el => el.scrollIntoView({ block: 'center', inline: 'center' })")
            page.wait_for_timeout(250)
            target.click(timeout=1200, force=True, no_wait_after=True)
            page.wait_for_timeout(700)
            if upload_article_image_modal(page, image_path):
                return True
        except Exception:  # noqa: BLE001
            continue
    return upload_article_image_by_coordinates(page, image_path)


def upload_article_image_by_coordinates(page: Any, image_path: str) -> bool:
    try:
        box = page.evaluate(
            """
            () => {
              const el = document.querySelector('.editor-menu-image__btn');
              if (!el) return null;
              el.scrollIntoView({ block: 'center', inline: 'center' });
              const rect = el.getBoundingClientRect();
              return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
            }
            """
        )
        if not box:
            return False
        page.mouse.click(float(box["x"]), float(box["y"]))
        page.wait_for_timeout(700)
        return upload_article_image_modal(page, image_path)
    except Exception:  # noqa: BLE001
        return False


def upload_article_image_modal(page: Any, image_path: str) -> bool:
    try:
        upload_box = page.locator(".editor-image-wrapper__box.upload").first
        if upload_box.count() <= 0:
            upload_box = page.locator(".editor-model__image .upload").first
        if upload_box.count() <= 0:
            return False
        with page.expect_file_chooser(timeout=5000) as chooser_info:
            upload_box.click(timeout=1500, force=True, no_wait_after=True)
        chooser = chooser_info.value
        chooser.set_files(image_path)
        page.wait_for_timeout(1600)
        confirm = page.locator(".editor-__model-frame-bottom-btn.hb-color__btn--confirm").last
        if confirm.count() <= 0:
            confirm = page.get_by_text("确定", exact=True).last
        confirm.click(timeout=1800, force=True, no_wait_after=True)
        page.wait_for_timeout(2200)
        return True
    except Exception:  # noqa: BLE001
        try:
            cancel = page.get_by_text("取消", exact=True).last
            if cancel.count() > 0:
                cancel.click(timeout=800, force=True, no_wait_after=True)
        except Exception:  # noqa: BLE001
            pass
        return False


def set_value_by_selector(page: Any, selector: str, value: str) -> bool:
    try:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible(timeout=500):
            locator.click(timeout=1200)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(value)
            page.wait_for_timeout(350)
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(
            page.evaluate(
                """
                ({ selector, value }) => {
                  const el = document.querySelector(selector);
                  if (!el) return false;
                  el.focus();
                  if (el.isContentEditable) {
                    el.innerText = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                  } else {
                    el.value = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                  }
                  const tip = document.getElementById('__tech_agent_pick_tip');
                  if (tip) tip.textContent = '已写入内容，可继续下一步';
                  return true;
                }
                """,
                {"selector": selector, "value": value},
            )
        )
    except Exception:  # noqa: BLE001
        return False


def set_title_by_dom(page: Any, title: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (title) => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                  };
                  const fields = [...document.querySelectorAll('input, textarea')].filter(visible);
                  const titleField = fields.find((el) => {
                    const text = `${el.placeholder || ''} ${el.name || ''} ${el.getAttribute('aria-label') || ''}`.toLowerCase();
                    return text.includes('标题') || text.includes('title');
                  }) || fields[0];
                  const editables = [...document.querySelectorAll('[contenteditable="true"], [contenteditable="plaintext-only"]')].filter(visible);
                  const titleEditable = editables.find((el) => {
                    const text = `${el.getAttribute('data-placeholder') || ''} ${el.getAttribute('placeholder') || ''} ${el.getAttribute('aria-label') || ''} ${el.className || ''}`.toLowerCase();
                    const rect = el.getBoundingClientRect();
                    return (text.includes('标题') || text.includes('title') || rect.height <= 90) && rect.width > 180;
                  });
                  const target = titleField || titleEditable;
                  if (!target) return false;
                  target.focus();
                  if (target.isContentEditable) {
                    target.innerText = title;
                  } else {
                    target.value = title;
                  }
                  target.dispatchEvent(new Event('input', { bubbles: true }));
                  target.dispatchEvent(new Event('change', { bubbles: true }));
                  return true;
                }
                """,
                title,
            )
        )
    except Exception:  # noqa: BLE001
        return False


def set_body_by_dom(page: Any, body: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (body) => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 20 && rect.height > 20;
                  };
                  const editables = [...document.querySelectorAll('[contenteditable="true"], [contenteditable="plaintext-only"]')].filter(visible);
                  const target = editables
                    .filter((el) => {
                      const text = `${el.getAttribute('data-placeholder') || ''} ${el.getAttribute('placeholder') || ''} ${el.getAttribute('aria-label') || ''} ${el.className || ''}`.toLowerCase();
                      const rect = el.getBoundingClientRect();
                      return text.includes('正文') || text.includes('内容') || text.includes('content') || rect.height >= 120;
                    })
                    .sort((a, b) => (b.getBoundingClientRect().height * b.getBoundingClientRect().width) - (a.getBoundingClientRect().height * a.getBoundingClientRect().width))[0];
                  if (target) {
                    target.focus();
                    target.innerText = body;
                    target.dispatchEvent(new Event('input', { bubbles: true }));
                    target.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                  }
                  const fields = [...document.querySelectorAll('textarea')].filter(visible);
                  const bodyField = fields.find((el) => {
                    const text = `${el.placeholder || ''} ${el.name || ''} ${el.getAttribute('aria-label') || ''}`.toLowerCase();
                    return text.includes('正文') || text.includes('内容') || text.includes('content');
                  }) || fields[0];
                  if (!bodyField) return false;
                  bodyField.focus();
                  bodyField.value = body;
                  bodyField.dispatchEvent(new Event('input', { bubbles: true }));
                  bodyField.dispatchEvent(new Event('change', { bubbles: true }));
                  return true;
                }
                """,
                body,
            )
        )
    except Exception:  # noqa: BLE001
        return False


def set_file_inputs(page: Any, image_paths: list[str]) -> bool:
    inputs = page.locator("input[type='file']")
    try:
        count = inputs.count()
    except Exception:  # noqa: BLE001
        return False
    for index in range(count):
        locator = inputs.nth(index)
        try:
            accept = str(locator.get_attribute("accept") or "").lower()
            if accept and "image" not in accept and ".png" not in accept and ".jpg" not in accept and ".jpeg" not in accept and ".webp" not in accept:
                continue
            multiple = bool(locator.evaluate("el => !!el.multiple"))
            locator.set_input_files(image_paths if multiple else image_paths[0])
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def upload_via_file_chooser(page: Any, image_paths: list[str]) -> bool:
    for text in ["插入图片", "上传图片", "添加图片", "图片", "封面"]:
        try:
            with page.expect_file_chooser(timeout=2500) as chooser_info:
                if not click_by_text(page, text):
                    continue
            chooser = chooser_info.value
            try:
                chooser.set_files(image_paths if chooser.is_multiple() else image_paths[0])
            except Exception:  # noqa: BLE001
                chooser.set_files(image_paths[0])
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def upload_heybox_image_text_images(page: Any, image_paths: list[str]) -> bool:
    selectors = [
        ".editor-image-wrapper__box.upload",
        ".editor__image-wrapper .upload",
        ".editor-image-text__image-seletor",
    ]
    for selector in selectors:
        try:
            target = page.locator(selector).first
            if target.count() <= 0 or not target.is_visible(timeout=700):
                continue
            with page.expect_file_chooser(timeout=3500) as chooser_info:
                target.click(timeout=1500)
            chooser = chooser_info.value
            try:
                chooser.set_files(image_paths if chooser.is_multiple() else image_paths[0])
            except Exception:  # noqa: BLE001
                chooser.set_files(image_paths[0])
            page.wait_for_timeout(1200)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def close_common_popups(page: Any) -> None:
    for text in ["知道了", "我知道了", "确定", "取消", "关闭", "稍后再说"]:
        try:
            locator = page.get_by_role("button", name=re.compile(re.escape(text))).first
            if locator.count() > 0 and locator.is_visible(timeout=300):
                locator.click(timeout=800)
        except Exception:  # noqa: BLE001
            continue
    for selector in ["[aria-label='关闭']", ".close", ".modal-close", ".ant-modal-close"]:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible(timeout=300):
                locator.click(timeout=800)
        except Exception:  # noqa: BLE001
            continue


def find_title_input(page: Any) -> Any | None:
    selectors = [
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
        "input[aria-label*='标题']",
        "textarea[aria-label*='标题']",
        "input[name*='title' i]",
        "textarea[name*='title' i]",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible(timeout=300):
                return locator
        except Exception:  # noqa: BLE001
            continue
    return None


def visible_contenteditable_count(page: Any) -> int:
    try:
        return int(
            page.evaluate(
                """
                () => [...document.querySelectorAll('[contenteditable="true"], [contenteditable="plaintext-only"]')]
                  .filter((el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 20 && rect.height > 20;
                  }).length
                """
            )
        )
    except Exception:  # noqa: BLE001
        return 0


def click_by_text(page: Any, text: str) -> bool:
    escaped = re.escape(text)
    locators = [
        page.get_by_role("button", name=re.compile(escaped)),
        page.get_by_text(re.compile(escaped)),
    ]
    for locator in locators:
        try:
            if locator.count() <= 0:
                continue
            target = locator.first
            if target.is_visible(timeout=600):
                target.click(timeout=1200)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def click_by_href(page: Any, href_part: str) -> bool:
    try:
        locator = page.locator(f"a[href*='{href_part}']").first
        if locator.count() > 0 and locator.is_visible(timeout=800):
            locator.click(timeout=1500)
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def locator_visible(page: Any, text: str, timeout: int = 500) -> bool:
    try:
        locator = page.get_by_text(text, exact=False).first
        return locator.count() > 0 and locator.is_visible(timeout=timeout)
    except Exception:  # noqa: BLE001
        return False
