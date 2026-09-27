// scripts/build.ts
import { build } from "bun";

const dashboard = await build({
  entrypoints: ["./src/main.tsx"],
  outdir: "./dist",
  target: "bun",
  format: "esm",
  naming: "[dir]/cli.[ext]",
  minify: true,
});

const inspector = await build({
  entrypoints: ["./src/traceMain.tsx"],
  outdir: "./dist",
  target: "bun",
  format: "esm",
  naming: "trace.[ext]",
  minify: true,
});

if (dashboard.success && inspector.success) {
  console.log("Build OK: dist/cli.js + dist/trace.js");
  for (const log of [...dashboard.logs, ...inspector.logs]) {
    console.log(log);
  }
} else {
  console.error("Build failed");
  for (const log of [...dashboard.logs, ...inspector.logs]) console.error(log);
  process.exit(1);
}
