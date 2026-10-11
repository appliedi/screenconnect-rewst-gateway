"""Regenerate the committed openapi.json from the app."""

import json
from pathlib import Path

from gateway.config import Settings
from gateway.main import create_app

if __name__ == "__main__":
    app = create_app(Settings(read_key="r-key-123456", write_key="w-key-123456"))
    target = Path(__file__).resolve().parent.parent / "openapi.json"
    target.write_text(json.dumps(app.openapi(), indent=2) + "\n")
    print(f"wrote {target}")
