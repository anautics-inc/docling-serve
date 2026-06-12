# SPICE Model Library

Vendor SPICE models for extracted schematic components, resolved by
NORMALIZED PART NUMBER (alphanumerics + underscores, upper-case):

    KIDDE 870929  ->  KIDDE_870929.lib

Accepted suffixes: .lib, .sub, .mod, .cir. Each file must contain one
`.subckt` (preferred) or `.model` entry point.

Drop a model here and every subsequent extraction binds it automatically:
the bundle's `.cir` netlist inlines it, and KiCad symbol instances carry
`Sim.*` properties pointing at it. No model = the part keeps a stub; we
never fabricate electrical parameters.

This directory is the future sync target for SPICE models attached to
`Part Catalog Item` rows in the ontology.
