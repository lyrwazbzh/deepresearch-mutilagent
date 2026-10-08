import { describe, expect, it } from "vitest";
import { parseFrame } from "../lib/sse/taskEvents";

describe("SSE frame parsing", () => {
  it("parses event id, name and changed projection fields", () => {
    expect(parseFrame('id: 42\nevent: task.updated\ndata: {"task_id":"task_1","changed":["phase","evidence_pool"]}')).toEqual({
      id: 42,
      event: "task.updated",
      data: { task_id: "task_1", changed: ["phase", "evidence_pool"] },
    });
  });

  it("ignores heartbeat comments", () => {
    expect(parseFrame(": heartbeat")).toBeNull();
  });

  it("accepts resync control events without an id", () => {
    expect(parseFrame('event: resync_required\ndata: {"task_id":"task_1","after_id":9}')).toEqual({
      event: "resync_required",
      data: { task_id: "task_1", after_id: 9 },
    });
  });
});
