import pytest

from agent.frontend.cli_state import CliState


def test_attachment_batch_and_snapshot(tmp_path):
    first, second = tmp_path / "中文 a.txt", tmp_path / "b.txt"
    first.write_text("a")
    second.write_text("b")
    state = CliState("openai", "model")
    state.add_files([str(first), str(first)])
    assert state.pending_files == [str(first.resolve())]
    snapshot = state.snapshot()
    state.add_files([str(second)])
    state.consume(snapshot)
    assert state.pending_files == [str(second.resolve())]


def test_invalid_batch_is_atomic(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("a")
    state = CliState("openai", "model")
    with pytest.raises(OSError):
        state.add_files([str(path), str(tmp_path / "missing")])
    assert state.pending_files == []
    with pytest.raises(ValueError):
        state.add_files([str(tmp_path)])


def test_deleted_attachment_and_invalid_number_are_retained(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("a")
    state = CliState("openai", "model")
    state.add_files([str(path)])
    path.unlink()
    with pytest.raises(OSError):
        state.snapshot()
    with pytest.raises(ValueError):
        state.remove(0)
    assert len(state.pending_files) == 1
    state.remove(1)
    assert not state.pending_files
