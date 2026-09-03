"""Every JSON writer goes through storage's atomic write.

Regression guard for the three modules that hand-rolled the temp-file rename
with a fixed "<name>.tmp" and no fsync: metrics.json, the scheduler state,
agent.json and credentials.json. A fixed temp name is shared state between
concurrent writers, and without the fsync a power cut can leave the rename
applied and the blocks unwritten.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros import improvement_runner, metrics, self_modify

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "ouroboros"


@pytest.fixture
def writes(monkeypatch):
    """Record every rename and fsync the process performs."""
    renames = []
    fsyncs = []
    real_replace = os.replace
    real_fsync = os.fsync

    def fake_replace(src, dst, **kwargs):
        renames.append((str(src), str(dst)))
        return real_replace(src, dst, **kwargs)

    def fake_fsync(fd):
        fsyncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", fake_replace)
    monkeypatch.setattr(os, "fsync", fake_fsync)
    return SimpleNamespace(renames=renames, fsyncs=fsyncs)


def assert_written_atomically(writes, target: Path) -> None:
    sources = [src for src, dst in writes.renames if dst == str(target)]
    assert sources, f"nothing was renamed onto {target}"
    for src in sources:
        assert src != f"{target}.tmp", (
            f"{target.name} was published from a fixed temp name: two writers "
            f"share that scratch file, so one can publish the other's half-write"
        )
        assert Path(src).parent == target.parent, (
            "the temp file must be a sibling, or the rename is not atomic"
        )
    assert writes.fsyncs, (
        f"{target.name} was renamed without an fsync: a power cut leaves the "
        f"rename applied and the data unwritten"
    )


def test_save_metrics_writes_atomically(tmp_path, writes):
    metrics.save_metrics(tmp_path, [{"timestamp": 1, "src_lines": 10}])

    assert_written_atomically(writes, tmp_path / "config" / "metrics.json")
    assert metrics.load_metrics(tmp_path) == [{"timestamp": 1, "src_lines": 10}]


def test_save_scheduler_state_writes_atomically(tmp_path, monkeypatch, writes):
    monkeypatch.setenv("HOME", str(tmp_path))

    improvement_runner.save_scheduler_state({"consecutive_failures": 2})

    state_file = tmp_path / ".config" / "moltbook" / "self_improvement_state.json"
    assert_written_atomically(writes, state_file)
    assert improvement_runner.load_scheduler_state() == {"consecutive_failures": 2}


def test_modify_runner_config_writes_both_files_atomically(
    tmp_path, monkeypatch, writes
):
    monkeypatch.setenv("HOME", str(tmp_path))

    self_modify.modify_runner_config(
        {"interval_seconds": 60, "telegram_bot_token": "new-token"}
    )

    cfg_dir = tmp_path / ".config" / "moltbook"
    assert_written_atomically(writes, cfg_dir / "agent.json")
    assert_written_atomically(writes, cfg_dir / "credentials.json")
    assert json.loads((cfg_dir / "agent.json").read_text())["interval_seconds"] == 60
    assert json.loads((cfg_dir / "credentials.json").read_text()) == {
        "telegram_bot_token": "new-token"
    }


def test_no_module_builds_a_fixed_temp_name():
    """The correct helper existing did not stop three modules re-rolling it."""
    offenders = []
    for module in sorted(SRC_DIR.glob("*.py")):
        if module.name == "storage.py":  # owns the mkstemp-based writer
            continue
        for lineno, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if '".tmp"' in line or "'.tmp'" in line:
                offenders.append(f"{module.name}:{lineno}: {line.strip()}")

    assert not offenders, (
        "build the temp file with tempfile.mkstemp via storage.save_json_file; "
        "a fixed name is shared between writers:\n" + "\n".join(offenders)
    )
