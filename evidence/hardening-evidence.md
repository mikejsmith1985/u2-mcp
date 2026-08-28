# Hardening evidence

Generated 2026-08-28 17:47 UTC by `scripts/verify_hardening.py`.

Each fix below is measured with the same tests against two checkouts: the upstream code at `f427768`, and this fork. A fix counts as proven only when its tests fail on the original and pass here.

| Fix | Defect | Upstream | This fork | Raw output |
|-----|--------|----------|-----------|------------|
| H1 | Non-ASCII business data was silently deleted from query output | fails | passes | [before](raw/H1-upstream.txt) / [after](raw/H1-fork.txt) |
| H2 | Auto-reconnect never reconnected | fails | passes | [before](raw/H2-upstream.txt) / [after](raw/H2-fork.txt) |

## What each defect cost

### H1 — Non-ASCII business data was silently deleted from query output

Accented customer names and currency symbols vanished from results with no error, so an operator could not tell a wrong answer from a right one.

### H2 — Auto-reconnect never reconnected

After the database session dropped, every later request received a null session. In stdio mode -- the default -- no watchdog exists to clear the stale record, so the server stayed broken until someone restarted it.

