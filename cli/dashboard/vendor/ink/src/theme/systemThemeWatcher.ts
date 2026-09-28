import type { TerminalQuerier } from '../core/terminal-querier.js'
import { oscColor } from '../core/terminal-querier.js'
import { setCachedSystemTheme, type SystemTheme } from './systemTheme.js'

const POLL_INTERVAL_MS = 5_000

function parseChannel(channel: string): number | null {
  if (!/^[0-9a-f]+$/i.test(channel)) return null
  const raw = Number.parseInt(channel, 16)
  const maximum = (16 ** channel.length) - 1
  return maximum > 0 ? raw / maximum : null
}

export function themeFromOscColor(data: string): SystemTheme | null {
  const rgb = /^rgb:([0-9a-f]+)\/([0-9a-f]+)\/([0-9a-f]+)$/i.exec(data)
  const hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(data)
  const channels = rgb?.slice(1) ?? hex?.slice(1)
  if (!channels || channels.length !== 3) return null

  const normalized = channels.map(parseChannel)
  if (normalized.some((channel) => channel === null)) return null
  const [red, green, blue] = normalized as [number, number, number]
  // Relative luminance is a better divider than a raw RGB average for
  // saturated terminal backgrounds.
  const linearize = (channel: number): number => (
    channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4
  )
  const luminance = (
    0.2126 * linearize(red)
    + 0.7152 * linearize(green)
    + 0.0722 * linearize(blue)
  )
  return luminance >= 0.5 ? 'light' : 'dark'
}

export function watchSystemTheme(
  querier: TerminalQuerier,
  onTheme: (theme: SystemTheme) => void,
): () => void {
  let stopped = false
  let polling = false

  const poll = async (): Promise<void> => {
    if (stopped || polling) return
    polling = true
    try {
      const responsePromise = querier.send(oscColor(11))
      await querier.flush()
      const response = await responsePromise
      const theme = response?.type === 'osc' ? themeFromOscColor(response.data) : null
      if (theme && !stopped) {
        setCachedSystemTheme(theme)
        onTheme(theme)
      }
    } finally {
      polling = false
    }
  }

  void poll()
  const timer = setInterval(() => { void poll() }, POLL_INTERVAL_MS)
  return () => {
    stopped = true
    clearInterval(timer)
  }
}
