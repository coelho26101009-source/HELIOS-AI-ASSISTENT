"""
H.E.L.I.O.S. Memory Engine
SQLite para histórico de conversas.
ChromaDB para RAG sobre PDFs e documentos locais.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("helios.memory")

DB_PATH = Path(__file__).parent.parent / "data" / "helios.db"


class MemoryEngine:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._init_db()
        self._chroma = None  # lazy-loaded

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                metadata  TEXT DEFAULT '{}'
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self.conn.commit()

    # ─── Mensagens ─────────────────────────────────────────────────────────

    def save_message(self, role: str, content: str, metadata: dict | None = None):
        try:
            self.conn.execute(
                "INSERT INTO messages (role, content, timestamp, metadata) VALUES (?,?,?,?)",
                (role, content, datetime.now().isoformat(), json.dumps(metadata or {}))
            )
            self.conn.commit()
        except Exception as exc:
            logger.error(f"Erro ao guardar mensagem: {exc}")

    def get_recent_messages(self, limit: int = 50) -> list[dict]:
        try:
            rows = self.conn.execute(
                "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [{"role": r[0], "content": r[1], "timestamp": r[2]} for r in reversed(rows)]
        except Exception as exc:
            logger.error(f"Erro ao ler mensagens: {exc}")
            return []

    def get_context_window(self, limit: int = 20) -> list[dict]:
        """Devolve mensagens formatadas para o LLM (role + content apenas)."""
        msgs = self.get_recent_messages(limit)
        return [{"role": m["role"], "content": m["content"]} for m in msgs]

    # ─── Preferências ──────────────────────────────────────────────────────

    def set_preference(self, key: str, value: Any):
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO preferences (key, value) VALUES (?,?)",
                (key, json.dumps(value))
            )
            self.conn.commit()
        except Exception as exc:
            logger.error(f"Erro ao guardar preferência: {exc}")

    def get_preference(self, key: str, default: Any = None) -> Any:
        try:
            row = self.conn.execute(
                "SELECT value FROM preferences WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else default
        except Exception:
            return default

    # ─── RAG (ChromaDB) ────────────────────────────────────────────────────

    def _get_chroma(self):
        if self._chroma is None:
            try:
                import chromadb
                chroma_path = Path(__file__).parent.parent / "data" / "chroma"
                chroma_path.mkdir(parents=True, exist_ok=True)
                self._chroma = chromadb.PersistentClient(path=str(chroma_path))
            except ImportError:
                logger.warning("chromadb não instalado. RAG desactivado.")
        return self._chroma

    def index_document(self, doc_id: str, text: str, metadata: dict | None = None) -> bool:
        """Indexa um documento para pesquisa semântica."""
        client = self._get_chroma()
        if client is None:
            return False
        try:
            col = client.get_or_create_collection("helios_docs")
            # Divide em chunks de ~500 chars
            chunks = [text[i:i+500] for i in range(0, len(text), 400)]
            col.upsert(
                documents=chunks,
                ids=[f"{doc_id}_chunk_{i}" for i in range(len(chunks))],
                metadatas=[{**(metadata or {}), "doc_id": doc_id, "chunk": i} for i in range(len(chunks))],
            )
            logger.info(f"Documento '{doc_id}' indexado ({len(chunks)} chunks)")
            return True
        except Exception as exc:
            logger.error(f"Erro ao indexar documento: {exc}")
            return False

    def search_documents(self, query: str, n_results: int = 5) -> list[dict]:
        """Pesquisa semântica nos documentos indexados."""
        client = self._get_chroma()
        if client is None:
            return []
        try:
            col = client.get_or_create_collection("helios_docs")
            results = col.query(query_texts=[query], n_results=n_results)
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            return [{"text": d, "metadata": m} for d, m in zip(docs, metas)]
        except Exception as exc:
            logger.error(f"Erro na pesquisa RAG: {exc}")
            return []
