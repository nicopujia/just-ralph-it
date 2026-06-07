"""Tests for deterministic MVP readiness checks."""

from textwrap import dedent

from jri.core.readiness import (
    check_mvp_readiness,
    format_missing_mvp_readiness,
)


def test_mvp_readiness_accepts_heading_and_label_facts() -> None:
    """Specs can satisfy readiness through headings and labels."""
    markdown = dedent("""\
        # Product

        ## Goal: Build a tiny CLI.

        - **Target User**: Developers trying JRI locally.
        - **Workflows**: Run the command once.
        - **Inputs**: No arguments or stdin.
        - **Outputs**: Print hello to stdout.
        - **Persistence**: No data is saved.
        - **Integrations**: No external services are used.
        - **Errors**: Exit non-zero for unexpected failures.
        - **Edge Cases**: Repeated runs print the same output.
        - **Non-goals**: No interactive prompt.
        - **Success Criteria**: The command prints hello exactly once.
        """)

    report = check_mvp_readiness(markdown)

    assert report.is_ready
    assert report.missing == ()


def test_mvp_readiness_rejects_placeholders() -> None:
    """Placeholder text does not count as a readiness fact."""
    markdown = dedent("""\
        ## Goal

        Build a tiny CLI.

        - **Target User**: TBD
        """)

    report = check_mvp_readiness(markdown)

    assert not report.is_ready
    assert "target user" in report.missing
    assert "goal" not in report.missing


def test_mvp_readiness_rejects_qualified_placeholders() -> None:
    """Qualified placeholder phrases do not count as readiness facts."""
    markdown = dedent("""\
        ## Goal

        Build a tiny CLI.

        ## Target User

        - Not specified yet.

        ## Workflows

        - TBD later.

        ## Inputs: Unknown for now.

        - **Outputs**: Print hello to stdout.
        - **Persistence**: No data is saved.
        - **Integrations**: No external services are used.
        - **Errors**: Exit non-zero for unexpected failures.
        - **Edge Cases**: Repeated runs print the same output.
        - **Non-goals**: No interactive prompt.
        - **Success Criteria**: The command prints hello exactly once.
        """)

    report = check_mvp_readiness(markdown)

    assert not report.is_ready
    assert report.missing == ("target user", "workflows", "inputs")


def test_mvp_readiness_accepts_facts_containing_placeholder_words() -> None:
    """Normal words inside real facts are not treated as placeholders."""
    markdown = dedent("""\
        ## Goal

        Build a tiny CLI.

        - **Target User**: Developers evaluating unknown repositories.
        - **Workflows**: Pending approvals stay visible until resolved.
        - **Inputs**: The project path is not specified by config.
        - **Outputs**: Print hello to stdout.
        - **Persistence**: No data is saved.
        - **Integrations**: No external services are used.
        - **Errors**: Exit non-zero for unexpected failures.
        - **Edge Cases**: Repeated runs print the same output.
        - **Non-goals**: No interactive prompt.
        - **Success Criteria**: The command prints hello exactly once.
        """)

    report = check_mvp_readiness(markdown)

    assert report.is_ready
    assert report.missing == ()


def test_format_missing_mvp_readiness_lists_required_facts() -> None:
    """Missing readiness facts are formatted for the user."""
    message = format_missing_mvp_readiness(("target user", "workflows"))

    assert message == (
        "Missing MVP readiness facts:\n"
        "- target user\n"
        "- workflows\n"
        "Please answer these before Ralph starts."
    )
