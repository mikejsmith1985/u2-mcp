# Changelog

All notable changes to this fork are recorded here. This fork tracks
[bpamiri/u2-mcp](https://github.com/bpamiri/u2-mcp) and adds security and
reliability hardening. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Every entry marked **[proven]** has tests that fail against the upstream code and
pass here; run `python scripts/verify_hardening.py` to regenerate that evidence.

## [Unreleased]

### Security

- **The HTTP transport no longer serves an unauthenticated database to the
  network.** The legacy `--http` (SSE) transport performs no authentication at
  all, and defaulted to binding every interface with `U2_HTTP_CORS_ORIGINS='*'`
  and credentials enabled — so `u2-mcp --http` exposed `write_record`,
  `delete_record` and `execute_tcl` to anyone who could reach the port.
  `U2_HTTP_HOST` now defaults to `127.0.0.1`, `U2_HTTP_CORS_ORIGINS` defaults to
  empty (a literal `*` is refused), and the server refuses to start when
  unauthenticated on a reachable interface unless
  `U2_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS=true` is set deliberately. **These are
  the only behaviour-changing defaults in this fork.**

- **CI now fails on security findings.** `bandit` (medium severity and above) and
  `pip-audit` run before the build job, so the assurance renews itself rather
  than describing the day it was run.

- **Authorization codes can no longer be replayed** (defect introduced by this
  fork's own durable-storage change). Consuming a one-time code was a `SELECT`
  followed by a separate `DELETE`, replacing an atomic `dict.pop()`. Racing forty
  threads through a deliberately widened window let all forty exchange the same
  code. Codes and pending authorization state are now claimed atomically with
  `DELETE ... RETURNING`.

- **Identity resolution now fails closed.** A resolver error fell back to the
  unauthenticated local operator, which would let a caller launder their identity
  out of the audit trail. With no resolver installed the local operator is still
  the honest answer for stdio mode; with one installed, a failure to name the
  caller raises.

- **Files holding credentials are restricted to their owner** where the operating
  system enforces permissions.

- **Per-user connections are bounded** by `U2_MAX_CONNECTIONS` (default 25) on a
  least-recently-used basis, so a crowd of callers cannot exhaust the database's
  connection limit.

- **Credential map entries may name an environment variable** with `password_env`
  instead of storing a password on disk, and errors never echo the map's contents.

### Fixed

- **Refreshing a token no longer crashes, and no longer loses the user.**
  `exchange_refresh_token` read an attribute the SDK's `RefreshToken` does not
  have, so every refresh raised `AttributeError`; and the replacement tokens were
  stored with an empty `user_subject` under a comment claiming the identity was
  preserved. From the first refresh onward the audit trail would have recorded an
  empty user and per-user credentials could not resolve at all. Rotation now
  recovers the stored token, carries its subject, claims and resource across, and
  refuses a refresh token this server did not issue.

- **Type checking passes.** Upstream's CI ran `mypy src/` against 14 errors, so
  that step was already failing. Each was fixed rather than suppressed, which is
  how the two refresh defects above were found. `strict_equality`,
  `warn_unused_ignores` and `warn_redundant_casts` are now on.

- **Non-ASCII business data is no longer deleted from query output** **[proven]**
  `_sanitize_output()` kept only plain ASCII, so accented customer names
  (`MÜLLER`, `José Peña`) and currency symbols (`£`, `€`, `¥`) disappeared from
  results with no error. Output is now filtered by Unicode category, which keeps
  ordinary printable text in any alphabet while still stripping terminal control
  codes.

- **Auto-reconnect now actually reconnects** **[proven]**
  When a session went inactive, `get_session()` cleared the session and called
  `connect()` — which returned early because the connection record was still
  marked active — and then handed the caller `None`. Every subsequent request
  failed with `AttributeError: 'NoneType' object has no attribute ...`. The stale
  record is now discarded before reconnecting. This mattered most in stdio mode,
  the default for desktop clients, where no watchdog exists to clear the record.

- **Truncated results are now reported as truncated** **[proven]**
  `execute_query` appended a `SAMPLE` clause to `LIST` statements without marking
  the answer partial, so a capped result read exactly like a complete one. Both
  `execute_query` and `get_select_list` now return `is_complete`, plus a `warning`
  in plain language when a limit was applied. An explicit caller-supplied `SAMPLE`
  is respected rather than doubled.

- **A timed-out query no longer keeps running** **[proven]**
  The timeout raised an error but never stopped the work: the query continued on
  the server, its thread stayed alive, and the session was handed to the next
  request with a command still in flight. Since uopy cannot cancel a command, the
  session is now closed — which drops the socket and lets the server abandon the
  work — and the next request reconnects. Commands are also serialized per session,
  and abandoned queries are counted and exposed as `abandoned_query_count`.

- **The audit trail now names who acted** **[proven]**
  Records carried a session id generated at server start, not the authenticated
  user, so no record could say who ran a query. Every entry now carries the
  identity provider's subject, a readable name and email, the OAuth client that
  presented the token, whether the caller was authenticated at all, and the
  database login the work ran under -- including whether that login was shared.
  Passwords never reach the file.

- **Each caller can now reach the database as themselves** **[proven]**
  One connection served the whole process under one account, so Universe's own
  file and field security could not act on the real caller. `U2_IDENTITY_MODE`
  now selects `shared` (the previous behaviour, kept as the default, but logged
  and flagged in the audit trail) or `mapped`, where each authenticated person
  connects under their own Universe login from `U2_CREDENTIAL_MAP_PATH`. An
  unmapped caller is refused rather than silently falling back to the shared
  account.

### Added

- **Durable OAuth state** **[proven]**
  Registrations, tokens and in-flight logins lived only in process memory, so a
  restart signed everyone out and a second instance could not recognise the
  first one's tokens. `SQLiteAuthStorage` keeps the same behaviour in a file:
  set `U2_AUTH_STORAGE=sqlite` (path via `U2_AUTH_STORAGE_PATH`). Bearer tokens
  are stored as SHA-256 hashes, so a stolen database file yields no usable
  credentials. SQLite is in the standard library, so this adds no dependency.
  Both backends are held to one shared contract test suite.

- **Test doubles that match the code they double.** The previous mocks modelled an
  obsolete `uopy` API (`session.open()`, `cmd.exec()`, `session.transaction_start()`)
  that the server has not called for some time, which is why `tests/test_tools/`
  was empty. They now mirror the `uopy` 1.4 surface actually in use
  (`uopy.File(name, session=...)`, `uopy.Command(text, session=...)`, `cmd.run()`,
  `session.tx_start()`), so a passing test exercises the real code path.

- **Connection test suite** — 22 tests covering connect/reuse/disconnect, session
  recovery, file-handle caching, command execution, output sanitization, query
  timeouts and transaction state.

- **Tool-layer test suite.** `tests/test_tools/` was empty upstream; every tool
  module that touches business data now has tests, taking the tool package from
  0% to 65% statement coverage and the suite from 45 to 203 tests. The emphasis
  is on the safety guarantees an operator relies on: read-only mode genuinely
  leaves data untouched rather than only returning an error, writes and deletes
  require explicit confirmation, refused commands never reach the server,
  MultiValue structure survives a write-then-read round trip, and batch reads
  name what they could not find.

- **`scripts/verify_hardening.py`** — runs each fix's tests against the upstream
  commit and against this fork, and writes `evidence/hardening-evidence.md` with
  the raw pytest output for both sides.
