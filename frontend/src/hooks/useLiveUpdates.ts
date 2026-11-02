import { useEffect, useRef, useState } from "react";

import {
  type LiveConnectionState,
  type LiveListener,
  liveClient,
} from "../services/liveUpdates";

/**
 * Receive live market data while mounted. All components share one connection; the
 * subscription is removed on unmount. Handlers may change between renders without
 * re-subscribing.
 */
export function useLiveUpdates(handlers: Omit<LiveListener, "state">, enabled = true): LiveConnectionState {
  const [state, setState] = useState<LiveConnectionState>("idle");
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    if (!enabled) {
      setState("idle");
      return;
    }
    return liveClient().subscribe({
      bar: (bar) => handlersRef.current.bar?.(bar),
      trade: (trade) => handlersRef.current.trade?.(trade),
      quote: (quote) => handlersRef.current.quote?.(quote),
      resync: (reason) => handlersRef.current.resync?.(reason),
      error: (message) => handlersRef.current.error?.(message),
      state: setState,
    });
  }, [enabled]);

  return state;
}
