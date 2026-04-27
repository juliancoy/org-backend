from __future__ import annotations

from typing import Mapping


def is_transition_allowed(current_status: str, target_status: str, transitions: Mapping[str, set[str]]) -> bool:
    allowed = transitions.get(current_status, set())
    return target_status in allowed
