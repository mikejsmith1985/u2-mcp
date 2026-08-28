# Changelog

All notable changes to this fork are recorded here. This fork tracks
[bpamiri/u2-mcp](https://github.com/bpamiri/u2-mcp) and adds security and
reliability hardening. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Every entry marked **[proven]** has tests that fail against the upstream code and
pass here; run `python scripts/verify_hardening.py` to regenerate that evidence.

## [Unreleased]

### Fixed

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

- **Connection test suite** — 17 tests covering connect/reuse/disconnect, session
  recovery, file-handle caching, command execution, output sanitization and
  transaction state.

- **`scripts/verify_hardening.py`** — runs each fix's tests against the upstream
  commit and against this fork, and writes `evidence/hardening-evidence.md` with
  the raw pytest output for both sides.
