/**
 * The conversation rail.
 *
 * Left column of the chat view: start a conversation, search the ones that
 * exist, reopen one, rename it, delete it.
 *
 * EVERY ROW IS A STORED THREAD. The list comes from `list_conversations`, which
 * reads the `conversations` table — real ids, real titles, real timestamps. It
 * is no longer reconstructed in the browser by splitting a flat log on silence,
 * which is why every row is now openable and writable instead of only the
 * newest one. Opening a row rebuilds the model's context from that thread, so
 * "continue where we left off" is literally what happens.
 *
 * The row actions live behind a per-row menu rather than as always-visible
 * buttons: a rail is a list you scan, and three controls per line turns
 * scanning into reading. The menu is quiet, not invisible — see
 * `.chat-item__menu` in globals.css for why that distinction cost a round of
 * human testing.
 *
 * SELECTION MODE is the second interaction this list supports. It is a MODE
 * rather than a permanent checkbox column for the same reason: deleting several
 * conversations is a rare, deliberate act, and paying for it with a checkbox on
 * every row of every scan is the wrong trade. Entering the mode is one click in
 * the rail header; leaving it is Cancel or Escape.
 */
import React from "react";

import NanoLogo from "./NanoLogo";
import { Button, ConfirmDialog, Popover, Skeleton } from "./ui";
import { Thread, groupThreads, matchesThread, threadStamp } from "../lib/conversations";

const Glyph = ({ d, size = 16 }: { d: string; size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={d} />
  </svg>
);

const SEARCH = "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm10 2-4.35-4.35";
const PLUS = "M12 5v14M5 12h14";
const DOC = "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6ZM14 2v6h6M9 13h6M9 17h4";
const BRAIN = "M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Zm0 0v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3";
const DOTS = "M12 5h.01M12 12h.01M12 19h.01";
const CHECK = "M20 6 9 17l-5-5";
const TRASH = "M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6";

function RowMenu({
  thread, onRename, onDelete,
}: {
  thread: Thread;
  onRename: (thread: Thread) => void;
  onDelete: (thread: Thread) => void;
}) {
  const [open, setOpen] = React.useState(false);
  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      label={`Ações de ${thread.title}`}
      trigger={(props) => (
        <button
          {...props}
          type="button"
          className="chat-item__menu"
          title="Mudar o nome ou apagar"
          aria-label={`Ações de ${thread.title}`}
          onClick={(event) => { event.stopPropagation(); setOpen((v) => !v); }}
        >
          <Glyph d={DOTS} size={15} />
        </button>
      )}
    >
      {/* Popover already renders role="menu" and the label; these are its items. */}
      <button type="button" className="popover__item" role="menuitem"
              onClick={() => { setOpen(false); onRename(thread); }}>
        <span className="popover__item-body">
          <span className="popover__item-label">Mudar o nome</span>
        </span>
      </button>
      <button type="button" className="popover__item popover__item--danger" role="menuitem"
              onClick={() => { setOpen(false); onDelete(thread); }}>
        <span className="popover__item-body">
          <span className="popover__item-label">Apagar conversa</span>
          <span className="popover__item-hint">As mensagens são apagadas deste computador.</span>
        </span>
      </button>
    </Popover>
  );
}

export default function Rail({
  threads, activeId, query, onQuery, onNew, onOpen, onRename, onDelete, onDeleteMany,
  loading, messageCount, onOpenMemory, drawer, onCloseDrawer, unavailable,
}: {
  threads: Thread[];
  /** The thread the Brain is holding. It is also the one on screen. */
  activeId: string | null;
  query: string;
  onQuery: (value: string) => void;
  onNew: () => void;
  onOpen: (thread: Thread) => void;
  onRename: (thread: Thread, title: string) => void;
  onDelete: (thread: Thread) => void;
  /** Bulk delete. ONE backend call for the whole selection, never a loop here. */
  onDeleteMany: (ids: string[]) => void;
  loading: boolean;
  /** Real count from the conversations table, or null before it is known. */
  messageCount: number | null;
  onOpenMemory: () => void;
  /** "open" | "closed" at narrow widths, where the rail is an overlay. */
  drawer: "open" | "closed" | "docked";
  onCloseDrawer: () => void;
  /** True when the memory database could not be migrated. Say so; do not fake a list. */
  unavailable?: boolean;
}) {
  const [renaming, setRenaming] = React.useState<Thread | null>(null);
  const [draftTitle, setDraftTitle] = React.useState("");
  const [deleting, setDeleting] = React.useState<Thread | null>(null);
  const [selecting, setSelecting] = React.useState(false);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [confirmBulk, setConfirmBulk] = React.useState(false);

  const matching = React.useMemo(
    () => threads.filter((thread) => matchesThread(thread, query)),
    [threads, query],
  );
  const groups = React.useMemo(() => groupThreads(matching), [matching]);

  const startRename = React.useCallback((thread: Thread) => {
    setDraftTitle(thread.title);
    setRenaming(thread);
  }, []);

  const exitSelection = React.useCallback(() => {
    setSelecting(false);
    setSelected(new Set());
    setConfirmBulk(false);
  }, []);

  /* ESCAPE LEAVES THE MODE, but only while no dialog is open on top of it.
     The confirmation owns Escape while it is up: closing both with one press
     would cancel the delete AND the selection the user spent time building. */
  React.useEffect(() => {
    if (!selecting || confirmBulk || renaming || deleting) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") exitSelection();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selecting, confirmBulk, renaming, deleting, exitSelection]);

  /* A selection can only ever name threads that still exist. Without this, a
     thread deleted from its own row menu (or by another window) would stay in
     the set and be re-sent to the backend as part of the next bulk delete. */
  React.useEffect(() => {
    setSelected((current) => {
      if (!current.size) return current;
      const alive = new Set(threads.map((thread) => thread.id));
      const next = new Set([...current].filter((id) => alive.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [threads]);

  const toggle = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // "Select all" means everything the user can currently SEE. Selecting rows
  // hidden behind a search filter would delete conversations that were never
  // on screen.
  const allVisibleSelected = matching.length > 0
    && matching.every((thread) => selected.has(thread.id));

  /* THE SELECTION IS RESOLVED AGAINST EVERY THREAD, NOT THE FILTERED VIEW.
     It used to be `matching.filter(...)`, which made the confirmation lie: the
     title and the button counted `selected.size` while the ids that actually
     reached the backend, and the message total beside them, counted only the
     rows the current search happened to be showing. Select four, type a query
     that matches one, and the dialog offered "Apagar 4 conversas" while
     deleting one.

     Resolving against `threads` makes all three numbers the same number by
     construction. A search filter is a lens on the list; it is not a second,
     invisible selection. Acquiring a selection is still bounded by what is on
     screen -- see `allVisibleSelected` -- so nothing can be selected that the
     user never saw. */
  const selectedThreads = threads.filter((thread) => selected.has(thread.id));
  const selectedMessages = selectedThreads.reduce(
    (total, thread) => total + (thread.messageCount ?? 0), 0);

  return (
    <aside
      className="rail surface-panel"
      data-drawer={drawer === "docked" ? undefined : drawer}
      aria-label="Conversas"
    >
      <div className="rail__top">
        {/* STARTING A CONVERSATION LEAVES SELECTION MODE.
            Everything else already treats the mode as modal: Escape leaves it,
            and while it is on a row click selects instead of opening. Creating
            a conversation was the one route that slipped through, so the user
            landed in a fresh chat with the rail still in a deletion mode they
            had stopped thinking about — and the next click on a conversation
            silently ticked it instead of opening it. */}
        <Button variant="primary" block title="Nova conversa (Ctrl+N)"
                onClick={() => { exitSelection(); onNew(); }}>
          <Glyph d={PLUS} size={17} />
          Nova conversa
        </Button>

        <div className="rail__search-row">
          <span className="search">
            <span className="search__icon"><Glyph d={SEARCH} size={15} /></span>
            <label className="sr-only" htmlFor="rail-search">Pesquisar conversas</label>
            <input
              id="rail-search" className="input" type="search"
              placeholder="Pesquisar conversas…"
              value={query} onChange={(event) => onQuery(event.target.value)}
            />
          </span>
          {drawer === "open" && (
            <button type="button" className="icon-btn" onClick={onCloseDrawer}
                    aria-label="Fechar conversas" title="Fechar conversas">
              ✕
            </button>
          )}
        </div>

        {/* SELECTION. One quiet entry point when idle; a real toolbar once the
            mode is on. The toolbar replaces the entry point rather than sitting
            beside it, so the header never holds two competing affordances. */}
        {!unavailable && threads.length > 0 && (
          selecting ? (
            <div className="rail__select-bar" role="group" aria-label="Seleção de conversas">
              <span className="rail__select-count" aria-live="polite">
                {selected.size === 1 ? "1 selecionada" : `${selected.size} selecionadas`}
              </span>
              <button
                type="button" className="rail__select-action"
                onClick={() => setSelected(allVisibleSelected
                  ? new Set()
                  : new Set(matching.map((thread) => thread.id)))}
              >
                {allVisibleSelected ? "Limpar" : "Selecionar tudo"}
              </button>
              <button
                type="button" className="rail__select-action rail__select-action--danger"
                disabled={!selected.size}
                onClick={() => setConfirmBulk(true)}
                title={selected.size ? "Apagar as conversas selecionadas" : "Seleciona pelo menos uma conversa"}
              >
                <Glyph d={TRASH} size={14} />
                Eliminar
              </button>
              <button type="button" className="rail__select-action" onClick={exitSelection}>
                Cancelar
              </button>
            </div>
          ) : (
            <div className="rail__select-bar rail__select-bar--idle">
              <button
                type="button" className="rail__select-action"
                onClick={() => setSelecting(true)}
                title="Selecionar várias conversas para apagar"
              >
                <Glyph d={CHECK} size={14} />
                Selecionar
              </button>
            </div>
          )
        )}
      </div>

      <div className="rail__scroll">
        {unavailable ? (
          <p className="rail__note">
            A base de dados de memória não pôde ser migrada, por isso não há lista de
            conversas. O chat continua a funcionar. Vê Memória para o detalhe.
          </p>
        ) : loading && !threads.length ? (
          <div className="stack stack--tight" style={{ padding: "0 8px" }}>
            <Skeleton height={42} /><Skeleton height={42} /><Skeleton height={42} />
          </div>
        ) : !threads.length ? (
          <p className="rail__note">
            Ainda não há conversas guardadas. A primeira mensagem que enviares começa uma.
          </p>
        ) : !matching.length ? (
          <p className="rail__note">Nenhuma conversa corresponde a “{query.trim()}”.</p>
        ) : (
          groups.map((group) => (
            <div className="rail-group" key={group.key}>
              <div className="rail-group__head">
                <span className="rail-group__label section-label">{group.label}</span>
                <span className="section-label" aria-hidden="true">{group.threads.length}</span>
              </div>
              {group.threads.map((thread) => {
                const isActive = thread.id === activeId;
                const isChecked = selected.has(thread.id);
                return (
                  <div
                    key={thread.id}
                    className="chat-item-row"
                    data-active={isActive ? "true" : undefined}
                    data-selected={selecting && isChecked ? "true" : undefined}
                  >
                    {/* In selection mode the row's primary action becomes
                        "select", not "open": clicking a title to open a
                        conversation the user is about to delete is a trap. */}
                    <button
                      type="button" className="chat-item"
                      aria-current={isActive ? "true" : undefined}
                      aria-pressed={selecting ? isChecked : undefined}
                      onClick={() => (selecting ? toggle(thread.id) : onOpen(thread))}
                      title={selecting
                        ? `${isChecked ? "Remover da seleção" : "Selecionar"}: ${thread.title}`
                        : thread.title}
                    >
                      <span className="chat-item__icon">
                        {selecting ? (
                          <span className={`chat-item__check${isChecked ? " is-on" : ""}`}
                                aria-hidden="true">
                            {isChecked ? <Glyph d={CHECK} size={12} /> : null}
                          </span>
                        ) : isActive ? <NanoLogo size={16} /> : <Glyph d={DOC} size={15} />}
                      </span>
                      <span className="chat-item__title">{thread.title}</span>
                      <span className="chat-item__time">{threadStamp(thread)}</span>
                    </button>
                    {!selecting && (
                      <RowMenu thread={thread} onRename={startRename} onDelete={setDeleting} />
                    )}
                  </div>
                );
              })}
            </div>
          ))
        )}
      </div>

      <div className="rail__footer">
        <Button block onClick={onOpenMemory} title="Abrir a memória do Nano">
          <Glyph d={BRAIN} size={16} />
          Memória
          {messageCount !== null && (
            <span className="dim" style={{ marginLeft: "auto", fontSize: 11 }}>
              {messageCount} msg
            </span>
          )}
        </Button>
      </div>

      <ConfirmDialog
        open={Boolean(renaming)}
        title="Mudar o nome da conversa"
        confirmLabel="Guardar"
        message={
          <>
            <label className="sr-only" htmlFor="rail-rename">Novo nome</label>
            <input
              id="rail-rename" className="input" value={draftTitle} autoFocus
              maxLength={120}
              onChange={(event) => setDraftTitle(event.target.value)}
              placeholder="Nome da conversa"
            />
            <p className="dim" style={{ fontSize: 11, marginTop: 8 }}>
              Depois de mudares o nome, o Nano deixa de o alterar sozinho.
            </p>
          </>
        }
        onConfirm={() => {
          if (renaming && draftTitle.trim()) onRename(renaming, draftTitle.trim());
          setRenaming(null);
        }}
        onCancel={() => setRenaming(null)}
      />

      <ConfirmDialog
        open={Boolean(deleting)} danger
        title="Apagar esta conversa?"
        confirmLabel="Apagar"
        message={
          <>
            <strong>{deleting?.title}</strong> e as suas {deleting?.messageCount ?? 0} mensagens
            são apagadas deste computador. Isto não pode ser desfeito.
            <br /><br />
            <span className="dim">
              As memórias de longo prazo que tenham nascido nesta conversa ficam
              guardadas — apagas cada uma em Memória › Memórias.
            </span>
          </>
        }
        onConfirm={() => { if (deleting) onDelete(deleting); setDeleting(null); }}
        onCancel={() => setDeleting(null)}
      />

      {/* ONE confirmation for the whole batch, and it counts what it is about
          to remove out loud. "Apagar 12 conversas e 340 mensagens" is a
          different decision from "apagar 1", and the dialog has to say which
          one the user is making. */}
      <ConfirmDialog
        open={confirmBulk} danger
        title={`Apagar ${selected.size} conversa${selected.size === 1 ? "" : "s"}?`}
        confirmLabel={`Apagar ${selected.size}`}
        message={
          <>
            <strong>{selected.size} conversa{selected.size === 1 ? "" : "s"}</strong>
            {" "}e as suas {selectedMessages} mensagens são apagadas deste computador.
            Isto não pode ser desfeito.
            <br /><br />
            <span className="dim">
              As memórias de longo prazo que tenham nascido nestas conversas ficam
              guardadas — apagas cada uma em Memória › Memórias.
            </span>
          </>
        }
        onConfirm={() => {
          const ids = selectedThreads.map((thread) => thread.id);
          setConfirmBulk(false);
          if (ids.length) onDeleteMany(ids);
          exitSelection();
        }}
        onCancel={() => setConfirmBulk(false)}
      />
    </aside>
  );
}
