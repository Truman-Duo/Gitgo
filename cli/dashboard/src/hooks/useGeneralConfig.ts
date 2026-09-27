import { useCallback, useEffect, useState } from "react";
import type { BackendClient } from "../backend/client.js";
import { configGet } from "../backend/tools.js";

export type GeneralConfig = {
  verbose: boolean;
  language: "en" | "zh";
  autoCompact: boolean;
  externalEditor: string;
  webSearchEndpoint: string;
  webSearchMode: string;
  webSearchEngine: string;
};
const listeners = new Set<() => void>();
export function notifyGeneralConfigChanged() {
  for (const listener of listeners) listener();
}

export function useGeneralConfig(client: BackendClient) {
  const [config, setConfig] = useState<GeneralConfig>({
    verbose: false, language: "en", autoCompact: true, externalEditor: "", webSearchMode: "auto", webSearchEndpoint: "", webSearchEngine: "duckduckgo",
  });
  const refresh = useCallback(async () => {
    try {
      const value: any = await configGet(client);
      setConfig({
        verbose: Boolean(value?.verbose),
        language: value?.language === "zh" ? "zh" : "en",
        autoCompact: value?.auto_compact !== false,
        externalEditor: String(value?.external_editor || ""),
        webSearchMode: String(value?.web_search_mode || "auto"),
        webSearchEndpoint: String(value?.web_search_endpoint || ""),
        webSearchEngine: String(value?.web_search_engine || "duckduckgo"),
      });
    } catch {
      // Host shutdown or a transient busy runtime must not terminate Ink.
    }
  }, [client]);
  useEffect(() => {
    void refresh();
    const listener = () => { void refresh(); };
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  }, [refresh]);
  return { ...config, refresh };
}
