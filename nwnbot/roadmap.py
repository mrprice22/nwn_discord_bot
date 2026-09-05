"""Async HTTP client for the roadmap editor's API.

Filled in by ``[b3-roadmap-client]``. Will hold ``RoadmapClient`` with
``login``, ``fetch``, ``save``, ``comment`` and ``new_idea`` over ``aiohttp``.

Facts the implementation must respect (verified, see plan.md):

- ``GET /api/data`` returns ``{ideas, vocab, environments, base_hashes,
  base_vocab, version, me}``.
- ``POST /api/save`` takes the whole ideas array plus ``base_version`` and the
  server's own ``base_hashes`` echoed back verbatim — never fingerprint
  locally. On conflict: re-fetch and retry once, then queue for review rather
  than forcing.
- ``POST /api/idea-comment`` appends to the item's internal append-only
  ``comments`` list; author and date are stamped server-side.
- Auth is username + password -> ``roadmap_session`` cookie; every request
  sends ``Content-Type: application/json``.

The hard rules (never write ``status: awarded|implemented|manual``, never
write ``merit_awarded``, never touch ``meta``/``groups``/``players``/``epics``/
``redemption``/``housing``, never add a name to ``players:``) are to be encoded
as assertions here, not as comments.
"""

# Endpoint paths, so callers do not spell them by hand.
API_DATA = "/api/data"
API_SAVE = "/api/save"
API_IDEA_COMMENT = "/api/idea-comment"
API_LOGIN = "/login"

SESSION_COOKIE = "roadmap_session"
JSON_CONTENT_TYPE = "application/json"

__all__ = [
    "API_DATA",
    "API_IDEA_COMMENT",
    "API_LOGIN",
    "API_SAVE",
    "JSON_CONTENT_TYPE",
    "SESSION_COOKIE",
]
