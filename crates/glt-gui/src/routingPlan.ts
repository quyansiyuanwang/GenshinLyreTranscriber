export type StemTarget = "melody" | "harmony" | "bass" | "percussion" | "ignore";

export interface StemRouteControl {
  muted: boolean;
  solo: boolean;
  gainDb: number;
  target: StemTarget;
  priority: number;
}

export interface RoutingPlanDocument {
  format_version: 1;
  mode: "custom";
  max_voices: number;
  collision: "priority";
  routes: Array<{
    id: string;
    enabled: boolean;
    sources: string[];
    target: StemTarget;
    gain_db: number;
    muted: boolean;
    solo: boolean;
    priority: number;
    start_us: null;
    end_us: null;
  }>;
}

export function defaultStemRoute(role: string): StemRouteControl {
  const defaults: Record<string, StemRouteControl> = {
    vocals: { muted: false, solo: false, gainDb: -2, target: "melody", priority: 90 },
    other: { muted: false, solo: false, gainDb: 0, target: "melody", priority: 80 },
    bass: { muted: false, solo: false, gainDb: -4, target: "bass", priority: 60 },
    drums: { muted: false, solo: false, gainDb: -2, target: "percussion", priority: 30 },
  };
  return defaults[role] ?? { muted: false, solo: false, gainDb: 0, target: "ignore", priority: 50 };
}

export function buildCustomRoutingPlan(
  roles: string[],
  controls: Record<string, StemRouteControl>,
  maxVoices: number,
): RoutingPlanDocument {
  return {
    format_version: 1,
    mode: "custom",
    max_voices: Math.min(21, Math.max(1, Math.round(maxVoices))),
    collision: "priority",
    routes: roles.map((role) => {
      const control = controls[role] ?? defaultStemRoute(role);
      return {
        id: `stem-${role}`,
        enabled: true,
        sources: [role],
        target: control.target,
        gain_db: Math.min(24, Math.max(-24, control.gainDb)),
        muted: control.muted,
        solo: control.solo,
        priority: Math.min(100, Math.max(0, Math.round(control.priority))),
        start_us: null,
        end_us: null,
      };
    }),
  };
}
