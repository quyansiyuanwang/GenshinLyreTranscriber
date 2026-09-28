import type { PerformanceDocument } from "./types";

export interface EditorRecoveryState {
  selected_ids: string[];
  view_start_us: number;
  view_duration_us: number;
  snap_to_beat: boolean;
  tool: "select" | "add";
}
export interface EditDraft {
  format_version: 1;
  source_result: string;
  base_revision_id: string;
  base_sha256: string;
  updated_at: string;
  performance: PerformanceDocument;
  editor_state: EditorRecoveryState;
}
export interface EditSession {
  format_version: 1;
  source_result: string;
  baseline: PerformanceDocument | null;
  base_revision_id: string | null;
  base_sha256: string | null;
  draft: EditDraft | null;
  draft_sha256: string | null;
  can_restore: boolean;
  warning: string | null;
}
interface SavedDraft { format_version: 1; draft_sha256: string; updated_at: string }
export interface DraftSnapshot {
  session: EditSession | null;
  performance: PerformanceDocument | null;
  editor: EditorRecoveryState;
  dirty: boolean;
  recoveryPending: boolean;
  status: "idle" | "loading" | "dirty" | "saving" | "saved" | "error";
  error: string | null;
  editorKey: number;
}
export const defaultEditorState = (duration = 30_000_000): EditorRecoveryState => ({
  selected_ids: [], view_start_us: 0, view_duration_us: Math.max(500_000, Math.min(duration, 30_000_000)), snap_to_beat: true, tool: "select",
});
export type DraftInvoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
export interface DraftPublication { source_result: string; expected_sha256: string | null }

// One writer per editor. Autosave, transition flush, discard and publication all
// pass through this owner; a delayed autosave can never resurrect a deleted draft.
export class EditDraftController {
  private snapshot: DraftSnapshot = { session: null, performance: null, editor: defaultEditorState(), dirty: false, recoveryPending: false, status: "idle", error: null, editorKey: 0 };
  private listeners = new Set<() => void>();
  private epoch = 0;
  private version = 0;
  private savedVersion = -1;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private writing: Promise<void> | null = null;
  private discarding = false;
  constructor(private invoke: DraftInvoke) {}
  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private set(patch: Partial<DraftSnapshot>) { this.snapshot = { ...this.snapshot, ...patch }; this.listeners.forEach((f) => f()); }
  private schedule() {
    clearTimeout(this.timer);
    if (this.snapshot.dirty && !this.snapshot.recoveryPending && !this.discarding) this.timer = setTimeout(() => { void this.flush().catch(() => undefined); }, 1000);
  }
  cancelLoad() { ++this.epoch; clearTimeout(this.timer); }
  async load(directory: string | null) {
    const epoch = ++this.epoch;
    clearTimeout(this.timer);
    if (this.writing) await this.writing.catch(() => undefined);
    if (epoch !== this.epoch) return;
    this.version = 0; this.savedVersion = -1;
    this.set({ session: null, performance: null, dirty: false, recoveryPending: false, error: null, status: directory ? "loading" : "idle", editorKey: this.snapshot.editorKey + 1 });
    if (!directory) return;
    try {
      const session = await this.invoke<EditSession>("read_edit_session", { resultDir: directory });
      if (epoch !== this.epoch) return;
      if (session.format_version !== 1) throw new Error("不支持的桌面草稿会话版本");
      this.set({ session, performance: session.baseline, editor: defaultEditorState(session.baseline?.duration_us), status: "idle", recoveryPending: session.draft_sha256 !== null, error: session.warning });
    } catch (error) { if (epoch === this.epoch) this.set({ status: "error", error: String(error) }); }
  }
  updatePerformance = (performance: PerformanceDocument) => {
    if (!this.snapshot.session?.baseline || this.snapshot.recoveryPending || this.discarding) return;
    ++this.version;
    // Conservatively keep "unpublished" after undo; a saved draft is not a revision.
    this.set({ performance, dirty: true, status: "dirty", error: null });
    this.schedule();
  };
  updateEditor = (editor: EditorRecoveryState) => {
    if (JSON.stringify(editor) === JSON.stringify(this.snapshot.editor)) return;
    ++this.version;
    this.set({ editor });
    this.schedule();
  };
  restore() {
    const session = this.snapshot.session;
    if (!session?.can_restore || !session.draft) return;
    ++this.version; this.savedVersion = this.version;
    this.set({ performance: session.draft.performance, editor: session.draft.editor_state, dirty: true, recoveryPending: false, status: "saved", error: null, editorKey: this.snapshot.editorKey + 1 });
  }
  async flush(): Promise<void> {
    clearTimeout(this.timer);
    if (this.discarding) return;
    while (this.writing) await this.writing;
    const { session, performance, editor, dirty, recoveryPending } = this.snapshot;
    if (!dirty || this.savedVersion === this.version) return;
    if (!session?.base_sha256 || !session.base_revision_id || !performance || recoveryPending) throw new Error("请先恢复或放弃已有草稿，再保存编辑");
    const epoch = this.epoch; const version = this.version;
    const draft: EditDraft = { format_version: 1, source_result: session.source_result, base_revision_id: session.base_revision_id, base_sha256: session.base_sha256, updated_at: "", performance, editor_state: editor };
    this.set({ status: "saving", error: null });
    const write = (async () => {
      try {
        const saved = await this.invoke<SavedDraft>("save_edit_draft", { draft, expectedSha256: session.draft_sha256 });
        if (epoch !== this.epoch) return;
        this.savedVersion = version;
        this.set({ session: { ...session, draft: { ...draft, updated_at: saved.updated_at }, draft_sha256: saved.draft_sha256, can_restore: true }, status: this.version === version ? "saved" : "dirty" });
      } catch (error) { if (epoch === this.epoch) this.set({ status: "error", error: `草稿保存失败，编辑仍在当前窗口：${String(error)}` }); throw error; }
    })();
    this.writing = write;
    try { await write; } finally { if (this.writing === write) this.writing = null; }
    if (epoch === this.epoch && this.version !== version && !this.discarding) await this.flush();
  }
  async discard() {
    if (this.discarding) throw new Error("草稿正在处理，请稍候");
    this.discarding = true;
    clearTimeout(this.timer);
    if (this.writing) await this.writing.catch(() => undefined);
    const session = this.snapshot.session;
    if (!session) { this.discarding = false; return; }
    try {
      if (session.draft_sha256) await this.invoke("delete_edit_draft", { sourceResult: session.source_result, expectedSha256: session.draft_sha256 });
      this.savedVersion = -1; ++this.version;
      this.set({ session: { ...session, draft: null, draft_sha256: null, can_restore: false, warning: null }, performance: session.baseline, editor: defaultEditorState(session.baseline?.duration_us), dirty: false, recoveryPending: false, status: "idle", error: null, editorKey: this.snapshot.editorKey + 1 });
    } catch (error) { this.set({ error: `放弃草稿失败，内容保持不变：${String(error)}`, status: "error" }); throw error; }
    finally { this.discarding = false; }
  }
  async abandonLocal() {
    this.discarding = true; clearTimeout(this.timer);
    try {
      if (this.writing) await this.writing.catch(() => undefined);
      this.set({ performance: this.snapshot.session?.baseline ?? null, dirty: false, recoveryPending: false, status: "idle", error: "仅放弃当前窗口编辑；磁盘恢复草稿未更改。", editorKey: this.snapshot.editorKey + 1 });
    } finally { this.discarding = false; }
  }
  async preparePublication(): Promise<DraftPublication | null> {
    await this.flush();
    const session = this.snapshot.session;
    return session ? { source_result: session.source_result, expected_sha256: session.draft_sha256 } : null;
  }
  async publicationSucceeded(publication: DraftPublication) {
    // Called only after worker confirms successful publication, not on button click.
    clearTimeout(this.timer);
    if (this.writing) await this.writing;
    if (publication.expected_sha256) await this.invoke("delete_edit_draft", { sourceResult: publication.source_result, expectedSha256: publication.expected_sha256 });
    if (this.snapshot.session?.source_result === publication.source_result) this.set({ dirty: false, status: "idle" });
  }
}
