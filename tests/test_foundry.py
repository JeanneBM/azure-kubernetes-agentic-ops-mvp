import json

import httpx
import pytest

from agentic_ops import Facts
from agentic_ops.foundry import FoundryDiagnosticProvider
from conftest import NEW, make_facts, make_trigger


def provider(reply):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    class Evidence:
        def collect(self, trigger):
            return make_facts(None, params=None, groundedness=1.0, summary="")

    p = FoundryDiagnosticProvider(
        Evidence(), endpoint="https://f.openai.azure.com/", deployment="gpt",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_provider=lambda: "tok",
    )
    return p, seen


def test_valid_reply_becomes_facts_and_keeps_cluster_read_pull_failures():
    reply = json.dumps({"groundedness": 0.93, "summary": "typo",
                        "safe_action": {"name": "fix_image", "parameters": {"image": NEW}}})
    p, seen = provider(reply)
    facts = p.collect(make_trigger())
    assert facts.groundedness == 0.93 and facts.summary == "typo"
    assert facts.safe_action.parameters["image"] == NEW
    assert facts.pull_failures  # came from the AKS provider, not the model
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["temperature"] == 0


def test_null_action_is_allowed():
    p, _ = provider(json.dumps({"groundedness": 0.9, "summary": "x", "safe_action": None}))
    assert p.collect(make_trigger()).safe_action is None


@pytest.mark.parametrize("reply", [
    "not json",
    json.dumps({"summary": "no groundedness"}),
    json.dumps({"groundedness": 2, "safe_action": None}),
    json.dumps({"groundedness": 0.9, "safe_action": {"name": "scale", "parameters": {"replicas": "0"}}}),
    json.dumps({"groundedness": 0.9, "safe_action": {"name": "fix_image", "parameters": "oops"}}),
    json.dumps({"groundedness": 0.9, "safe_action": "fix_image"}),
])
def test_contract_violations_raise_value_error(reply):
    p, _ = provider(reply)
    with pytest.raises(ValueError):
        p.collect(make_trigger())
