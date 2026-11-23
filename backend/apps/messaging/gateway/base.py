"""WhatsApp sending interface (ARCHITECTURE §5, §9). The Cloud API adapter is added in Phase 7."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from apps.core.integrations import PermanentError, TransientError

__all__ = [
    "Button",
    "OutsideServiceWindow",
    "PermanentError",
    "SendResult",
    "TransientError",
    "WhatsAppGateway",
]


class OutsideServiceWindow(PermanentError):
    """Free-form message attempted outside the 24-hour customer service window."""

    def __init__(self, message: str = "free-form message outside the 24h window"):
        super().__init__(message, code="outside_service_window")


@dataclass(frozen=True)
class SendResult:
    wa_message_id: str


@dataclass(frozen=True)
class Button:
    id: str
    title: str  # WhatsApp limits reply-button titles to 20 characters


class WhatsAppGateway(ABC):
    name: str

    @abstractmethod
    def send_text(self, to: str, body: str) -> SendResult: ...

    @abstractmethod
    def send_buttons(self, to: str, body: str, buttons: list[Button]) -> SendResult: ...

    @abstractmethod
    def send_template(
        self,
        to: str,
        template: str,
        language: str,
        params: list[str],
        document_media_id: str | None = None,
        document_filename: str | None = None,
    ) -> SendResult: ...

    @abstractmethod
    def upload_media(self, content: bytes, mime_type: str, filename: str) -> str:
        """Upload a file to WhatsApp's media store; returns a media id (no public URL is created)."""

    @abstractmethod
    def send_document(self, to: str, media_id: str, filename: str, caption: str = "") -> SendResult: ...
