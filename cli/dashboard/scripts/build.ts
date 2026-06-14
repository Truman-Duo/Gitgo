// scripts/build.ts
import { build } from "bun";

const result = await build({
  entrypoints: ["./src/main.tsx"],
  outdir: "./dist",
  target: "bun",
  format: "esm",
  naming: "[dir]/cli.[ext]",
  minify: true,
});

if (result.success) {
  console.log("Build OK: dist/cli.js");
  for (const log of result.logs) {
    console.log(log);
  }
} else {
  console.error("Build failed");
  process.exit(1);
}
