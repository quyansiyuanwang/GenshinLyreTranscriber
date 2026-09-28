// Nested request objects use Serde snake_case, unlike Tauri's outer command arguments.
export interface SeparationRequest {
  component: string;
  input: string;
  output: string;
  model: string;
  worker_path: string | null;
}
export interface RoutingRequest {
  stem_set: string;
  output: string;
  mode: string;
  max_voices: number | null;
  plan: unknown;
  worker_path: string | null;
}
export function separationArgs(request: SeparationRequest) { return { request }; }
export function routingArgs(request: RoutingRequest) { return { request }; }
