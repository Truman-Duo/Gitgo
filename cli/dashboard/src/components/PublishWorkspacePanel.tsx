import React from "react";
import {PublishWorkbench} from "./PublishWorkbench.js";
import {ModuleHelpBoundary} from "./ModuleHelpBoundary.js";

// Preserve the existing entry point; navigation and operation ownership are
// separate components, with no circular module dependency.
export {PublishItemPanel} from "./PublishItemPanel.js";
export function PublishWorkspacePanel(props: React.ComponentProps<typeof PublishWorkbench>) {
  return <ModuleHelpBoundary onFooter={props.onFooter}>{onFooter=><PublishWorkbench key={props.project} {...props} onFooter={onFooter}/>}</ModuleHelpBoundary>;
}
