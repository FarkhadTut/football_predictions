import type { ReactElement } from "react";
import { Route, Routes } from "react-router-dom";

import { Fixtures } from "./pages/Fixtures";
import { Match } from "./pages/Match";

export function App(): ReactElement {
  return (
    <Routes>
      <Route path="/" element={<Fixtures />} />
      <Route path="/matches/:matchId" element={<Match />} />
    </Routes>
  );
}
