# Security model

- **Auth**: optional `ACCESS_KEYS` (constant-time compare) gate every write endpoint. Job IDs are 128-bit random capabilities; video/status URLs cannot be guessed.
- **Uploads**: magic-byte check, size and pixel caps (decompression-bomb safe), then full re-encode to PNG (strips EXIF, ICC and polyglot payloads).
- **Abuse control**: per-IP token-bucket rate limits, per-IP active-job cap, bounded queue, render timeout, 30s socket timeout, no chunked uploads.
- **Web**: strict CSP (no inline script/style, no third-party origins), nosniff, frame-deny, no-referrer, COOP/CORP, HSTS behind HTTPS. UI writes DOM via `textContent` only.
- **Server**: static files confined to `web/`, no shell invocation (ffmpeg is called with an argv list), errors never leak internals, runs as non-root in Docker.
- **Secrets**: only environment variables; `.env` is git-ignored.

Report vulnerabilities privately to the repository owner.
