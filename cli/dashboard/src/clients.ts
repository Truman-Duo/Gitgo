// Shared production backend client reference.

import type { BackendClient } from "./backend/client.js";

let _backend: BackendClient | null = null;

export function setBackendClient(client: BackendClient) { _backend = client; }
export function getBackendClient(): BackendClient | null { return _backend; }
