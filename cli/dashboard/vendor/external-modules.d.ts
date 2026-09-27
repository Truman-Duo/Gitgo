// Type surfaces for small CommonJS packages used by the vendored Ink runtime.
// Keep these narrow: they describe only the APIs this repository consumes and
// avoid turning the vendored renderer into an unchecked `any` island.

declare module 'stack-utils' {
  type ParsedStackLine = {
    file?: string
    line?: number
    column?: number
    function?: string
  }

  export default class StackUtils {
    constructor(options?: { cwd?: string; internals?: RegExp[] })
    static nodeInternals(): RegExp[]
    parseLine(line: string): ParsedStackLine | null
  }
}

declare module 'bidi-js' {
  type Bidi = {
    getEmbeddingLevels(
      text: string,
      defaultDirection?: string,
    ): { paragraphLevel: number; levels: Uint8Array }
    getReorderSegments(
      text: string,
      embeddingLevels: { paragraphLevel: number; levels: Uint8Array },
      start?: number,
      end?: number,
    ): [number, number][]
    getVisualOrder(reorderSegments: [number, number][]): number[]
  }

  export default function createBidi(): Bidi
}

declare module 'lodash-es/noop.js' {
  export default function noop(...args: unknown[]): undefined
}

declare module 'lodash-es/throttle.js' {
  type Throttled<T extends (...args: never[]) => unknown> = T & {
    cancel(): void
    flush(): ReturnType<T> | undefined
  }

  export default function throttle<T extends (...args: never[]) => unknown>(
    callback: T,
    wait?: number,
    options?: { leading?: boolean; trailing?: boolean },
  ): Throttled<T>
}

declare module 'semver' {
  export type SemVer = { version: string }
  export function coerce(value: string | null | undefined): SemVer | null
  export function gte(left: string | SemVer, right: string | SemVer): boolean
}

declare module 'react-reconciler/constants.js' {
  export const ConcurrentRoot: number
  export const LegacyRoot: number
  export const DiscreteEventPriority: number
  export const ContinuousEventPriority: number
  export const DefaultEventPriority: number
  export const NoEventPriority: number
}

declare module 'react-reconciler' {
  export type FiberRoot = object

  type Reconciler = {
    createContainer(...args: unknown[]): FiberRoot
    updateContainerSync(...args: unknown[]): void
    flushSyncWork(): void
    flushSyncFromReconciler(): void
    injectIntoDevTools(options: Record<string, unknown>): void
    discreteUpdates<T>(callback: (...args: never[]) => T, ...args: unknown[]): T
  }

  export default function createReconciler<
    Type,
    Props,
    Container,
    Instance,
    TextInstance,
    SuspenseInstance,
    HydratableInstance,
    FormInstance,
    PublicInstance,
    HostContext,
    UpdatePayload,
    ChildSet,
    TimeoutHandle,
    NoTimeout,
  >(config: unknown): Reconciler
}
