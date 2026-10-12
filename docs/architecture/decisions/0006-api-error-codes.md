# 0006. Return error codes, not prose, for new API errors

- **Status:** Proposed
- **Date:** 2026-10-12

## Context

The API uses DRF's default error responses: `{"detail": "..."}` or field dictionaries with English messages. The frontend shows these strings to users as they are, in a UI that is translated into Polish, English, German and Bulgarian. Some messages are even produced in Polish by the backend (realtime rejection causes in `data_manager/tasks.py`), so users of other languages see Polish. Text produced by the backend cannot be translated, reworded or kept consistent with the UI's terminology.

## Decision

- New domain errors are returned as a stable code plus data:

  ```json
  {"code": "feeds.static_source_url_and_file", "details": {"field": "url"}}
  ```

  Codes are `<area>.<snake_case_reason>`, never change once released, and are listed in `docs/api/`.
- Field validation keeps DRF's field dictionary shape, but each message is a code (raise `ValidationError` with `code=` set and expose the code), so the frontend can translate it.
- The frontend maps every code to a message in all four dictionaries and falls back to a generic translated message for unknown codes. It never displays backend text.
- Existing endpoints migrate gradually, one area per pull request, with an entry in [`docs/api/changelog.md`](../../api/changelog.md). Rejection causes stored in the database become codes too.

## Consequences

- The backend never produces user-facing text; wording lives only in the frontend's dictionaries.
- A custom DRF exception handler is needed to emit the `{code, details}` shape; it must keep DRF's existing shapes for endpoints that have not migrated yet.
- During the migration the frontend handles both shapes.

## Alternatives considered

- **Translate on the backend** (Django i18n with `Accept-Language`) — keeps wording in two places and needs four backend catalogs.
- **Keep English prose** — users of the Polish, German and Bulgarian UI keep seeing English or Polish backend text.
