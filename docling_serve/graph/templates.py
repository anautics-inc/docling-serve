"""Built-in docling-graph templates for the knowledge-graph enhancer.

A template is a plain Pydantic model whose ``model_config`` carries graph
metadata: ``is_entity`` marks a model as a graph node, and ``graph_id_fields``
gives the fields that form its stable identity. Nested *entity* lists become
typed edges; nested *component* models (``is_entity=False``) embed as data.

:class:`DocumentGraph` is the generic fallback used when no domain template is
configured. It follows the same hyper-edge shape that performs well in practice:
``Relation`` nodes group the ``Entity`` nodes they connect (like nets grouping
components), which yields real entity-to-entity structure rather than the flat,
relationship-free spans a generic NER service returns.

Domain templates (schematics, Access tables, …) live alongside this module or
are supplied by callers via ``DOCLING_SERVE_GRAPH_EXTRACTION_TEMPLATE`` (a dotted
import path); the graph is only as good as the template, so prefer a specific
one whenever the document type is known.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Entity(BaseModel):
    """A salient entity mentioned in the document."""

    model_config = {"is_entity": True, "graph_id_fields": ["name"]}

    name: str = Field(description="Canonical name of the entity, exactly as written")
    type: str = Field(
        description=(
            "Entity type. PREFER one of the platform's controlled vocabulary when it fits: "
            "Person, Organization, Place, Product, Event, Date, Quantity, Title. "
            "If none fit, use a specific domain type in PascalCase "
            "(e.g. WeaponSystem, SupplyItem, Standard, PartNumber, Material) — these are "
            "accepted as proposed types for review. Never invent a generic catch-all. "
            "BE CONSISTENT: the same entity must get the same type every time it appears. "
            "Identifier/coding SCHEMES (NSN, CAGE, FSC, DEMIL codes, AMC/AMSC) are "
            "Standard; a SPECIFIC identifier value (e.g. '5820-01-234-5678', part number "
            "'12345-A') is PartNumber."
        )
    )
    description: str | None = Field(
        default=None, description="Short factual description grounded in the document"
    )


class Relation(BaseModel):
    """A named relationship that connects two or more entities."""

    model_config = {"is_entity": True, "graph_id_fields": ["name"]}

    name: str = Field(
        description=(
            "Relationship predicate in SCREAMING_SNAKE_CASE, e.g. PART_OF, CONNECTS_TO, "
            "DEPENDS_ON, GOVERNED_BY, SUPPLIED_BY, LOCATED_IN, PRODUCED_BY, MEMBER_OF. "
            "Use a concise, reusable predicate that names how the members relate. "
            "Reuse the same predicate name for every relationship of that kind."
        )
    )
    members: list[Entity] = Field(
        default_factory=list,
        description=(
            "Entities participating in this relationship. Re-list each member by its "
            "exact `name` and `type` only — OMIT `description` here to save space "
            "(the top-level entity already carries it)."
        ),
    )


class DocumentGraph(BaseModel):
    """Generic knowledge graph for an arbitrary document."""

    model_config = {"is_entity": True, "graph_id_fields": ["title"]}

    title: str = Field(description="Document title or best available identifier")
    entities: list[Entity] = Field(
        default_factory=list, description="All salient entities found in the document"
    )
    relations: list[Relation] = Field(
        default_factory=list,
        description=(
            "Relationships connecting the entities. COVERAGE REQUIREMENT: every entity "
            "in `entities` must appear as a member of at least one relation — an entity "
            "with no relationship is incomplete output. Extract ALL relationships the "
            "document states or directly implies (hierarchy, governance, supply, "
            "location, sequence, responsibility), not just the most prominent ones."
        ),
    )


# --------------------------------------------------------------------------- #
# Domain templates (selected per profile; see PROFILE_TEMPLATES)              #
# --------------------------------------------------------------------------- #


class SchematicComponent(BaseModel):
    """A component referenced in schematic/wiring documentation text."""

    model_config = {"is_entity": True, "graph_id_fields": ["name"]}

    name: str = Field(
        description="Reference designator when printed (R1, K1, J4, TB2) else the "
        "component's name exactly as written"
    )
    type: str = Field(
        description="Component kind, e.g. Resistor, Relay, Connector, CircuitBreaker, "
        "Pump, Valve, Harness, TerminalBoard — PascalCase, consistent across mentions"
    )
    description: str | None = Field(
        default=None, description="Value/rating/role grounded in the document"
    )


class SchematicConnection(BaseModel):
    """A named electrical/physical connection grouping components."""

    model_config = {"is_entity": True, "graph_id_fields": ["name"]}

    name: str = Field(
        description="Net/signal/line label from the document (GND, +28V, SIG_A, "
        "fuel line L-7) or a SCREAMING_SNAKE predicate (CONNECTS_TO, FEEDS, SWITCHES)"
    )
    members: list[SchematicComponent] = Field(
        default_factory=list,
        description="Components on this connection, re-listed by name + type only",
    )


class SchematicDocumentGraph(BaseModel):
    """Knowledge graph for schematic / wiring / drawing documentation.

    Used when graph extraction runs over the *text* of schematic-profile
    documents (wiring notes, legend tables, maintenance text). The vector
    drawing itself goes through the vision-based SchematicExtractor; this
    template keeps the text-side entities in the same component/net vocabulary
    so both land compatibly in the ontology.
    """

    model_config = {"is_entity": True, "graph_id_fields": ["title"]}

    title: str = Field(description="Drawing/document title or number")
    components: list[SchematicComponent] = Field(
        default_factory=list, description="Every component the text references"
    )
    connections: list[SchematicConnection] = Field(
        default_factory=list,
        description="Connections/nets relating the components. Every component should "
        "appear in at least one connection when the text states its wiring.",
    )


class DatabaseTable(BaseModel):
    """A table in an exported database (e.g. Access)."""

    model_config = {"is_entity": True, "graph_id_fields": ["name"]}

    name: str = Field(description="Table name exactly as exported")
    type: str = Field(default="DatabaseTable", description="Always DatabaseTable")
    description: str | None = Field(
        default=None,
        description="What the table holds, inferred from its name/columns/rows",
    )


class TableRelationship(BaseModel):
    """A relationship between tables (shared key columns, lookups, foreign keys)."""

    model_config = {"is_entity": True, "graph_id_fields": ["name"]}

    name: str = Field(
        description="Predicate in SCREAMING_SNAKE_CASE, e.g. REFERENCES, LOOKS_UP, "
        "CHILD_OF — derived from shared key columns or explicit foreign keys"
    )
    members: list[DatabaseTable] = Field(
        default_factory=list, description="Tables participating in this relationship"
    )


class AccessDatabaseGraph(BaseModel):
    """Knowledge graph for an exported Access/relational database.

    Run over the table inventory + schema text the Access extractor emits;
    captures the table topology so the ontology can reason about what data
    lives where and how tables join.
    """

    model_config = {"is_entity": True, "graph_id_fields": ["title"]}

    title: str = Field(description="Database file name or title")
    tables: list[DatabaseTable] = Field(
        default_factory=list, description="Every table in the export"
    )
    relationships: list[TableRelationship] = Field(
        default_factory=list,
        description="Table-to-table relationships evidenced by shared key columns, "
        "schema constraints, or naming conventions",
    )


class SustainmentEntity(BaseModel):
    """An entity in the USAF sustainment domain vocabulary.

    Types and rules are NORMATIVE — defined in
    ``captify-pytology/docs/usaf-sustainment-domain/vocabulary.md`` and mirrored
    by the ingestion default vocabulary, so extracted entities land published in
    the ontology instead of queued as proposed.
    """

    model_config = {"is_entity": True, "graph_id_fields": ["name"]}

    name: str = Field(description="Canonical name exactly as written")
    type: str = Field(
        description=(
            "Entity type from the USAF sustainment vocabulary. BOM side: "
            "WeaponSystem (platform by MDS, e.g. B-52H, F-16C — NEVER Product), "
            "EndItem (a specific tail/serial number), Subsystem (WUC/LCN node), "
            "Part (a BOM item, keyed by NSN/part number/CAGE), Supplier (vendor, "
            "keyed by CAGE). Organization side: OrgUnit (wing/group/center/ALC/"
            "program office), Role (duty position: Item Manager, Equipment "
            "Specialist, TO Manager — a named human is Person), Authority, "
            "Process (a defined workflow: ETAR, TAR/MAR, code validation), "
            "ITSystem (system of record: ETIMS, D200, JCALS, ILS-S). Events/"
            "artifacts: AssistanceRequest (a submitted form instance: AFMC 202, "
            "DLA 339, AFTO 22), EngineeringDisposition, DMSMSCase, TechnicalOrder "
            "(a TO/pub as artifact), WorkControlDocument, Modification, Funding, "
            "Baseline. Generic fallbacks when nothing fits: Person, Organization, "
            "Place, Event, Date, Quantity, Title. Identifier/coding SCHEMES (NSN "
            "system, SMR/DEMIL/AMC codes) are Standard; a SPECIFIC identifier "
            "value is PartNumber. BE CONSISTENT: the same entity gets the same "
            "type every time."
        )
    )
    description: str | None = Field(
        default=None, description="Short factual description grounded in the document"
    )


class SustainmentRelation(BaseModel):
    """A relationship from the sustainment edge catalog."""

    model_config = {"is_entity": True, "graph_id_fields": ["name"]}

    name: str = Field(
        description=(
            "Predicate in SCREAMING_SNAKE_CASE, PREFERRING the domain catalog: "
            "HAS_END_ITEM, HAS_PART, PART_OF, NEXT_HIGHER_ASSEMBLY, SUPPLIED_BY, "
            "EXPERIENCES, TRIGGERS, REFERENCES, ROUTED_TO, PRODUCES, MODIFIES, "
            "CHANGES, USES, EXECUTES, HOLDS, GOVERNED_BY, MANAGED_BY, MEMBER_OF, "
            "ASSIGNED_TO, LOCATED_IN. Coin a new predicate only when none of "
            "these fit, and reuse it consistently."
        )
    )
    members: list[SustainmentEntity] = Field(
        default_factory=list,
        description=(
            "Entities in this relationship, re-listed by exact name + type only "
            "(omit description here)."
        ),
    )


class UsafSustainmentGraph(BaseModel):
    """Knowledge graph for USAF weapon-system sustainment documents.

    The domain steering for TOs, TCTOs, supply/DEMIL briefings, assistance-request
    forms, and program documentation: weapon systems grounded in their BOM,
    organizations in their people/process/technology/data, and forms as the
    change-initiating events connecting them.
    """

    model_config = {"is_entity": True, "graph_id_fields": ["title"]}

    title: str = Field(description="Document title or best available identifier")
    entities: list[SustainmentEntity] = Field(
        default_factory=list, description="All salient entities found in the document"
    )
    relations: list[SustainmentRelation] = Field(
        default_factory=list,
        description=(
            "Relationships connecting the entities. COVERAGE REQUIREMENT: every "
            "entity must appear as a member of at least one relation. Extract ALL "
            "relationships the document states or directly implies (BOM indenture, "
            "governance, supply, routing, disposition, org membership), not just "
            "the most prominent ones."
        ),
    )


# Profile -> dotted template path. Used by /v1/graph/extract (``profile`` field)
# and the knowledge_graph enhancer (extraction profile) when no explicit
# template override is given. Unlisted profiles fall back to DocumentGraph.
PROFILE_TEMPLATES: dict[str, str] = {
    "schematic": f"{__name__}.SchematicDocumentGraph",
    "schematics": f"{__name__}.SchematicDocumentGraph",
    "drawing": f"{__name__}.SchematicDocumentGraph",
    "drawings": f"{__name__}.SchematicDocumentGraph",
    "access": f"{__name__}.AccessDatabaseGraph",
    "accessdb": f"{__name__}.AccessDatabaseGraph",
    "database": f"{__name__}.AccessDatabaseGraph",
    "usaf-sustainment": f"{__name__}.UsafSustainmentGraph",
    "sustainment": f"{__name__}.UsafSustainmentGraph",
    "usaf": f"{__name__}.UsafSustainmentGraph",
}


def resolve_profile_template(profile: str | None) -> str | None:
    """Dotted template path for an extraction profile, or None for the default."""
    if not profile:
        return None
    return PROFILE_TEMPLATES.get(profile.strip().lower())
