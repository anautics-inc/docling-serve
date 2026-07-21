from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from docling_serve.__main__ import main
from docling_serve.staging_smoke import main as staging_smoke_main
from docling_serve.websocket_notifier import WebsocketNotifier

REPOSITORY = Path(__file__).parents[1]
OPERATIONAL_SCRIPTS = sorted(
    path for path in (REPOSITORY / "scripts").glob("*.py") if path.name != "__init__.py"
)


@pytest.mark.parametrize("script", OPERATIONAL_SCRIPTS, ids=lambda path: path.name)
def test_operational_script_imports_without_execution(script: Path) -> None:
    namespace = runpy.run_path(str(script), run_name="docling_operational_validation")
    assert namespace


def test_installed_cli_entrypoints_are_callable() -> None:
    assert callable(main)
    assert callable(staging_smoke_main)


@pytest.mark.asyncio
async def test_websocket_notifier_closes_subscribers_on_cleanup() -> None:
    class FakeWebSocket:
        closed = False

        async def close(self) -> None:
            self.closed = True

    websocket = FakeWebSocket()
    notifier = object.__new__(WebsocketNotifier)
    notifier.orchestrator = SimpleNamespace()
    notifier.task_subscribers = {"task-1": {websocket}}

    await notifier.remove_task("task-1")

    assert websocket.closed is True
    assert "task-1" not in notifier.task_subscribers
