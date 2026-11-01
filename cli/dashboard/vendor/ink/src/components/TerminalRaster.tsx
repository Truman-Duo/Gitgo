import React from 'react';
import type {Raster} from '../core/raster.js';
export function TerminalRaster({image, columns, rows}: {image: Raster; columns: number; rows: number}) {
  return <ink-box terminalRaster={image} style={{width: columns, height: rows, flexShrink: 0}}/>;
}
