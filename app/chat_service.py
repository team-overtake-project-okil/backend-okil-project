from collections.abc import Sequence

from app.chat_sessions import ChatTurn

REPLY_TIMEOUT_SECONDS = 30


async def generate_reply(text: str, history: Sequence[ChatTurn]) -> str:
    """Return a test reply until an AI provider can use the conversation history."""
    return f"Test reply: {text}"
