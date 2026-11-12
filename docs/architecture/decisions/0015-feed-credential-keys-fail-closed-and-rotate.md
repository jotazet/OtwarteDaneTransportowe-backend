# 0015. Fail closed on feed credential keys and rotate them with MultiFernet

- **Status:** Proposed
- **Date:** 2026-11-12

## Context

Providers may give ODT credentials (API key, bearer token, basic auth) for their feed URLs.
`EncryptedCharField` (`data_manager/fields.py`) stores them encrypted with the Fernet key
`FEED_AUTH_ENCRYPTION_KEY` and hands plaintext to the fetchers. The field used to degrade
quietly: without a key it stored plaintext, a value it could not decrypt was handed on as
it was stored, input that already looked encrypted was stored as is, and with a single key
the key could never change.

## Decision

- `settings_prod` refuses to start without a valid Fernet key. Development without a key
  keeps storing plaintext and logs a warning once.
- Everything the application assigns is encrypted; only values read from the database carry
  the `fernet$` prefix.
- `FEED_AUTH_ENCRYPTION_KEY` encrypts, it and the optional `FEED_AUTH_ENCRYPTION_OLD_KEYS`
  decrypt (`MultiFernet`). `manage.py rotate_feed_auth_key` re-encrypts every row with the
  current key. The release check accepts a new key only when the previous one is in
  `FEED_AUTH_ENCRYPTION_OLD_KEYS`.
- A stored value no key decrypts is read as an `UndecryptableValue`, not as text, and an
  error is logged once without the value. Building the request headers refuses it
  (`CredentialUndecryptable`), so the fetch fails with `auth_error` /
  `fetch.credential_undecryptable`; GTFS validation records
  `gtfs_validation.credential_undecryptable` and realtime verification stays at stage 2.
  Saving the row again keeps the stored token, so the credential comes back once the right
  key is configured.

## Consequences

- A key mistake shows up as coded fetch errors on the affected feeds, not as a broken
  catalogue: rows still load, and every page that does not use the credential works.
- Every reader of `auth_value` must go through `_build_auth_headers` (the scheduler and the
  validation tasks do); a new reader must handle `UndecryptableValue` too. Code that keeps
  the stored credential on an edit passes it through unchanged (the admin form, and the
  realtime endpoint serializer's `_CredentialField`).
- Backups taken before a rotation need the old key; it stays in the vault until they expire.

## Alternatives considered

- **Raise in `from_db_value`** — fails closed, but one bad row would make every query that
  loads it fail: lists, the admin, the catalogue and the planner configuration.
- **Return `None` or an empty string** — the fetch would go out without credentials, and a
  later save would erase the stored token.
- **A data migration that encrypts existing plaintext rows** — the rotate command does it on
  demand, with no migration that depends on a secret.
