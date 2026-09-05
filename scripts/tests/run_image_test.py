import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402

from app.image_client import generate_image, load_image_config


def first_generated_prompt() -> str:
    drafts_path = Path("data/drafts.json")
    if drafts_path.exists():
        drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
        for draft in drafts:
            for image in draft.get("image_plan", []):
                if image.get("type") != "official" and image.get("prompt_or_url"):
                    return image["prompt_or_url"]
    return (
        "A clean futuristic AI workflow cover image, abstract neural network nodes, "
        "blue and purple gradient, no text, no watermark, no logos, 16:9 composition."
    )


if __name__ == "__main__":
    config = load_image_config()
    print(f"Image config loaded: model={config.model}, base_url={config.base_url}")
    path = generate_image(first_generated_prompt(), output_name="image-api-test.png")
    print(f"Image saved: {path}")
