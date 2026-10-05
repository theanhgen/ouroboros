"""program.md: the cycle's step prompts, loaded by name and immutable to the cycle."""

import pytest

from ouroboros import prompts
from ouroboros.config import SafetyConfig
from ouroboros.improvement import _is_path_allowed
from ouroboros.llm import identify_improvements

SECTIONS = ("identify", "react_final", "plan", "edit", "review")


@pytest.mark.parametrize("name", SECTIONS)
def test_every_cycle_step_has_a_section(name):
    assert prompts.load_program_section(name).strip()


def test_missing_section_raises_instead_of_falling_back():
    with pytest.raises(KeyError, match="nope"):
        prompts.load_program_section("nope")


def test_section_text_is_returned_verbatim(tmp_path, monkeypatch):
    program = tmp_path / "program.md"
    program.write_text("intro\n<!-- section: plan -->\n  two\n\nlines  \n<!-- end: plan -->\n")
    monkeypatch.setattr(prompts, "_PROGRAM_PATH", program)
    assert prompts.load_program_section("plan") == "  two\n\nlines  "


def test_loader_ignores_a_program_md_in_the_cwd(tmp_path, monkeypatch):
    # Under the bench the cwd is a task snapshot with its own, older program.md.
    (tmp_path / "program.md").write_text("<!-- section: plan -->\nWRONG\n<!-- end: plan -->\n")
    monkeypatch.chdir(tmp_path)
    assert prompts.load_program_section("plan") != "WRONG"


def test_the_cycle_cannot_edit_program_md():
    config = SafetyConfig()
    assert not _is_path_allowed("src/ouroboros/program.md", config)
    assert _is_path_allowed("src/ouroboros/prompts.py", config)


def test_identify_sends_the_program_md_section(tmp_path, monkeypatch):
    program = tmp_path / "program.md"
    program.write_text("<!-- section: identify -->\nFROM PROGRAM.MD\n<!-- end: identify -->\n")
    monkeypatch.setattr(prompts, "_PROGRAM_PATH", program)
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise ValueError("stop here -- only the prompt matters")

    class Client:
        pass

    client = Client()
    client.chat = Client()
    client.chat.completions = Completions()
    identify_improvements(client, "summary", "tests ok", "history", model="gpt-test")
    assert captured["messages"][0] == {"role": "system", "content": "FROM PROGRAM.MD"}
