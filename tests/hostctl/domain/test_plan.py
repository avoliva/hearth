from __future__ import annotations

from hostctl.domain.commands import cmd
from hostctl.domain.plan import Plan, change, warn


def test_plans_join_in_order_and_keep_warnings() -> None:
    plan = change("a", "first", cmd("true")) + warn("careful") + change("b", "second")
    assert [action.summary for action in plan.actions] == ["first", "second"]
    assert plan.warnings == ("careful",)
    assert Plan.join([plan, Plan()]) == plan


def test_empty_plan_renders_as_no_changes() -> None:
    assert Plan().is_empty
    assert "No changes" in Plan().render()


def test_a_rendered_plan_shows_stdin_only_as_a_byte_count() -> None:
    plan = change("cf", "Write token", cmd("tee", "/f", sudo=True, stdin="s3cr3t-token-value"))
    assert "s3cr3t-token-value" not in plan.render()
    assert "stdin: 18 bytes" in plan.render()
