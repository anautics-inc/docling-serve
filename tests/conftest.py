# Must run before any `docling_serve` import. The settings model fails closed
# when no API key is configured (Article VI.1 / N5): instantiating
# `DoclingServeSettings()` at import time raises unless auth is explicitly
# opted out. Tests run unauthenticated, so opt in here so importing the app
# (and the whole suite) does not crash in environments without a key.
import os

os.environ.setdefault("DOCLING_SERVE_ALLOW_UNAUTHENTICATED", "true")

# The test_1-* / test_2-* suites are live-server integration tests: they POST to
# a running instance at localhost:5001 and open real websockets. They cannot run
# in the unit CI job (no server) so they are ignored by default. Set
# DOCLING_SERVE_RUN_INTEGRATION=1 to collect them (against a running server).
if not os.environ.get("DOCLING_SERVE_RUN_INTEGRATION"):
    collect_ignore_glob = ["test_1-*", "test_2-*"]
