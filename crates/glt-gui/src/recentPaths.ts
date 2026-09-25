export const RECENT_PATH_LIMIT = 8;

export function addRecentPath(paths: string[], path: string): string[] {
  const normalized = path.trim();
  if (!normalized) return paths;
  return [normalized, ...paths.filter((value) => value !== normalized)].slice(0, RECENT_PATH_LIMIT);
}

export function parseRecentPaths(value: string | null): string[] {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === "string" && item.trim().length > 0).slice(0, RECENT_PATH_LIMIT)
      : [];
  } catch {
    return [];
  }
}
