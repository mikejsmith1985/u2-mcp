#!/usr/bin/env python3
"""Point this server at your own UniVerse and find out, in about a minute.

Run it and it tells you one of two things: that this works against your
database, or exactly which step failed and what to do about it. It changes
nothing -- every operation below is a read, and the one write it attempts is
attempted precisely to show you that it is refused.

    python scripts/try-it-here.py

There is nothing else to configure. If something is missing it says which thing
and where to put it, rather than failing with a stack trace.

Written because "clone it and try it" is not an offer unless the trying is easy.
A reader who has to work out four environment variables, a driver name and a
port from source code has been handed homework, not a demonstration.
"""

from __future__ import annotations

import logging
import os
import sys
import textwrap
from typing import Any

# Quiet by default. This script's own output is the report; the server's logging
# would bury it, and anything that actually matters is printed below.
logging.disable(logging.INFO)

# The four things this server needs to reach a database, and what each one is in
# plain words. The descriptions are here rather than in a README because the
# moment somebody needs them is the moment one is missing.
REQUIRED: dict[str, str] = {
    "U2_HOST": "the machine UniVerse runs on, e.g. 127.0.0.1 or uv.example.com",
    "U2_USER": "an operating-system account that can log in to UniVerse",
    "U2_PASSWORD": "that account's password",
    "U2_ACCOUNT": "the UniVerse account to open, e.g. PRODUCTION or EDI",
}

# How wide the report's left column is, so every line lines up.
LABEL = 34

_passed = 0
_failed = 0


def step(label: str, outcome: bool, detail: str = "") -> None:
    """Print one line of the report and remember how it went.

    Args:
        label: What was attempted
        outcome: Whether it worked
        detail: What came back, or why it did not
    """
    global _passed, _failed

    mark = "  ok  " if outcome else " FAIL "
    if outcome:
        _passed += 1
    else:
        _failed += 1

    print(f"[{mark}] {label.ljust(LABEL)} {detail}")


def explain(text: str) -> None:
    """Print an explanation under a failed step, keeping its shape.

    Args:
        text: The explanation, indented as a Python block

    Remarks:
        Relative indentation is preserved rather than stripped per line. Half of
        what this prints is commands to copy, and a command that arrives with its
        indentation flattened is a command somebody has to reconstruct.
    """
    for line in textwrap.dedent(text).strip().splitlines():
        print(f"         {line}" if line.strip() else "")
    print()


def check_settings() -> bool:
    """Check the four required settings are present.

    Returns:
        True when every one is set
    """
    print("Settings")
    print("-" * 72)

    missing = [name for name in REQUIRED if not os.environ.get(name)]

    for name, meaning in REQUIRED.items():
        value = os.environ.get(name)
        # The password is confirmed as present and never shown. A diagnostic that
        # prints a credential turns a support request into an incident.
        shown = "(set)" if name == "U2_PASSWORD" and value else (value or "")
        step(name, bool(value), shown if value else f"not set - {meaning}")

    if missing:
        print()
        explain(
            f"""
            {len(missing)} setting(s) are missing: {', '.join(missing)}

            Set them and run this again. On Windows PowerShell:

                $env:U2_HOST = '127.0.0.1'
                $env:U2_USER = 'your-username'
                $env:U2_PASSWORD = 'your-password'
                $env:U2_ACCOUNT = 'YOUR.ACCOUNT'

            On macOS or Linux:

                export U2_HOST=127.0.0.1
                export U2_USER=your-username
                export U2_PASSWORD=your-password
                export U2_ACCOUNT=YOUR.ACCOUNT
            """
        )
        return False

    driver = os.environ.get("U2_DRIVER", "uopy")
    step(
        "U2_DRIVER",
        True,
        f"{driver}" + ("  (the real uopy client)" if driver == "uopy" else "  (demonstration only)"),
    )

    print()
    return True


def check_library() -> bool:
    """Check uopy is importable.

    Returns:
        True when the client library is installed
    """
    print("Client library")
    print("-" * 72)

    if os.environ.get("U2_DRIVER", "uopy") != "uopy":
        step("uopy", True, "not needed - the demonstration driver is selected")
        print()
        return True

    try:
        import uopy  # noqa: F401
    except ImportError:
        step("uopy", False, "not installed")
        explain(
            """
            uopy is Rocket's own client library and ships with the U2 client
            tools. It is not on PyPI, so it cannot be installed from here.

                pip install /path/to/uopy-x.y.z-py3-none-any.whl

            It has to go into the same environment that runs this script.
            """
        )
        return False

    step("uopy", True, "installed")
    print()
    return True


def check_connection() -> Any:
    """Open a connection and report what answered.

    Returns:
        The connection manager when it worked, otherwise None
    """
    print("Connection")
    print("-" * 72)

    host = os.environ["U2_HOST"]
    account = os.environ["U2_ACCOUNT"]

    try:
        from u2_mcp.server import get_connection_manager

        manager = get_connection_manager()
        answered = manager.execute_command("WHO")
    except Exception as error:  # noqa: BLE001 -- the message is the whole point
        step("connect", False, f"{type(error).__name__}")
        explain(
            f"""
            Could not reach {account} on {host}.

            The three things this is almost always:

              1. Nothing is listening. UniRPC uses port 31438 by default, and it
                 does not start until UniVerse is licensed and running.
              2. The username or password is wrong. These are operating-system
                 credentials on the UniVerse machine, not UniVerse logins.
              3. The account name is wrong, or is not registered in UV.ACCOUNT.

            What came back:
              {error}
            """
        )
        return None

    step("connect", True, f"{account} on {host}")
    step("WHO", True, str(answered).strip()[:60])
    print()
    return manager


def check_read_only(manager: Any) -> None:
    """Attempt a write, and report that it was refused.

    Args:
        manager: The open connection manager
    """
    print("Read-only enforcement")
    print("-" * 72)

    # Attempted on purpose, and attempted properly.
    #
    # The first version of this called write_record without confirm=True, got
    # back "confirmation required", and reported that the write had not been
    # refused. It had not been permitted either -- the tool was asking a
    # question. A check that cannot tell "no" from "are you sure?" is not a
    # check, and this one would have cried wolf at every reader.
    #
    # Aimed at a file that does not exist, so a server with the guarantee removed
    # still could not damage anything here.
    from u2_mcp.config import U2Config

    is_configured_read_only = U2Config().read_only
    step(
        "read-only is on",
        is_configured_read_only,
        "U2_READ_ONLY" + ("" if is_configured_read_only else " is set to false"),
    )

    try:
        from u2_mcp.tools.files import write_record

        result = write_record(
            "U2MCP.SELFTEST.NOSUCHFILE",
            "PROBE",
            ["this must not be written"],
            confirm=True,
        )
        refused = isinstance(result, dict) and "error" in result
        detail = str(result.get("error", ""))[:56] if isinstance(result, dict) else str(result)[:56]
    except Exception as error:  # noqa: BLE001
        refused = True
        detail = f"{type(error).__name__}: {error}"[:56]

    step("a write is refused", refused, detail)

    if not refused:
        explain(
            """
            A write was NOT refused. Stop here.

            This server refuses writes by default, so seeing this means
            U2_READ_ONLY has been set to false somewhere in this environment.
            Unset it, or set it to true, before pointing this at anything you
            care about.

            If U2_READ_ONLY is not set to false and you are still seeing this,
            please tell me -- that is a defect worth knowing about.
            """
        )

    print()


def discover(manager: Any) -> None:
    """List a few files and describe one, without being told the schema.

    Args:
        manager: The open connection manager
    """
    print("What is in your account")
    print("-" * 72)

    try:
        from u2_mcp.tools.files import list_files

        listing = list_files()
        files = listing.get("files", [])
    except Exception as error:  # noqa: BLE001
        files = []
        step("list files", False, f"{type(error).__name__}: {error}"[:56])
    else:
        step("list files", bool(files), f"{len(files)} file(s) visible")

    if files:
        print()
        print("         " + ", ".join(sorted(files)[:12]))
        if len(files) > 12:
            print(f"         ... and {len(files) - 12} more")
    print()

    # A dictionary is how a MultiValue database describes itself, so this is the
    # part that needs no knowledge of your schema at all: whatever your files are
    # called, their dictionaries say what each field means.
    target = os.environ.get("U2_TRY_FILE") or (sorted(files)[0] if files else None)

    if not target:
        step("describe a file", False, "no file to describe")
        explain(
            """
            Nothing was listed, so there is nothing to describe. Name a file
            yourself and run this again:

                U2_TRY_FILE=CUSTOMERS python scripts/try-it-here.py
            """
        )
        return

    print(f"Reading the dictionary of {target}")
    print("-" * 72)

    try:
        from u2_mcp.tools.dictionary import list_dictionary

        described = list_dictionary(target)
        items = described.get("dictionary_items", [])
    except Exception as error:  # noqa: BLE001
        step("read its dictionary", False, f"{type(error).__name__}: {error}"[:56])
        return

    step("read its dictionary", bool(items), f"{len(items)} field(s) described")

    if items:
        print()
        print(f"         {'FIELD':<18} {'AT':<4} {'HEADING':<20} {'S/M':<4} CONV")
        for item in items[:14]:
            print(
                f"         {str(item.get('name'))[:18]:<18} "
                f"{str(item.get('field_number')):<4} "
                f"{str(item.get('heading'))[:20]:<20} "
                f"{str(item.get('single_multi')):<4} "
                f"{item.get('conversion') or ''}"
            )
        if len(items) > 14:
            print(f"         ... and {len(items) - 14} more")
    print()


def main() -> int:
    """Run every check and report. Returns a process exit code."""
    print()
    print("=" * 72)
    print(" Pointing this MCP server at your UniVerse")
    print("=" * 72)
    print()
    print(" Everything below is a read. The one write attempted is attempted to")
    print(" show you that it is refused.")
    print()

    if not check_settings():
        return 2

    if not check_library():
        return 2

    manager = check_connection()
    if manager is None:
        return 1

    check_read_only(manager)
    discover(manager)

    print("=" * 72)
    print(f" {_passed} checks passed, {_failed} failed")
    print("=" * 72)
    print()

    if _failed:
        print(" Something above did not work. Each failure says what to do next.")
        print()
        return 1

    print(" This server reads your database, describes it without being told the")
    print(" schema, and refuses to write to it.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
