# FoodRescue AI
Dual-sided platform: inventory + expiry tracking, AI waste-risk prediction, surplus redistribution marketplace, impact dashboard.

## Run
    pip install -r requirements.txt
    uvicorn backend.main:app --reload
Open http://localhost:8000 → click **Load demo data** (Business tab). API docs: /docs

## Modules (mapped to the project plan)
- **Inventory/expiry**: `POST /api/items`, CSV `POST /api/items/upload/{biz}` (POS can POST here), barcode `POST /api/scan`, `GET /api/alerts/{biz}`; food taxonomy in `TAXONOMY`.
- **Prediction**: `forecast()` uses Prophet if installed, else seasonal-trend baseline; `risk()` scores 0-100 (55% overstock vs forecast, 30% urgency, 15% perishability); `GET /api/reorder/{biz}`.
- **Marketplace**: `/api/listings`, `/api/match/{id}` (40% food type, 35% distance, 25% quantity), `/api/pickups`, `/api/impact`.

## Known limitations / next steps
SQLite single-tenant-by-id (no auth); LSTM, notifications (email/SMS), calendar UI, POS auth, charts and cloud deployment are not yet implemented.
