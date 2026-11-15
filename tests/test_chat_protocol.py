"""Validation rules for incoming chat WebSocket frames.
Assertions use error ``code`` (the stable client-facing contract) and the
``request_id`` echoed back to the client, not the human-readable messages.
"""

import pytest

from app.chat_protocol import (
    MAX_MESSAGE_BYTES,
    MAX_REQUEST_ID_LENGTH,
    MAX_TEXT_LENGTH,
    ChatProtocolError,
    parse_message,
    parse_question,
    parse_session_token,
)

VALID_TOKEN = "Abc_123-" * 5 + "xyz"  # 43 characters from the allowed alphabet


def error_of(call, *args):
    with pytest.raises(ChatProtocolError) as error:
        call(*args)
    return error.value


def message_with_size(size):
    envelope = '{"a":"' + '"}'
    return '{"a":"' + "x" * (size - len(envelope)) + '"}'


# --- parse_message ------------------------------------------------------------


def test_token_fixture_has_the_documented_length():
    assert len(VALID_TOKEN) == 43


def test_valid_json_object_is_returned_unchanged():
    payload = parse_message('{"type": "session.open", "n": 1, "nested": {"ok": [1, null, "є"]}}')
    assert payload == {"type": "session.open", "n": 1, "nested": {"ok": [1, None, "є"]}}


def test_missing_text_frame_is_rejected():
    assert error_of(parse_message, None).code == "invalid_message"


@pytest.mark.parametrize("raw", ["", "   ", "not json", "{", '{"a": }', "[1, 2]", '"text"', "42", "null", "true"])
def test_anything_but_a_json_object_is_rejected(raw):
    error = error_of(parse_message, raw)
    assert error.code == "invalid_message"
    assert error.request_id is None


def test_message_at_the_size_limit_is_accepted_and_one_byte_more_is_not():
    assert len(message_with_size(MAX_MESSAGE_BYTES).encode()) == MAX_MESSAGE_BYTES
    assert "a" in parse_message(message_with_size(MAX_MESSAGE_BYTES))
    assert error_of(parse_message, message_with_size(MAX_MESSAGE_BYTES + 1)).code == "message_too_long"


def test_size_limit_counts_bytes_not_characters():
    # 16_400 two-byte characters are well below 32 KiB as characters but above it as UTF-8.
    raw = '{"a":"' + "є" * 16_400 + '"}'
    assert len(raw) < MAX_MESSAGE_BYTES < len(raw.encode("utf-8"))
    assert error_of(parse_message, raw).code == "message_too_long"


# --- parse_session_token --------------------------------------------------------


def test_missing_token_means_a_new_session():
    assert parse_session_token(None) is None


def test_well_formed_token_is_accepted():
    assert parse_session_token(VALID_TOKEN) == VALID_TOKEN


@pytest.mark.parametrize(
    "token",
    [
        "",
        VALID_TOKEN[:-1],
        VALID_TOKEN + "a",
        VALID_TOKEN[:-1] + "!",
        VALID_TOKEN[:-1] + "є",
        VALID_TOKEN + "\n",
        " " + VALID_TOKEN[1:],
        123,
        True,
        [VALID_TOKEN],
        {"token": VALID_TOKEN},
    ],
)
def test_malformed_token_is_rejected(token):
    assert error_of(parse_session_token, token).code == "invalid_message"


# --- parse_question -------------------------------------------------------------


def question(**overrides):
    return {"type": "message.send", "requestId": "req_1", "text": "Що таке OKIL?", **overrides}


def test_valid_question_returns_request_id_and_untouched_text():
    assert parse_question(question(text="  Привіт  \n")) == ("req_1", "  Привіт  \n")


def test_request_id_is_accepted_up_to_the_limit():
    request_id = "r" * MAX_REQUEST_ID_LENGTH
    assert parse_question(question(requestId=request_id)) == (request_id, "Що таке OKIL?")


@pytest.mark.parametrize("request_id", [None, "", "   ", "r" * (MAX_REQUEST_ID_LENGTH + 1), 7, ["req_1"]])
def test_invalid_request_id_is_rejected_without_echoing_it(request_id):
    error = error_of(parse_question, question(requestId=request_id))
    assert error.code == "invalid_message"
    assert error.request_id is None


def test_missing_request_id_key_is_rejected():
    payload = question()
    del payload["requestId"]
    assert error_of(parse_question, payload).code == "invalid_message"


@pytest.mark.parametrize("message_type", [None, "", "message.sent", "session.open", 5])
def test_unexpected_type_is_rejected_and_echoes_the_valid_request_id(message_type):
    error = error_of(parse_question, question(type=message_type))
    assert error.code == "invalid_message"
    assert error.request_id == "req_1"


@pytest.mark.parametrize("text", [None, "", " ", "\n\t", 42, ["Привіт"]])
def test_empty_or_non_string_text_is_rejected_and_echoes_request_id(text):
    error = error_of(parse_question, question(text=text))
    assert error.code == "invalid_message"
    assert error.request_id == "req_1"


def test_text_limit_counts_characters_and_rejects_one_more():
    assert parse_question(question(text="є" * MAX_TEXT_LENGTH))[1] == "є" * MAX_TEXT_LENGTH
    error = error_of(parse_question, question(text="x" * (MAX_TEXT_LENGTH + 1)))
    assert error.code == "message_too_long"
    assert error.request_id == "req_1"


def test_protocol_error_exposes_code_message_and_request_id():
    error = ChatProtocolError("invalid_message", "Reason", "req_9")
    assert (error.code, str(error), error.request_id) == ("invalid_message", "Reason", "req_9")
    assert ChatProtocolError("invalid_message", "Reason").request_id is None
