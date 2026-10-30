"""Tests for the Notion export, with the HTTP layer faked."""
import io
import json
import urllib.error

import pytest

from meeting_notes import notion_export
from meeting_notes.notion_export import (
    MAX_TEXT_LENGTH,
    NotionError,
    create_page,
    main,
    markdown_to_blocks,
    parse_page_id,
)

PAGE_ID = "0123456789abcdef0123456789abcdef"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code, body=None, headers=None):
    return urllib.error.HTTPError(
        "https://api.notion.com", code, "error", headers or {},
        io.BytesIO(json.dumps(body or {}).encode()),
    )


@pytest.fixture
def api(monkeypatch):
    """Record requests and replay queued results (default: success)."""
    class Api:
        requests = []
        results = []

    Api.requests = []
    Api.results = []

    def fake_urlopen(request, timeout):
        Api.requests.append(request)
        if Api.results:
            result = Api.results.pop(0)
            if isinstance(result, Exception):
                raise result
        return FakeResponse(json.dumps({"id": "new-page", "url": "https://notion.so/new"}).encode())

    monkeypatch.setattr(notion_export.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notion_export.time, "sleep", lambda seconds: None)
    return Api


@pytest.mark.parametrize("value", [
    PAGE_ID,
    "01234567-89ab-cdef-0123-456789abcdef",
    f"https://www.notion.so/team/Meeting-Notes-{PAGE_ID}?pvs=4",
])
def test_parse_page_id(value):
    assert parse_page_id(value) == PAGE_ID


def test_parse_page_id_rejects_invalid():
    with pytest.raises(NotionError):
        parse_page_id("not-a-page")


def test_markdown_to_blocks_maps_note_sections():
    blocks = markdown_to_blocks(
        "---\ndate: 2026-10-30\n---\n\n"
        "## Summary\n- a point\n\n- [ ] open task\n- [x] done task\n1. first\nplain text\n---\n"
    )
    assert [block["type"] for block in blocks] == [
        "heading_2", "bulleted_list_item", "to_do", "to_do",
        "numbered_list_item", "paragraph", "divider",
    ]
    assert blocks[2]["to_do"]["checked"] is False
    assert blocks[3]["to_do"]["checked"] is True
    assert blocks[0]["heading_2"]["rich_text"][0]["text"]["content"] == "Summary"


def test_long_transcript_line_is_split_within_limits():
    line = "word " * 5000
    blocks = markdown_to_blocks(line)
    assert len(blocks) > 1
    pieces = [block["paragraph"]["rich_text"][0]["text"]["content"] for block in blocks]
    assert all(len(piece) <= MAX_TEXT_LENGTH for piece in pieces)
    assert " ".join(pieces).split() == line.split()


def test_create_page_sends_title_auth_and_parent(api):
    assert create_page("secret", PAGE_ID, "Title", "text") == "https://notion.so/new"
    request = api.requests[0]
    body = json.loads(request.data)
    assert request.full_url.endswith("/pages")
    assert request.get_header("Authorization") == "Bearer secret"
    assert request.get_header("Notion-version") == notion_export.NOTION_VERSION
    assert body["parent"] == {"page_id": PAGE_ID}
    assert body["properties"]["title"]["title"][0]["text"]["content"] == "Title"


def test_create_page_appends_blocks_past_the_request_limit(api):
    create_page("secret", PAGE_ID, "Title", "\n".join(f"line {i}" for i in range(250)))
    assert [request.get_method() for request in api.requests] == ["POST", "PATCH", "PATCH"]
    assert [len(json.loads(request.data)["children"]) for request in api.requests] == [100, 100, 50]
    assert api.requests[1].full_url.endswith("/blocks/new-page/children")


def test_rate_limit_is_retried_with_retry_after(api, monkeypatch):
    delays = []
    monkeypatch.setattr(notion_export.time, "sleep", delays.append)
    api.results = [http_error(429, headers={"Retry-After": "7"})]
    create_page("secret", PAGE_ID, "Title", "text")
    assert len(api.requests) == 2
    assert 7 <= delays[0] < 7.5


@pytest.mark.parametrize("code", [400, 500, 503])
def test_other_errors_are_not_retried(api, code):
    api.results = [http_error(code, {"message": "nope"})]
    with pytest.raises(NotionError, match="nope"):
        create_page("secret", PAGE_ID, "Title", "text")
    assert len(api.requests) == 1


def test_not_found_explains_page_sharing(api):
    api.results = [http_error(404, {"message": "Could not find page."})]
    with pytest.raises(NotionError, match="shared with your connection"):
        create_page("secret", PAGE_ID, "Title", "text")


def test_failed_append_reports_the_incomplete_page(api):
    api.results = [None, http_error(400, {"message": "bad block"})]
    with pytest.raises(NotionError, match="incomplete: https://notion.so/new"):
        create_page("secret", PAGE_ID, "Title", "\n".join(f"line {i}" for i in range(150)))


def test_main_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert main(["--title", "T", "--parent", PAGE_ID]) == 1
    assert "NOTION_TOKEN" in capsys.readouterr().err


def test_main_prints_url_and_hides_token_on_failure(api, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_private")
    monkeypatch.setattr("sys.stdin", io.StringIO("text"))
    assert main(["--title", "T", "--parent", PAGE_ID]) == 0
    assert capsys.readouterr().out.strip() == "https://notion.so/new"

    api.results = [http_error(401, {"message": "API token is invalid."})]
    monkeypatch.setattr("sys.stdin", io.StringIO("text"))
    assert main(["--title", "T", "--parent", PAGE_ID]) == 1
    assert "ntn_private" not in "".join(capsys.readouterr())
