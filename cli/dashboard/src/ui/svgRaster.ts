import {useEffect, useMemo, useState} from "react";
import {initWasm, Resvg} from "@resvg/resvg-wasm";
import {svgWasmBase64} from "./svgAssets.macro.ts" with {type: "macro"};
import type {Raster} from "@anthropic/ink";
import {colors} from "../theme/index.js";

export type SvgScene = {svg: string; width: number; height: number};
let initializing: Promise<void> | undefined;
export function initializeSvgRenderer() {
  return initializing ??= initWasm(Buffer.from(svgWasmBase64(), "base64"));
}

const channel = (hex: string) => [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16));
const background = channel(colors.graph.background);
const palette = [colors.graph.background, colors.graph.background, ...[...colors.graph.lanes, colors.graph.inactive, colors.graph.selected]
  .flatMap(color => {const rgb = channel(color); return Array.from({length: 28}, (_, index) => "#" + rgb.map((value, axis) =>
    Math.round(background[axis]! + (value - background[axis]!) * (index + 1) / 28).toString(16).padStart(2, "0")).join(""));})];
const rgbPalette = palette.map(channel);

/** SVG is generated internally from numeric geometry. No repository text, URLs or fonts enter the renderer. */
export function renderSvgRaster(scene: SvgScene): Raster {
  if (!Number.isInteger(scene.width) || !Number.isInteger(scene.height) || scene.width < 1 || scene.height < 1
    || scene.width > 2048 || scene.height > 1024 || scene.width * scene.height > 524288 || scene.svg.length > 262144
    || /<(?:image|text|foreignObject|script)\b|(?:href|url\s*\()/i.test(scene.svg)) throw Error("INVALID_GRAPH_SVG");
  const resvg = new Resvg(scene.svg, {font: {loadSystemFonts: false}, shapeRendering: 2});
  let rendered: ReturnType<typeof resvg.render> | undefined;
  try {
    rendered = resvg.render();
    if (rendered.width !== scene.width || rendered.height !== scene.height) throw Error("GRAPH_SVG_SIZE_MISMATCH");
    const rgba = rendered.pixels, pixels = new Uint8Array(scene.width * scene.height), cache = new Map<number, number>();
    for (let index = 0; index < pixels.length; index++) {
      const offset = index * 4, alpha = rgba[offset + 3]! / 255;
      if (alpha < .01) continue;
      // resvg exposes premultiplied RGBA. Applying alpha again would darken thin edges.
      const rgb = [0, 1, 2].map(axis => Math.round(rgba[offset + axis]! + background[axis]! * (1 - alpha)));
      const key = rgb[0]! * 65536 + rgb[1]! * 256 + rgb[2]!;
      let nearest = cache.get(key);
      if (nearest === undefined) {
        let distance = Infinity; nearest = 1;
        for (let color = 1; color < rgbPalette.length; color++) {
          const candidate = rgbPalette[color]!, next = (rgb[0]! - candidate[0]!) ** 2 + (rgb[1]! - candidate[1]!) ** 2 + (rgb[2]! - candidate[2]!) ** 2;
          if (next < distance) {distance = next; nearest = color;}
        }
        cache.set(key, nearest);
      }
      pixels[index] = nearest;
    }
    return {width: scene.width, height: scene.height, palette, pixels};
  } finally {rendered?.free(); resvg.free();}
}

export function useSvgRaster(scene: SvgScene | null) {
  const [state, setState] = useState<{ready: boolean; error: string}>({ready: false, error: ""});
  const enabled = !!scene;
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    void initializeSvgRenderer().then(() => {if (active) setState({ready: true, error: ""});},
      () => {if (active) setState({ready: false, error: "SVG_RENDERER_UNAVAILABLE"});});
    return () => {active = false;};
  }, [enabled]);
  return useMemo(() => {
    if (!scene || !state.ready) return {image: null, error: state.error};
    try {return {image: renderSvgRaster(scene), error: ""};}
    catch {return {image: null, error: "GRAPH_SVG_RENDER_FAILED"};}
  }, [scene, state]);
}
