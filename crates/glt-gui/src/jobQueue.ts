import type { JobRequest } from "./types";

export type QueueItemStatus = "pending" | "running" | "done" | "failed" | "cancelled";

export interface QueueItem {
  id: string;
  request: JobRequest;
  status: QueueItemStatus;
  added_at: number;
  started_at: number | null;
  completed_at: number | null;
  error: string | null;
  result_dir: string | null;
}

export interface QueueCounts {
  total: number;
  pending: number;
  running: number;
  done: number;
  failed: number;
  cancelled: number;
}

export interface QueueRunSummary {
  completed: number;
  failed: number;
  cancelled: number;
  skipped: number;
  stopped: boolean;
}

export interface RunQueueOptions {
  items: QueueItem[];
  ids: string[];
  execute: (item: QueueItem) => Promise<string | void>;
  update: (id: string, patch: Partial<QueueItem>) => void;
  stopRequested: () => boolean;
  consumeSkip: () => boolean;
  now?: () => number;
}

const QUEUE_STATUSES = new Set<QueueItemStatus>([
  "pending",
  "running",
  "done",
  "failed",
  "cancelled",
]);

function queueKey(request: JobRequest): string {
  return `${request.input.toLocaleLowerCase()}\u0000${request.output.toLocaleLowerCase()}`;
}

export function createQueueItem(
  request: JobRequest,
  id: string,
  now = Date.now(),
): QueueItem {
  return {
    id,
    request: { ...request },
    status: "pending",
    added_at: now,
    started_at: null,
    completed_at: null,
    error: null,
    result_dir: null,
  };
}

export function appendQueueRequests(
  current: QueueItem[],
  requests: JobRequest[],
  idFactory: () => string,
  now = Date.now(),
): QueueItem[] {
  const keys = new Set(current.map((item) => queueKey(item.request)));
  const next = [...current];
  for (const request of requests) {
    const key = queueKey(request);
    if (keys.has(key)) continue;
    keys.add(key);
    next.push(createQueueItem(request, idFactory(), now));
  }
  return next;
}

export function updateQueueItem(
  items: QueueItem[],
  id: string,
  patch: Partial<QueueItem>,
): QueueItem[] {
  return items.map((item) => (item.id === id ? { ...item, ...patch } : item));
}

export function queueCounts(items: QueueItem[]): QueueCounts {
  const counts: QueueCounts = {
    total: items.length,
    pending: 0,
    running: 0,
    done: 0,
    failed: 0,
    cancelled: 0,
  };
  for (const item of items) counts[item.status] += 1;
  return counts;
}

export function pendingQueueIds(items: QueueItem[]): string[] {
  return items.filter((item) => item.status === "pending").map((item) => item.id);
}

export function retryableQueueIds(items: QueueItem[]): string[] {
  return items
    .filter((item) => item.status === "failed" || item.status === "cancelled")
    .map((item) => item.id);
}

export function removeQueueItem(items: QueueItem[], id: string): QueueItem[] {
  return items.filter((item) => item.id !== id);
}

export function clearCompletedQueue(items: QueueItem[]): QueueItem[] {
  return items.filter((item) => item.status !== "done");
}

export function syncPendingQueue(items: QueueItem[], template: JobRequest): QueueItem[] {
  const { input: _input, output: _output, operation: _operation, ...parameters } = template;
  return items.map((item) =>
    item.status === "pending"
      ? {
          ...item,
          request: {
            ...item.request,
            ...parameters,
            filter: null,
            filter_preset: null,
          },
        }
      : item,
  );
}

export function resetInterruptedQueue(items: QueueItem[]): QueueItem[] {
  return items.map((item) =>
    item.status === "running"
      ? {
          ...item,
          status: "failed",
          completed_at: Date.now(),
          error: "应用上次退出时任务未完成，可点击“重试失败”继续。",
        }
      : item,
  );
}

export async function runQueuePlan({
  items,
  ids,
  execute,
  update,
  stopRequested,
  consumeSkip,
  now = Date.now,
}: RunQueueOptions): Promise<QueueRunSummary> {
  const summary: QueueRunSummary = {
    completed: 0,
    failed: 0,
    cancelled: 0,
    skipped: 0,
    stopped: false,
  };
  for (const id of ids) {
    if (stopRequested()) {
      summary.stopped = true;
      break;
    }
    const item = items.find((candidate) => candidate.id === id);
    if (!item || item.status === "done") continue;
    update(id, {
      status: "running",
      started_at: now(),
      completed_at: null,
      error: null,
    });
    try {
      const resolvedOutput = await execute(item);
      update(id, {
        status: "done",
        completed_at: now(),
        result_dir: typeof resolvedOutput === "string" ? resolvedOutput : item.request.output,
        error: null,
      });
      summary.completed += 1;
    } catch (reason) {
      const skipped = consumeSkip() && !stopRequested();
      if (skipped) {
        update(id, {
          status: "cancelled",
          completed_at: now(),
          error: "已跳过当前项，可单独重试",
        });
        summary.skipped += 1;
        continue;
      }
      if (stopRequested()) {
        update(id, {
          status: "cancelled",
          completed_at: now(),
          error: "队列已停止，可稍后重试",
        });
        summary.cancelled += 1;
        summary.stopped = true;
        break;
      }
      update(id, {
        status: "failed",
        completed_at: now(),
        error: String(reason),
      });
      summary.failed += 1;
    }
  }
  return summary;
}

function isJobRequest(value: unknown): value is JobRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Partial<JobRequest>;
  return typeof request.input === "string" && typeof request.output === "string";
}

export function parseQueue(raw: string | null): QueueItem[] {
  if (!raw) return [];
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    const items: QueueItem[] = [];
    for (const entry of value) {
      if (!entry || typeof entry !== "object") continue;
      const candidate = entry as Partial<QueueItem>;
      if (
        typeof candidate.id !== "string" ||
        !QUEUE_STATUSES.has(candidate.status as QueueItemStatus) ||
        !isJobRequest(candidate.request)
      ) {
        continue;
      }
      items.push({
        id: candidate.id,
        request: {
          ...candidate.request,
          mapping_profile: candidate.request.mapping_profile ?? null,
          mapping_keys: candidate.request.mapping_keys ?? null,
        },
        status: candidate.status as QueueItemStatus,
        added_at: typeof candidate.added_at === "number" ? candidate.added_at : Date.now(),
        started_at: typeof candidate.started_at === "number" ? candidate.started_at : null,
        completed_at: typeof candidate.completed_at === "number" ? candidate.completed_at : null,
        error: typeof candidate.error === "string" ? candidate.error : null,
        result_dir: typeof candidate.result_dir === "string" ? candidate.result_dir : null,
      });
    }
    return resetInterruptedQueue(items);
  } catch {
    return [];
  }
}
