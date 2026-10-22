import pytest

from app import main
from app.services.warehouse_sync import AmbiguousBatchError


@pytest.mark.parametrize("error, code", [
    (None, main.EXIT_OK),
    (RuntimeError("PIM unreachable"), main.EXIT_FAILED),
    (AmbiguousBatchError("batch 7 unknown"), main.EXIT_NEEDS_RECONCILIATION),
])
def test_exit_codes_tell_the_orchestrator_what_to_do(monkeypatch, capsys, error, code):
    def fake_run(run_id):
        if error:
            raise error
        return {}

    monkeypatch.setattr(main, "run", fake_run)
    assert main.main(["--run-id", "day-1"]) == code
    if error:
        assert ("RunNeedsReconciliation" if code == main.EXIT_NEEDS_RECONCILIATION else "RunFailed") in capsys.readouterr().out
