from docling_serve.app import create_app


def test_composed_openapi_route_parity(monkeypatch):
    monkeypatch.setattr(
        "docling_serve.app.setup_otel_instrumentation", lambda *args, **kwargs: None
    )

    schema = create_app().openapi()
    actual = {
        (path, method)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    expected = {
        ("/health", "get"),
        ("/openapi-3.0.json", "get"),
        ("/ready", "get"),
        ("/ready/adapters", "get"),
        ("/v1/capabilities", "get"),
        ("/v1/chunk/hierarchical/file", "post"),
        ("/v1/chunk/hierarchical/file/async", "post"),
        ("/v1/chunk/hierarchical/source", "post"),
        ("/v1/chunk/hierarchical/source/async", "post"),
        ("/v1/chunk/hybrid/file", "post"),
        ("/v1/chunk/hybrid/file/async", "post"),
        ("/v1/chunk/hybrid/source", "post"),
        ("/v1/chunk/hybrid/source/async", "post"),
        ("/v1/clear/converters", "get"),
        ("/v1/clear/results", "get"),
        ("/v1/convert/file", "post"),
        ("/v1/convert/file/async", "post"),
        ("/v1/convert/source", "post"),
        ("/v1/convert/source/async", "post"),
        ("/v1/convert/source/batch", "post"),
        ("/v1/extract/access", "post"),
        ("/v1/extract/auto", "post"),
        ("/v1/extract/form", "post"),
        ("/v1/extract/schematic", "post"),
        ("/v1/extract/technical-order", "post"),
        ("/v1/graph/extract", "post"),
        ("/v1/memory/counts", "get"),
        ("/v1/memory/stats", "get"),
        ("/v1/result/{task_id}", "get"),
        ("/v1/schematic/check", "post"),
        ("/v1/schematic/revise", "post"),
        ("/v1/schematic/simulate", "post"),
        ("/v1/status/poll/{task_id}", "get"),
        ("/version", "get"),
    }

    assert actual == expected


def test_dynamic_chunk_routes_keep_distinct_operation_names(monkeypatch):
    monkeypatch.setattr(
        "docling_serve.app.setup_otel_instrumentation", lambda *args, **kwargs: None
    )

    schema = create_app().openapi()

    assert (
        schema["paths"]["/v1/chunk/hybrid/source"]["post"]["summary"]
        == "Chunk Sources With Hybridchunker"
    )
    assert (
        schema["paths"]["/v1/chunk/hierarchical/source"]["post"]["summary"]
        == "Chunk Sources With Hierarchicalchunker"
    )
