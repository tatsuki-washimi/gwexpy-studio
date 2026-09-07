"""Workspace command shapes shared by the Qt bridge and window."""

WORKSPACE_GESTURES = frozenset({"load", "apply", "read_io", "apply_multi"})

WORKSPACE_REQUIRED = {
    "new_project": frozenset(),
    "open_project": frozenset({"path"}),
    "save_project": frozenset(),
    "close_project": frozenset(),
    "set_ui_state": frozenset({"ui_state"}),
    "undo_analysis": frozenset(),
    "redo_analysis": frozenset(),
    "review_restore": frozenset(),
    "restore_project": frozenset({"review", "confirmed"}),
    "list_recoveries": frozenset(),
    "recover_project": frozenset({"run_id"}),
    "discard_recovery": frozenset({"run_id"}),
}
WORKSPACE_OPTIONAL: dict[str, frozenset[str]] = {
    kind: frozenset() for kind in WORKSPACE_REQUIRED
}
WORKSPACE_OPTIONAL.update(
    {
        "save_project": frozenset({"path", "ui_state"}),
        "new_project": frozenset({"ui_state"}),
        "review_restore": frozenset({"redo"}),
    }
)
