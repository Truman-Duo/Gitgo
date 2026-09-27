import React, { memo } from "react";
import type { BackendClient } from "../backend/client.js";
import type { UseTextInputReturn } from "../hooks/useTextInput.js";
import type { FooterConfig } from "./CommandBar.js";
import { BinWorkspace } from "./BinWorkspace.js";

type Props = {
  client: BackendClient;
  cmdInput: UseTextInputReturn;
  onFooter: (cfg: FooterConfig | null) => void;
  onDismiss: () => void;
  onRefresh?: () => void;
};

export const BinPanel = memo(function BinPanel({
  client, cmdInput, onFooter, onDismiss, onRefresh,
}: Props) {
  return (
    <BinWorkspace client={client} cmdInput={cmdInput} onFooter={onFooter}
      onDismiss={onDismiss} onRefresh={onRefresh}/>
  );
});
