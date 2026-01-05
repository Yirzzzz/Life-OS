from __future__ import annotations

from typing import Dict

from app.agent.base import Skill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        return self._skills[name]

    def all(self) -> Dict[str, Skill]:
        return dict(self._skills)
