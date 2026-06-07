import type { ReactElement } from "react";
import { Route, Routes } from "react-router-dom";

import { Fixtures } from "./pages/Fixtures";

export function App(): ReactElement {
  return (
    <Routes>
      <Route path="/" element={<Fixtures />} />
      {/* Match page placeholder — Sub-step 8.3 swaps in the 3-panel layout. */}
      <Route path="/matches/:matchId" element={<MatchPlaceholder />} />
    </Routes>
  );
}

function MatchPlaceholder(): ReactElement {
  return (
    <section>
      <h1>Match</h1>
      <p>Match detail view coming in Sub-step 8.3.</p>
    </section>
  );
}
