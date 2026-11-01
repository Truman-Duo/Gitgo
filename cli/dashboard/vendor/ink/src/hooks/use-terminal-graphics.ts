import {useEffect, useState} from 'react';
import useStdin from './use-stdin.js';
import {useTerminalSize} from './useTerminalSize.js';
import {useTerminalFocus} from './use-terminal-focus.js';
import {cellPixelSize, da1} from '../core/terminal-querier.js';

export type GraphicsCapability = {sixel: boolean; cellWidth: number; cellHeight: number; reason: string};
const unavailable: GraphicsCapability = {sixel: false, cellWidth: 0, cellHeight: 0, reason: 'not-confirmed'};
export function graphicsCapability(params: number[] = [], width = 0, height = 0): GraphicsCapability {
  const sixel = params.slice(1).includes(4) && Number.isInteger(width) && Number.isInteger(height)
    && width >= 4 && width <= 32 && height >= 6 && height <= 64;
  return sixel ? {sixel, cellWidth: width, cellHeight: height, reason: 'device-reply'} : unavailable;
}
/** Queries use Ink's existing parser and query coordinator; never a second stdin reader. */
export function useTerminalGraphics(enabled: boolean): GraphicsCapability {
  const {internal_querier: querier, isRawModeSupported} = useStdin();
  const size = useTerminalSize(), focused = useTerminalFocus();
  const [observed, setObserved] = useState<{columns: number; rows: number; value: GraphicsCapability} | null>(null);
  useEffect(() => {
    if (!enabled || !querier || !isRawModeSupported) return;
    let active = true;
    void Promise.all([querier.send(cellPixelSize()), querier.send(da1()), querier.flush()]).then(([cell, attributes]) => {
      if (active) setObserved({columns: size.columns, rows: size.rows, value: graphicsCapability(attributes?.params, cell?.width, cell?.height)});
    });
    return () => {active = false;};
  }, [enabled, querier, isRawModeSupported, size.columns, size.rows, focused]);
  return enabled && observed?.columns === size.columns && observed?.rows === size.rows ? observed.value : unavailable;
}
