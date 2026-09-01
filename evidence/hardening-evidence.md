# Hardening evidence

Generated 2026-08-29 11:23 UTC by `scripts/verify_hardening.py`.

Each fix below is measured with the same tests against two checkouts: the upstream code at `f427768`, and this fork. A fix counts as proven only when its tests fail on the original and pass here.

| Fix | Defect | Upstream | This fork | Raw output |
|-----|--------|----------|-----------|------------|
| H1 | Non-ASCII business data was silently deleted from query output | fails the assertion | passes | [before](raw/H1-upstream.txt) / [after](raw/H1-fork.txt) |
| H2 | Auto-reconnect never reconnected | fails the assertion | passes | [before](raw/H2-upstream.txt) / [after](raw/H2-fork.txt) |
| H3 | A truncated answer was presented as a complete one | fails the assertion | passes | [before](raw/H3-upstream.txt) / [after](raw/H3-fork.txt) |
| H4 | A timed-out query kept running against the database | fails the assertion | passes | [before](raw/H4-upstream.txt) / [after](raw/H4-fork.txt) |
| H5 | OAuth state was lost on restart, and could not be shared | test could not run (new module) | passes | [before](raw/H5-upstream.txt) / [after](raw/H5-fork.txt) |
| H6 | The audit trail could not name who acted | test could not run (new module) | passes | [before](raw/H6-upstream.txt) / [after](raw/H6-fork.txt) |
| H7 | Every caller reached the database as the same account | test could not run (new module) | passes | [before](raw/H7-upstream.txt) / [after](raw/H7-fork.txt) |

## What each defect cost

### H1 — Non-ASCII business data was silently deleted from query output

Accented customer names and currency symbols vanished from results with no error, so an operator could not tell a wrong answer from a right one.

### H2 — Auto-reconnect never reconnected

After the database session dropped, every later request received a null session. In stdio mode -- the default -- no watchdog exists to clear the stale record, so the server stayed broken until someone restarted it.

### H3 — A truncated answer was presented as a complete one

A row limit was silently appended to LIST queries, and nothing in the response said so. 'How many of these do we have?' could return a capped number that reads exactly like the real one.

### H4 — A timed-out query kept running against the database

The timeout returned an error but never stopped the work: the query continued on the server and its thread stayed alive against a session the next request would reuse. Under load, abandoned queries accumulated.

### H5 — OAuth state was lost on restart, and could not be shared

Every registration, token and in-flight login lived in process memory. A restart signed everyone out, and a second instance behind a load balancer would not recognise the first one's tokens -- so the service could not be made redundant.

### H6 — The audit trail could not name who acted

Records carried a session id generated when the server started, not the authenticated user. Single sign-on proved who someone was, and then that answer was discarded -- so no record could say who ran a query.

### H7 — Every caller reached the database as the same account

One connection served the whole process under one login, so Universe's own file and field security could not act on the real caller. Each authenticated person can now hold their own session under their own account, and an unmapped caller is refused rather than silently sharing.

