# oil-git graph port

Source: [oil-oil/oil-git](https://github.com/oil-oil/oil-git), commit
`9e6abfabe63503b5122b1762a8902ecefe9473fc` (MIT, see `LICENSE`).

`graph.ts` preserves the initially adapted upstream allocator/path generator as
source reference; it is no longer used by the product layout. The SVG styling in
`src/components/publishGraphSvg.ts` is ported from `src/HistoryGraph.tsx`: line width
1.6, opacity 0.8, pending dashes, circle radius 5, stroke width 1.8 and HEAD glow.
Gitgo routes horizontal parallel rails with local rounded corners instead of
long endpoint-to-endpoint Beziers. Its allocator follows the parallel-until-parent
invariant in [gitgraph-tui](https://github.com/daanjiri/gitgraph-tui), pinned at
`eed9e40c7eb75382fe23229ed962eb4c984ad962`, with a local change that also reserves
separate tracks for repeated pending merge parents. Its MIT notice is in
`GGRAPH-LICENSE`. Track colors and pending lanes survive fixed-snapshot pagination.
Both Git and role diagrams share local rounding and crossing gaps; their data
remain separate. DOM, Tauri and upstream Git execution are not included.

The six dark lane colors originate in `scripts/theme-palettes.json`.
Upstream attributes those colors to Atom One Dark (MIT); its notice is included
in `THEME-LICENSE`. Other Gitgo theme tokens are not changed.

When updating, compare those pinned source files, preserve the notices, and run
the Publish topology, SVG, pagination and real-terminal tests. SVG rendering uses
the pinned unmodified `@resvg/resvg-wasm` package (MPL-2.0).
