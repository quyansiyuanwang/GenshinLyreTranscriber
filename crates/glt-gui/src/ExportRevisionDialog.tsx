import { useEffect, useRef } from "react";
export default function ExportRevisionDialog({ revision, onSaved, onPublish, onCancel }: {
  revision: string; onSaved: () => void; onPublish: () => void; onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} className="draft-transition" aria-labelledby="export-dialog-title" onCancel={(event) => { event.preventDefault(); onCancel(); }}>
    <h3 id="export-dialog-title">当前还有未发布编辑</h3>
    <p>已保存的恢复草稿不包含在现有导出中。当前发布目录：{revision}</p>
    <p>选择打开已发布产物，或先发布编辑版本，再从新版本选择产物。所有派生文件始终来自同一发布版本。</p>
    <div className="button-row">
      <button onClick={onSaved}>打开已发布版本产物</button>
      <button className="primary-button" onClick={onPublish}>先发布编辑版本</button>
      <button autoFocus onClick={onCancel}>取消</button>
    </div>
  </dialog>;
}
