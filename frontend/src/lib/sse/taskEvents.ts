import type { ApiClient } from "../api/client";

export interface TaskEvent {
  id?: number;
  event: string;
  data: Record<string, unknown>;
}

export async function consumeTaskEvents(
  client: ApiClient,
  taskId: string,
  afterId: number,
  signal: AbortSignal,
  onEvent: (event: TaskEvent) => void,
): Promise<void> {
  const response = await client.stream(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`, {
    signal,
    headers: afterId > 0 ? { "Last-Event-ID": String(afterId) } : {},
  });
  if (!response.body) throw new Error("The event stream did not include a response body.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (!signal.aborted) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const event = parseFrame(frame);
      if (event) onEvent(event);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
}

export function parseFrame(frame: string): TaskEvent | null {
  if (!frame || frame.startsWith(":")) return null;
  let id: number | undefined;
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "id" && /^\d+$/.test(value)) id = Number(value);
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  }
  let payload: Record<string, unknown> = {};
  if (data.length) {
    const decoded: unknown = JSON.parse(data.join("\n"));
    if (decoded && typeof decoded === "object" && !Array.isArray(decoded)) payload = decoded as Record<string, unknown>;
  }
  return id === undefined ? { event, data: payload } : { id, event, data: payload };
}
