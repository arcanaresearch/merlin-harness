from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .sanitize import sanitize


def export_config(values: Mapping[str, Any]) -> str:
    """Serialize only sanitized, non-secret configuration as stable JSON."""
    return json.dumps(sanitize(values), indent=2, sort_keys=True) + "\n"
