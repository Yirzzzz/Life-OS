from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Type

from pydantic import BaseModel


class Skill(ABC):
    name: str
    description: str
    input_schema: Type[BaseModel]
    output_schema: Type[BaseModel]

    @abstractmethod
    def run(self, data: BaseModel, context: Dict[str, Any]) -> BaseModel:
        raise NotImplementedError
