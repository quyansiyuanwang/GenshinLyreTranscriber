import { useEffect, useState } from "react";

export type WorkspaceView = "analysis" | "filter" | "editor";
export interface WorkspaceLayout {
  version: 1;
  view: WorkspaceView;
  resources: boolean;
  inspector: boolean;
  tasks: boolean;
  resourceWidth: number;
  inspectorWidth: number;
  taskHeight: number;
}
export const DEFAULT_LAYOUT: WorkspaceLayout = {
  version: 1, view: "analysis", resources: true, inspector: true, tasks: false,
  resourceWidth: 240, inspectorWidth: 300, taskHeight: 210,
};
const KEY = "glt.workspace-layout.v1";
export function parseWorkspaceLayout(text: string | null): WorkspaceLayout {
  try {
    const value = JSON.parse(text ?? "null");
    if (!value || value.version !== 1) return { ...DEFAULT_LAYOUT };
    const width = (key: keyof WorkspaceLayout, min: number, max: number) =>
      typeof value[key] === "number" && Number.isFinite(value[key])
        ? Math.round(Math.max(min, Math.min(max, value[key]))) : DEFAULT_LAYOUT[key] as number;
    return {
      ...DEFAULT_LAYOUT,
      view: ["analysis", "filter", "editor"].includes(value.view) ? value.view : "analysis",
      resources: typeof value.resources === "boolean" ? value.resources : true,
      inspector: typeof value.inspector === "boolean" ? value.inspector : true,
      tasks: typeof value.tasks === "boolean" ? value.tasks : false,
      resourceWidth: width("resourceWidth", 200, 360), inspectorWidth: width("inspectorWidth", 250, 420), taskHeight: width("taskHeight", 120, 320),
    };
  } catch { return { ...DEFAULT_LAYOUT }; }
}
export function useWorkspaceLayout() {
  const [layout, setLayout] = useState(() => {
    try { return parseWorkspaceLayout(localStorage.getItem(KEY)); } catch { return { ...DEFAULT_LAYOUT }; }
  });
  const [storageError, setStorageError] = useState<string | null>(null);
  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify(layout)); setStorageError(null); }
    catch { setStorageError("无法保存工作区布局；本次仍可使用，重新打开后可能恢复默认。"); }
  }, [layout]);
  return { layout, setLayout, storageError };
}
