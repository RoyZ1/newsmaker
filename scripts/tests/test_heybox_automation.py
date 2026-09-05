from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
import app.heybox_automation as heybox_automation


class ClosedPage:
    def is_closed(self) -> bool:
        return True


class FinishedThread:
    def is_alive(self) -> bool:
        return False


class AliveThread:
    def __init__(self) -> None:
        self.join_timeout: float | None = None

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        self.join_timeout = timeout


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeCDPSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def send(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 9}
        return {}


class WindowContext:
    def __init__(self) -> None:
        self.cdp = FakeCDPSession()

    def new_cdp_session(self, page) -> FakeCDPSession:
        return self.cdp


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class FakeMouse:
    def __init__(self) -> None:
        self.wheels: list[tuple[int, int]] = []

    def wheel(self, x: int, y: int) -> None:
        self.wheels.append((x, y))


class ReviewPage:
    def __init__(self) -> None:
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()
        self.evaluated = False
        self.waited = False

    def evaluate(self, script: str) -> None:
        self.evaluated = "scrollTo" in script

    def wait_for_timeout(self, timeout: int) -> None:
        self.waited = timeout == 500


def make_session(status: str = "waiting_review") -> heybox_automation.HeyboxAutomationSession:
    return heybox_automation.HeyboxAutomationSession(id="test-session", draft_index=0, status=status)


def test_closed_browser_marks_session_stopped_and_releases_commands() -> None:
    session = make_session()
    session.page = ClosedPage()
    done = threading.Event()
    result: dict = {}
    session.command_queue.put({"type": "screenshot", "done": done, "result": result})

    changed = heybox_automation.mark_session_browser_closed(session)

    assert changed is True
    assert session.status == "stopped"
    assert session.step == "closed"
    assert "浏览器已关闭" in session.message
    assert session.stop_event.is_set()
    assert done.is_set()
    assert "浏览器已关闭" in result["error"]


def test_status_refreshes_finished_nonterminal_session() -> None:
    previous = heybox_automation._active_session
    session = make_session()
    session.thread = FinishedThread()  # type: ignore[assignment]
    heybox_automation._active_session = session
    try:
        status = heybox_automation.heybox_automation_status()
    finally:
        heybox_automation._active_session = previous

    assert status["status"] == "stopped"
    assert "可以重新开始导入" in status["message"]


def test_continue_refreshes_closed_browser_before_running() -> None:
    previous = heybox_automation._active_session
    session = make_session(status="waiting_user")
    session.page = ClosedPage()
    heybox_automation._active_session = session
    try:
        status = heybox_automation.continue_heybox_automation()
    finally:
        heybox_automation._active_session = previous

    assert status["status"] == "stopped"
    assert "可以重新开始导入" in status["message"]
    assert session.continue_event.is_set()


def test_start_replaces_active_nonterminal_session() -> None:
    previous = heybox_automation._active_session
    old_session = make_session(status="running")
    old_thread = AliveThread()
    old_context = FakeContext()
    old_session.thread = old_thread  # type: ignore[assignment]
    old_session.context = old_context

    original_thread_class = heybox_automation.threading.Thread

    class FakeNewThread:
        def __init__(self, target, args, daemon=False) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon
            self.started = False

        def start(self) -> None:
            self.started = True

        def is_alive(self) -> bool:
            return False

    heybox_automation._active_session = old_session
    heybox_automation.threading.Thread = FakeNewThread  # type: ignore[assignment]
    try:
        snapshot = heybox_automation.start_heybox_automation(2, "http://127.0.0.1:5050")
        new_session = heybox_automation._active_session
    finally:
        heybox_automation.threading.Thread = original_thread_class
        heybox_automation._active_session = previous

    assert old_session.stop_event.is_set()
    assert old_session.continue_event.is_set()
    assert old_context.closed is True
    assert old_thread.join_timeout == 3
    assert old_session.status == "stopped"
    assert new_session is not old_session
    assert new_session is not None
    assert snapshot["draft_index"] == 2
    assert snapshot["status"] == "starting"


def test_reset_profile_window_state_removes_maximized_restore() -> None:
    previous_profile_dir = heybox_automation.PROFILE_DIR
    with tempfile.TemporaryDirectory() as temp_dir:
        profile_dir = Path(temp_dir)
        preferences_dir = profile_dir / "Default"
        preferences_dir.mkdir(parents=True)
        preferences_path = preferences_dir / "Preferences"
        preferences_path.write_text(
            '{"browser":{"window_placement":{"left":0,"top":0,"right":1600,"bottom":900,"maximized":true}},"profile":{"exit_type":"Crashed"}}',
            encoding="utf-8",
        )
        heybox_automation.PROFILE_DIR = profile_dir
        try:
            heybox_automation.reset_heybox_profile_window_state()
        finally:
            heybox_automation.PROFILE_DIR = previous_profile_dir

        preferences = json.loads(preferences_path.read_text(encoding="utf-8"))

    placement = preferences["browser"]["window_placement"]
    assert placement["maximized"] is False
    assert placement["left"] == heybox_automation.HEYBOX_BROWSER_X
    assert placement["top"] == heybox_automation.HEYBOX_BROWSER_Y
    assert placement["right"] == heybox_automation.HEYBOX_BROWSER_X + heybox_automation.HEYBOX_BROWSER_WIDTH
    assert placement["bottom"] == heybox_automation.HEYBOX_BROWSER_Y + heybox_automation.HEYBOX_BROWSER_HEIGHT
    assert preferences["profile"]["exit_type"] == "Normal"


def test_normalize_window_sets_normal_bounds() -> None:
    context = WindowContext()
    page = object()

    heybox_automation.normalize_heybox_window(context, page)

    assert context.cdp.calls[0] == ("Browser.getWindowForTarget", None)
    assert context.cdp.calls[1] == ("Browser.setWindowBounds", {"windowId": 9, "bounds": {"windowState": "normal"}})
    assert context.cdp.calls[2] == (
        "Browser.setWindowBounds",
        {
            "windowId": 9,
            "bounds": {
                "left": heybox_automation.HEYBOX_BROWSER_X,
                "top": heybox_automation.HEYBOX_BROWSER_Y,
                "width": heybox_automation.HEYBOX_BROWSER_WIDTH,
                "height": heybox_automation.HEYBOX_BROWSER_HEIGHT,
            },
        },
    )


def test_reveal_review_actions_scrolls_toward_publish_controls() -> None:
    page = ReviewPage()

    heybox_automation.reveal_heybox_review_actions(page)

    assert page.keyboard.pressed == ["End"]
    assert page.mouse.wheels == [(0, 3600)]
    assert page.evaluated is True
    assert page.waited is True


def main() -> None:
    tests = [
        test_closed_browser_marks_session_stopped_and_releases_commands,
        test_status_refreshes_finished_nonterminal_session,
        test_continue_refreshes_closed_browser_before_running,
        test_start_replaces_active_nonterminal_session,
        test_reset_profile_window_state_removes_maximized_restore,
        test_normalize_window_sets_normal_bounds,
        test_reveal_review_actions_scrolls_toward_publish_controls,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
