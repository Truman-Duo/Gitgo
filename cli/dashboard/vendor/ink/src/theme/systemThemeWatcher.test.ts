import { describe, expect, test } from 'bun:test'
import { themeFromOscColor } from './systemThemeWatcher.js'

describe('OSC 11 terminal theme detection', () => {
  test('supports variable-width rgb responses', () => {
    expect(themeFromOscColor('rgb:0000/0000/0000')).toBe('dark')
    expect(themeFromOscColor('rgb:ffff/ffff/ffff')).toBe('light')
    expect(themeFromOscColor('rgb:ff/ff/ff')).toBe('light')
  })

  test('uses luminance and rejects malformed responses', () => {
    expect(themeFromOscColor('#001020')).toBe('dark')
    expect(themeFromOscColor('#ffff00')).toBe('light')
    expect(themeFromOscColor('not-a-color')).toBeNull()
    expect(themeFromOscColor('rgb:zz/00/00')).toBeNull()
  })
})
