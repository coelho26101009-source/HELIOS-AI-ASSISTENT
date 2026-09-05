"""The one object the rest of Nano talks to about memory.

WHAT IT IS
----------
``MemoryStack`` wires the six pieces — schema, retrieval index, conversation
store, long-term memory, knowledge graph, context composer — into a single
facade with a small, verb-shaped API: record a message, compose the context,
open a thread, delete a memory. Nothing outside this module needs to know that
five tables and an FTS index are involved, and nothing outside it opens a
connection or writes SQL.

That matters for more than tidiness. Memory now has ordering constraints (a
message must exist before it can be summarised; a memory must exist before a
node can link to it; deleting a thread must also clear its index rows), and a
facade is where those are stated once instead of being re-derived by every
caller.

THE LATENCY RULE
----------------
Everything on the path between the user pressing Enter and the first token
arriving is synchronous and cheap: one INSERT, one FTS write, a handful of
bounded SELECTs. Everything else — summarisation, memory extraction, promoting
entities into the knowledge graph — runs on a single background worker thread,
because none of it changes the answer to the message that triggered it.

One worker, not a thread per event: a thread per message is unbounded
concurrency against one SQLite writer, and SQLite serialises writers anyway. The
queue is bounded and drops its oldest item under pressure rather than growing —
losing a derived summary is a quality regression, running out of memory is an
outage.

Construct with ``background=False`` to run those jobs inline. Tests do that so a
behaviour is asserted where it happens instead of after a sleep.

FAILURE IS CONTAINED
--------------------
Every public method here is written so that a database problem degrades memory
and leaves the conversation working. If the migration failed, ``ready`` is False
and the stack answers with empty context instead of raising into the chat path.
That is deliberate: memory must never become a single point of failure for
talking to Nano.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable

from core import memory_extraction, memory_schema, summarizer
from core.context_composer import ComposedContext, ContextComposer
from core.conversation_store import ConversationStore
from core.knowledge_graph import DEFAULT_RELATION, KnowledgeGraph
from core.long_term_memory import LongTermMemory
from core.retrieval import RetrievalIndex
from core.trust import TrustLevel

logger = logging.getLogger("nano.memory_stack")

#: Bounded so a burst cannot grow without limit. See the module docstring.
_QUEUE_SIZE = 64


class MemoryStack:
    """Threads, memories, knowledge and context — assembled and ready to use."""

    def __init__(self, memory_engine, *, background: bool = True,
                 long_term_enabled: bool = True, capture_enabled: bool = True):
        self.engine = memory_engine
        self.conn = memory_engine.conn
        self._lock = memory_engine._lock
        self.ready = False
        self.migration: dict = {"ok": False, "error": "not_run"}

        self.migration = memory_schema.apply(self.conn)
        self.ready = bool(self.migration.get("ok"))

        self.index = RetrievalIndex(self.conn, self._lock)
        self.conversations = ConversationStore(self.conn, self._lock, self.index)
        self.memories = LongTermMemory(self.conn, self._lock, self.index)
        self.knowledge = KnowledgeGraph(self.conn, self._lock, self.index)
        self.composer = ContextComposer(self.conversations, self.memories, self.knowledge)

        #: Whether cross-conversation memory may be written and read at all.
        self.long_term_enabled = bool(long_term_enabled)
        #: Whether Nano may PROPOSE memories on its own. Explicit "lembra-te
        #: que..." requests are honoured regardless: that is the user asking.
        self.capture_enabled = bool(capture_enabled)

        self._active_id: str | None = None
        self._background = bool(background)
        self._jobs: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()
        if self._background:
            self._start_worker()

        if self.ready:
            # An existing database arrives with rows nothing has ever indexed:
            # the legacy facts the migration imported, and every message written
            # before the index existed. Without this, retrieval would only work
            # for things said AFTER the upgrade -- which does not look like a
            # bug, it just looks like Nano not remembering.
            #
            # Deferred, so a long history never delays the window appearing.
            self._defer(self._backfill)

    def _backfill(self) -> None:
        try:
            memories = self.memories.reindex_all()
            messages = self.conversations.backfill_index()
            if memories or messages:
                logger.info("Índice preenchido: %d memórias, %d mensagens",
                            memories, messages)
            nodes, edges = self.reconcile_knowledge()
            if nodes or edges:
                logger.info("Second Brain reconciliado: %d nós, %d ligações",
                            nodes, edges)
        except Exception:
            logger.exception("Falha a preencher o índice de recuperação")

    def reconcile_knowledge(self, limit: int = 300) -> tuple[int, int]:
        """Re-derive Second Brain nodes and edges from the ACTIVE memories.

        WHY THIS EXISTS. Nodes and edges are derived at capture time, so
        improving the derivation only ever affects memories captured
        afterwards. An install that already holds memories keeps showing
        whatever the OLD rule produced, and the improvement looks like it did
        not work. Running the current rule over the memories that already exist
        is what makes the fix visible on a machine with history rather than
        only on a fresh one.

        WHAT IT DOES NOT FIX, MEASURED. This build's own development database
        was checked: 15 conversations, 177 messages, 0 memories, and 2
        knowledge nodes -- both ``origin='manual'``, typed by hand in the
        Second Brain. Reconciliation correctly returns ``(0, 0)`` there and the
        graph still reads "2 nós · 0 ligações", because there are no memories
        to re-derive anything from. That is the honest outcome and not a
        failure of this method: the graph fills as memories are captured. It is
        recorded here so the next reader does not mistake a correct no-op for a
        broken pass.

        SAFE TO RUN REPEATEDLY. ``upsert_node`` recognises a node by the slug
        of its title and ``link`` collapses duplicates, so a second pass adds
        nothing; it only bumps mention counts, which is what recurring evidence
        is supposed to do. It reads ACTIVE memories only, which have already
        passed the safety gate, so nothing new enters the store.

        Returns ``(nodes, edges)`` as they stand afterwards, so a caller can
        log a real number instead of claiming a result it did not measure.
        """
        if not self.ready or not self.long_term_enabled:
            return (0, 0)
        try:
            # Collapse mirrored copies of a symmetric relation FIRST. Storing
            # one canonical direction is a rule about writes, so a database
            # written by an earlier build still holds both rows of every pair
            # it saw twice -- and that database is the only one that has the
            # problem. Done before the snapshot so the returned numbers stay
            # "what this pass added"; the removal logs its own count.
            self.knowledge.dedupe_symmetric_edges()
            before = self.knowledge.stats()
            for memory in self.memories.list(limit=limit, status="active"):
                self.promote_to_knowledge(
                    memory, conversation_id=memory.get("sourceConversationId"))
            after = self.knowledge.stats()
        except Exception:
            logger.exception("Falha a reconciliar o Second Brain")
            return (0, 0)
        return (after["nodes"] - before["nodes"], after["edges"] - before["edges"])

    # ------------------------------------------------------------- lifecycle

    def _start_worker(self) -> None:
        self._worker = threading.Thread(target=self._pump, name="nano-memory",
                                        daemon=True)
        self._worker.start()

    def _pump(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job()
            except Exception:  # noqa: BLE001 - a bad job must not kill the worker
                logger.exception("Trabalho de memória em segundo plano falhou")
            finally:
                self._jobs.task_done()

    def _defer(self, job: Callable[[], Any]) -> None:
        if not self._background:
            try:
                job()
            except Exception:
                logger.exception("Trabalho de memória falhou")
            return
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            # Drop the OLDEST, keep the newest: stale derived work is the least
            # valuable thing in the queue.
            try:
                self._jobs.get_nowait()
                self._jobs.task_done()
                self._jobs.put_nowait(job)
            except (queue.Empty, queue.Full):
                logger.warning("Fila de memória cheia; trabalho derivado descartado")

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for deferred work to finish. For shutdown and for tests."""
        if not self._background:
            return
        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while not self._jobs.empty() and not deadline.is_set():
                deadline.wait(0.05)
        finally:
            timer.cancel()

    def stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------ active thread

    @property
    def active_conversation_id(self) -> str | None:
        return self._active_id

    def ensure_active(self) -> str | None:
        """The thread new messages belong to, creating one if there is none.

        On a fresh start this resumes the most recently active thread rather
        than opening a blank one: the user closed Nano mid-conversation and
        expects to find it where they left it.
        """
        if not self.ready:
            return None
        if self._active_id and self.conversations.exists(self._active_id):
            return self._active_id
        latest = self.conversations.latest()
        thread = latest or self.conversations.create()
        self._active_id = thread["id"]
        return self._active_id

    def open_conversation(self, conversation_id: str) -> dict | None:
        thread = self.conversations.get(conversation_id)
        if thread is None:
            return None
        self._active_id = thread["id"]
        return thread

    def new_conversation(self, title: str | None = None) -> dict | None:
        if not self.ready:
            return None
        thread = self.conversations.create(title)
        self._active_id = thread["id"]
        return thread

    def delete_conversation(self, conversation_id: str) -> dict:
        result = self.conversations.delete(conversation_id)
        if result.get("ok") and self._active_id == conversation_id:
            self._active_id = None
        # A node linked to a conversation that no longer exists would draw an
        # edge into nothing in the Second Brain.
        self.knowledge.prune_links("conversation", [str(conversation_id)])
        return result

    # --------------------------------------------------------- record turns

    def record_user_message(self, text: str, *, conversation_id: str | None = None,
                            metadata: dict | None = None) -> dict | None:
        """Persist a user turn. Synchronous part only; the rest is deferred."""
        thread_id = conversation_id or self.ensure_active()
        if not thread_id:
            return None
        stored = self.conversations.append(
            thread_id, "user", text, trust=TrustLevel.USER.value, metadata=metadata)
        if stored is None:
            return None
        self._defer(lambda: self._after_user_message(thread_id, stored))
        return stored

    def record_assistant_message(self, text: str, *, conversation_id: str | None = None,
                                 metadata: dict | None = None) -> dict | None:
        thread_id = conversation_id or self.ensure_active()
        if not thread_id:
            return None
        stored = self.conversations.append(
            thread_id, "assistant", text, trust=TrustLevel.USER.value, metadata=metadata)
        if stored is not None:
            self._defer(lambda: self.compact(thread_id))
        return stored

    def _after_user_message(self, conversation_id: str, stored: dict) -> None:
        self.capture_memories(stored.get("content") or "",
                              conversation_id=conversation_id,
                              message_id=stored.get("id"))
        self.compact(conversation_id)

    # -------------------------------------------------------- long-term memory

    def capture_memories(self, text: str, *, conversation_id: str | None = None,
                         message_id: int | None = None) -> list[dict]:
        """Turn a user message into zero or more memories. Usually zero.

        An EXPLICIT request ("lembra-te que...") is always honoured, because
        that is the user asking directly. Inference is skipped entirely when
        automatic capture is off, so the switch in Definições means what it
        says rather than merely lowering a threshold.

        WHY THE STATUS COMES FROM THE EXTRACTOR AND NOT FROM HERE.
        A well-evidenced inferred fact may now be stored ACTIVE rather than as
        an inert candidate (see core.memory_extraction). The decision belongs to
        the extractor because that is where the evidence is; this method's job
        is to check the two things the extractor cannot see -- whether long-term
        memory is on at all, and whether the user allowed Nano to propose
        memories by itself -- and then to hand the result to the store, which
        applies the safety gate one final time.

        ``memory_auto_capture`` is the single switch. When it is off, NOTHING
        inferred is written: not active, not candidate. A switch that only
        downgraded automatic memories to candidates would still be filling a
        list the user asked Nano not to fill.
        """
        if not self.ready or not self.long_term_enabled:
            return []
        saved: list[dict] = []
        for candidate in memory_extraction.extract(text):
            if candidate.origin == "inferred" and not self.capture_enabled:
                continue
            result = self.memories.remember(
                candidate.text, kind=candidate.kind, origin=candidate.origin,
                trust=TrustLevel.USER.value, confidence=candidate.confidence,
                importance=candidate.importance, status=candidate.status,
                source_conversation_id=conversation_id, source_message_id=message_id)
            if result.get("ok") and result.get("memory"):
                memory = result["memory"]
                saved.append(memory)
                if memory.get("status") == "active":
                    # SAME PIPELINE AS AN EXPLICIT MEMORY, deliberately. An
                    # automatically captured fact is not a second class of
                    # thing living in its own silo -- it becomes a Second Brain
                    # node through the identical derivation, so the graph, the
                    # retrieval index and the ContextComposer see it exactly
                    # like anything the user typed by hand.
                    self.promote_to_knowledge(memory, conversation_id=conversation_id)
        return saved

    def remember(self, text: str, **kwargs) -> dict:
        """Store one memory on the user's behalf and mirror it into the graph."""
        if not self.ready:
            return {"ok": False, "error": "memory_unavailable"}
        if not self.long_term_enabled:
            return {"ok": False, "error": "long_term_disabled",
                    "detail": "a memória de longo prazo está desligada nas Definições"}
        conversation_id = kwargs.pop("conversation_id", None) or self._active_id
        result = self.memories.remember(text, source_conversation_id=conversation_id,
                                        **kwargs)
        memory = result.get("memory")
        if result.get("ok") and memory and memory.get("status") == "active":
            self._defer(lambda: self.promote_to_knowledge(
                memory, conversation_id=conversation_id))
        return result

    def forget(self, memory_id: str) -> dict:
        return self.memories.delete(memory_id)

    # ------------------------------------------------------- knowledge graph

    #: How many entities one memory may name. Three, because "trabalho com
    #: Python e Docker no Windows" is a real sentence with three; more than that
    #: is a list, and a list produces a hairball.
    MAX_ENTITIES_PER_MEMORY = 3

    def promote_to_knowledge(self, memory: dict, *,
                             conversation_id: str | None = None) -> list[dict]:
        """Derive Second Brain nodes and EDGES from ONE memory.

        WHY THE GRAPH USED TO BE DOTS
        -----------------------------
        This method created a node per proper noun and drew an edge only when a
        single sentence happened to contain exactly two of them. "O meu PC tem
        uma GTX 1660 Ti" contains one, so it produced one node and no edge, and
        the graph honestly reported "2 nós · 0 ligações" -- honest, and useless.

        The missing half was the SUBJECT. A sentence like that has two ends: the
        thing being described and the thing it is described with. The left end
        is not a proper noun, so the entity extractor could never see it, but
        the grammar names it outright. ``memory_extraction.subject`` reads it,
        and the machine collapses onto one canonical node so every hardware fact
        lands on the same "O meu PC" instead of on three synonyms.

        WHAT IS EVIDENCE AND WHAT WOULD BE INVENTION
        --------------------------------------------
        * subject + entity  -> an edge, direction subject -> entity, with the
          relation the sentence's own verb supports ("tem" -> has, "usa" ->
          uses). The verb is in the text; nothing is guessed.
        * no subject, two or more entities -> ``related_to`` between them. They
          co-occur in one stored fact, which is evidence that they belong
          together and NOT evidence of what the connection is.
        * no verb this module recognises -> ``related_to``, always. An invented
          ``depends_on`` is worse than an honest generic, because it reads as
          something the user said.

        Nothing here creates a node from raw message text: the input is an
        ACTIVE memory that has already passed the safety gate.
        """
        text = str(memory.get("text") or "")
        node_type = memory_extraction.NODE_TYPE_FOR_KIND.get(str(memory.get("kind")))
        names = memory_extraction.entities(text, limit=self.MAX_ENTITIES_PER_MEMORY)
        subject_node = memory_extraction.subject(text)

        # A memory with no subject AND no usable entity names nothing that can
        # be drawn. "Prefiro respostas curtas" is exactly that, and a node
        # called "respostas curtas" is the clutter this design exists to avoid.
        if not subject_node and (not node_type or not names):
            return []

        created: list[dict] = []

        def _add(title: str, kind: str) -> dict | None:
            node = self.knowledge.upsert_node(
                title, node_type=kind, summary=text, origin="derived")
            if node is None:
                return None
            self.knowledge.attach(node["id"], "memory", memory["id"])
            if conversation_id:
                self.knowledge.attach(node["id"], "conversation", str(conversation_id))
            created.append(node)
            return node

        head: dict | None = None
        if subject_node:
            head = _add(subject_node[0], subject_node[1])

        entity_nodes: list[dict] = []
        if node_type:
            for name in names:
                node = _add(name, node_type)
                if node is not None:
                    entity_nodes.append(node)

        relation = memory_extraction.relation_for(text)
        if head is not None:
            for node in entity_nodes:
                if node["id"] != head["id"]:
                    self.knowledge.link(head["id"], node["id"], relation=relation)
        elif len(entity_nodes) >= 2:
            for index, left in enumerate(entity_nodes):
                for right in entity_nodes[index + 1:]:
                    self.knowledge.link(left["id"], right["id"],
                                        relation=DEFAULT_RELATION)
        return created

    # ------------------------------------------------------------ summaries

    def compact(self, conversation_id: str) -> dict | None:
        """Extend the thread summary if enough new messages have accumulated.

        Never destroys anything: the messages remain the authority, and the
        summary is regenerated from them by ``rebuild_summary`` on demand.
        """
        if not self.ready or not conversation_id:
            return None
        try:
            stored = self.conversations.get_summary(conversation_id)
            pending = self.conversations.messages_after(
                conversation_id, stored.get("coveredThrough", 0))
            if not summarizer.should_compact(pending):
                return None
            compactable = pending[:-summarizer.KEEP_RECENT_MESSAGES]
            result = summarizer.summarize(compactable, previous=stored.get("summary", ""))
            if result.empty:
                return None
            self.conversations.set_summary(
                conversation_id, result.text,
                covered_through=result.covered_through,
                covered_messages=stored.get("coveredMessages", 0) + result.covered_messages,
                generator="extractive")
            for item in result.decisions[:3]:
                self.conversations.add_fact(conversation_id, item, kind="decision")
            for item in result.facts[:3]:
                self.conversations.add_fact(conversation_id, item, kind="fact")
            logger.info("Conversa %s compactada: %d mensagens resumidas",
                        conversation_id, result.covered_messages)
            return {"ok": True, "coveredMessages": result.covered_messages}
        except Exception:
            # A failed summary must never break the chat. The previous summary
            # stays, and the next turn tries again.
            logger.exception("Falha a compactar a conversa %s", conversation_id)
            return {"ok": False, "error": "summary_failed"}

    def rebuild_summary(self, conversation_id: str) -> dict:
        """Recompute a summary from the source messages. The recovery path."""
        if not self.ready:
            return {"ok": False, "error": "memory_unavailable"}
        messages = self.conversations.messages_after(conversation_id, 0)
        if len(messages) <= summarizer.KEEP_RECENT_MESSAGES:
            self.conversations.set_summary(conversation_id, "", covered_through=0,
                                           covered_messages=0, generator="extractive")
            return {"ok": True, "summary": "", "coveredMessages": 0}
        result = summarizer.rebuild(messages[:-summarizer.KEEP_RECENT_MESSAGES])
        self.conversations.set_summary(
            conversation_id, result.text, covered_through=result.covered_through,
            covered_messages=result.covered_messages, generator="extractive")
        return {"ok": True, "summary": result.text,
                "coveredMessages": result.covered_messages}

    # -------------------------------------------------------------- context

    def compose(self, query: str, *, conversation_id: str | None = None,
                recent_messages: list[dict] | None = None) -> ComposedContext:
        thread_id = conversation_id or self.ensure_active() or ""
        if not self.ready:
            return ComposedContext(conversation_id=str(thread_id))
        context = self.composer.compose(
            thread_id, query, recent_messages=recent_messages,
            long_term_enabled=self.long_term_enabled,
            knowledge_enabled=self.long_term_enabled)
        if context.memory_ids:
            self._defer(lambda: self.memories.touch(context.memory_ids))
        return context

    def recent_messages(self, conversation_id: str | None = None, *,
                        limit: int = 20) -> list[dict]:
        thread_id = conversation_id or self.ensure_active()
        if not thread_id:
            return []
        return self.conversations.messages(thread_id, limit=limit)

    # ------------------------------------------------------------- overview

    def overview(self) -> dict:
        """Everything the Memória page needs, in counts and rows. No secrets."""
        threads = self.conversations.list(limit=1) if self.ready else []
        return {
            "ready": self.ready,
            "migration": {"from": self.migration.get("from"),
                          "to": self.migration.get("to"),
                          "ok": bool(self.migration.get("ok")),
                          "error": self.migration.get("error")},
            "longTermEnabled": self.long_term_enabled,
            "captureEnabled": self.capture_enabled,
            "memories": self.memories.stats() if self.ready else {},
            "knowledge": self.knowledge.stats() if self.ready else {},
            "retrieval": self.index.stats(),
            "conversations": len(self.conversations.list(limit=200)) if self.ready else 0,
            "messages": self.conversations.total_messages() if self.ready else 0,
            "activeConversationId": self._active_id,
            "lastConversationAt": threads[0]["lastMessageAt"] if threads else None,
        }

    def purge_everything(self) -> dict:
        """Delete conversations, memories and the graph. Confirmed in the UI first."""
        conversations = self.conversations.delete_all()
        memories = self.memories.clear()
        knowledge = self.knowledge.clear()
        self._active_id = None
        return {"ok": True, "conversations": conversations.get("removed", 0),
                "memories": memories.get("removed", 0),
                "nodes": knowledge.get("removed", 0)}


__all__ = ["MemoryStack"]
