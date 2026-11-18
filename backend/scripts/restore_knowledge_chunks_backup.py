"""Restore one document's chunk and vector set from an ingestion backup."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.services.rag.retrieval import restore_document_chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_path", help="Path to a knowledge chunk JSON backup")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        restored = restore_document_chunks(args.backup_path, db)
        print(f"Restored {restored} chunks from {args.backup_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
