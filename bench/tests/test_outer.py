"""Outer-loop and habit tests: the parts that decide, with no LLM and no bench run."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402
import habits  # noqa: E402
import outer  # noqa: E402

PROGRAM = (common.REPO / outer.PROGRAM_REL).read_text()


def run(resolved_ids, n=10, scored=None, false_merge=0, broke_other=0):
    return {"n": str(n), "scored": str(n if scored is None else scored),
            "resolved": str(len(resolved_ids)), "resolved_ids": set(resolved_ids),
            "false_merge": str(false_merge), "broke_other": str(broke_other)}


def test_screen_passes_only_a_clear_win_over_mains_mean():
    main = [run({"a", "b"}), run({"a", "b", "c"})]
    assert outer.screen(main, run({"a", "b", "c", "d"}))[0] == "screen_pass"   # 4 >= 2.5 + 1
    assert outer.screen(main, run({"a", "b", "c"}))[0] == "discard"            # 3 < 3.5


def test_screen_discards_new_harm_and_flags_short_runs():
    main = [run({"a"}), run({"a"})]
    assert outer.screen(main, run({"a", "b", "c"}, false_merge=1))[0] == "discard"
    assert outer.screen(main, run({"a", "b", "c"}, broke_other=1))[0] == "discard"
    assert outer.screen(main, run({"a", "b", "c"}, scored=7))[0] == "incomplete"


def test_mcnemar_p():
    assert outer.mcnemar_p(0, 0) == 1.0
    assert outer.mcnemar_p(4, 0) == 1 / 16
    assert outer.mcnemar_p(3, 0) == 1 / 8
    assert outer.mcnemar_p(5, 1) == 7 / 64


def test_keep_needs_a_significant_paired_win():
    m, c = run(set(), n=19), run({"a", "b", "c", "d"}, n=19)
    assert outer.paired_verdict([(m, c)])[0] == "keep"           # 4-0, p=.0625
    assert outer.paired_verdict([(m, run({"a", "b"}, n=19))])[0] == "confirming"


def test_a_score_bought_with_losses_is_not_a_keep():
    # Same +2 net, but 4 wins against 2 losses is noise (p=.34), and stays so.
    pairs = [(run({"x", "y"}, n=19), run({"a", "b", "c", "d"}, n=19))] * 3
    decision, stats = outer.paired_verdict(pairs)
    assert decision == "discard" and (stats["wins"], stats["losses"]) == (12, 6)


def test_harm_discards_even_a_significant_win():
    m, c = run(set(), n=19), run({"a", "b", "c", "d", "e"}, n=19, false_merge=1)
    assert outer.paired_verdict([(m, c)])[0] == "discard"


def test_out_of_replicates_discards():
    pairs = [(run(set(), n=19), run({"a"}, n=19))] * outer.MAX_REPLICATES
    assert outer.paired_verdict(pairs)[0] == "discard"


def test_parse_reply():
    hyp, prog = outer.parse_reply("chatter\nHYPOTHESIS: answer sooner\n<<<PROGRAM\nX\nPROGRAM>>>\n")
    assert (hyp, prog) == ("answer sooner", "X\n")
    with pytest.raises(ValueError):
        outer.parse_reply("HYPOTHESIS: no program block")


def test_validate_accepts_a_one_section_edit():
    new = PROGRAM.replace("Create a step-by-step plan", "Create a short step-by-step plan")
    assert outer.validate(PROGRAM, new, set()) == []


@pytest.mark.parametrize("mutate, error", [
    (lambda p: p.replace("<!-- section: plan -->", "<!-- section: planning -->")
              .replace("<!-- end: plan -->", "<!-- end: planning -->"), "sections changed"),
    (lambda p: p, "no change"),
    (lambda p: p.replace("Create a step-by-step plan", "x" * 2000), "grew more than 2x"),
    (lambda p: p.replace("Create a step-by-step plan", "test_bundle_empty first"), "names benchmark tests"),
])
def test_validate_rejects(mutate, error):
    errors = outer.validate(PROGRAM, mutate(PROGRAM), {"test_bundle_empty"})
    assert any(error in e for e in errors), errors


def test_scrub_leaves_nothing_task_specific():
    assert outer.scrub("[improve] ReAct final answer unparseable (Expecting value); starts '{\"a\": 1'") \
        == "[improve] ReAct final answer unparseable (Expecting value); starts <reply>"
    assert outer.scrub("Out of scope: targets forbidden file(s): src/ouroboros/git_ops.py") \
        == "Out of scope: <details>"
    assert outer.scrub("EditMismatch: block for src/ouroboros/backlog.py in test_add_item") \
        == "EditMismatch: block for <file> in <test>"


def test_editor_prompt_carries_no_task_content():
    table = {"react_empty_final": {"count": 2, "meaning": "m", "tasks": [
        {"id": "2026-09-25-3cc39930", "evidence": "ReAct final answer unparseable; starts 'x'"}]}}
    prompt = outer.editor_prompt(PROGRAM, table, [], "def f(): pass")
    assert "ReAct final answer unparseable" in prompt
    assert "2026-09-25-3cc39930" not in prompt
    for d in common.TASKS_DIR.glob("*/gold.patch"):
        added = [l[1:].strip() for l in d.read_text().splitlines()
                 if l.startswith("+") and not l.startswith("+++") and len(l.strip()) > 30]
        assert not any(a in prompt for a in added), d.parent.name


def test_editor_context_includes_the_react_loop():
    ctx = outer.code_context(common.REPO / "src")
    assert "def identify_improvements" in ctx and "Handle Tool Calls" in ctx


def test_habits_take_the_first_signature_in_pipeline_order():
    row = {"id": "t", "cycle_status": None, "run_message": "No improvements identified.",
           "f2p_passed": "0/1"}
    assert habits.classify(row, "WARNING x: ReAct final answer unparseable")[0] == "react_empty_final"
    assert habits.classify(row, "")[0] == "no_task_other"
    assert habits.classify({**row, "resolved": True}, "ReAct final answer unparseable")[0] == "resolved"
    assert habits.classify({**row, "invalid": True, "resolved": True})[0] == "invalid"
    assert habits.classify({"id": "t", "cycle_status": "failed", "f2p_passed": "1/3",
                            "changed_files": ["x"]})[0] == "partial_fix"


def test_top_habit_skips_outcomes_that_are_not_habits():
    table = {"resolved": {"count": 5}, "invalid": {"count": 4}, "generate_empty": {"count": 1}}
    assert habits.top_habit(table) == "generate_empty"


def test_request_note_flags_tool_calls_when_no_tools_were_offered():
    log = ("INFO bench: request 1: model=m tools=True response_format=None history_tool_calls=0 -> "
           "finish=tool_calls content_chars=0 tool_calls=1 completion_tokens=1 reasoning_tokens=1\n"
           "INFO bench: request 2: model=m tools=False response_format=json_object history_tool_calls=1"
           " -> finish=error content_chars=0 tool_calls=1 completion_tokens=1 reasoning_tokens=1\n")
    note = habits.request_note(log)
    assert note.startswith("1 of 2 requests: offered no tools")
    assert outer.scrub(note) == note  # carries nothing task-specific
