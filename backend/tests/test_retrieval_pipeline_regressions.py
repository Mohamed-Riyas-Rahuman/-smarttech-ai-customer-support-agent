"""Regression coverage for product agnostic indexing and hybrid retrieval."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.rag.chunking import chunk_text, extract_text_from_file
from app.services.rag.retrieval import (
    RetrievedContext,
    identify_product_id,
    resolve_product,
    search_knowledge_base,
    select_relevant_evidence_excerpt,
)
from app.services.response_validation import _extract_factual_numbers, validate_generated_response


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ProductDb:
    def __init__(self, products):
        self.products = products

    def scalars(self, _query):
        return _Rows(self.products)


def test_product_matching_supports_partial_names_and_normalized_skus():
    db = _ProductDb([
        SimpleNamespace(id=7, name="VisionBeam 4K Portable Projector", sku="ST-VIS-002"),
        SimpleNamespace(id=8, name="AeroBook Ultra 14 Laptop", sku="ST-CMP-003"),
    ])

    assert identify_product_id("VisionBeam 4K projector HDMI no signal", db) == 7
    assert identify_product_id("my AeroBook laptop battery drains", db) == 8
    assert identify_product_id("SKU st vis 002: connection issue", db) == 7


def test_similarly_named_products_remain_ambiguous_without_model_context():
    db = _ProductDb([
        SimpleNamespace(id=7, name="VisionBeam 4K Portable Projector", sku="ST-VIS-002"),
        SimpleNamespace(id=9, name="VisionBeam 4K Home Projector", sku="ST-VIS-009"),
    ])
    assert identify_product_id("VisionBeam projector is not working", db) is None
    assert identify_product_id("VisionBeam 4K Portable", db) == 7


def test_product_resolution_preserves_registered_unknown_and_ambiguous_states():
    db = _ProductDb([
        SimpleNamespace(
            id=7,
            name="VisionBeam 4K Portable Projector",
            sku="ST-VIS-002",
            description="Aliases: Beam Portable",
        ),
        SimpleNamespace(
            id=8,
            name="AeroBook Ultra 14 Laptop",
            sku="ST-CMP-003",
            description="Aliases: AeroBook 14",
        ),
        SimpleNamespace(
            id=9,
            name="VisionBeam 4K Home Projector",
            sku="ST-VIS-009",
            description="",
        ),
    ])

    assert resolve_product("SKU: ST-VIS-002 HDMI issue", db).state == "registered"
    assert resolve_product("Beam Portable HDMI issue", db).product_id == 7
    assert resolve_product("AeroBook Ultra 14 battery issue", db).product_id == 8
    unknown = resolve_product("NovaPhone X2 smartphone will not charge", db)
    assert unknown.state == "unregistered"
    assert resolve_product("NovaPhone X2 keeps restarting", db).state == "unregistered"
    ambiguous = resolve_product("VisionBeam 4K Projector HDMI issue", db)
    assert ambiguous.state == "ambiguous"
    assert set(ambiguous.candidate_ids) == {7, 9}
    assert resolve_product("What is the refund policy?", db).state == "unspecified"


def test_shared_brand_does_not_register_an_unknown_model_variant():
    from app.services.langchain_workflow.workflow import _has_unregistered_product_variant

    product = SimpleNamespace(
        id=7, name="VisionBeam 4K Portable Projector", sku="ST-VIS-002",
    )

    class _SelectedProductDb(_ProductDb):
        def get(self, _model, product_id):
            return product if product_id == product.id else None

    db = _SelectedProductDb([product])
    query = "VisionBeam Mini Projector will not focus"
    resolution = resolve_product(query, db)

    assert resolution.state == "registered"
    assert _has_unregistered_product_variant(query, db, resolution.product_id)


def test_document_association_uses_content_when_manual_sku_differs_from_catalog():
    root = Path(__file__).resolve().parents[2]
    manual = root / "data" / "uploads" / "AeroBook_Ultra_14_Test_Manual.pdf"
    text = extract_text_from_file(str(manual))
    db = _ProductDb([
        SimpleNamespace(
            id=8, name="AeroBook Ultra 14 Laptop", sku="ST-CMP-003",
            description="Computing laptop",
        ),
    ])

    # The uploaded guide advertises ST-LAP-014; the catalog uses ST-CMP-003.
    # Its distinctive model name still establishes the one unambiguous match.
    assert "ST-LAP-014" in text
    assert identify_product_id(f"AeroBook manual\n{text}", db) == 8


def test_uploaded_manual_sections_keep_issue_heading_reference_and_steps():
    root = Path(__file__).resolve().parents[2]
    manual = root / "data" / "uploads" / "VisionBeam_4K_Projector_Test_Manual.pdf"
    text = extract_text_from_file(str(manual))
    chunks = chunk_text(text, chunk_size=500, chunk_overlap=60)
    issue_chunks = [chunk for chunk in chunks if "PRJ-HDMI-01" in chunk.content]

    assert issue_chunks
    joined = "\n".join(chunk.content for chunk in issue_chunks)
    for phrase in (
        "No HDMI Signal or HDCP Error",
        "3840x2160",
        "HDCP handshake reset",
        "HDMI EDID Mode",
        "HDMI 2.0",
        "HDMI 1.4",
    ):
        assert phrase.casefold() in joined.casefold()
    assert issue_chunks[0].metadata.get("reference") == "PRJ-HDMI-01"


def test_aerobook_uploaded_manual_contains_battery_issue_and_verified_steps():
    root = Path(__file__).resolve().parents[2]
    manual = root / "data" / "uploads" / "AeroBook_Ultra_14_Test_Manual.pdf"
    chunks = chunk_text(extract_text_from_file(str(manual)), chunk_size=500)
    battery_chunks = [chunk for chunk in chunks if "BAT-DRN-01" in chunk.content]

    assert battery_chunks
    text = "\n".join(chunk.content for chunk in battery_chunks)
    assert "Battery Draining Rapidly" in text
    assert "Task Manager" in text
    assert "Battery Saver Mode" in text
    assert "Calibrate battery state" in text


def test_hybrid_keyword_match_recovers_relevant_chunk_below_vector_cutoff():
    document = SimpleNamespace(id=4, title="Projector Guide", filename="guide.pdf")
    relevant = SimpleNamespace(
        id=11, document_id=4, content="Issue 3.1 No HDMI Signal or HDCP Error PRJ-HDMI-01",
        metadata_json='{"issue":"Issue 3.1: No HDMI Signal or HDCP Error","reference":"PRJ-HDMI-01"}',
        document=document,
    )
    unrelated = SimpleNamespace(
        id=12, document_id=4, content="Projector lamp maintenance schedule",
        metadata_json=None, document=document,
    )

    class _HybridDb:
        call_count = 0

        def scalar(self, _query):
            return 3

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, _query):
            self.call_count += 1
            # The vector candidate is weak; full text independently finds the
            # exact technical issue and should still make it into the results.
            return _Rows([(unrelated, 0.72)] if self.call_count == 1 else [(relevant, 0.60)])

    with patch("app.services.rag.retrieval.embedding_service.get_embedding", return_value=[0.0] * 384):
        results = search_knowledge_base(
            "VisionBeam HDMI laptop No Signal HDCP", _HybridDb(), product_id=3,
            top_k=4, min_similarity=0.45,
        )

    assert len(results) == 1
    assert results[0].chunk_id == 11
    assert results[0].retrieval_method == "hybrid"
    assert "PRJ-HDMI-01" in results[0].content


def test_unrelated_low_similarity_candidates_are_rejected():
    document = SimpleNamespace(id=4, title="Projector Guide", filename="guide.pdf")
    chunk = SimpleNamespace(
        id=12, document_id=4, content="Lamp filter maintenance",
        metadata_json=None, document=document,
    )

    class _VectorOnlyDb:
        call_count = 0

        def scalar(self, _query):
            return 3

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, _query):
            self.call_count += 1
            return _Rows([(chunk, 0.72)] if self.call_count == 1 else [])

    with patch("app.services.rag.retrieval.embedding_service.get_embedding", return_value=[0.0] * 384):
        results = search_knowledge_base(
            "refund status on an unrelated order", _VectorOnlyDb(),
            product_id=3, top_k=4, min_similarity=0.45,
        )
    assert results == []


def test_grounding_accepts_sentence_terminated_refresh_rate_and_resolution():
    evidence = (
        "Check HDMI source output resolution; ensure output does not exceed "
        "3840x2160 @ 60Hz. Disconnect the HDMI cable, turn source device OFF, "
        "restart the projector, reconnect the cable, then turn the source device "
        "back ON. In Settings > Inputs, change HDMI EDID Mode from Auto to "
        "HDMI 2.0 or HDMI 1.4."
    )
    assert {"3840", "2160", "60hz"}.issubset(set(_extract_factual_numbers(evidence)))

    result = SimpleNamespace(document_id=6, content=evidence)
    context = RetrievedContext(total_chunks=1, citations=[], formatted_text=evidence, results=[result])

    class _ReadOnlyDb:
        def scalars(self, _query):
            return _Rows([])

    response = (
        "Set the source output to no more than 3840 x 2160 at 60 Hz. "
        "Disconnect the HDMI cable, turn the source device off, restart the "
        "projector, reconnect the cable, turn the source device on, and change "
        "HDMI EDID Mode from Auto to HDMI 2.0."
    )
    assert validate_generated_response(
        body=response,
        inquiry="The HDMI display says No Signal.",
        context=context,
        expected_product_id=None,
        db=_ReadOnlyDb(),
    ) is None


def test_grounding_rejects_undocumented_extra_tests_ui_actions_and_promises():
    evidence = (
        "Disconnect the HDMI cable. Restart the projector. Reconnect the HDMI cable. "
        "In Settings > Inputs, change HDMI EDID Mode from Auto to HDMI 2.0."
    )
    context = RetrievedContext(
        total_chunks=1,
        citations=[],
        formatted_text=evidence,
        results=[SimpleNamespace(document_id=6, content=evidence)],
    )

    class _ReadOnlyDb:
        def scalars(self, _query):
            return _Rows([])

    for answer in (
        "Try a different HDMI cable or port.",
        "Press the Menu button and wait for the status LED to turn blue.",
        "We can arrange a repair if the issue persists.",
        "These steps can resolve handshake issues.",
        "The projector will not display a signal above 60 Hz.",
        "If it persists, check the cable or inspect the projector firmware.",
    ):
        reason = validate_generated_response(
            body=answer,
            inquiry="The HDMI display says No Signal.",
            context=context,
            expected_product_id=None,
            db=_ReadOnlyDb(),
        )
        assert reason and "absent from the inquiry and retrieved evidence" in reason


def test_grounded_excerpt_selects_matching_issue_and_skips_unsupported_question():
    result = SimpleNamespace(
        document_title="VisionBeam Troubleshooting Guide",
        filename="visionbeam.pdf",
        citation="Source: VisionBeam Troubleshooting Guide",
        content=(
            "3. HDMI Connection Troubleshooting\n"
            "Issue 3.1: No HDMI Signal or HDCP Error PRJ-HDMI-01\n"
            "1. Check HDMI source output resolution; ensure output does not exceed 3840x2160 @ 60Hz.\n"
            "2. Disconnect HDMI cable, restart projector, then reconnect the cable.\n"
            "3. In Settings > Inputs, change HDMI EDID Mode from Auto to HDMI 2.0 or HDMI 1.4.\n"
            "4. Image Calibration & Distortion Fixes\n"
            "Issue 4.1: Blurry image\n"
            "1. Adjust focus."
        ),
    )
    results = [result]
    excerpt = select_relevant_evidence_excerpt(
        "VisionBeam 4K Projector HDMI No Signal laptop cable troubleshooting",
        results,
    )
    assert excerpt is not None
    assert "PRJ-HDMI-01" in excerpt
    assert "HDMI EDID Mode" in excerpt
    assert "Blurry image" not in excerpt

    unsupported = select_relevant_evidence_excerpt(
        "VisionBeam projector built-in oxygen sensor calibration interval",
        results,
    )
    assert unsupported is None

    from app.services.langchain_workflow.workflow import (
        _build_documentation_limitation_reply,
        _build_evidence_grounded_reply,
    )

    context = RetrievedContext(1, [], result.content, results)
    reply = _build_evidence_grounded_reply(
        query="VisionBeam 4K Projector HDMI No Signal laptop cable troubleshooting",
        customer_name="Arun",
        context=context,
    )
    assert reply is not None
    assert "PRJ-HDMI-01" in reply
    assert "3840x2160 @ 60Hz" in reply
    assert "Please share the exact model number" not in reply
    assert _build_evidence_grounded_reply(
        query="VisionBeam projector built-in oxygen sensor calibration interval",
        customer_name="Arun",
        context=context,
    ) is None
    unsupported_reply = _build_documentation_limitation_reply(
        customer_name="Arun",
        product=SimpleNamespace(name="VisionBeam 4K Portable Projector", sku="ST-VIS-002"),
    )
    assert "ST-VIS-002" in unsupported_reply
    assert "exact model number" not in unsupported_reply
    assert "do not want to guess" in unsupported_reply


def test_sql_scope_preserves_model_dimension_and_product_association_filters():
    from sqlalchemy.dialects import postgresql

    class _CaptureDb:
        statement = None

        def scalar(self, _query):
            return 3

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        def execute(self, statement):
            self.statement = statement
            return _Rows([])

    db = _CaptureDb()
    with patch("app.services.rag.retrieval.embedding_service.get_embedding", return_value=[0.0] * 384):
        assert search_knowledge_base("screen flickers", db, product_id=3) == []

    sql = str(db.statement.compile(dialect=postgresql.dialect())).lower()
    assert "knowledge_documents.product_id =" in sql
    assert "knowledge_documents.product_id is null" in sql
    assert "knowledge_documents.document_type in" in sql
    assert "knowledge_chunks.embedding_model =" in sql
    assert "knowledge_chunks.embedding_dimension =" in sql


def test_unknown_product_search_is_limited_to_unassigned_general_docs():
    from sqlalchemy.dialects import postgresql

    class _CaptureDb:
        statement = None

        def scalar(self, _query):
            return None

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        def execute(self, statement):
            self.statement = statement
            return _Rows([])

    db = _CaptureDb()
    with patch("app.services.rag.retrieval.embedding_service.get_embedding", return_value=[0.0] * 384):
        assert search_knowledge_base("unknown model troubleshooting", db) == []

    sql = str(db.statement.compile(dialect=postgresql.dialect())).lower()
    assert "knowledge_documents.product_id is null" in sql
    assert "knowledge_documents.document_type in" in sql
    assert "knowledge_documents.product_id =" not in sql
