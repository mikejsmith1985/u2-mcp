"""Changing one value of one field, without disturbing the ones beside it.

Separate from `write_record`, and the separation is the point rather than
tidiness.

`write_record` takes a whole record and stores it. That puts responsibility for
the record's *shape* on whoever assembled the fields — and in a MultiValue file
the shape is load-bearing. An inventory record holds parallel fields: position n
of the branch list, the on-hand list and the committed list all describe the same
branch. A field may legitimately be shorter than its siblings, because bins are
recorded for some branches and not others.

Assemble that record carelessly and a field comes back a different length. The
record is still well formed. Nothing errors. What has changed is which branch a
quantity belongs to, and no constraint, index or later read will notice — the
first anybody hears of it is a customer being promised stock that is somewhere
else.

So this tool names the field and the value instead of taking the record, which
keeps the shape the server's problem where it can be enforced once. It refuses a
position that does not exist rather than creating one, and refuses an index past
the end of a field rather than padding to reach it. Padding is the specific
mistake: reaching an index by filling the gap invents positions, and here a
position is a claim about a branch that nobody made.
"""

from typing import Any

from ..server import get_connection_manager, mcp


@mcp.tool()
def update_value(
    file_name: str,
    record_id: str,
    position: int,
    index: int,
    value: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Change one value of one field in a record, leaving other positions alone.

    Prefer this over write_record for any change to an existing record. It cannot
    move a value onto a different position, which is the way a MultiValue record
    is most easily corrupted without anything reporting it.

    Args:
        file_name: Target file name
        record_id: The record to change
        position: Which field, counting from one
        index: Which value within that field, counting from zero
        value: What to put there
        confirm: Must be True to execute the write. This is a safety measure.

    Returns:
        The record before and after, with the number of values each field held
        both times, so the caller can see that no field changed length. Disabled
        in read-only mode.
    """
    manager = get_connection_manager()

    if manager.config.read_only:
        return {"error": "Write operations disabled in read-only mode"}

    if not confirm:
        return {
            "status": "confirmation_required",
            "message": "Set confirm=True to execute this change",
            "file": file_name,
            "id": record_id,
            "position": position,
            "index": index,
        }

    try:
        file_handle = manager.open_file(file_name)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return {"error": str(error), "file": file_name, "id": record_id}

    # Not every driver can do this. `uopy` against a real Universe and the
    # writable demonstration driver both can; the read-only driver deliberately
    # cannot, and says so rather than pretending.
    if not hasattr(file_handle, "update_value"):
        return {
            "error": (
                "This driver has no in-place update. The read-only driver cannot "
                "write at all; select a writable one to change a record."
            ),
            "file": file_name,
            "id": record_id,
        }

    try:
        before = str(file_handle.read(record_id))
        after = str(file_handle.update_value(record_id, position, index, value))
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return {"error": str(error), "file": file_name, "id": record_id}

    lengths_before = _field_lengths(before)
    lengths_after = _field_lengths(after)

    return {
        "file": file_name,
        "id": record_id,
        "before": before,
        "after": after,
        "field_lengths_before": lengths_before,
        "field_lengths_after": lengths_after,
        # Reported rather than left for the caller to work out. A write that
        # changed a field's length has moved a value onto a different branch, and
        # it is the one outcome nothing downstream can detect.
        "alignment_preserved": lengths_before == lengths_after,
    }


# The three separators, in their hierarchy.
_AM = chr(254)
_VM = chr(253)


def _field_lengths(raw: str) -> list[int]:
    """Count the values in each field.

    Args:
        raw: The record as stored

    Returns:
        One count per field, in order

    Remarks:
        The shape a write has to leave alone. Two records can both be well formed
        while one has quietly moved a quantity onto a different branch; the
        lengths are where that difference is visible.
    """
    if not raw:
        return []

    return [len(field.split(_VM)) for field in raw.split(_AM)]
