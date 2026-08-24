# EventHarbor Control Room

The Control Room is a React and TypeScript single-page application. It calls
the FastAPI service through the relative `/api` path, so the browser never
needs a separate backend hostname or a cross-origin policy.

## Development

Start the backend stack from the repository root, then run the Vite development
server:

```powershell
docker compose up postgres migrate api worker receiver-lab
cd frontend
npm ci
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api/*` to
`http://localhost:8000/*`.

## Checks

```powershell
npm run typecheck
npm test
npm run build
```

## Container delivery

`Dockerfile` builds the static application and serves it with Nginx. The Nginx
configuration:

- serves generated assets from `/assets` with immutable caching;
- falls back to `index.html` for client-side routes such as `/events/{id}`;
- proxies `/api/*` to the Compose service named `api` on port `8000`.

Running `docker compose up --build` exposes this production-shaped path at
<http://localhost:3000>.
