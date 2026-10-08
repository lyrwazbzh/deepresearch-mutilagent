import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiProblem } from "../lib/api/client";
import { keys } from "../lib/api/queryKeys";
import type { TaskProjection } from "../lib/api/types";
import { useAuth } from "../auth/AuthProvider";
import { consumeTaskEvents, type TaskEvent } from "../lib/sse/taskEvents";

const terminal = new Set(["completed", "failed", "cancelled"]);

export function useTaskEvents(taskId: string | undefined) {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  const cursor = useRef(0);
  const [connection, setConnection] = useState<"idle" | "live" | "reconnecting" | "resyncing">("idle");

  useEffect(() => {
    cursor.current = 0;
    if (!taskId) return;
    const controller = new AbortController();
    let disposed = false;
    let backoff = 600;

    const reconcileAll = async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.task(taskId) }),
        queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
      ]);
    };
    const handle = (event: TaskEvent) => {
      if (event.id !== undefined) cursor.current = event.id;
      if (event.event === "resync_required") {
        cursor.current = 0;
        setConnection("resyncing");
        void reconcileAll();
        return;
      }
      const changed = Array.isArray(event.data.changed) ? event.data.changed.map(String) : [];
      void queryClient.invalidateQueries({ queryKey: keys.task(taskId) });
      const task = queryClient.getQueryData<TaskProjection>(keys.task(taskId));
      if (changed.includes("messages") && task?.thread_id) void queryClient.invalidateQueries({ queryKey: keys.messages(task.thread_id) });
      if (changed.includes("evidence_pool") || changed.includes("evidence")) void queryClient.invalidateQueries({ queryKey: keys.evidence(taskId) });
      if (changed.includes("knowledge_graph")) void queryClient.invalidateQueries({ queryKey: ["task", taskId, "knowledge-graph"] });
      if (changed.includes("report")) void queryClient.invalidateQueries({ queryKey: keys.report(taskId) });
      if (changed.includes("workspace")) void queryClient.invalidateQueries({ queryKey: keys.workspace(taskId) });
      if (changed.includes("pending_write") || changed.includes("task_status")) {
        void client.task(taskId).then((projection) => {
          queryClient.setQueryData(keys.task(taskId), projection);
          if (terminal.has(projection.task_status)) {
            void reconcileAll().finally(() => controller.abort());
          }
        });
      }
    };

    const connect = async () => {
      while (!disposed) {
        try {
          setConnection(cursor.current ? "reconnecting" : "live");
          await consumeTaskEvents(client, taskId, cursor.current, controller.signal, handle);
          if (controller.signal.aborted) return;
          const task = await client.task(taskId);
          queryClient.setQueryData(keys.task(taskId), task);
          if (terminal.has(task.task_status)) return;
        } catch (error) {
          if (controller.signal.aborted) return;
          if (error instanceof ApiProblem && error.problem.code === "event_cursor_expired") {
            cursor.current = 0;
            setConnection("resyncing");
            await reconcileAll();
          }
        }
        setConnection("reconnecting");
        await new Promise((resolve) => window.setTimeout(resolve, backoff));
        backoff = Math.min(backoff * 2, 8_000);
      }
    };
    void connect();
    return () => {
      disposed = true;
      controller.abort();
      setConnection("idle");
    };
  }, [client, queryClient, taskId]);

  return connection;
}
