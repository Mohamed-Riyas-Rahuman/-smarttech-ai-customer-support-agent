"""Document text extraction and chunking service."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from app.core.config import settings
from app.core.logging import logger


MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}


@dataclass
class TextChunk:
    """Represents a chunk of extracted document text."""

    chunk_index: int
    content: str
    metadata: dict


def validate_file(filename: str, file_size: int) -> None:
    """Validate an uploaded file's extension and size."""

    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{extension}'. "
            f"Allowed types: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    if file_size <= 0:
        raise ValueError("The uploaded file is empty.")

    if file_size > MAX_FILE_SIZE_BYTES:
        raise ValueError("The uploaded file exceeds the 10 MB limit.")


def extract_text_from_file(file_path: str) -> str:
    """Extract readable text from a supported PDF, TXT, or Markdown file."""

    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    extension = path.suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {extension}")

    if path.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise ValueError("The file exceeds the 10 MB limit.")

    if extension == ".pdf":
        reader = PdfReader(str(path))
        pages = []

        for page_number, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""

            if page_text.strip():
                pages.append(
                    f"[Page {page_number}]\n{page_text.strip()}"
                )

        return "\n\n".join(pages)

    return path.read_text(encoding="utf-8", errors="replace")


def _extract_chunk_metadata(text: str) -> dict:
    """Extract section, subsection, and PDF page metadata where available."""

    metadata = {}

    section_match = re.search(
        r"(?im)^\s*Section\s+(\d+(?:\.\d+)*)\s*:\s*([^\n]+)",
        text,
    )

    if section_match:
        metadata["section"] = (
            f"Section {section_match.group(1)}: "
            f"{section_match.group(2).strip()}"
        )
    else:
        chapter_match = re.search(
            r"(?im)^\s*(?:Chapter|Part)\s+([\d.]+)\s*:?\s*([^\n]*)",
            text,
        )

        if chapter_match:
            metadata["section"] = (
                f"{chapter_match.group(0).strip()}"
            )

    issue_match = re.search(
        r"(?im)^\s*(Issue\s+[\w.-]+\s*:\s*[^\n]+)", text
    )
    if issue_match:
        metadata["issue"] = issue_match.group(1).strip()

    reference_match = re.search(
        r"(?i)\b(?:Reference|Ref(?:erence)?\s*code)?\s*[:#-]?\s*([A-Z]{2,}[A-Z0-9-]*\d[A-Z0-9-]*)\b",
        text,
    )
    if reference_match:
        metadata["reference"] = reference_match.group(1)

    heading_match = re.search(r"(?m)^\s{0,3}#{1,6}\s+([^\n]+)", text)
    if heading_match and "section" not in metadata:
        metadata["section"] = heading_match.group(1).strip()

    page_match = re.search(r"\[Page\s+(\d+)\]", text)

    if page_match:
        metadata["page"] = int(page_match.group(1))

    return metadata


def _split_long_paragraph(
    paragraph: str,
    chunk_size: int,
    chunk_overlap: int = 0,
) -> list[str]:
    """Split oversized text with a bounded word-aligned overlap."""

    paragraph = paragraph.strip()

    if not paragraph:
        return []

    if len(paragraph) <= chunk_size:
        return [paragraph]

    pieces = []
    remaining = paragraph

    while len(remaining) > chunk_size:
        split_at = remaining.rfind(" ", 0, chunk_size + 1)

        if split_at <= 0:
            split_at = chunk_size

        piece = remaining[:split_at].strip()

        if piece:
            pieces.append(piece)

        next_start = max(0, split_at - min(chunk_overlap, chunk_size // 2))
        while next_start > 0 and remaining[next_start - 1] != " ":
            next_start -= 1
        remaining = remaining[next_start:].strip()

    if remaining:
        pieces.append(remaining)

    return pieces


def _make_chunk(index: int, content: str) -> TextChunk:
    """Create a chunk with extracted metadata."""

    content = content.strip()

    return TextChunk(
        chunk_index=index,
        content=content,
        metadata=_extract_chunk_metadata(content),
    )


def chunk_text(
    text: str,
    chunk_size: int = settings.rag_chunk_size,
    chunk_overlap: int = settings.rag_chunk_overlap,
) -> list[TextChunk]:
    """Split document text while carrying section and issue headings forward."""

    if not text or not text.strip():
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero.")

    if chunk_overlap < 0:
        raise ValueError("chunk_overlap cannot be negative.")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    chunks: list[TextChunk] = []
    heading_pattern = re.compile(
        r"(?im)^\s*(?:#{1,6}\s+|(?:section|chapter|part)\s+|"
        r"issue\s+[\w.-]+\s*:|resolution\s+procedure\b)"
    )
    active_headings: list[str] = []
    current_parts: list[str] = []
    current_length = 0

    def flush() -> None:
        nonlocal current_parts, current_length
        if current_parts:
            chunks.append(_make_chunk(len(chunks), "\n\n".join(current_parts)))
            current_parts = []
            current_length = 0

    def add_text(text_part: str, heading_context: str) -> None:
        nonlocal current_length
        prefix = f"{heading_context}\n" if heading_context else ""
        available = max(1, chunk_size - len(prefix))
        pieces = _split_long_paragraph(text_part, available, chunk_overlap)
        for piece_idx, piece in enumerate(pieces):
            missing_headings = [
                heading for heading in heading_context.splitlines()
                if heading.casefold() not in piece.casefold()
            ]
            heading_prefix = "\n".join(missing_headings)
            rendered = (
                f"{heading_prefix}\n{piece}"
                if missing_headings else piece
            )
            if current_parts and current_length + 2 + len(rendered) > chunk_size:
                flush()
            current_parts.append(rendered)
            current_length += len(rendered) + (2 if len(current_parts) > 1 else 0)
            if len(pieces) > 1 and piece_idx < len(pieces) - 1:
                flush()

    segment_lines: list[str] = []

    def flush_segment() -> None:
        nonlocal segment_lines
        content = "\n".join(segment_lines).strip()
        if content:
            add_text(content, "\n".join(active_headings[-2:]))
        segment_lines = []

    for line in normalized.splitlines():
        line = line.strip()
        if not line:
            continue
        if heading_pattern.match(line):
            flush_segment()
            flush()
            # A section heading replaces the previous section; issue headings
            # retain one parent heading when the source provides one.
            if re.match(r"(?i)(?:section|chapter|part|#{1,6})", line):
                active_headings = [line.lstrip("# ")]
            else:
                parent = (
                    active_headings[-1:]
                    if active_headings and re.match(
                        r"(?i)(?:section|chapter|part|#{1,6})",
                        active_headings[-1],
                    ) else []
                )
                active_headings = parent + [line]
            segment_lines.append(line)
        else:
            segment_lines.append(line)
            # Flush long segments incrementally, keeping issue/section title
            # context attached to each resulting piece.
            # Keep numbered procedures intact. Flushing halfway through a
            # numbered list can split a single instruction across chunks and
            # make otherwise valid evidence unusable to retrieval and drafting.
            is_numbered_step = bool(re.match(r"^\s*\d+[.)]\s+", line))
            if sum(map(len, segment_lines)) >= chunk_size and not is_numbered_step:
                flush_segment()
    flush_segment()
    flush()

    # Guarantee continuous zero-based chunk indices.
    chunks = [
        TextChunk(
            chunk_index=index,
            content=chunk.content,
            metadata=chunk.metadata,
        )
        for index, chunk in enumerate(chunks)
    ]

    logger.info(
        "Chunked document into %s chunks (chunk_size=%s, overlap=%s).",
        len(chunks),
        chunk_size,
        chunk_overlap,
    )

    return chunks

