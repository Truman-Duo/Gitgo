import {expect, test} from "bun:test";
import {parseUnifiedDiff} from "./diff.js";

test("compact tool diff infers added file and ignores the terminal newline", () => {
  const files = parseUnifiedDiff(
    "--- /dev/null\n+++ b/src/new file.ts\n@@ -0,0 +1,2 @@\n+const a = 1;\n+const b = 2;\n",
  );
  expect(files).toHaveLength(1);
  expect(files[0]?.file).toBe("src/new file.ts");
  expect(files[0]?.status).toBe("added");
  expect(files[0]?.hunks[0]?.lines).toHaveLength(2);
});

test("header paths use the authoritative plus header including timestamp trimming", () => {
  const files = parseUnifiedDiff(
    "diff --git a/old.ts b/stale.ts\n--- a/old.ts\tdate\n+++ b/renamed.ts\tdate\n@@ -1 +1 @@\n-old\n+new\n",
  );
  expect(files[0]?.file).toBe("renamed.ts");
  expect(files[0]?.additions).toBe(1);
  expect(files[0]?.deletions).toBe(1);
});
