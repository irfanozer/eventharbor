import { Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { ControlRoomPage } from "./pages/ControlRoomPage";
import { DeadLettersPage } from "./pages/DeadLettersPage";
import { EndpointDetailPage } from "./pages/EndpointDetailPage";
import { EndpointsPage } from "./pages/EndpointsPage";
import { EventDetailPage } from "./pages/EventDetailPage";
import { EventsPage } from "./pages/EventsPage";
import { NotFoundPage } from "./pages/NotFoundPage";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<ControlRoomPage />} />
        <Route path="events" element={<EventsPage />} />
        <Route path="events/:eventId" element={<EventDetailPage />} />
        <Route path="dead-letters" element={<DeadLettersPage />} />
        <Route path="endpoints" element={<EndpointsPage />} />
        <Route path="endpoints/:endpointId" element={<EndpointDetailPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
