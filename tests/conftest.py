# Must run before any `docling_serve` import. The settings model fails closed
# when no API key is configured (Article VI.1 / N5): instantiating
# `DoclingServeSettings()` at import time raises unless auth is explicitly
# opted out. Tests run unauthenticated, so opt in here so importing the app
# (and the whole suite) does not crash in environments without a key.
import os

os.environ.setdefault("DOCLING_SERVE_ALLOW_UNAUTHENTICATED", "true")
