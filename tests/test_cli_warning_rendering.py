"""CLI rendering contracts for structured push diagnostics."""

from __future__ import annotations

from slidesmith.cli_commands._support import print_push_warnings
from slidesmith.engine.diff_model import PushWarning, WarningSeverity


def test_cli_renders_warnings_before_notices_with_mixed_summary(capsys) -> None:
    print_push_warnings(
        [
            PushWarning(WarningSeverity.NOTICE, "canonicalized paragraph defaults"),
            PushWarning(WarningSeverity.WARNING, "authored font was dropped"),
            PushWarning(WarningSeverity.NOTICE, "canonicalized weight"),
        ]
    )

    assert capsys.readouterr().err == (
        "warning: authored font was dropped\n"
        "notice: canonicalized paragraph defaults\n"
        "notice: canonicalized weight\n"
        "push warning summary: 1 warning(s), 2 notice(s)\n"
    )
