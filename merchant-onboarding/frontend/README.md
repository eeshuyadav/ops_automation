# Merchant Onboarding — Frontend

Vite + React + Tailwind + shadcn-style primitives. Talks to the FastAPI
backend on `:8001`; dev server proxies `/api` automatically.

## Setup

```bash
cd /home/eeshu/Desktop/ops_infra/3/frontend
npm install
npm run dev          # http://localhost:5173
```

## Build

```bash
npm run build        # outputs to ./dist
npm run preview      # serve ./dist locally
```

## Routes

| Path | Page |
|---|---|
| `/onboarding`    | Easebuzz onboarding table (search, status filter, inline edits) |
| `/new-merchants` | Gokwik MIDs with no Easebuzz row yet — "new arrivals" view |
| `/sync`          | Sync runs log + "Sync now" trigger |

## Inline edits

Dropdown on `onboarding_status` and free-text on `ops_remarks` write directly
to the API via `PATCH /api/easebuzz/{id}`. The backend marks `source='dashboard'`
on edited rows, so the next hourly sync won't clobber the user's changes for
the locked fields (`onboarding_status`, `remarks`, `ops_remarks`, `delivery`).

## Layout

```
src/
├── main.tsx              router + react-query providers
├── App.tsx               sidebar layout + routes
├── index.css             tailwind directives
├── lib/
│   ├── api.ts            fetch client + types
│   └── utils.ts          cn() + timeAgo()
├── components/ui/        button, badge, input, table (shadcn-style, vendored)
└── pages/
    ├── Onboarding.tsx
    ├── NewMerchants.tsx
    └── SyncStatus.tsx
```
