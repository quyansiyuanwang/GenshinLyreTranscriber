import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../../../tests/fixtures/edit-draft-v1.json";
import { EditDraftController, type DraftInvoke, type EditDraft, type EditSession } from "./editDraft";
const draft = fixture as EditDraft;
const baseline = draft.performance;
function session(saved = false): EditSession { return { format_version: 1, source_result: draft.source_result, baseline, base_revision_id: draft.base_revision_id, base_sha256: draft.base_sha256, draft: saved ? draft : null, draft_sha256: saved ? "old-hash" : null, can_restore: saved, warning: null }; }
function edited(velocity = 65) { return { ...baseline, notes: baseline.notes.map((n) => ({ ...n, velocity })) }; }
function setup(saved = false) {
  const invoke = vi.fn(async (command: string) => {
    if (command === "read_edit_session") return session(saved);
    if (command === "save_edit_draft") return { format_version: 1, draft_sha256: "new-hash", updated_at: "2026-09-01T01:00:00Z" };
    return undefined;
  });
  return { invoke, controller: new EditDraftController(invoke as DraftInvoke) };
}
afterEach(() => vi.useRealTimers());
describe("recoverable edit drafts", () => {
  it("debounces writes for one second and never publishes a revision", async () => {
    vi.useFakeTimers(); const { controller, invoke } = setup(); await controller.load("result");
    controller.updatePerformance(edited()); await vi.advanceTimersByTimeAsync(900);
    controller.updatePerformance(edited(75)); await vi.advanceTimersByTimeAsync(999);
    expect(invoke.mock.calls.filter(([c]) => c === "save_edit_draft")).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(controller.getSnapshot().status).toBe("saved"); expect(controller.getSnapshot().dirty).toBe(true);
    expect(invoke.mock.calls.map(([c]) => c)).toEqual(["read_edit_session", "save_edit_draft"]);
  });
  it("requires explicit restore and restores editor state from the shared fixture", async () => {
    const { controller, invoke } = setup(true); await controller.load("result");
    expect(controller.getSnapshot().recoveryPending).toBe(true);
    controller.updatePerformance(edited()); expect(controller.getSnapshot().performance).toEqual(baseline);
    controller.restore(); expect(controller.getSnapshot().editor.selected_ids).toEqual(["n1"]);
    expect(controller.getSnapshot().dirty).toBe(true); expect(controller.getSnapshot().recoveryPending).toBe(false);
    await controller.flush(); expect(invoke.mock.calls).toHaveLength(1);
  });
  it("retains in-memory edits and exposes save errors to transition guards", async () => {
    const { controller, invoke } = setup(); await controller.load("result");
    invoke.mockRejectedValueOnce(new Error("disk full")); controller.updatePerformance(edited());
    await expect(controller.flush()).rejects.toThrow("disk full");
    expect(controller.getSnapshot().performance).toEqual(edited()); expect(controller.getSnapshot().status).toBe("error");
    await controller.flush(); expect(controller.getSnapshot().status).toBe("saved");
  });
  it("serializes slow autosave, later edits and discard without resurrecting the file", async () => {
    vi.useFakeTimers(); const { controller, invoke } = setup(); await controller.load("result");
    let finish!: (value: unknown) => void;
    invoke.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }) as never);
    controller.updatePerformance(edited()); await vi.advanceTimersByTimeAsync(1000);
    controller.updatePerformance(edited(80)); const discard = controller.discard();
    finish({ format_version: 1, draft_sha256: "slow-hash", updated_at: draft.updated_at }); await discard;
    await vi.advanceTimersByTimeAsync(2000);
    expect(invoke.mock.calls.map(([c]) => c)).toEqual(["read_edit_session", "save_edit_draft", "delete_edit_draft"]);
    expect(controller.getSnapshot().dirty).toBe(false); expect(controller.getSnapshot().performance).toEqual(baseline);
  });
  it("retains drafts until successful publication and protects against stale delete", async () => {
    const { controller, invoke } = setup(); await controller.load("result"); controller.updatePerformance(edited());
    const publication = await controller.preparePublication();
    expect(invoke.mock.calls.map(([c]) => c)).not.toContain("delete_edit_draft");
    invoke.mockRejectedValueOnce(new Error("changed in another window"));
    await expect(controller.publicationSucceeded(publication!)).rejects.toThrow("changed");
    expect(controller.getSnapshot().dirty).toBe(true);
  });
  it("does not restore or overwrite a draft with a mismatched baseline", async () => {
    const { controller, invoke } = setup(); invoke.mockResolvedValueOnce({ ...session(true), can_restore: false, warning: "基准已变化" });
    await controller.load("result"); controller.restore(); controller.updatePerformance(edited()); await controller.flush();
    expect(controller.getSnapshot().recoveryPending).toBe(true); expect(invoke.mock.calls).toHaveLength(1);
  });
  it("drops an old asynchronous result after a later load", async () => {
    const { controller, invoke } = setup(); let finish!: (value: unknown) => void;
    invoke.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }) as never);
    const first = controller.load("old"); await controller.load("new");
    finish({ ...session(), source_result: "old" }); await first;
    expect(controller.getSnapshot().session?.source_result).toBe(draft.source_result);
  });
  it("can explicitly abandon only local edits after an error without deleting another writer's draft", async () => {
    const { controller, invoke } = setup(true); await controller.load("result"); controller.restore();
    controller.updatePerformance(edited()); invoke.mockRejectedValueOnce(new Error("conflict"));
    await expect(controller.flush()).rejects.toThrow("conflict");
    await controller.abandonLocal();
    expect(controller.getSnapshot().dirty).toBe(false);
    expect(controller.getSnapshot().performance).toEqual(baseline);
    expect(invoke.mock.calls.map(([c]) => c)).not.toContain("delete_edit_draft");
  });

});
