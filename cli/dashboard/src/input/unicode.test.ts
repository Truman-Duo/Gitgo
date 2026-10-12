import {test} from 'node:test';
import assert from 'node:assert/strict';
import {stringWidth} from '../../vendor/ink/src/core/stringWidth.js';
import {wrapAnsi} from '../../vendor/ink/src/core/wrapAnsi.js';
test('identical Unicode cell widths and wrapping under both runtimes', () => {
  for (const [text, width] of [['中文𠮷', 6], ['e\u0301', 1], ['👩‍💻', 2], ['क्ष', 2], ['क्‍ष', 2]] as const) {
    assert.equal(stringWidth(text), width, text);
  }
  assert.equal(wrapAnsi('中文中文', 4, {hard: true}), '中文\n中文');
});
