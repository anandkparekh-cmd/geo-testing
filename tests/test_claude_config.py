"""The Claude Code layer: agents, commands, permissions and CLAUDE.md.

The agents themselves run on the user's machine, so these tests check what can be checked
here: every file parses, every command and flag an agent is told to run exists, the
agents the workflow hands steps to exist, and the permissions keep gates with the user.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from geo_engine.plan_cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
AGENTS = sorted((ROOT / ".claude" / "agents").glob("*.md"))
SKILLS = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
DOCS = AGENTS + SKILLS + [ROOT / "CLAUDE.md"]


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name}: frontmatter must open on the first line"
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


def _subcommands() -> dict:
    p = build_parser()
    sub = next(a for a in p._actions if a.__class__.__name__ == "_SubParsersAction")
    return {name: {o for a in sp._actions for o in a.option_strings} for name, sp in sub.choices.items()}


def test_seven_agents_with_valid_frontmatter():
    assert [a.stem for a in AGENTS] == ["geo-analysis", "geo-brief", "geo-data", "geo-design",
                                        "geo-launch", "geo-monitoring", "geo-readout"]
    for a in AGENTS:
        fm, body = _frontmatter(a)
        assert fm["name"] == a.stem and len(fm["description"]) > 40
        assert set(t.strip() for t in fm["tools"].split(",")) == {"Bash", "Read"}
        if a.stem in ("geo-brief", "geo-design", "geo-launch", "geo-analysis"):
            assert "Never approve a gate yourself" in body          # their step ends at a gate
    _, readout = _frontmatter(ROOT / ".claude/agents/geo-readout.md")
    assert "never record it without their confirmation" in readout
    _, monitoring = _frontmatter(ROOT / ".claude/agents/geo-monitoring.md")
    assert "never comment on performance" in monitoring and "results.md" in monitoring


def test_three_commands():
    names = {s.parent.name for s in SKILLS}
    assert names == {"geo-test", "geo-status", "geo-approve"}
    for s in SKILLS:
        fm, body = _frontmatter(s)
        assert fm["name"] == s.parent.name and fm["description"]
        assert "$ARGUMENTS" in body
    approve, _ = _frontmatter(ROOT / ".claude/skills/geo-approve/SKILL.md")
    assert approve["disable-model-invocation"] is True       # only the user can invoke it


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name if p.name != "SKILL.md" else p.parent.name)
def test_every_command_and_flag_named_exists(doc):
    cmds = _subcommands()
    for line in doc.read_text().splitlines():
        for m in re.finditer(r"plan_cli (\w+)([^`]*)", line):
            cmd, rest = m.group(1), m.group(2)
            if cmd in ("new|brief|approve|data|design|choose|launch|monitor|analyse|readout|deviation|status|list",):
                continue
            if cmd == "new" and "|" in rest:
                continue
            assert cmd in cmds, f"{doc.name}: unknown command plan_cli {cmd}"
            for flag in re.findall(r"(--[a-z][a-z-]*)", rest):
                assert flag in cmds[cmd], f"{doc.name}: plan_cli {cmd} has no {flag}"


def test_steps_are_handed_to_agents_that_exist():
    from geo_engine import workflow as wf
    from geo_engine.plan import TestPlan
    names = {a.stem for a in AGENTS}
    import inspect
    src = inspect.getsource(wf.next_step)
    for agent in re.findall(r'"agent": "([a-z/]+)"', src):
        for part in agent.split("/"):
            assert f"geo-{part}" in names, part
    claude_md = (ROOT / "CLAUDE.md").read_text()
    for n in names:
        assert f"`{n}`" in claude_md


def test_permissions_keep_gates_with_the_user():
    perms = json.loads((ROOT / ".claude/settings.json").read_text())["permissions"]
    for py in ("python", "python3"):
        for cmd in ("approve", "deviation", "readout"):
            assert f"Bash({py} -m geo_engine.plan_cli {cmd} *)" in perms["ask"]
    assert "Edit(/geotests/**/test_plan.yaml)" in perms["deny"]
    assert "Read(/data/_ground_truth/**)" in perms["deny"]
    assert "Bash" not in perms["allow"] and "Bash(*)" not in perms["allow"]
    assert all(r.startswith(("Bash(python -m geo_engine", "Bash(python3 -m geo_engine",
                             "Bash(python -m pytest", "Bash(python3 -m pytest")) for r in perms["allow"])
