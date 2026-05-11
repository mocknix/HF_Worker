# HookFinder Worker

External worker that fetches YouTube transcripts via `yt-dlp` and forwards
them to the `analyze-transcript` Supabase edge function (which holds the
JohnBot prompt and calls the Lovable AI Gateway). Polls the Supabase
`analysis_jobs` table.

## Deploy on Railway

1. Push the contents of this `worker/` folder to a new GitHub repo (or use
   Railway's "Deploy from local" via the Railway CLI).
2. In Railway, create a new project → "Deploy from GitHub repo" → pick the
   repo. Railway auto-detects the `Dockerfile` and `railway.json`.
3. Set these environment variables in the Railway service:

   | Name | Value |
   |------|-------|
   | `SUPABASE_URL` | `https://yvmdrbihmihtbtuoycxg.supabase.co` |
   | `SUPABASE_SERVICE_ROLE_KEY` | from Lovable Cloud → Backend → API keys (service role) |
   | `POLL_INTERVAL_SECONDS` | `3` (optional) |

   You do **not** need `LOVABLE_API_KEY` here. That key stays managed inside
   Lovable Cloud and is only used by the `analyze-transcript` edge function.

4. Deploy. The service has no public port — it's a background worker.
5. Watch the logs: you should see `[worker] starting as worker-…, polling every 3s`.

## How it works

- Polls `analysis_jobs` for rows with `status='queued'`, oldest first.
- Atomically claims one by setting `status='running'`.
- Runs `yt-dlp` to download English auto-subs as SRT (no video download).
- Parses SRT into `{start, end, text}` segments, dedupes rolling-caption
  duplicates, and preserves absolute timestamps.
- For short videos: single POST to the `analyze-transcript` edge function.
- For long videos (> ~35 min / 80k chars): chunks into 12-minute windows,
  POSTs each chunk with `mode: "chunk"`, then POSTs the candidate pool with
  `mode: "final"` for ranking.
- Writes the final `AnalysisResult` JSON back to the `result` column.

## Local testing

```bash
cd worker
docker build -t hookfinder-worker .
docker run --rm \
  -e SUPABASE_URL=... \
  -e SUPABASE_SERVICE_ROLE_KEY=... \
  hookfinder-worker
```

## Updating yt-dlp

Bump the version in `requirements.txt` and redeploy. Railway rebuilds from
the Dockerfile.
