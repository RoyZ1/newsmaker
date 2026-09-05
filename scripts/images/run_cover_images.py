import sys

import scripts._bootstrap  # noqa: F401

from app.cover_images import generate_cover_for_draft, generate_cover_images, save_drafts_data
from app.writer import load_drafts


if __name__ == "__main__":
    if len(sys.argv) > 1:
        index = int(sys.argv[1])
        drafts = load_drafts()
        if index < 0 or index >= len(drafts):
            raise SystemExit(f"Draft index out of range: {index}")
        results = [generate_cover_for_draft(drafts[index], index)]
        save_drafts_data(drafts)
    else:
        results = generate_cover_images(force=True)
    if not results:
        print("No draft needed a new cover image.")
    for result in results:
        print(f"Draft: {result['title']}")
        print(f"Image saved: {result['image_path']}")
        if result.get("image_prompt"):
            print("Prompt:")
            print(result["image_prompt"])
        else:
            print("Prompt: official image reused")
