# Prototype: PPTX OOXML Geometry JSON

The prototype skips rendering entirely. It opens the `.pptx` package as OOXML,
extracts slide XML, resolves theme typography through the local `deep_document`
helpers, and writes a normalized JSON artifact with native PowerPoint
coordinates preserved.

Generated artifacts:

- `out/pptx-ooxml-geometry.json`
- `out/canvas-contract.json`
- `out/pptx-ooxml-geometry.tldr`
- `out/preview.html`
- `out/xml/**`
- `out/assets/**`

This is the preferred production direction for PPTX deep mode because it keeps
editable structure instead of relying on screenshot-style rendering.

Current fixture result:

- 27 slides
- 67 positioned elements
- 52 text elements
- 11 image elements/assets
- 2 table elements
- 114 indexed OOXML/XML relationship parts
- 0 renderer/conversion calls
