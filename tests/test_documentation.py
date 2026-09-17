"""Regression checks for user-facing command examples."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
README_FILES = (
    "README.md",
    "README.hi-IN.md",
    "README.ja-JP.md",
    "README.ko-KR.md",
    "README.zh-CN.md",
)
OPTIONAL_GROUPS = (
    "embeddings",
    "google-embeddings",
    "communities",
    "enrichment",
    "eval",
    "wiki",
    "all",
)
USER_DOC_FILES = README_FILES + (
    "docs/COMMANDS.md",
    "docs/FAQ.md",
    "gryphon/docs/LLM-OPTIMIZED-REFERENCE.md",
    "docs/TROUBLESHOOTING.md",
)


def test_pip_extra_examples_use_cross_shell_double_quotes():
    """Extras must survive zsh globbing without breaking Windows cmd.exe.

    Checks the quoting invariant on the extras each README actually shows,
    rather than requiring every README to document every optional group.
    A README is free to mention only the extras it needs; whichever it does
    mention must be double-quoted. Since this fork installs from Git, the
    extra is followed by a PEP 508 ``@ git+...`` URL inside the same quotes.
    """
    unquoted = re.compile(r"(?<!\")pip install gryphon\[([A-Za-z0-9-]+)\]")
    quoted = re.compile(r'pip install "gryphon\[([A-Za-z0-9-]+)\][^"]*"')
    for readme_name in README_FILES:
        content = (ROOT / readme_name).read_text(encoding="utf-8")
        bad = unquoted.findall(content)
        assert not bad, f"{readme_name} has unquoted pip extras: {bad}"
        for group in quoted.findall(content):
            assert group in OPTIONAL_GROUPS, (
                f"{readme_name} documents unknown extra '{group}'"
            )


def test_current_user_docs_have_no_unquoted_pip_extras():
    pattern = re.compile(r"pip install gryphon\[[A-Za-z0-9-]+\]")
    for doc_name in USER_DOC_FILES:
        content = (ROOT / doc_name).read_text(encoding="utf-8")
        assert pattern.search(content) is None, f"unquoted pip extras in {doc_name}"


def test_github_action_references_use_current_supported_majors():
    """Keep active workflows and copy-paste examples on supported majors."""
    files = [
        ROOT / "action.yml",
        ROOT / "README.md",
        ROOT / "docs/GITHUB_ACTION.md",
        *(ROOT / ".github/workflows").glob("*.yml"),
    ]
    expected_majors = {"checkout": "7", "cache": "6"}
    for path in files:
        content = path.read_text(encoding="utf-8")
        for action, expected in expected_majors.items():
            for actual in re.findall(rf"actions/{action}@v(\d+)", content):
                assert actual == expected, (
                    f"{path.relative_to(ROOT)} uses actions/{action}@v{actual}; "
                    f"expected v{expected}"
                )


def test_codebuddy_install_docs_cover_project_artifacts():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs/USAGE.md").read_text(encoding="utf-8")

    assert "install --platform codebuddy" in readme
    for artifact in (
        ".mcp.json",
        "CODEBUDDY.md",
        ".codebuddy/settings.json",
        ".codebuddy/skills/<name>/SKILL.md",
    ):
        assert artifact in usage
