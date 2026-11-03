from __future__ import annotations

from hostctl.domain.commands import cmd
from hostctl.ports.shell import CommandFailed, Result

SECRET = "s3cr3t-token-value"


def test_command_failed_message_omits_stdin() -> None:
    command = cmd("curl", "-K", "-", stdin=f'header = "Authorization: Bearer {SECRET}"')
    error = CommandFailed(command, Result(22, "", "curl: (22) The requested URL returned error"))
    assert SECRET not in str(error)
    assert "stdin" in str(error)
