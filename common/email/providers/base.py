"""Provider-agnostic email primitives.

`EmailMessage` is the rendered payload (already-resolved subject, text + HTML
bodies, recipient). `EmailProvider.send` returns an `EmailResult` rather than
raising for delivery failures — call sites decide how to react. Constructor-
or configuration-time failures (missing API key, etc.) still raise, since
those are programmer errors.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EmailMessage:
    to_email: str
    to_name: str
    subject: str
    text: str
    html: str


@dataclass(frozen=True)
class EmailResult:
    ok: bool
    provider_message_id: Optional[str] = None
    error: Optional[str] = None


class EmailProvider(ABC):
    """Subclasses must set ``name`` (lowercase identifier used in logs)
    and implement ``send``."""

    name: str = 'unknown'

    @abstractmethod
    def send(self, message: EmailMessage) -> EmailResult:
        ...
