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

### Added

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
