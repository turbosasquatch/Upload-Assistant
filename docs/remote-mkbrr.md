# Remote mkbrr

Upload Assistant can optionally ask a remote mkbrr worker to hash content. This is useful when Upload Assistant can access the same files as a faster machine through a shared filesystem.

The integration is disabled by default. Configure it with container environment variables:

```env
UA_REMOTE_MKBRR_ENABLED=false
UA_REMOTE_MKBRR_URL=http://mac-worker:8080
UA_REMOTE_MKBRR_TOKEN=replace-with-a-long-random-token
UA_REMOTE_MKBRR_TIMEOUT=3600
UA_REMOTE_MKBRR_FALLBACK=true
UA_REMOTE_MKBRR_PATH_ROOT=/mnt/user/torrents
```

`UA_REMOTE_MKBRR_PATH_ROOT` is the local root shared with the worker. Upload Assistant sends only the path relative to this root; the worker resolves it beneath its own configured media root. A source path outside this root is rejected.

When enabled, Upload Assistant sends a bearer-authenticated `POST /v1/mkbrr` request containing the relative source path and the active mkbrr tracker, entropy, piece-length, worker-count, include, and exclude settings. A successful worker returns the raw `.torrent` bytes. Upload Assistant validates the response as torrent metainfo before atomically replacing the output file.

With `UA_REMOTE_MKBRR_FALLBACK=true`, network errors, worker errors, timeouts, and invalid torrent responses run the existing local mkbrr path. With fallback disabled, the remote error follows Upload Assistant's existing mkbrr error handling and falls back to its internal torrent builder.

Keep the worker on a trusted network, use a unique high-entropy token, and do not publish the worker port directly to the internet.
