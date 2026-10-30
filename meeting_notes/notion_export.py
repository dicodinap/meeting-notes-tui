"""Export a processed note to Notion as a new page.

Standard library only, so the optional pipeline adds no dependency. The token
comes from the NOTION_TOKEN environment variable and is never accepted as an
argument, which would expose it in the process list.
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
# The API allows 2000 characters. Keep a margin because Notion and Python can
# count characters such as emoji differently.
MAX_TEXT_LENGTH = 1900
MAX_ITEMS = 100  # Blocks per request, and rich text items per block
MAX_ATTEMPTS = 6

PAGE_ID_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}")
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
TODO_RE = re.compile(r"^[-*] \[([ xX])\] (.*)$")
BULLET_RE = re.compile(r"^[-*] (.*)$")
NUMBERED_RE = re.compile(r"^\d+\. (.*)$")
HEADING_RE = re.compile(r"^(#{1,3}) (.*)$")


class NotionError(Exception):
    """A Notion request failed."""


def parse_page_id(value: str) -> str:
    """Return the page ID from a raw ID or a page URL."""
    matches = PAGE_ID_RE.findall(value.split("?")[0].lower())
    if not matches:
        raise NotionError(f"No Notion page ID found in: {value}")
    return matches[-1].replace("-", "")


def split_text(text: str) -> List[str]:
    """Split text at spaces into pieces Notion accepts."""
    chunks = []
    while len(text) > MAX_TEXT_LENGTH:
        cut = text.rfind(" ", 0, MAX_TEXT_LENGTH)
        if cut <= 0:
            cut = MAX_TEXT_LENGTH
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def _rich_text(text: str) -> List[Dict[str, Any]]:
    return [{"type": "text", "text": {"content": chunk}} for chunk in split_text(text)[:MAX_ITEMS]]


def _block(block_type: str, text: str, **extra: Any) -> Dict[str, Any]:
    return {"object": "block", "type": block_type, block_type: {"rich_text": _rich_text(text), **extra}}


def markdown_to_blocks(markdown: str) -> List[Dict[str, Any]]:
    """Convert the note's markdown to Notion blocks, line by line."""
    blocks: List[Dict[str, Any]] = []
    for line in FRONTMATTER_RE.sub("", markdown).splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif match := HEADING_RE.match(line):
            blocks.append(_block(f"heading_{len(match.group(1))}", match.group(2)))
        elif match := TODO_RE.match(line):
            blocks.append(_block("to_do", match.group(2), checked=match.group(1) != " "))
        elif match := BULLET_RE.match(line):
            blocks.append(_block("bulleted_list_item", match.group(1)))
        elif match := NUMBERED_RE.match(line):
            blocks.append(_block("numbered_list_item", match.group(1)))
        else:
            # A verbatim transcript can be one huge line, so split across blocks
            blocks.extend(_block("paragraph", chunk) for chunk in split_text(line))
    return blocks


def _error_detail(error: urllib.error.HTTPError) -> str:
    try:
        message = json.loads(error.read()).get("message", "")
    except (ValueError, AttributeError):
        message = ""
    if error.code == 404:
        message += " Check that the parent page is shared with your connection."
    return message.strip()


def _request(method: str, path: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        },
    )
    # Only 429 and 529 are retried. Repeating a write after any other error
    # could create the page twice.
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 529) or attempt == MAX_ATTEMPTS - 1:
                raise NotionError(f"Notion returned {error.code}: {_error_detail(error)}") from error
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
            time.sleep(delay + random.uniform(0, 0.25))
        except urllib.error.URLError as error:
            raise NotionError(f"Could not reach Notion: {error.reason}") from error
    raise NotionError("Notion request failed")


def create_page(token: str, parent_page_id: str, title: str, markdown: str) -> str:
    """Create a page under the parent and return its URL."""
    blocks = markdown_to_blocks(markdown)
    page = _request("POST", "/pages", token, {
        "parent": {"page_id": parent_page_id},
        "properties": {"title": {"title": _rich_text(title)}},
        "children": blocks[:MAX_ITEMS],
    })
    url = page.get("url", page["id"])
    # Page creation takes 100 blocks at most, so the rest are appended
    for start in range(MAX_ITEMS, len(blocks), MAX_ITEMS):
        try:
            _request("PATCH", f"/blocks/{page['id']}/children", token, {
                "children": blocks[start:start + MAX_ITEMS],
            })
        except NotionError as error:
            raise NotionError(f"{error} The page is incomplete: {url}") from error
    return url


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create a Notion page from markdown on stdin.")
    parser.add_argument("--title", required=True)
    parser.add_argument("--parent", required=True, help="Parent page URL or ID")
    args = parser.parse_args(argv)

    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        print("Error: NOTION_TOKEN is not set.", file=sys.stderr)
        return 1
    try:
        print(create_page(token, parse_page_id(args.parent), args.title, sys.stdin.read()))
    except NotionError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
