import re

from pydantic import JsonValue, TypeAdapter, ValidationError

MAX_MESSAGE_BYTES = 32 * 1024
MAX_TEXT_LENGTH = 4_000
MAX_REQUEST_ID_LENGTH = 64
SESSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
json_object = TypeAdapter(dict[str, JsonValue])


class ChatProtocolError(Exception):
    def __init__(self, code: str, message: str, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id


def parse_message(raw: str | None) -> dict[str, JsonValue]:
    if raw is None:
        raise ChatProtocolError("invalid_message", "Send a JSON text message.")
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ChatProtocolError("message_too_long", "Message exceeds 32 KiB.")
    try:
        return json_object.validate_json(raw)
    except ValidationError:
        raise ChatProtocolError("invalid_message", "Send a valid JSON object.") from None


def parse_session_token(token: JsonValue) -> str | None:
    if token is None:
        return None
    if not isinstance(token, str) or not SESSION_TOKEN_PATTERN.fullmatch(token):
        raise ChatProtocolError("invalid_message", "Invalid sessionToken.")
    return token


def parse_question(payload: dict[str, JsonValue]) -> tuple[str, str]:
    raw_id = payload.get("requestId")
    request_id = None
    if isinstance(raw_id, str) and raw_id.strip() and len(raw_id) <= MAX_REQUEST_ID_LENGTH:
        request_id = raw_id

    if payload.get("type") != "message.send":
        raise ChatProtocolError(
            "invalid_message", "Expected type session.open or message.send.", request_id
        )
    if request_id is None:
        raise ChatProtocolError(
            "invalid_message", "Provide a non-empty requestId of up to 64 characters."
        )

    question = payload.get("text")
    if not isinstance(question, str) or not question.strip():
        raise ChatProtocolError("invalid_message", "Provide non-empty text.", request_id)
    if len(question) > MAX_TEXT_LENGTH:
        raise ChatProtocolError("message_too_long", "Text exceeds 4000 characters.", request_id)
    return request_id, question
