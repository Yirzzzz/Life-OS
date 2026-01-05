from __future__ import annotations

from typing import Dict


class Planner:
    def __init__(self) -> None:
        self._routes: Dict[str, str] = {}

    def register_route(self, action: str, skill_name: str) -> None:
        self._routes[action] = skill_name

    def route(self, action: str) -> str:
        if action not in self._routes:
            raise KeyError(f"Unknown action: {action}")
        return self._routes[action]
