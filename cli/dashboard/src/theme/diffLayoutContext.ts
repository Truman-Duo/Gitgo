import {createContext} from "react";
import type {DiffLayout} from "../types.js";

// App projects the saved General setting once; diff cards do not query the Host.
export const DiffLayoutContext = createContext<DiffLayout>("split");
