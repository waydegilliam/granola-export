import json

import httpx
import pytest

from granola_export.client import GranolaClient
from granola_export.errors import ApiError, AuthError, ShapeError


def test_pagination_follows_offsets_and_preserves_shared():
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        offset = body["offset"]
        docs = [{"id": str(i)} for i in range(offset, min(offset + 2, 5))]
        return httpx.Response(200, json={"docs": docs, "shared": [{"id": "shared"}]})

    with GranolaClient("secret", transport=httpx.MockTransport(handle), delay=0) as client:
        pages = list(client.pages("/v2/get-documents", "docs", page_size=2))
    assert [request["offset"] for request in requests] == [0, 2, 4]
    assert sum(len(page["docs"]) for page in pages) == 5
    assert pages[0]["shared"] == [{"id": "shared"}]


def test_cursor_takes_precedence_over_short_page():
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "cursor" not in body:
            return httpx.Response(200, json={"docs": [{"id": "a"}], "next_cursor": "next"})
        return httpx.Response(200, json={"docs": [{"id": "b"}], "has_more": False})

    with GranolaClient("secret", transport=httpx.MockTransport(handle), delay=0) as client:
        assert len(list(client.pages("/v2/get-documents", "docs"))) == 2
    assert bodies[1]["cursor"] == "next"


@pytest.mark.parametrize(
    "response",
    [
        {"docs": [{"id": "a"}], "has_more": True},
        {"docs": [{"id": "a"}], "next_cursor": "same"},
        {"docs": [], "has_more": True},
        {"docs": [{"id": "a"}], "next_offset": 0},
    ],
)
def test_broken_pagination_is_not_silently_complete(response):
    with (
        GranolaClient(
            "secret",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
            delay=0,
        ) as client,
        pytest.raises(ShapeError),
    ):
        list(client.pages("/v2/get-documents", "docs"))


def test_retry_rate_limits_and_transient_errors():
    replies = iter(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(503),
            httpx.Response(200, json=[]),
        ]
    )
    sleeps = []
    with GranolaClient(
        "secret",
        transport=httpx.MockTransport(lambda _: next(replies)),
        sleep=sleeps.append,
        delay=0,
    ) as client:
        assert client.post("/v1/get-document-transcript") == []
    assert sleeps == [0, 3, 0, 2, 0]


@pytest.mark.parametrize("status,exception", [(401, AuthError), (403, ApiError), (302, ApiError)])
def test_auth_and_permission_errors_do_not_retry_or_leak(status, exception):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="sensitive-server-response",
            headers={
                "Location": "https://attacker.invalid",
            },
        )

    with GranolaClient("secret", transport=httpx.MockTransport(handle), delay=0) as client:
        with pytest.raises(exception) as error:
            client.post("/v1/get-document-transcript")
        with pytest.raises(ValueError):
            client.post("/capture", base="https://attacker.invalid")
    assert len(calls) == 1
    assert "secret" not in str(error.value)
    assert "sensitive-server-response" not in str(error.value)


def test_error_payload_is_rejected():
    with (
        GranolaClient(
            "secret",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"error": "x"})),
            delay=0,
        ) as client,
        pytest.raises(ApiError),
    ):
        client.post("/v2/get-documents")
