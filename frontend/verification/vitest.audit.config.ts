import { mergeConfig } from "vitest/config";
import base from "../vitest.config.ts";

export default mergeConfig(base, {
  test: { include: ["verification/interaction-audit.ts"] },
});
