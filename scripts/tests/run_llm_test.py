import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402

from app.llm_client import chat_completion, load_llm_config


if __name__ == "__main__":
    config = load_llm_config()
    print(f"LLM config loaded: model={config.model}, base_url={config.base_url}")
    result = chat_completion(
        [
            {
                "role": "user",
                "content": "请只回复 OK 两个字母，用于接口连通性测试。",
            }
        ],
        temperature=0,
    )
    print("LLM response:", result.strip()[:200])
