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

Optional but **strongly recommended** (YouTube bot-checks Railway IPs):

```txt
YTDLP_COOKIES_B64=<base64 of a Netscape-format cookies.txt exported from a
                   logged-in YouTube session>
```

Other optional vars:

```txt
WORKER_ID=worker-1
POLL_INTERVAL_SECONDS=3
YTDLP_COOKIES_TXT=<raw cookies.txt content; fallback if you can't use base64>
```

That's it. No `LOVABLE_API_KEY`. No `SUPABASE_SERVICE_ROLE_KEY`.

### Creating `YTDLP_COOKIES_B64`

1. In a logged-in browser, export your YouTube cookies in **Netscape
   `cookies.txt` format** (e.g. with the "Get cookies.txt LOCALLY" extension).
2. Base64-encode the file:
   - macOS / Linux: `base64 -w0 cookies.txt`
   - Windows: `certutil -encode cookies.txt cookies.b64` then strip header/footer.
3. Paste the resulting single-line string into Railway as `YTDLP_COOKIES_B64`.

Never commit real cookies to the repo. The worker writes them to
`/tmp/youtube_cookies.txt` at runtime and never logs the value.

## Docker / runtime notes

The Dockerfile installs Node.js 20 so `yt-dlp` has a JavaScript runtime
available (`--js-runtimes node`). Without this, modern YouTube extraction
breaks with "No supported JavaScript runtime could be found".

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
