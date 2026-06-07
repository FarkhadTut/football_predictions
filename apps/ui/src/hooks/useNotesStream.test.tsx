import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { queryKeys } from "../api/queries";
import type { ClaudeNote } from "../api/client";
import { useNotesStream } from "./useNotesStream";

/**
 * Minimal `EventSource` stand-in. Only implements the surface
 * `useNotesStream` touches: per-event listeners, `close`, and a tiny
 * helper to drive events from the test.
 */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  private listeners = new Map<string, Set<(event: MessageEvent) => void>>();
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(listener);
  }

  removeEventListener(type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: unknown): void {
    const set = this.listeners.get(type);
    if (!set) return;
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const listener of set) {
      listener(event);
    }
  }

  emitOpen(): void {
    const set = this.listeners.get("open");
    if (!set) return;
    const event = new MessageEvent("open");
    for (const listener of set) {
      listener(event);
    }
  }

  emitError(): void {
    const set = this.listeners.get("error");
    if (!set) return;
    const event = new MessageEvent("error");
    for (const listener of set) {
      listener(event);
    }
  }
}

function makeNote(overrides: Partial<ClaudeNote> = {}): ClaudeNote {
  return {
    match_id: 7,
    summary: "starting XI confirmed; momentum favors home",
    confidence: 0.6,
    qualitative_deltas: [{ market: "1x2", log_odds_shift: 0.15 }],
    sources: ["https://example.com/lineup"],
    created_at: "2026-06-11T10:00:00Z",
    ...overrides,
  };
}

function setup() {
  FakeEventSource.instances = [];
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

describe("useNotesStream", () => {
  it("writes a matching note.updated event into the matchNote cache", () => {
    const { client, wrapper } = setup();

    renderHook(
      () =>
        useNotesStream(7, {
          EventSourceCtor: FakeEventSource as unknown as typeof EventSource,
        }),
      { wrapper },
    );

    const source = FakeEventSource.instances[0]!;
    const note = makeNote();
    act(() => source.emit("note.updated", note));

    expect(client.getQueryData(queryKeys.matchNote(7))).toEqual(note);
  });

  it("ignores note.updated events for other matches", () => {
    const { client, wrapper } = setup();

    renderHook(
      () =>
        useNotesStream(7, {
          EventSourceCtor: FakeEventSource as unknown as typeof EventSource,
        }),
      { wrapper },
    );

    const source = FakeEventSource.instances[0]!;
    act(() => source.emit("note.updated", makeNote({ match_id: 99 })));

    expect(client.getQueryData(queryKeys.matchNote(7))).toBeUndefined();
  });

  it("tracks connected state via open / error events", () => {
    const { wrapper } = setup();

    const { result } = renderHook(
      () =>
        useNotesStream(7, {
          EventSourceCtor: FakeEventSource as unknown as typeof EventSource,
        }),
      { wrapper },
    );

    expect(result.current.connected).toBe(false);

    const source = FakeEventSource.instances[0]!;
    act(() => source.emitOpen());
    expect(result.current.connected).toBe(true);

    act(() => source.emitError());
    expect(result.current.connected).toBe(false);
  });

  it("forwards note.invalid events for the watched match to onInvalid", () => {
    const { wrapper } = setup();
    const onInvalid = vi.fn();

    renderHook(
      () =>
        useNotesStream(7, {
          EventSourceCtor: FakeEventSource as unknown as typeof EventSource,
          onInvalid,
        }),
      { wrapper },
    );

    const source = FakeEventSource.instances[0]!;
    act(() => source.emit("note.invalid", { match_id: 7, errors: [{ msg: "bad" }] }));
    act(() => source.emit("note.invalid", { match_id: 9, errors: [] }));

    expect(onInvalid).toHaveBeenCalledTimes(1);
    expect(onInvalid).toHaveBeenCalledWith({ match_id: 7, errors: [{ msg: "bad" }] });
  });

  it("closes the EventSource on unmount", () => {
    const { wrapper } = setup();

    const { unmount } = renderHook(
      () =>
        useNotesStream(7, {
          EventSourceCtor: FakeEventSource as unknown as typeof EventSource,
        }),
      { wrapper },
    );

    const source = FakeEventSource.instances[0]!;
    unmount();
    expect(source.closed).toBe(true);
  });
});
