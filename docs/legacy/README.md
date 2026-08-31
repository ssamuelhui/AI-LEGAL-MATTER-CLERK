# Legacy files

Kept for historical reference only. Nothing here is used at runtime.

## docker-compose.yml

Started the Qdrant container that backed the vector store through Phase 2b.
Phase 3 replaced Qdrant with ChromaDB in embedded mode, which needs no daemon
and no container — see `docs/ARCHITECTURE.md` (2026-08-30) and `MIGRATION.md`.

Retained because it documents the exact image and volume layout an existing
Phase-2b installation is running, which is what you would need if you ever had
to read an old `qdrant_storage/` directory.
