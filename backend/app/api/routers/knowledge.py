"""Knowledge Base and RAG Document management router."""
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_user, get_db, require_agent_or_above, require_manager_or_admin
from app.core.config import settings, WORKSPACE_ROOT
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.product import Product
from app.models.user import User
from app.schemas.knowledge import KnowledgeChunkOut, KnowledgeDocOut, SearchQuery, SearchResult
from app.services.rag.chunking import validate_file, extract_text_from_file
from app.services.rag.retrieval import identify_product_id, ingest_document, search_knowledge_base

router = APIRouter(prefix="/api/knowledge", tags=["Knowledge & RAG"])

UPLOAD_FOLDER = WORKSPACE_ROOT / settings.upload_dir
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


@router.get("/documents", response_model=list[KnowledgeDocOut])
def list_documents(
    product_id: int | None = Query(None),
    is_indexed: bool | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List knowledge base documents with indexing status and chunk count."""
    query = select(KnowledgeDocument)
    if product_id is not None:
        query = query.where(KnowledgeDocument.product_id == product_id)
    if is_indexed is not None:
        query = query.where(KnowledgeDocument.is_indexed == is_indexed)

    return db.scalars(query.order_by(KnowledgeDocument.id.desc()).offset(skip).limit(limit)).all()


@router.get("/documents/{doc_id}", response_model=KnowledgeDocOut)
def get_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve knowledge document details by ID."""
    doc = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/documents/{doc_id}/chunks", response_model=list[KnowledgeChunkOut])
def get_document_chunks(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve indexed chunks and embedding metadata for a document."""
    chunks = db.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc_id).order_by(KnowledgeChunk.chunk_index)
    ).all()
    results = []
    for c in chunks:
        out = KnowledgeChunkOut.model_validate(c)
        out.has_embedding = c.embedding is not None
        results.append(out)
    return results


@router.post("/upload", response_model=KnowledgeDocOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form("manual"),
    product_id: int | None = Form(None),
    auto_index: bool = Form(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager_or_admin),
):
    """Upload PDF, TXT, or MD support manual, validate, and automatically index for RAG."""
    # Read file content safely
    contents = await file.read()
    file_size = len(contents)

    try:
        validate_file(file.filename, file_size)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    safe_filename = Path(file.filename).name.replace(" ", "_")
    target_path = UPLOAD_FOLDER / (
        f"{Path(safe_filename).stem}_{uuid4().hex[:12]}{Path(safe_filename).suffix}"
    )

    # Save to disk
    with open(target_path, "wb") as f:
        f.write(contents)

    if product_id is None:
        # Infer association from the full title, filename, and extracted body.
        # The shared catalog matcher returns None when the evidence is
        # ambiguous, leaving the document explicitly unassigned for safe
        # general-document retrieval rules.
        try:
            document_text = extract_text_from_file(str(target_path))
        except Exception:
            document_text = ""
        product_id = identify_product_id(
            f"{title}\n{safe_filename}\n{document_text}", db
        )

    ext = Path(safe_filename).suffix.lstrip(".").lower()
    doc = KnowledgeDocument(
        title=title,
        filename=safe_filename,
        file_path=str(target_path),
        file_type=ext,
        file_size_bytes=file_size,
        document_type=document_type,
        product_id=product_id,
        is_indexed=False,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    if auto_index:
        try:
            ingest_document(doc.id, db)
            db.refresh(doc)
        except Exception as e:
            # Keep the upload and make its indexing failure visible for retry.
            doc.is_indexed = False
            doc.error_message = f"Indexing failed: {type(e).__name__}"
            db.commit()

    return doc


@router.post("/reindex/{doc_id}", response_model=KnowledgeDocOut)
def reindex_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager_or_admin),
):
    """Re-extract and re-index an existing knowledge document."""
    doc = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        ingest_document(doc.id, db)
        db.refresh(doc)
        return doc
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reindexing failed: {str(e)}")


@router.post("/search", response_model=list[SearchResult])
def search_knowledge(
    payload: SearchQuery,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Perform PostgreSQL pgvector semantic search over company knowledge base."""
    return search_knowledge_base(
        query=payload.query,
        db=db,
        product_id=payload.product_id,
        top_k=payload.top_k,
        min_similarity=payload.min_similarity,
    )


@router.post("/seed-sample-docs")
def seed_sample_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager_or_admin),
):
    """Auto-ingest sample knowledge base documents from data/sample_knowledge_base/."""
    sample_dir = WORKSPACE_ROOT / settings.sample_knowledge_dir
    if not sample_dir.exists():
        raise HTTPException(status_code=404, detail="Sample knowledge directory not found")

    files_info = [
        ("SoundPulse_Pro_Manual.txt", "SoundPulse Pro ANC Headphones User & Troubleshooting Guide", "manual", "ST-AUD-001"),
        ("VisionBeam_4K_Projector_Guide.md", "VisionBeam 4K Portable Projector User & Calibration Manual", "manual", "ST-VIS-002"),
        ("AeroBook_Ultra_14_Manual.txt", "AeroBook Ultra 14 Display & Hardware Manual", "manual", "ST-CMP-003"),
        ("SmartTech_Warranty_Policy.md", "SmartTech Global Warranty, Refund, and Return Policies", "policy", None),
    ]

    ingested_count = 0
    for filename, title, doc_type, sku in files_info:
        file_path = sample_dir / filename
        if not file_path.exists():
            continue

        prod_id = None
        if sku:
            prod = db.scalar(select(Product).where(Product.sku == sku))
            if prod:
                prod_id = prod.id

        existing = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.filename == filename))
        if not existing:
            doc = KnowledgeDocument(
                title=title,
                filename=filename,
                file_path=str(file_path),
                file_type=Path(filename).suffix.lstrip(".").lower(),
                file_size_bytes=file_path.stat().st_size,
                document_type=doc_type,
                product_id=prod_id,
                is_indexed=False,
            )
            db.add(doc)
            db.commit()
            db.refresh(doc)
            ingest_document(doc.id, db)
            ingested_count += 1
        elif not existing.is_indexed:
            ingest_document(existing.id, db)
            ingested_count += 1

    return {"status": "success", "ingested_count": ingested_count}
