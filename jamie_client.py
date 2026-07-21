"""
Jamie meeting API client (READ-ONLY) for the customer portal.

Pulls a customer's meetings and AI meeting notes from Jamie. Only ever issues
GET requests, so this app can never modify or delete anything in Jamie.

A customer is linked to a Jamie "tag" (the tag NAME, e.g. "DMU"). meetings.list
accepts a `tag` filter by name, which returns exactly that customer's meetings.

The API key is read from the JAMIE_API_KEY environment variable, with a local
fallback so it runs out-of-the-box on this machine. NOTE: rotate the fallback key
and set JAMIE_API_KEY properly before transferring this software to anyone else.
"""

import json
import os

import requests

BASE_URL = os.environ.get("JAMIE_BASE_URL", "https://beta-api.meetjamie.ai")
API_KEY = os.environ.get(
    "JAMIE_API_KEY",
    "jk_9f3149457743d7f0ef523d2ed584043f67b04ea845bad7e0a5c0a2b5d32577a6",
)


class JamieError(Exception):
    pass


def is_configured():
    return bool(API_KEY)


def jamie_get(path, trpc_input=None, params=None, timeout=30):
    """Issue a READ-ONLY GET request to Jamie and unwrap the tRPC envelope."""
    url = f"{BASE_URL}{path}"
    headers = {"x-api-key": API_KEY, "accept": "application/json"}
    query = dict(params or {})
    if trpc_input is not None:
        query["input"] = json.dumps({"json": trpc_input})
    try:
        resp = requests.get(url, headers=headers, params=query, timeout=timeout)
    except requests.RequestException as e:
        raise JamieError(f"Could not reach Jamie: {e}")
    try:
        payload = resp.json()
    except ValueError:
        raise JamieError(f"Non-JSON response ({resp.status_code}) from {path}")
    if isinstance(payload, dict) and payload.get("error"):
        msg = payload["error"].get("json", {}).get("message", "Unknown Jamie API error")
        raise JamieError(f"Jamie API error on {path}: {msg}")
    try:
        return payload["result"]["data"]["json"]
    except (KeyError, TypeError):
        raise JamieError(f"Unexpected response shape from {path}: {str(payload)[:200]}")


def fetch_tags():
    """List all Jamie tags (customers): [{id, name, shared}]."""
    return jamie_get("/v1/me/tags.list").get("tags", [])


def fetch_meetings(tag, limit=50):
    """Meetings for one customer tag (by NAME). Returns list of meeting-list items
    (id, title, startTime, ...). Server-side filtered by Jamie's real tag link."""
    if not tag:
        return []
    meetings, cursor, guard = [], None, 0
    while len(meetings) < limit and guard < 20:
        guard += 1
        inp = {"limit": min(50, limit - len(meetings)), "tag": tag}
        if cursor:
            inp["cursor"] = cursor
        data = jamie_get("/v1/me/meetings.list", trpc_input=inp)
        batch = data.get("meetings", [])
        if not batch:
            break
        meetings.extend(batch)
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return meetings[:limit]


def fetch_meeting_detail(meeting_id):
    """Full meeting incl. summary{markdown,html,short}, tags[], tasks[]."""
    return jamie_get("/v1/me/meetings.get", trpc_input={"meetingId": meeting_id})
