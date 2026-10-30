import { describe, expect, it } from "vitest";
import { pendingSession } from "../capture/pending-session";

describe("permission capture session", () => {
  it("uses a session shared by pending requests", () => {
    expect(pendingSession({ permissions: [{ session_id: "one" }, { session_id: "one" }] })).toBe("one");
  });
  it.each([{}, { permissions: [] }, { permissions: [{ session_id: "" }] },
    { permissions: [{ session_id: "one" }, { session_id: "two" }] }])("refuses an unusable selection %j", (payload) => {
    expect(() => pendingSession(payload)).toThrow();
  });
});
