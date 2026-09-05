# TripMate backend

FastAPI backend for the TripMate travel planner.

## Before running

1. Run the Supabase SQL from this chat, including `trip_days`. Before using
   Google place search and map routes, also run
   `supabase/20260904_google_places_cache.sql` in the Supabase SQL Editor.
2. Put your own values in `.env`. The required values are `SUPABASE_URL`,
   `SUPABASE_ANON_KEY`, and `GEMINI_API_KEY` if you want AI chat.
   The classroom password-reset feature additionally needs
   `SUPABASE_SERVICE_ROLE_KEY` in this backend-only file. Never put that key
   in the frontend `.env` or commit it to Git.
   To use place search and route data, add `GOOGLE_MAPS_API_KEY` here for
   backend calls.
   A Maps Demo Key supports Places API (New) and Compute Routes for local
   prototyping; a standard key needs the corresponding APIs enabled in Google
   Cloud. For this classroom prototype, the interactive frontend map can reuse
   this same key value through its own `GOOGLE_MAPS_API_KEY` secret.
3. Redis is optional. Leave all Redis values blank to run without caching.

## Run

```powershell
uv sync
uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/docs> to test the API.

## Current API scope

- Supabase email/password sign-up and login
- classroom-only password reset after matching the profile name and email
- profile lookup and username editing
- trip creation and automatic `DAY 1` to `DAY N` plus editable AI itinerary-draft generation
- trip pinning with a gap-free sidebar display order
- trip period editing with automatic DAY extension and safe shortening
- itinerary item creation, editing, and deletion
- one chat history per trip
- Gemini travel-planner response streamed to the chat screen when `GEMINI_API_KEY` is configured
- backend-only Google Places (New) search, selected-place persistence, and
  Google Routes path/duration for the interactive browser map

The backend retains an optional Maps Static API image proxy for a standard
billed key, but the Streamlit screen now uses Maps JavaScript API so local
prototyping can use Google Maps Demo Keys without Maps Static API access.
