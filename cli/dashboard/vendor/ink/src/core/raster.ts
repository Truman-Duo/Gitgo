import type {DOMElement} from './dom.js';
import {nodeCache} from './node-cache.js';

export type Raster = Readonly<{width: number; height: number; palette: readonly string[]; pixels: Uint8Array}>;
export type PositionedRaster = {x: number; y: number; columns: number; rows: number; image: Raster};
const encoded = new WeakMap<Raster, string>();
export function validRaster(image: Raster): boolean {
  return Number.isInteger(image?.width) && Number.isInteger(image?.height) && image.width > 0 && image.height > 0
    && image.width <= 2048 && image.height <= 1024 && image.width * image.height <= 524288
    && image.pixels instanceof Uint8Array && image.pixels.length === image.width * image.height
    && image.palette.length > 0 && image.palette.length <= 256 && image.palette.every(color => /^#[0-9a-f]{6}$/i.test(color))
    && image.pixels.every(pixel => pixel < image.palette.length);
}
/** Only bounded pixel data is accepted. No externally supplied escape strings. Index 0 is transparent. */
export function encodeSixel(image: Raster): string {
  const cached = encoded.get(image); if (cached) return cached;
  if (!validRaster(image)) throw Error('INVALID_TERMINAL_RASTER');
  const out = [`\x1bP0;1;0q"1;1;${image.width};${image.height}`];
  image.palette.forEach((color, index) => {
    const channels = [1, 3, 5].map(offset => Math.round(parseInt(color.slice(offset, offset + 2), 16) * 100 / 255));
    out.push(`#${index};2;${channels.join(';')}`);
  });
  for (let y = 0; y < image.height; y += 6) {
    const planes = new Map<number, Uint8Array>();
    for (let bit = 0; bit < 6 && y + bit < image.height; bit++) for (let x = 0; x < image.width; x++) {
      const color = image.pixels[(y + bit) * image.width + x]!;
      if (!color) continue;
      let plane = planes.get(color);
      if (!plane) {plane = new Uint8Array(image.width); planes.set(color, plane);}
      plane[x] |= 1 << bit;
    }
    for (const [color, plane] of planes) {
      let line = '';
      for (const mask of plane) line += String.fromCharCode(63 + mask);
      line = line.replace(/\?+$/, '');
      if (line) out.push(`#${color}`, line.replace(/(.)\1{3,}/g, run => `!${run.length}${run[0]}`), '$');
    }
    out.push('-');
  }
  out.push('\x1b\\'); const value = out.join(''); encoded.set(image, value); return value;
}

/** Images participate in the normal layout; clipped/scrolling images are omitted, never painted over the footer. */
export function collectRasters(root: DOMElement, columns: number, rows: number): PositionedRaster[] {
  const result: PositionedRaster[] = [];
  const visit = (node: DOMElement, inScroll = false, clip = {left: 0, top: 0, right: columns, bottom: rows}) => {
    if (node.style.display === 'none' || node.isHidden) return;
    const scrolling = inScroll || node.scrollViewportHeight !== undefined;
    const image = node.attributes.terminalRaster as Raster | undefined;
    const rect = nodeCache.get(node);
    const boundary = rect && node.style.overflow === 'hidden' ? {left: Math.max(clip.left, rect.x), top: Math.max(clip.top, rect.y),
      right: Math.min(clip.right, rect.x + rect.width), bottom: Math.min(clip.bottom, rect.y + rect.height)} : clip;
    if (image && rect && !scrolling && result.length < 2 && validRaster(image)
      && rect.x >= boundary.left && rect.y >= boundary.top && rect.width > 0 && rect.height > 0
      && rect.x + rect.width <= boundary.right && rect.y + rect.height <= boundary.bottom) {
      result.push({x: Math.floor(rect.x), y: Math.floor(rect.y), columns: Math.floor(rect.width), rows: Math.floor(rect.height), image});
    }
    for (const child of node.childNodes) if (child.nodeName !== '#text') visit(child as DOMElement, scrolling, boundary);
  };
  visit(root); return result;
}
export function sameRasters(a: readonly PositionedRaster[] = [], b: readonly PositionedRaster[] = []): boolean {
  return a.length === b.length && a.every((value, index) => {
    const next = b[index]!;
    return value.image === next.image && value.x === next.x && value.y === next.y && value.columns === next.columns && value.rows === next.rows;
  });
}
export function clearRasters(images: readonly PositionedRaster[], columns: number, rows: number): string {
  let output = '\x1b[0m';
  for (const image of images) for (let row = image.y; row < Math.min(rows, image.y + image.rows); row++)
    output += `\x1b[${row + 1};${image.x + 1}H` + ' '.repeat(Math.max(0, Math.min(image.columns, columns - image.x)));
  return output + '\x1b[H';
}
