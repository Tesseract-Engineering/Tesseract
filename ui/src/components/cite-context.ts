import { createContext, useContext } from "react";

/** Citation hover/click wiring between the answer column and the rail. */
export interface CiteState {
  activeId: string | null;
  hovered: number | null;
  setHovered: (msgId: string, n: number | null) => void;
  open: (msgId: string, n: number) => void;
}

export const CiteContext = createContext<CiteState>({
  activeId: null,
  hovered: null,
  setHovered: () => {},
  open: () => {},
});

export const useCite = () => useContext(CiteContext);
