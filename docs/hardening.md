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

## Finding 5 — authentication state did not survive a restart

**Where:** `auth/storage.py::InMemoryAuthStorage`

**What happened:** every registered client, issued token and in-flight login was
held in dictionaries on the process. The class documents this honestly, but the
consequences are heavier than they first appear.

**What it cost:** three things. A restart -- a deploy, a crash, a patch window --
signed every user out and forced them back through the identity provider. The
service could not be made redundant, because a second instance behind a load
balancer would reject tokens the first had issued. And there was no way to revoke
a token except by restarting, which revoked everyone's.

**The fix:** `SQLiteAuthStorage` implements the same surface against a file.
SQLite is in the standard library, so no dependency was added, and it covers the
common case of several workers on one host. Two deliberate choices go beyond
merely persisting:

- **Bearer tokens are stored as SHA-256 hashes, never in the clear.** The lookup
  is by hash, and the raw value is put back on the returned object for the caller.
  A stolen database file therefore yields no usable credentials -- the same reason
  passwords are not stored in the clear. Upstream held raw tokens in memory, which
  was survivable; writing them to disk would not have been.
- **Both backends answer to one contract test suite**, run twice over a fixture
  parameter, so choosing a backend cannot change how authentication behaves.

The in-memory store remains the default and now logs a warning naming what it
gives up. For deployments spread across hosts, this same interface is the seam a
Redis or Postgres backend would implement.

**Proof:** `tests/test_auth/test_storage.py` -- 35 tests, including a restart
simulated with a second instance on the same file, and an assertion that the raw
token bytes never appear in the database.

## Finding 6 — the audit trail could not name who acted

**Where:** `utils/audit.py::AuditLogger.log_tool_call()`

**What happened:** every record carried `session_id`, generated when the server
process started. It identified the *process*, not the person.

**What it cost:** the server went to real trouble to authenticate people through
Duo, Auth0 or Okta, and then discarded the answer. Asked "who ran that query
against the customer file on Tuesday?", the log could say only that a query was
run. That is a debugging aid, not an audit trail, and it is the gap that matters
first in a regulated environment.

**The fix:** a caller identity now travels with the request, and every audit
record carries the identity provider's subject, a readable name and email, the
OAuth client that presented the token, whether the request was authenticated at
all, and the database login the work actually ran under. Local stdio use is
recorded honestly as unauthenticated rather than left ambiguous. Passwords are
still never written: the login is recorded as `USER@ACCOUNT`, never with its
password, and existing parameter redaction is covered by tests.

**Proof:** `tests/test_utils/test_audit.py`

## Finding 7 — every caller reached the database as the same account

**Where:** `server.py::get_connection_manager()` and `ConnectionManager`

**What happened:** a single module-level `ConnectionManager` served the whole
process, built from the credentials in the server's own environment. Whoever was
asking, the query arrived at Universe under one account.

**What it cost:** this is the finding that decides whether the server can go near
regulated data. Universe has its own file and field security, and it was being
shown a service account rather than the person. Nothing the database could do
would restrict Alice differently from Bob, because it never saw either of them.
The single shared session also meant one person's transaction state and open file
handles were shared with everyone else's requests.

**The fix:** a `ConnectionRegistry` holds one `ConnectionManager` per database
login and hands out the one belonging to the current caller. `U2_IDENTITY_MODE`
chooses how a caller becomes a login:

- `shared` keeps the upstream behaviour and remains the default, so no existing
  deployment changes. It now logs what it gives up at startup and marks every
  audit record as having used a shared login.
- `mapped` reads a JSON map of identity-provider subject to Universe login, so
  each person connects as themselves and the database's own security applies to
  them. A caller with no entry is **refused**: falling back to the shared account
  would undo the point of mapping.

Composition rather than rewriting kept this contained -- `ConnectionManager` was
left as the per-login object it already was, and gained only the credentials it
connects with.

**Proof:** `tests/test_registry.py` and `tests/test_identity.py`

## Finding 8 — the tools that touch data had no tests at all

**Where:** `tests/test_tools/`, which contained only an empty `__init__.py`

Finding 0 explained why: the test doubles modelled an API the server had stopped
calling, so no tool test could have been written against them. With the doubles
rebuilt, the tools became testable, and every module that touches business data
now has coverage.

The emphasis is deliberately on the guarantees an operator actually relies on,
rather than on line coverage for its own sake:

- **Read-only mode leaves the data untouched**, not merely returns an error. Each
  refusal is asserted twice: once on the response, once on the stored record.
- **Writes and deletes require explicit confirmation**, and an unconfirmed call
  changes nothing.
- **A refused command never reaches the server** — asserted against the commands
  the mock session actually received.
- **MultiValue structure survives a write-then-read round trip**, which is the
  property the whole project exists to preserve.
- **A batch read names what it could not find** instead of silently returning
  fewer records than asked for.
- **A nested transaction is refused without disturbing the open one.**
- **Knowledge de-duplicates near-identical topic names**, so one file does not
  accrue five entries under five spellings.

| | Upstream | This fork |
| --- | --- | --- |
| Tests in the suite | 45 | 203 |
| Tool-package coverage | 0% | 65% |
| Tool modules with tests | 0 of 6 | 6 of 6 |

Two things surfaced while writing them. The mock subroutine had to gain the real
`set_arg`/`get_arg` API, because the previous double exposed a bare argument list
the production code never uses -- the same drift as Finding 0, one layer down.
And `delete_knowledge` requires confirmation, which is good behaviour that was
nowhere asserted; it is now.

## Reviewing the remediation itself

Every fix above is new code, and new code is new attack surface. Proving the
original defects are gone says nothing about whether closing them opened
something else, so the fixes were reviewed as adversarially as the original code.

Three things were done. `bandit` was run across `src/` and reported no high or
medium findings beyond a pre-existing default of binding to all interfaces.
Each new mechanism was then examined for what happens when it *fails*, on the
principle that a security control is only as good as its failure mode. Finally
`tests/test_hardening_regressions.py` attacks the remediations directly.

Four defects were found in the remediation. All four are fixed, and all four
were introduced by this fork rather than inherited.

### R1 — the durable auth store reintroduced authorization-code replay

The in-memory store consumed a one-time code with `dict.pop()`, which is atomic.
The SQLite backend replaced that with a `SELECT` followed by a separate `DELETE`,
leaving a window in which two requests both read the code before either removed
it.

A timing test alone did not catch this: the window is small enough that a naive
race usually loses, and the first version of the test passed while the bug was
present. Widening the window deliberately — delaying the read, then racing forty
threads — made it unambiguous: **forty threads exchanged the same authorization
code**. It is now claimed atomically with `DELETE ... RETURNING`, and the same
test yields exactly one success.

This is the most serious finding in the document, and it was one this fork
created. It is worth stating plainly: making state durable is not a
behaviour-preserving change, because atomicity that came free in memory has to
be re-established explicitly in a database.

### R2 — identity resolution failed open

`current_caller()` caught every exception from the resolver and fell back to the
unauthenticated local operator. In `mapped` mode that failed closed by accident,
because the local caller has no mapped login — but in `shared` mode a caller who
could make token lookup fail would still reach the database and have their
actions recorded as `local`, which is identity laundering against the audit
trail the fork had just added.

Resolution now distinguishes two situations. No resolver installed means stdio
mode, where the local operator is the honest answer. A resolver installed means
authentication is in force, and a failure to name the caller raises rather than
downgrading.

### R3 — files holding credentials were world-readable

The OAuth database and its directory were created with default permissions. They
are now restricted to their owner where the operating system enforces that;
Windows ignores POSIX bits, so the test is skipped there and the deployment
documentation carries the ACL requirement instead.

### R4 — per-user connections grew without limit

One connection per caller becomes one connection per caller *ever seen*, which a
crowd of authenticated users could turn into exhaustion of the database's own
connection limit. The registry now holds a bounded, least-recently-used set
governed by `U2_MAX_CONNECTIONS`; an evicted caller simply reconnects on their
next request.

A related weakness was addressed at the same time: the credential map held
plaintext passwords on disk with no alternative. An entry may now name an
environment variable with `password_env` instead, so the password can come from a
vault or a systemd credential, and error messages never echo the map's contents.

## Finding 9 — the HTTP transport served an unauthenticated database to the network

**Where:** `server.py::run_sse_server()` and the `U2_HTTP_HOST` default

This was found by following up a static-analysis warning about binding to all
interfaces, and it is the most serious defect in the codebase.

**What happened:** the legacy `--http` (SSE) transport performs no authentication
of any kind. There is no provider, no token check, no identity — it simply serves
every registered tool. Upstream defaulted its bind address to `0.0.0.0` and its
CORS policy to `*` with `allow_credentials` set.

**What it cost:** running `u2-mcp --http` on a server with no further
configuration exposes `write_record`, `delete_record` and `execute_tcl` to anyone
who can reach the port. Read-only mode is off by default, so that includes
writing and deleting records. The wildcard CORS policy compounds it: because the
server sends credentials, any web page a signed-in user visited could drive the
server from their browser.

**The fix:** three changes, all failing closed.

- `U2_HTTP_HOST` now defaults to `127.0.0.1`. A container or proxied deployment
  sets `0.0.0.0` deliberately, which is the case where someone is thinking about
  exposure.
- `U2_HTTP_CORS_ORIGINS` now defaults to empty rather than `*`, blocking
  cross-origin browser access until an operator names an origin. Non-browser MCP
  clients are unaffected. A literal `*` is refused outright, because it cannot be
  combined safely with credentials.
- `exposure.py` checks both **before the port opens**, not on the first request.
  An unauthenticated server on a reachable interface is refused with an error
  naming the three ways forward, one of which is
  `U2_ALLOW_UNAUTHENTICATED_NETWORK_ACCESS=true` for an operator who means it.

These are the only changes in this fork that alter existing behaviour. That is
deliberate: a default that quietly exposes a production database is not a default
worth preserving.

**Proof:** `tests/test_network_exposure.py` — 20 tests covering loopback
detection, the safe defaults, the refusal and its wording, the explicit override,
and stdio being unaffected.

## Continuous assurance

A one-off audit describes the day it was run. The CI pipeline now fails the build
on either of two conditions, so the assurance renews itself:

- `bandit -r src/ --severity-level medium --confidence-level medium` — currently
  clean, with the two findings it raised both resolved rather than suppressed.
  The composed `DELETE ... RETURNING` statement was rewritten as module constants
  so no table name is built at runtime, and the bind-address finding became
  Finding 9.
- `pip-audit` — currently reports no known vulnerabilities in the dependency tree.

Both run before the build job, which now depends on them.

## Finding 10 — TCL could escape to the operating system

**Where:** `utils/safety.py::DEFAULT_BLOCKED_COMMANDS`

This is not an edge case. `execute_tcl` is a first-class tool the model calls
routinely, and the blocklist named nine destructive file verbs while omitting
every escape route out of the database.

**What it cost:** `SH -c '...'` and `!ls` reach a shell on the database server.
`OSDELETE`, `OSWRITE` and `OSREAD` reach its filesystem. `RUN`, `BASIC` and
`CATALOG` compile and execute arbitrary code. `LOGTO` leaves the configured
account, escaping every other control in the server. The SQL verbs Universe also
accepts -- `DROP`, `GRANT`, `UPDATE` -- were likewise absent. In short, one tool
call away from remote code execution, in the default configuration.

The blocklist was also evadable by formatting. `!ls` produced a first word of
`!LS`, which matched nothing, so the shell escape passed even when the verb was
listed.

**The fix:** the blocklist now enumerates every escape route by category --
shell and OS access, code compilation and execution, account switching, system
state -- with the reason each is there. A leading shell metacharacter is treated
as an escape rather than parsed as a verb, and verdicts are unaffected by case or
leading whitespace.

**Proof:** `tests/edge/test_adversarial_input.py` — 59 tests, each escape verb
asserted twice: refused by the validator, and never reaching the server.

## Finding 11 — the row cap could be removed by a file name

**Where:** `tools/query.py::execute_query`

The cap was skipped whenever the text `SAMPLE` appeared anywhere in the query, so
a file called `SAMPLES` or a field called `SAMPLE.DATE` silently removed the row
limit — and, after Finding 3, the answer then claimed to be complete. The test is
now for an actual `SAMPLE n` clause.

## Finding 12 — saved knowledge could forge other topics

**Where:** `utils/knowledge.py`

Content is written verbatim beneath its markdown header, so a line beginning with
`##` created a new section — letting saved content invent a topic nobody wrote or
overwrite one that existed. A topic name containing a line break did the same.
Names are collapsed to one line and header lines in content are escaped.

## Finding 13 — a reconnect storm opened one session per caller

**Where:** `ConnectionManager.get_session()`

Nothing serialized establishing a session, so a burst of requests arriving to find
none open would each create one. Twenty concurrent callers opened twenty database
connections for a single slot: a connection leak invisible until the server runs
out.

As with the authorization-code race, a naive concurrency test passed while the
bug was present. Holding the window open made it deterministic. Session
establishment is now serialized separately from command execution, so a burst
opens exactly one.

## Finding 14 — the audit log corrupted itself under load

**Where:** `utils/audit.py::_write_entry`

Records were written without a lock. Fifty concurrent calls interleaved into
lines that could not be parsed — destroying both records involved and making the
integrity of the surrounding ones unprovable. This defect sat directly beneath
the attribution work of Finding 6: an audit trail that shreds itself under load is
worse than none, because it looks complete.

Writes are now serialized, and each record is emitted as one complete line in a
single write.

## Still to come
