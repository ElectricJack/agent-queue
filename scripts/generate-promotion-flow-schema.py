#!/usr/bin/env python3
"""Generate the published promotion-flow schema."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    from src.integration.promotion_steps import FlowSchema

    path = ROOT / "docs/reference/promotion-flow-schema.json"
    path.write_text(
        json.dumps(FlowSchema.schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {path.relative_to(ROOT)}")
