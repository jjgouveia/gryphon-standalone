"""The vendor-neutral ``.agents`` skill store.

``.agents/skills/<name>/SKILL.md`` is the tool-independent location that
per-tool directories (``~/.claude/skills`` and friends) symlink into. It is
user-level and shared between projects, so it holds skills from many
unrelated sources side by side. Every test here asserts the same contract:
install writes exactly the workflows this package ships, uninstall removes
exactly those, and everything else in the store is left untouched.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from gryphon import skills, uninstall
from gryphon.cli import _handle_init
from gryphon.skills import _agents_home, install_agents_skills

SHIPPED_SKILLS = {
    "build-graph",
    "debug-issue",
    "explore-codebase",
    "refactor-safely",
    "review-changes",
    "review-delta",
    "review-multi",
    "review-pr",
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestAgentsHome:
    def test_defaults_to_dot_agents_in_the_user_home(self, monkeypatch):
        monkeypatch.delenv("AGENTS_HOME", raising=False)
        assert _agents_home() == Path.home() / ".agents"

    def test_env_override_wins_and_expands_user(self, monkeypatch):
        monkeypatch.setenv("AGENTS_HOME", "~/elsewhere/.agents")
        assert _agents_home() == Path.home() / "elsewhere" / ".agents"

    def test_blank_env_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("AGENTS_HOME", "   ")
        assert _agents_home() == Path.home() / ".agents"


class TestInstallAgentsSkills:
    def test_writes_every_shipped_workflow_flat(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_HOME", str(tmp_path / ".agents"))

        skills_root = install_agents_skills()

        assert skills_root == tmp_path / ".agents" / "skills"
        assert {p.parent.name for p in skills_root.glob("*/SKILL.md")} == SHIPPED_SKILLS

    def test_copies_are_byte_identical_to_the_bundled_files(self, tmp_path, monkeypatch):
        """The store is git-versioned by its users; a newline rewrite is a diff."""
        monkeypatch.setenv("AGENTS_HOME", str(tmp_path / ".agents"))
        bundled = Path(__file__).resolve().parents[1] / "skills"

        skills_root = install_agents_skills()

        for name in SHIPPED_SKILLS:
            assert (skills_root / name / "SKILL.md").read_bytes() == (
                bundled / name / "SKILL.md"
            ).read_bytes()

    def test_leaves_unrelated_skills_and_the_lock_file_alone(self, tmp_path, monkeypatch):
        agents_home = tmp_path / ".agents"
        monkeypatch.setenv("AGENTS_HOME", str(agents_home))
        _write(agents_home / ".skill-lock.json", '{"version": 3, "skills": {}}\n')
        _write(agents_home / "skills" / "grill-me" / "SKILL.md", "someone else's\n")

        install_agents_skills()

        assert (agents_home / ".skill-lock.json").read_text(encoding="utf-8").startswith(
            '{"version": 3'
        )
        assert (agents_home / "skills" / "grill-me" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == "someone else's\n"

    def test_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_HOME", str(tmp_path / ".agents"))
        first = install_agents_skills()
        second = install_agents_skills()
        assert first == second
        assert len(list(second.iterdir())) == len(SHIPPED_SKILLS)


class TestUninstallAgentsSkills:
    def test_removes_only_the_shipped_slugs(self, tmp_path, monkeypatch):
        agents_home = tmp_path / ".agents"
        monkeypatch.setenv("AGENTS_HOME", str(agents_home))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)

        install_agents_skills()
        _write(agents_home / "skills" / "grill-me" / "SKILL.md", "keep\n")
        _write(agents_home / ".skill-lock.json", "keep\n")

        uninstall.run(repo=repo, keep_data=True)

        store = agents_home / "skills"
        assert not any((store / slug).exists() for slug in SHIPPED_SKILLS)
        assert (store / "grill-me" / "SKILL.md").read_text(encoding="utf-8") == "keep\n"
        assert (agents_home / ".skill-lock.json").read_text(encoding="utf-8") == "keep\n"

    def test_keeps_a_user_file_sitting_beside_a_removed_skill(self, tmp_path, monkeypatch):
        agents_home = tmp_path / ".agents"
        monkeypatch.setenv("AGENTS_HOME", str(agents_home))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)

        install_agents_skills()
        _write(agents_home / "skills" / "review-pr" / "notes.md", "my notes\n")

        uninstall.run(repo=repo, keep_data=True)

        assert not (agents_home / "skills" / "review-pr" / "SKILL.md").exists()
        assert (agents_home / "skills" / "review-pr" / "notes.md").read_text(
            encoding="utf-8"
        ) == "my notes\n"


class TestInstallTrigger:
    """install populates the store when the user keeps one, and only then."""

    @staticmethod
    def _run_install(repo: Path, monkeypatch, platform: str = "all") -> None:
        monkeypatch.setattr("gryphon.incremental.find_repo_root", lambda: repo)
        monkeypatch.setattr(
            "gryphon.incremental.ensure_repo_gitignore_excludes_crg",
            lambda repo_root: "created",
        )
        monkeypatch.setattr(
            "gryphon.skills.install_platform_configs",
            lambda repo_root, target, dry_run=False: [],
        )
        _handle_init(
            argparse.Namespace(
                repo=str(repo),
                dry_run=False,
                platform=platform,
                yes=True,
                no_instructions=True,
                no_skills=False,
                no_hooks=True,
            )
        )

    def test_populates_an_existing_store(self, tmp_path, monkeypatch, capsys):
        agents_home = tmp_path / ".agents"
        (agents_home / "skills").mkdir(parents=True)
        monkeypatch.setenv("AGENTS_HOME", str(agents_home))
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)

        self._run_install(repo, monkeypatch)

        assert {
            p.parent.name for p in (agents_home / "skills").glob("*/SKILL.md")
        } == SHIPPED_SKILLS
        assert "Installed agent-neutral skills in" in capsys.readouterr().out

    def test_does_not_create_a_store_on_a_machine_without_one(
        self, tmp_path, monkeypatch, capsys
    ):
        agents_home = tmp_path / ".agents"
        monkeypatch.setenv("AGENTS_HOME", str(agents_home))
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)

        self._run_install(repo, monkeypatch)

        assert not agents_home.exists()
        assert "agent-neutral" not in capsys.readouterr().out

    def test_slug_list_covers_the_store(self):
        """uninstall walks the store by slug, so the list must match what install writes."""
        assert set(skills._SKILL_SLUGS) == SHIPPED_SKILLS
