import json

import httpx
import pytest

from grantrunner.browser import draft_answer


async def test_optional_drafting_sends_grounded_context_to_fixed_provider(monkeypatch):
    monkeypatch.setenv("GRANTRUNNER_LLM_API_KEY", "unit-test-only-not-a-real-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"answer": "We teach introductory robotics."})}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    field = {"type": "textarea", "label": "Describe your community work", "maxlength": 1000}
    answer = await draft_answer(field, {"facts": {"outreach": "We teach introductory robotics."}})
    assert answer == "We teach introductory robotics."
    assert str(requests[0].url) == "https://api.openai.com/v1/chat/completions"
    messages = json.loads(requests[0].content)["messages"]
    assert "Never invent" in messages[0]["content"]
    assert json.loads(messages[1]["content"])["facts"]["outreach"] == answer


async def test_drafting_is_optional_and_does_not_guess_non_narrative_fields(monkeypatch):
    monkeypatch.delenv("GRANTRUNNER_LLM_API_KEY", raising=False)
    assert await draft_answer({"type": "textarea"}, {"facts": {"mission": "Robotics"}}) is None
    monkeypatch.setenv("GRANTRUNNER_LLM_API_KEY", "unit-test-only-not-a-real-key")
    assert await draft_answer({"type": "number"}, {"facts": {"mission": "Robotics"}}) is None
