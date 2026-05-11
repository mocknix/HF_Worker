# HookFinder Worker (Railway)

Polls Lovable Cloud edge functions for queued analysis jobs, fetches transcripts
with `yt-dlp`, parses SRT, chunks long transcripts, and sends each chunk to the
`analyze-transcript` edge function for JohnBot analysis. All AI calls and DB
writes happen inside Lovable Cloud — the worker never holds a Lovable API key
or a Supabase service role key.

## Required environment variables

```txt
SUPABASE_URL=https://yvmdrbihmihtbtuoycxg.supabase.co
WORKER_SHARED_SECRET=<the same value you saved in Lovable as WORKER_SHARED_SECRET>
```

Optional:

```txt
WORKER_ID=worker-1
POLL_INTERVAL_SECONDS=3
```

That's it. No `LOVABLE_API_KEY`. No `SUPABASE_SERVICE_ROLE_KEY`.

## How it talks to Lovable Cloud

Every request includes the header `x-worker-secret: $WORKER_SHARED_SECRET`,
verified by these edge functions:

- `worker-next-job` — atomically claims the next queued job
- `analyze-transcript` — runs the JohnBot prompt via the Lovable AI Gateway
- `worker-update-job` — writes progress / status / result back to `analysis_jobs`

## Run locally

```bash
pip install -r requirements.txt
SUPABASE_URL=... WORKER_SHARED_SECRET=... python worker.py
```

## Run on Railway

1. Push this `worker/` directory to GitHub.
2. Create a Railway service from the repo (Dockerfile auto-detected).
3. Add the two env vars above. Deploy.
