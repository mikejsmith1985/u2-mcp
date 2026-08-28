# Hardening this fork

This fork exists to make u2-mcp safe to point at a production MultiValue
database. Upstream is a capable and well-documented project; what it lacks is the
identity, attribution and durability guarantees an operator needs before letting
an AI assistant near real business data.

This document records what was wrong, how it was found, and how each fix is
proven. It is a working record, updated as the work proceeds — not a summary
written afterwards.

## Method

Three rules govern the work.

**Find defects with tests, not with reading.** Every fix starts as a test that
fails against the unfixed code. That test is the specification of the defect, and
later the proof it is gone.

**Prove every claim against the original.** `scripts/verify_hardening.py` checks
out the upstream commit this fork started from, copies the current test suite over
it, and runs each fix's tests on both sides. A fix counts as proven only when its
tests fail upstream and pass here. The raw pytest output for both runs is kept in
`evidence/raw/`.

**Change behaviour, not architecture.** Fixes stay inside the shapes upstream
already chose, so this fork remains mergeable rather than becoming a rewrite.

## Finding 0 — the test doubles had drifted from the code

`tests/test_tools/` was empty, and the reason turned out to be structural rather
than a matter of effort. The mock `uopy` layer modelled an API the server no
longer calls:

| The mocks provided | The code actually calls |
| --- | --- |
| `session.open(name)` | `uopy.File(name, session=...)` |
| `session.command()` then `cmd.exec()` | `uopy.Command(text, session=...)` then `cmd.run()` |
| `session.transaction_start()` | `session.tx_start()` |
| `session.disconnect()` | `session.close()` |

Any test written against those doubles would have exercised a fiction. Rebuilding
them against the real `uopy` 1.4 surface was therefore the first change, and it
immediately surfaced Finding 2 below — a defect that reading the code had not
revealed.

## Finding 1 — non-ASCII business data was silently deleted

**Where:** `ConnectionManager._sanitize_output()`

**What happened:** after converting MultiValue delimiters to readable separators,
the sanitizer kept only characters in the range 32–126. Every other character was
dropped without comment.

**What it cost:** `MÜLLER GmbH` was returned as `MLLER GmbH`. `£1,250.00` was
returned as `1,250.00`. No error, no warning, no indication that the answer had
been altered — which makes it worse than a failure, because an operator cannot
tell a corrupted result from a correct one. For any distributor with European or
Latin American trading partners this corrupts names, addresses and amounts.

**The fix:** filter by Unicode category instead of by code point. Newlines and
tabs are kept, the control, format, surrogate, private-use and unassigned
categories are dropped, and every ordinary printable character survives whatever
alphabet it belongs to. Terminal control codes are still stripped.

**Proof:** `tests/test_connection.py::TestOutputSanitization` — accented names,
currency symbols, delimiter conversion and control-code removal are each asserted
separately.

## Finding 2 — auto-reconnect never reconnected

**Where:** `ConnectionManager.get_session()`

**What happened:** on detecting a dead session the method set `self._session` to
`None`, cleared the file cache, and called `connect()`. But `connect()` begins by
returning early if a connection record for that name exists and is still marked
active — and nothing had cleared that record. So no reconnect occurred, and
`get_session()` returned `None` to its caller.

**What it cost:** every request after a dropped connection failed with
`AttributeError: 'NoneType' object has no attribute ...`, and kept failing. The
watchdog masks this in HTTP mode because `force_disconnect()` clears the
connection records — but the watchdog only runs in HTTP modes. In stdio mode, the
default for desktop clients, a single network blip left the server permanently
broken until someone restarted it.

**The fix:** a `_discard_connection()` helper drops the session, the file cache,
the connection record and any transaction state before reconnecting, so the
subsequent `connect()` genuinely opens a new session. `get_session()` now raises a
`ConnectionError` rather than returning `None` if a session still cannot be
established.

**Proof:**
`tests/test_connection.py::TestConnectionLifecycle::test_get_session_reconnects_when_session_went_inactive`

## Finding 3 — a truncated answer was presented as a complete one

**Where:** `tools/query.py::execute_query` and `::get_select_list`

**What happened:** `execute_query` appended `SAMPLE <n>` to any `LIST` that did not
already have one, capping results at `U2_MAX_RECORDS`. The response said which
limit had been applied, but nothing marked the answer as partial — and an AI
client reading that response has no reason to treat it as incomplete.

**What it cost:** "list the open invoices over $1,000" could return the first
hundred and read exactly like all of them. `get_select_list` had the same shape:
it set a `truncated` flag but offered nothing an operator would notice. A wrong
answer that looks right is the failure mode that actually harms a business.

**The fix:** both tools now return `is_complete`, and when the answer was capped
they carry a `warning` in plain language naming the limit and how to raise it. A
`COUNT` is marked complete, because nothing was truncated. An explicit `SAMPLE`
supplied by the caller is still respected rather than doubled.

**Proof:** `tests/test_tools/test_query.py::TestResultCompleteness`

## Finding 4 — a timed-out query kept running

**Where:** `ConnectionManager.execute_command()`

**What happened:** the command ran on a daemon thread, and the caller waited on an
event with a timeout. When the timeout fired, a `TimeoutError` was raised — and
that was all. The thread stayed alive, the query kept running on the server, and
the session it was using was handed straight to the next request.

**What it cost:** three compounding problems. The database kept doing work nobody
was waiting for. Abandoned threads accumulated under load. And the next request
reused a session with an in-flight command on it, so replies could interleave.

**The fix:** uopy exposes no way to cancel a command in flight, so the honest
remedy is to close the session the query is running on. The socket drops, the
server abandons the command, and the reconnect repaired in Finding 2 makes the
next request safe. Commands are now also serialized with a lock, because a uopy
session is a single conversation with the server. Abandoned queries are counted and
exposed as `abandoned_query_count`, so a rising number is visible rather than silent.

**Proof:** `tests/test_connection.py::TestQueryTimeouts` and
`::TestConcurrentCommands`

## Still to come

- Per-user database identity, so the database's own security sees the real caller
- Audit records that name the authenticated user rather than a per-process session id
- Durable OAuth state, so a restart does not sign everyone out
- Test coverage for the remaining tool modules
