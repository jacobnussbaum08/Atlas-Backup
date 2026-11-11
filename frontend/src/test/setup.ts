import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// Most sections are lazy-loaded route chunks (Phase 9): under a full parallel run, importing a
// chunk can take longer than the default 1 s for `findBy*`. Assertions are unchanged.
configure({ asyncUtilTimeout: 4_000 });

afterEach(() => {
  cleanup();
});
