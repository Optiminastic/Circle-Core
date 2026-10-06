"""Port for placing AI voice calls (Dependency Inversion).

ScreeningCallService depends on this Protocol, not on Vapi, so tests use a fake
and the provider can be swapped without touching business rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CreatedCall:
    provider_call_id: str
    # Only for browser test calls: the link HR opens to talk to the agent.
    web_call_url: str | None = None


class VoiceCallError(RuntimeError):
    """The provider refused or could not be reached. `.status` when known."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class VoiceCallProvider(Protocol):
    def create_phone_call(self, assistant: dict[str, Any], to_number: str) -> CreatedCall: ...

    def create_web_call(self, assistant: dict[str, Any]) -> CreatedCall: ...
