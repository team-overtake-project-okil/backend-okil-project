import asyncio

import pytest

from app import ai_assistant
from app.ai_assistant import (
    GROQ_CHAT_COMPLETIONS_URL,
    MAX_COMPLETION_TOKENS,
    GroqClient,
    answer_intent,
    format_poi_answer,
    get_hex_information,
    get_poi_information,
    get_project_information,
)
from app.intent_classifier import Intent


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeDatabase:
    def __init__(self, results):
        self._results = iter(results)
        self.statements = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return next(self._results)


def test_get_hex_information_returns_score_and_explanations(monkeypatch):
    hexagon = {
        "h3_id": "8928308280bffff",
        "health": 80,
        "study": 60,
        "daily": 70,
        "mobility": 90,
        "sport": 50,
        "enjoyment": 65,
        "food": 75,
        "security": 85,
    }

    async def fetch_hexagon(db, h3_id):
        assert h3_id == hexagon["h3_id"]
        return hexagon

    async def fetch_explanations(db, received_hexagon, weights):
        assert received_hexagon is hexagon
        assert set(weights) == set(hexagon) - {"h3_id"}
        return {
            category: {"explanation": f"Explanation for {category}"}
            for category in weights
        }

    monkeypatch.setattr(ai_assistant, "fetch_hexagon", fetch_hexagon)
    monkeypatch.setattr(
        ai_assistant,
        "fetch_hexagon_explanations",
        fetch_explanations,
    )

    result = asyncio.run(get_hex_information(object(), hexagon["h3_id"]))

    assert result["h3_id"] == hexagon["h3_id"]
    assert 0 < result["overall_rating"] <= 100
    assert result["categories"][0]["explanation"] == "Explanation for health"


def test_get_hex_information_returns_none_when_hex_is_missing(monkeypatch):
    async def fetch_hexagon(db, h3_id):
        return None

    monkeypatch.setattr(ai_assistant, "fetch_hexagon", fetch_hexagon)

    assert asyncio.run(get_hex_information(object(), "8928308280bffff")) is None


def test_get_poi_information_reads_points_and_stops():
    db = FakeDatabase(
        [
            FakeResult([{"name": "Кафе", "type": "cafe", "category": "food"}]),
            FakeResult(
                [
                    {
                        "name": "Площа Ринок",
                        "type": "gtfs_stop",
                        "category": "mobility",
                    }
                ]
            ),
        ]
    )

    result = asyncio.run(get_poi_information(db, "8928308280bffff"))

    assert [item["name"] for item in result["objects"]] == [
        "Кафе",
        "Площа Ринок",
    ]
    assert result["objects"][0]["category_name"] == "Харчування"
    assert result["objects"][0]["type_name"] == "Кафе"
    assert result["truncated"] is False
    assert len(db.statements) == 2


def test_format_poi_answer_reports_no_stops_when_database_has_none():
    answer = format_poi_answer(
        {
            "h3_id": "891e76c6db3ffff",
            "objects": [
                {
                    "name": "Inside Dental Center",
                    "type": "dentistry",
                    "category": "health",
                    "category_name": "Здоров'я",
                },
                {
                    "name": "Аптека",
                    "type": "pharmacy",
                    "category": "daily",
                    "category_name": "Щоденні потреби",
                },
            ],
            "total_returned": 2,
            "truncated": False,
        }
    )

    assert "891e76c6db3ffff" in answer
    assert "Inside Dental Center" in answer
    assert answer.splitlines()[-1].endswith("немає.")
    assert "gtfs_stop" not in answer


def test_format_poi_answer_handles_empty_hex():
    answer = format_poi_answer(
        {
            "h3_id": "891e76c6db3ffff",
            "objects": [],
            "total_returned": 0,
            "truncated": False,
        }
    )

    assert answer == "У гексі 891e76c6db3ffff у базі не знайдено POI або зупинок."


def test_get_project_information_reads_category_point_type_catalog():
    db = FakeDatabase(
        [
            FakeResult(
                [
                    {
                        "type": "cafe",
                        "category": "food",
                        "radius_of_influence_1": 500.0,
                        "radius_of_influence_2": 1000.0,
                        "radius_of_influence_3": 1500.0,
                    }
                ]
            )
        ]
    )

    result = asyncio.run(get_project_information(db))

    assert result["point_types"][0]["type"] == "cafe"
    assert any(category["id"] == "food" for category in result["categories"])


def test_groq_sends_context_and_returns_answer(monkeypatch):
    requests = []

    class FakeResponse:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Ось відповідь."}}]}

    class FakeClient:
        def __init__(self, *, timeout):
            assert timeout == 60.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            requests.append((url, headers, json))
            return FakeResponse()

    monkeypatch.setattr(ai_assistant.httpx, "AsyncClient", FakeClient)
    groq = GroqClient("test-api-key", "test-model")

    answer = asyncio.run(
        groq.answer(
            "Поясни цей гекс",
            Intent.HEX_SCORE,
            {"h3_id": "8928308280bffff"},
        )
    )

    assert answer == "Ось відповідь."
    url, headers, body = requests[0]
    assert url == GROQ_CHAT_COMPLETIONS_URL
    assert headers["Authorization"] == "Bearer test-api-key"
    assert body["model"] == "test-model"
    assert body["max_completion_tokens"] == MAX_COMPLETION_TOKENS
    assert "8928308280bffff" in body["messages"][1]["content"]


def test_groq_prompt_limits_poi_answer_to_database_rows(monkeypatch):
    requests = []

    class FakeResponse:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Ось відповідь."}}]}

    class FakeClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            requests.append(json)
            return FakeResponse()

    monkeypatch.setattr(ai_assistant.httpx, "AsyncClient", FakeClient)
    groq = GroqClient("test-api-key", "test-model")

    asyncio.run(
        groq.answer(
            "Які зупинки тут є?",
            Intent.POI,
            {
                "h3_id": "891e76c6db3ffff",
                "objects": [
                    {"name": "Inside Dental Center", "type": "dentistry"}
                ],
            },
        )
    )

    user_message = requests[0]["messages"][1]["content"]
    assert "Не перелічуй типи, яких немає у списку" in user_message
    assert "891e76c6db3ffff" in user_message
    assert "Inside Dental Center" in user_message


def test_groq_reports_rate_limit_without_fallback_models(monkeypatch):
    requested_models = []

    class FakeResponse:
        status_code = 429
        text = "temporarily rate-limited"

        def raise_for_status(self):
            raise AssertionError("The client handles HTTP errors directly")

    class FakeClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            requested_models.append(json["model"])
            return FakeResponse()

    monkeypatch.setattr(ai_assistant.httpx, "AsyncClient", FakeClient)
    groq = GroqClient("test-api-key", "test-model")

    with pytest.raises(RuntimeError, match="Groq returned HTTP 429"):
        asyncio.run(
            groq.answer(
                "Поясни гекс",
                Intent.HEX_SCORE,
                {"h3_id": "8928308280bffff"},
            )
        )

    assert requested_models == ["test-model"]


def test_groq_does_not_return_incomplete_length_limited_answer(monkeypatch):
    requested_models = []

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, finish_reason, content):
            self._finish_reason = finish_reason
            self._content = content

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "finish_reason": self._finish_reason,
                        "message": {"content": self._content},
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            requested_models.append(json["model"])
            return FakeResponse("length", "Обрізана відповідь...")

    monkeypatch.setattr(ai_assistant.httpx, "AsyncClient", FakeClient)
    groq = GroqClient("test-api-key", "test-model")

    with pytest.raises(RuntimeError, match="output limit"):
        asyncio.run(
            groq.answer(
                "Поясни гекс",
                Intent.HEX_SCORE,
                {"h3_id": "8928308280bffff"},
            )
        )

    assert requested_models == ["test-model"]


def test_groq_requires_api_key():
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        GroqClient("")


def test_answer_intent_does_not_call_model_for_unknown_hex(monkeypatch):
    async def get_hex_information(db, h3_id):
        return None

    class UnusedGroq:
        async def answer(self, question, intent, context) -> str:
            pytest.fail("Groq must not be called when the hex is missing")

    monkeypatch.setattr(ai_assistant, "get_hex_information", get_hex_information)

    answer = asyncio.run(
        answer_intent(
            object(),
            UnusedGroq(),
            "Поясни оцінку",
            Intent.HEX_SCORE,
            "8928308280bffff",
        )
    )

    assert "не знайдено" in answer


def test_answer_intent_sends_database_pois_to_llm(monkeypatch):
    observed = {}

    async def get_poi_information(db, h3_id):
        assert h3_id == "891e76c6db3ffff"
        return {
            "h3_id": h3_id,
            "objects": [
                {
                    "name": "Inside Dental Center",
                    "type": "dentistry",
                    "category": "health",
                    "category_name": "Здоров'я",
                }
            ],
            "total_returned": 1,
            "truncated": False,
        }

    class FakeGroq:
        async def answer(self, question, intent, context):
            observed.update(question=question, intent=intent, context=context)
            return "Біля цього гекса є стоматологія Inside Dental Center."

    monkeypatch.setattr(ai_assistant, "get_poi_information", get_poi_information)

    answer = asyncio.run(
        answer_intent(
            object(),
            FakeGroq(),
            "Які місця у цьому гексі?",
            Intent.POI,
            "891e76c6db3ffff",
        )
    )

    assert answer == "Біля цього гекса є стоматологія Inside Dental Center."
    assert observed["intent"] is Intent.POI
    assert observed["context"]["h3_id"] == "891e76c6db3ffff"
    assert observed["context"]["objects"][0]["name"] == "Inside Dental Center"


def test_answer_intent_returns_empty_poi_result_without_calling_llm(monkeypatch):
    async def get_poi_information(db, h3_id):
        return {
            "h3_id": h3_id,
            "objects": [],
            "total_returned": 0,
            "truncated": False,
        }

    class UnusedGroq:
        async def answer(self, question, intent, context) -> str:
            pytest.fail("Do not ask the model to invent POIs for an empty cell")

    monkeypatch.setattr(ai_assistant, "get_poi_information", get_poi_information)

    answer = asyncio.run(
        answer_intent(
            object(),
            UnusedGroq(),
            "Які зупинки у цьому гексі?",
            Intent.POI,
            "891e76c6db3ffff",
        )
    )

    assert "не знайдено POI або зупинок" in answer
