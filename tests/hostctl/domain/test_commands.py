from __future__ import annotations

from hostctl.domain.commands import cmd


def test_command_render_marks_sudo_and_hides_stdin() -> None:
    command = cmd("tee", "/etc/x y", sudo=True, stdin="secret")
    assert command.render() == "sudo tee '/etc/x y'  # stdin: 6 bytes"
    assert "secret" not in command.render()
