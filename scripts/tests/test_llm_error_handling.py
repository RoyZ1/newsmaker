from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import httpx

import scripts._bootstrap  # noqa: F401,E402
from app.workflow import format_exception


def test_remote_protocol_error_gets_actionable_message() -> None:
    message = format_exception(httpx.RemoteProtocolError("Server disconnected without sending a response."))

    assert "Server disconnected without sending a response" not in message
    assert "模型 API" in message
    assert "自动重试" in message


def main() -> None:
    tests = [test_remote_protocol_error_gets_actionable_message]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
