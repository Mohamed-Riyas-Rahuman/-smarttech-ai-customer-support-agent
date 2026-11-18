"""Knowledge ingestion, pgvector semantic retrieval, and citation assembly."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, func, text
from sqlalchemy.orm import Session, selectinload
from pgvector.sqlalchemy import Vector

from app.core.logging import logger
from app.core.config import WORKSPACE_ROOT
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.schemas.knowledge import SearchResult
from app.services.rag.chunking import chunk_text, extract_text_from_file
from app.services.rag.embeddings import embedding_service


@dataclass
class RetrievedContext:
    total_chunks: int
    citations: list[str]
    formatted_text: str
    results: list[SearchResult]


@dataclass(frozen=True)
class ProductResolution:
    """Product identity without collapsing unknown and ambiguous mentions."""

    state: str  # registered, unregistered, ambiguous, or unspecified
    product_id: int | None = None
    candidate_ids: tuple[int, ...] = ()


TROUBLESHOOTING_TERMS = {
    "firmware",
    "update",
    "stuck",
    "stall",
    "stalled",
    "flashing",
    "flash",
    "led",
    "reset",
    "recovery",
    "troubleshoot",
    "troubleshooting",
    "headphones",
    "bluetooth",
    "flicker",
    "flickering",
    "screen",
    "display",
    "blank",
    "black",
    "audio",
    "sound",
    "charge",
    "charging",
    "battery",
    "power",
    "boot",
    "reboot",
    "restart",
    "crash",
    "freeze",
    "freezing",
    "noise",
    "distortion",
    "overheat",
    "overheating",
    "issue",
    "problem",
    "broken",
    "fail",
    "failed",
    "failing",
    "disconnect",
    "disconnecting",
    "connect",
    "connection",
    "hdmi",
    "port",
    "cable",
    "monitor",
}

GENERIC_PRODUCT_TOKENS = {
    "product", "device", "laptop", "notebook", "projector", "headphone",
    "headphones", "headset", "earbuds", "earphones", "monitor", "display",
    "speaker", "speakers", "soundbar", "keyboard", "mouse", "printer",
    "tablet", "camera", "router", "gateway", "charger", "charging",
    "adapter", "accessory", "accessories", "computer", "phone", "mobile",
    "television", "screen", "hub", "station", "smartphone", "smartphones",
    "smartwatch", "smartwatches",
}


def _normalize_text(value: str | None) -> str:
    """Normalize text for safe product-name comparison."""

    return re.sub(
        r"[^a-z0-9]+",
        " ",
        (value or "").lower(),
    ).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    """Check whether a normalized phrase occurs as complete words."""

    if not text or not phrase:
        return False

    return bool(
        re.search(
            rf"(?:^|\s){re.escape(phrase)}(?:$|\s)",
            text,
        )
    )


def _is_troubleshooting_query(query: str) -> bool:
    """Identify queries that may need troubleshooting-focused retrieval."""

    words = set(re.findall(r"[a-z0-9]+", query.lower()))

    return bool(words & TROUBLESHOOTING_TERMS)


def resolve_product(
    query: str,
    db: Session,
    customer_id: int | None = None,
) -> ProductResolution:
    """
    Resolve a product while keeping an unregistered product, an ambiguous
    catalog match, and an email without a product identifier distinct.
    """

    from app.models.product import Product

    normalized_query = _normalize_text(query)

    if not normalized_query:
        return ProductResolution("unspecified")

    products = db.scalars(
        select(Product).order_by(Product.id)
    ).all()

    matches: list[tuple[int, int, int]] = []
    for product in products:

        normalized_name = _normalize_text(product.name)
        normalized_sku = _normalize_text(product.sku)
        normalized_aliases = [
            _normalize_text(alias)
            for alias in re.findall(
                r"(?i)\b(?:aliases?|also known as|aka)\s*:?\s*([^.;\n]+)",
                getattr(product, "description", None) or "",
            )
        ]

        # Exact SKU match has the highest priority.
        if normalized_sku and _contains_phrase(
            normalized_query,
            normalized_sku,
        ):
            matches.append((3, len(normalized_sku), product.id))

        # Complete product name match.
        if normalized_name and _contains_phrase(
            normalized_query,
            normalized_name,
        ):
            matches.append((2, len(normalized_name), product.id))

        for alias in normalized_aliases:
            if alias and _contains_phrase(normalized_query, alias):
                matches.append((2, len(alias), product.id))

        # Match partial product/model names, including a model mentioned
        # naturally in the email. Short generic words are not identifiers.
        name_tokens = normalized_name.split()
        for start in range(len(name_tokens)):
            for end in range(start + 1, len(name_tokens) + 1):
                phrase = " ".join(name_tokens[start:end])
                if (
                    len(phrase) >= 4
                    and not (len(name_tokens[start:end]) == 1 and phrase in GENERIC_PRODUCT_TOKENS)
                    and _contains_phrase(normalized_query, phrase)
                ):
                    matches.append((1, len(phrase), product.id))

        # Explicit catalog aliases and model numbers in the product description
        # are supported without treating arbitrary feature words as aliases.
        for alias in normalized_aliases:
            alias_tokens = alias.split()
            for start in range(len(alias_tokens)):
                for end in range(start + 1, len(alias_tokens) + 1):
                    phrase = " ".join(alias_tokens[start:end])
                    if len(phrase) >= 4 and _contains_phrase(normalized_query, phrase):
                        matches.append((1, len(phrase), product.id))

    if matches:

        # Prefer SKU matches over product-name matches.
        # Prefer longer names over shorter overlapping names.
        highest_priority = max(
            (priority, length)
            for priority, length, _ in matches
        )

        best_matches = {
            product_id
            for priority, length, product_id in matches
            if (priority, length) == highest_priority
        }

        # Only identify a product when the best match is unambiguous.
        if len(best_matches) == 1:
            return ProductResolution("registered", next(iter(best_matches)))

        logger.warning(
            "Ambiguous product identification for query: %s",
            query,
        )

        return ProductResolution("ambiguous", candidate_ids=tuple(sorted(best_matches)))

    # Use customer device information only when the customer explicitly
    # refers to their own device and exactly one product is registered.
    refers_to_own_device = bool(
        re.search(
            r"\b(?:my|our)\s+"
            r"(?:device|laptop|headphones|projector|monitor|product)\b",
            query,
            re.I,
        )
    )

    if customer_id is not None and refers_to_own_device:

        from app.models.device import Device

        product_ids = set(
            db.scalars(
                select(Device.product_id).where(
                    Device.customer_id == customer_id,
                    Device.is_registered.is_(True),
                    Device.product_id.is_not(None),
                )
            ).all()
        )

        if len(product_ids) == 1:
            return ProductResolution("registered", next(iter(product_ids)))
        if len(product_ids) > 1:
            return ProductResolution("ambiguous", candidate_ids=tuple(sorted(product_ids)))

    if _looks_like_unregistered_product(query, products):
        return ProductResolution("unregistered")
    return ProductResolution("unspecified")


def identify_product_id(
    query: str,
    db: Session,
    customer_id: int | None = None,
) -> int | None:
    """Compatibility wrapper for callers that only need an unambiguous ID."""

    return resolve_product(query, db, customer_id=customer_id).product_id


def _looks_like_unregistered_product(query: str, products: list) -> bool:
    """Recognize explicit but uncatalogued product names/model identifiers."""

    raw = query or ""
    normalized = _normalize_text(raw)
    product_types = GENERIC_PRODUCT_TOKENS - {
        "product", "device", "computer", "phone", "mobile", "screen",
        "display", "accessory", "accessories", "station", "hub",
    }
    has_product_type = any(
        _contains_phrase(normalized, product_type)
        for product_type in product_types
    )
    sku_like_identifier = re.search(r"\b[A-Z]{2,}(?:[-_/][A-Z0-9]{2,})+\b", raw)
    explicit_identifier_label = bool(re.search(
        r"\b(?:sku|model(?:\s+number)?)\s*[:#-]?\s*[A-Z0-9][A-Z0-9-]{2,}\b",
        raw,
        re.I,
    ))
    excluded_code_prefix = bool(
        sku_like_identifier
        and sku_like_identifier.group(0).split("-")[0].casefold()
        in {"ord", "order", "ticket", "case", "serial", "tr"}
    )
    if explicit_identifier_label or (
        sku_like_identifier and has_product_type and not excluded_code_prefix
    ):
        return True

    # Camel-cased product names remain identifiable even when the customer
    # omits a generic category word (for example, "NovaPhone X2" or
    # "AcmeBeam keeps restarting").
    if re.search(r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]+)+\b", raw):
        return True

    # A recognized catalog brand paired with a product category indicates an
    # uncatalogued variant when no exact/unique catalog match was found.
    if has_product_type:
        catalog_brands = {
            _normalize_text(product.name).split()[0]
            for product in products
            if _normalize_text(product.name)
        }
        if any(_contains_phrase(normalized, brand) for brand in catalog_brands if len(brand) >= 4):
            return True

        # Title-cased model labels are product identifiers; ordinary
        # category words are not.
        if re.search(
            r"\b(?:model|product)\s+(?:name\s+)?[A-Z0-9][A-Za-z0-9-]{2,}\b",
            raw,
        ):
            return True
    return False


def _make_search_result(chunk, distance, retrieval_method: str = "semantic") -> SearchResult:
    """Build a result while preserving the original source citation."""

    similarity = max(
        0.0,
        1.0 - float(
            distance if distance is not None else 1.0
        ),
    )
    doc = chunk.document

    try:
        metadata = (
            json.loads(chunk.metadata_json)
            if chunk.metadata_json
            else {}
        )

        if not isinstance(metadata, dict):
            metadata = {}

    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "Invalid metadata JSON for knowledge chunk %s",
            chunk.id,
        )
        metadata = {}

    citation_parts = [
        f"Source: {doc.title} ({doc.filename})"
    ]

    if metadata.get("section"):
        citation_parts.append(
            str(metadata["section"])
        )

    page = metadata.get("page")

    if isinstance(page, int) and page > 0:
        citation_parts.append(
            f"Page {page}"
        )

    citation_parts.append(
        f"Relevance {similarity:.0%}"
    )

    return SearchResult(
        chunk_id=chunk.id,
        document_id=doc.id,
        document_title=doc.title,
        filename=doc.filename,
        content=chunk.content,
        similarity=round(similarity, 4),
        citation=" | ".join(citation_parts),
        retrieval_method=retrieval_method,
    )


def _backup_document_chunks(document: KnowledgeDocument, chunks: list[KnowledgeChunk]) -> Path | None:
    """Persist the current chunk set before a re-index replaces any vectors."""
    if not chunks:
        return None
    backup_dir = WORKSPACE_ROOT / "backups" / "knowledge_chunks"
    backup_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "backup_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "document": {
            "id": document.id,
            "title": document.title,
            "filename": document.filename,
            "product_id": document.product_id,
        },
        "chunks": [
            {
                "id": chunk.id,
                "document_id": chunk.document_id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "metadata_json": chunk.metadata_json,
                "embedding": [float(value) for value in chunk.embedding] if chunk.embedding is not None else None,
                "embedding_model": chunk.embedding_model,
                "embedding_dimension": chunk.embedding_dimension,
                "created_at": chunk.created_at.isoformat() if chunk.created_at else None,
            }
            for chunk in chunks
        ],
    }
    backup_path = backup_dir / (
        f"document_{document.id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid4().hex[:8]}.json"
    )
    temporary_path = backup_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(backup_path)
    return backup_path


def restore_document_chunks(backup_path: str | Path, db: Session) -> int:
    """Restore a previously backed up chunk set as one database transaction."""
    payload = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    if payload.get("backup_version") != 1 or not payload.get("document", {}).get("id"):
        raise ValueError("Unsupported or incomplete knowledge chunk backup.")
    document_id = int(payload["document"]["id"])
    if db.get(KnowledgeDocument, document_id) is None:
        raise ValueError(f"Knowledge document {document_id} no longer exists.")
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("Knowledge chunk backup contains no chunks.")
    try:
        for saved in chunks:
            vector = saved.get("embedding")
            if vector is not None and len(vector) != int(saved["embedding_dimension"]):
                raise ValueError(f"Backup chunk {saved.get('id')} has an incompatible vector dimension.")
        db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).delete(synchronize_session=False)
        for saved in chunks:
            db.add(KnowledgeChunk(
                id=saved["id"], document_id=document_id,
                chunk_index=saved["chunk_index"], content=saved["content"],
                metadata_json=saved.get("metadata_json"), embedding=saved.get("embedding"),
                embedding_model=saved["embedding_model"],
                embedding_dimension=saved["embedding_dimension"],
                created_at=datetime.fromisoformat(saved["created_at"]) if saved.get("created_at") else None,
            ))
        document = db.get(KnowledgeDocument, document_id)
        document.chunk_count = len(chunks)
        document.is_indexed = all(
            item.get("embedding") is not None
            and item.get("embedding_model") == embedding_service.model_name
            and int(item.get("embedding_dimension", 0)) == embedding_service.dimension
            for item in chunks
        )
        document.error_message = None
        db.flush()
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text(
                "SELECT setval(pg_get_serial_sequence('knowledge_chunks', 'id'), "
                "GREATEST((SELECT COALESCE(MAX(id), 1) FROM knowledge_chunks), 1), true)"
            ))
        db.commit()
        return len(chunks)
    except Exception:
        db.rollback()
        raise


def ingest_document(
    document_id: int,
    db: Session,
) -> int:
    """
    Extract, chunk, embed, and persist a knowledge document.

    This function changes the database only when explicitly called.
    """

    doc = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id
        )
    )

    if not doc:
        raise ValueError(
            f"KnowledgeDocument {document_id} not found."
        )

    from app.models.product import Product
    product = db.get(Product, doc.product_id) if doc.product_id else None
    document_identity = " ".join(
        part for part in (
            doc.title,
            product.name if product else "",
            product.sku if product else "",
        ) if part
    )

    try:
        raw_text = extract_text_from_file(
            doc.file_path
        )

        if not raw_text.strip():
            raise ValueError(
                "Document contains no readable text."
            )

        chunks = chunk_text(raw_text)

        if not chunks:
            raise ValueError(
                "No valid chunks generated from document."
            )

        # Generate embeddings before replacing existing chunks.
        prepared_chunks = []

        for chunk in chunks:

            vector = embedding_service.get_embedding(
                f"{document_identity}\n{chunk.content}"
            )

            prepared_chunks.append(
                {
                    "chunk": chunk,
                    "embedding": vector,
                }
            )

        existing_chunks = db.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.document_id == doc.id
            )
        ).all()

        backup_path = _backup_document_chunks(doc, existing_chunks)

        for existing_chunk in existing_chunks:
            db.delete(existing_chunk)

        db.flush()

        for prepared in prepared_chunks:

            chunk = prepared["chunk"]

            db.add(
                KnowledgeChunk(
                    document_id=doc.id,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    metadata_json=json.dumps({
                        **chunk.metadata,
                        "document_id": doc.id,
                        "product_id": doc.product_id,
                        "product_name": product.name if product else None,
                        "sku": product.sku if product else None,
                        "document_title": doc.title,
                    }),
                    embedding=prepared["embedding"],
                    embedding_model=embedding_service.model_name,
                    embedding_dimension=embedding_service.dimension,
                )
            )

        doc.is_indexed = True
        doc.chunk_count = len(chunks)
        doc.error_message = None

        db.commit()
        db.refresh(doc)

        logger.info(
            "Successfully indexed document '%s' (%s chunks; prior chunk backup=%s).",
            doc.title,
            len(chunks),
            str(backup_path) if backup_path else "not_needed",
        )

        return len(chunks)

    except Exception as exc:

        db.rollback()

        logger.error(
            "Failed ingesting document %s: %s",
            document_id,
            exc,
        )

        raise


def search_knowledge_base(
    query: str,
    db: Session,
    product_id: int | None = None,
    top_k: int = 4,
    min_similarity: float = 0.45,
) -> list[SearchResult]:
    """
    Search with product-scoped cosine similarity and PostgreSQL full text.

    Product-specific searches retrieve:
        1. Documents assigned to the exact product.
        2. Unassigned company-wide policy documents.

    Searches without a product ID retrieve only unassigned policy
    documents. They never search other products' manuals.
    """

    if not query or not query.strip() or top_k <= 0:
        return []

    query = query.strip()

    from app.models.product import Product

    # Validate the product ID before running retrieval.
    if product_id is not None:

        product_exists = db.scalar(
            select(Product.id).where(
                Product.id == product_id
            )
        )

        if product_exists is None:

            logger.warning(
                "Invalid product_id=%s. Knowledge retrieval stopped.",
                product_id,
            )

            return []

    query_vector = embedding_service.get_embedding(query)

    distance_expr = KnowledgeChunk.embedding.cast(
        Vector(embedding_service.dimension)
    ).cosine_distance(query_vector)

    stmt = (
        select(
            KnowledgeChunk,
            distance_expr.label("distance"),
        )
        .join(
            KnowledgeDocument,
            KnowledgeChunk.document_id == KnowledgeDocument.id,
        )
        .options(
            selectinload(KnowledgeChunk.document)
        )
        .where(
            KnowledgeDocument.is_indexed.is_(True),
            KnowledgeChunk.embedding.is_not(None),
            KnowledgeChunk.embedding_model == embedding_service.model_name,
            KnowledgeChunk.embedding_dimension == embedding_service.dimension,
        )
    )

    # ---------------------------------------------------------
    # STRICT PRODUCT FILTERING
    # ---------------------------------------------------------

    if product_id is not None:

        # Retrieve only the exact product's documents,
        # plus unassigned company-wide policy documents.
        stmt = stmt.where(
            (
                KnowledgeDocument.product_id == product_id
            )
            |
            (
                KnowledgeDocument.product_id.is_(None)
                & KnowledgeDocument.document_type.in_(("policy", "faq"))
            )
        )

    else:

        # Product identity is unknown.
        # Never retrieve product-specific manuals or FAQs.
        # Only retrieve unassigned company-wide policy documents.
        stmt = stmt.where(
            KnowledgeDocument.product_id.is_(None),
            KnowledgeDocument.document_type.in_(("policy", "faq")),
        )

    # ---------------------------------------------------------
    # SEMANTIC SEARCH
    # ---------------------------------------------------------

    candidate_limit = max(top_k * 12, 48)
    semantic_rows = db.execute(
        stmt.order_by(distance_expr).limit(candidate_limit)
    ).all()

    # PostgreSQL simple dictionaries preserve technical tokens such as HDMI,
    # HDCP, EDID, SKU fragments, and reference codes. This supplements vectors
    # for exact terms that generic sentence embeddings can under-rank.
    lexical_rows = []
    try:
        stop_words = {
            "the", "and", "for", "with", "that", "this", "from", "have",
            "your", "you", "are", "was", "were", "been", "when", "what",
            "how", "can", "could", "would", "should", "please", "recently",
            "after", "before", "into", "through", "still", "very", "much",
            "issue", "problem", "help", "support", "product", "device",
            # Common function words carry almost no topical evidence. In
            # particular, allowing a lone "not" hit admitted unrelated FAQ
            # chunks for vague troubleshooting questions.
            "not", "on", "off", "out", "my", "its", "their", "them",
            "it", "do", "does", "did", "is", "am", "be", "me", "i",
        }
        lexical_terms = list(dict.fromkeys(
            token for token in re.findall(r"[a-z0-9]+", query.casefold())
            if len(token) >= 3 and token not in stop_words
        ))[:32]
        dialect_name = db.get_bind().dialect.name
        if dialect_name == "postgresql" and lexical_terms:
            lexical_query = func.to_tsquery("simple", " | ".join(lexical_terms))
            lexical_vector = func.to_tsvector("simple", KnowledgeChunk.content)
            lexical_rank = func.ts_rank_cd(lexical_vector, lexical_query)
            lexical_stmt = (
                stmt.where(lexical_vector.bool_op("@@")(lexical_query))
                .order_by(lexical_rank.desc())
                .limit(candidate_limit)
            )
            lexical_rows = db.execute(lexical_stmt).all()
    except Exception as exc:
        # Keep semantic retrieval available for non-PostgreSQL unit adapters;
        # production diagnostics make the degraded strategy visible.
        logger.warning("Knowledge full-text search unavailable; using vector candidates (%s)", type(exc).__name__)

    candidates: dict[int, dict] = {}
    for rank, (chunk, distance) in enumerate(semantic_rows, start=1):
        candidates[chunk.id] = {
            "chunk": chunk, "distance": distance,
            "semantic_rank": rank, "lexical_rank": None,
        }
    for rank, (chunk, distance) in enumerate(lexical_rows, start=1):
        item = candidates.setdefault(chunk.id, {
            "chunk": chunk, "distance": distance,
            "semantic_rank": None, "lexical_rank": rank,
        })
        item["lexical_rank"] = rank

    # Reciprocal-rank fusion is robust to the different scales of cosine
    # similarity and full-text rank; a meaningful exact-term hit can recover
    # a chunk whose vector score falls just below the former hard cutoff.
    ranked = []
    for item in candidates.values():
        semantic_rank = item["semantic_rank"]
        lexical_rank_value = item["lexical_rank"]
        distance = item["distance"]
        similarity = max(0.0, 1.0 - float(distance if distance is not None else 1.0))
        if similarity < min_similarity and lexical_rank_value is None:
            continue
        fused = (
            (1.0 / (60 + semantic_rank) if semantic_rank else 0.0)
            + (1.25 / (60 + lexical_rank_value) if lexical_rank_value else 0.0)
        )
        ranked.append((fused, similarity, item))
    ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)

    results: list[SearchResult] = []
    seen_chunks: set[int] = set()
    seen_text: set[str] = set()
    for _fused, _similarity, item in ranked:
        chunk = item["chunk"]
        normalized_content = re.sub(r"\W+", " ", chunk.content.casefold()).strip()
        if chunk.id in seen_chunks or normalized_content in seen_text:
            continue
        seen_chunks.add(chunk.id)
        seen_text.add(normalized_content)
        results.append(_make_search_result(
            chunk, item["distance"],
            "hybrid" if item["lexical_rank"] is not None else "semantic",
        ))
        if len(results) >= top_k:
            break

    # If a high-relevance chunk is one part of a split section or procedure,
    # include only its immediate neighbors from the same indexed document and
    # same section. This restores complete evidence without widening product
    # filtering or returning unrelated documents.
    for index, result in enumerate(results):
        # Lightweight retrieval test adapters may implement only scalar/execute;
        # adjacent-section enrichment is an ORM optimization, not a requirement
        # for those adapters.
        if not callable(getattr(db, "get", None)):
            break
        source_chunk = db.get(KnowledgeChunk, result.chunk_id)
        if not source_chunk:
            continue
        try:
            source_metadata = json.loads(source_chunk.metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            source_metadata = {}
        if not isinstance(source_metadata, dict):
            source_metadata = {}
        source_section = source_metadata.get("section")
        if not source_section:
            continue
        neighbors = db.scalars(
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.document_id == source_chunk.document_id,
                KnowledgeChunk.chunk_index.between(
                    source_chunk.chunk_index - 1,
                    source_chunk.chunk_index + 1,
                ),
            )
            .order_by(KnowledgeChunk.chunk_index)
        ).all()
        section_parts = []
        for neighbor in neighbors:
            try:
                neighbor_metadata = json.loads(neighbor.metadata_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(neighbor_metadata, dict):
                continue
            if neighbor_metadata.get("section") == source_section:
                section_parts.append(neighbor.content.strip())
        if section_parts:
            merged_content = "\n\n".join(dict.fromkeys(section_parts))
            results[index] = result.model_copy(update={"content": merged_content})

    logger.info(
        "Knowledge search diagnostics: product_id=%s strategy=%s query=%r "
        "product_filter=%s embedding_model=%s embedding_dimension=%s "
        "semantic_candidates=%s lexical_candidates=%s selected=%s threshold=%.3f",
        product_id, "hybrid" if lexical_rows else "vector_only", query[:500],
        ("assigned product + unassigned policy/FAQ" if product_id else "unassigned policy/FAQ only"),
        embedding_service.model_name, embedding_service.dimension,
        len(semantic_rows), len(lexical_rows),
        [{"chunk_id": result.chunk_id, "document_id": result.document_id,
          "similarity": result.similarity} for result in results], min_similarity,
    )
    logger.debug(
        "Knowledge candidate diagnostics: product_id=%s semantic=%s lexical=%s "
        "rejected=%s",
        product_id,
        [{"chunk_id": row[0].id, "document_id": row[0].document_id,
          "similarity": round(max(0.0, 1.0 - float(row[1] if row[1] is not None else 1.0)), 4)}
         for row in semantic_rows[:candidate_limit]],
        [{"chunk_id": row[0].id, "document_id": row[0].document_id, "rank": rank}
         for rank, row in enumerate(lexical_rows[:candidate_limit], 1)],
        [{"chunk_id": row[0].id, "reason": "below_similarity_threshold_without_lexical_match"}
         for row in semantic_rows
         if max(0.0, 1.0 - float(row[1] if row[1] is not None else 1.0)) < min_similarity
         and row[0].id not in {lex[0].id for lex in lexical_rows}],
    )
    return results


def retrieve_knowledge_context(
    query: str,
    db: Session,
    product_id: int | None = None,
    top_k: int = 4,
) -> RetrievedContext:
    """Return semantic results and source citations for the LangChain workflow."""

    results = search_knowledge_base(
        query=query,
        db=db,
        product_id=product_id,
        top_k=top_k,
    )

    # Keep one citation per distinct source location.
    unique_results = []

    seen_citations = set()

    for result in results:

        key = tuple(
            part.strip().casefold()
            for part in result.citation.split(" | ")
            if not part.strip().startswith("Relevance ")
        )

        if key not in seen_citations:

            seen_citations.add(key)

            unique_results.append(result)

    citations = []

    for result in unique_results:

        citation_parts = result.citation.split(" | ")

        source_prefix = (
            f"Source: {result.document_title} "
            f"({result.filename})"
        )

        section = next(
            (
                part
                for part in citation_parts
                if part != source_prefix
                and not part.startswith(("Page ", "Relevance "))
            ),
            None,
        )

        page = next(
            (
                int(part[5:])
                for part in citation_parts
                if part.startswith("Page ")
                and part[5:].isdigit()
            ),
            None,
        )

        citations.append(
            {
                "document": result.document_title,
                "filename": result.filename,
                "section": section,
                "page": page,
                "similarity": result.similarity,
                "citation": result.citation,
            }
        )

    return RetrievedContext(
        total_chunks=len(results),
        citations=citations,
        formatted_text=assemble_context_block(results),
        results=results,
    )


def assemble_context_block(
    results: list[SearchResult],
) -> str:
    """Format search results into a context string for LLM prompting."""

    if not results:
        return (
            "No relevant documentation found in company knowledge base."
        )

    context_lines = [
        "=== APPROVED SMARTTECH COMPANY DOCUMENTATION ==="
    ]

    for index, result in enumerate(results, 1):

        context_lines.append(
            f"\n[Evidence {index}]: {result.citation}"
        )

        context_lines.append(
            f"Relevance: {int(result.similarity * 100)}%"
        )

        context_lines.append(
            f"Content:\n{result.content}"
        )

        context_lines.append(
            "-" * 40
        )

    return "\n".join(context_lines)


def select_relevant_evidence_excerpt(
    query: str,
    results: list[SearchResult],
) -> str | None:
    """Select an exact issue block only when the query shares a specific phrase with it.

    This is used when generated prose fails grounding validation. It allows the
    workflow to return source text for a matching issue, while an unrelated
    chunk about the same product cannot be presented as an answer.
    """

    excluded = GENERIC_PRODUCT_TOKENS | {
        "smarttech", "visionbeam", "aerobook", "4k", "sku", "st", "vis",
        "support", "team", "hello", "recently", "purchased", "using",
        "please", "could", "would", "thank", "thanks", "what", "when",
        "where", "does", "have", "has", "this", "that", "with", "from",
        "your", "you", "they", "them", "their", "about", "issue",
        "problem", "help", "troubleshoot", "troubleshooting", "error",
    }
    tokens = _normalize_text(query).split()
    query_phrases = {
        f"{tokens[index]} {tokens[other]}"
        for index, left in enumerate(tokens)
        for other in range(index + 1, min(index + 5, len(tokens)))
        if left not in excluded
        and tokens[other] not in excluded
        and len(left) >= 3
        and len(tokens[other]) >= 3
    }
    if not query_phrases:
        return None

    candidates: list[tuple[int, int, str, str]] = []
    for rank, result in enumerate(results):
        content = result.content or ""
        issue_heads = list(re.finditer(
            r"(?im)^\s*Issue\s+\d+(?:\.\d+)*\s*:\s*[^\r\n]+",
            content,
        ))
        blocks: list[str] = []
        if issue_heads:
            for index, heading in enumerate(issue_heads):
                end = issue_heads[index + 1].start() if index + 1 < len(issue_heads) else len(content)
                block = content[heading.start():end].strip()
                # A neighboring section title can precede the next issue heading.
                final_line = block.rsplit("\n", 1)[-1].strip()
                if (
                    index + 1 < len(issue_heads)
                    and re.match(r"^\d+\.\s+[A-Z][^\r\n]*$", final_line)
                    and not re.search(r"[.!?:;]$", final_line)
                ):
                    block = block.rsplit("\n", 1)[0].strip()
                blocks.append(block)
        else:
            blocks.append(content.strip())

        for block in blocks:
            normalized = _normalize_text(block)
            score = sum(phrase in normalized for phrase in query_phrases)
            if score:
                source = f"Source: {result.document_title} ({result.filename})\n{block}"
                candidates.append((score, -rank, result.citation, source))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]
