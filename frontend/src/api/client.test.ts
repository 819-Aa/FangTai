import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { subscribeEvents } from "./client";

class TestEventSource extends EventTarget {
  static instances: TestEventSource[] = [];
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  constructor(_url: string) { super(); TestEventSource.instances.push(this); }
  close() { this.closed = true; }
  fail() { const event = new Event("error"); this.dispatchEvent(event); this.onerror?.(event); }
  message(type: string, data: string) {
    const event = new MessageEvent(type, { data, lastEventId: "stable-id" });
    this.dispatchEvent(event);
    if (type === "error") this.onerror?.(event);
  }
}

beforeEach(() => { TestEventSource.instances = []; vi.stubGlobal("EventSource", TestEventSource); });
afterEach(() => vi.unstubAllGlobals());

it("native network error events must reach the third-failure polling threshold", () => {
  const errors: number[] = [];
  const messages = vi.fn();
  subscribeEvents("r", messages, (count) => errors.push(count));
  const source = TestEventSource.instances[0];
  source.fail(); source.fail(); source.fail();
  expect(errors).toEqual([1, 2, 3]);
  expect(source.closed).toBe(true);
  expect(messages).not.toHaveBeenCalled();
});

it("malformed messages cannot reset network failure counting", () => {
  const errors: number[] = [];
  const connection = subscribeEvents("r", vi.fn(), (count) => errors.push(count));
  const source = TestEventSource.instances[0];
  source.fail();
  source.message("thought_node", "not json");
  source.fail();
  expect(errors).toEqual([1, 2]);
  connection.close();
});

it("a JSON business error is delivered once without counting as a transport failure", () => {
  const errors = vi.fn();
  const messages = vi.fn();
  const connection = subscribeEvents("r", messages, errors);
  const source = TestEventSource.instances[0];
  source.message("error", '{"message":"处理失败"}');
  expect(messages).toHaveBeenCalledWith({ id: "stable-id", event: "error", data: { message: "处理失败" } });
  expect(errors).not.toHaveBeenCalled();
  connection.close();
});
