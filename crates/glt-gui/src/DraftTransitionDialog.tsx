import { useEffect, useRef } from "react";
export default function DraftTransitionDialog({ reason, busy, error, onChoice, canSave = true }: {
  reason: string; busy: boolean; error: string | null; canSave?: boolean; onChoice: (choice: "save" | "discard" | "cancel" | "abandon") => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} className="draft-transition" aria-labelledby="draft-dialog-title" onCancel={(event) => { event.preventDefault(); if (!busy) onChoice("cancel"); }}>
    <h3 id="draft-dialog-title">有未发布的音符编辑</h3>
    <p>{reason}前，请选择如何处理当前编辑。</p>
    <p>保存恢复草稿不会发布 edit-NN，也不会更改已有导出。工程保存与音符发布是独立操作。</p>
    {error && <p role="alert" className="draft-error">{error}</p>}
    <div className="button-row">
      <button autoFocus disabled={busy || !canSave} className="primary-button" onClick={() => onChoice("save")}>{busy ? "正在处理…" : "保存恢复草稿并继续"}</button>
      <button disabled={busy} className="danger-button" onClick={() => onChoice("discard")}>放弃草稿与编辑</button>
      {error && <button disabled={busy} className="danger-button" onClick={() => onChoice("abandon")}>不更改磁盘，放弃本窗口编辑并继续</button>}
      <button disabled={busy} onClick={() => onChoice("cancel")}>取消，继续编辑</button>
    </div>
  </dialog>;
}
