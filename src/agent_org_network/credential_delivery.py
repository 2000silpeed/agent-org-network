"""Credential delivery transport values shared below MCP/runtime boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final, final

_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_DELIVERY_REF: Final = re.compile(r"delivery:v1:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class DeliveryStage:
    stage_key: str
    delivery_ref: str

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.stage_key) is None or _DELIVERY_REF.fullmatch(self.delivery_ref) is None:
            raise ValueError("canonical delivery stage 결과가 필요합니다.")


@final
class StageMissing:
    """recover-stage의 explicit miss value."""
