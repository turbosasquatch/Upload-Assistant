# Remote FFmpeg screenshots

Upload Assistant can optionally ask a remote worker to render its main video screenshots. The integration is disabled by default and does not change the existing local FFmpeg path unless explicitly enabled.

```env
UA_REMOTE_FFMPEG_ENABLED=false
UA_REMOTE_FFMPEG_URL=http://mac-worker:8080
UA_REMOTE_FFMPEG_TOKEN=replace-with-a-long-random-token
UA_REMOTE_FFMPEG_TIMEOUT=180
UA_REMOTE_FFMPEG_FALLBACK=true
UA_REMOTE_FFMPEG_PATH_ROOT=/media/torrents
```

`UA_REMOTE_FFMPEG_PATH_ROOT` is the local root shared with the worker. Upload Assistant sends only the source path relative to this root; the worker resolves it beneath its own configured media root. Paths outside the root are rejected.

For an eligible screenshot, Upload Assistant sends a bearer-authenticated `POST /v1/ffmpeg` request containing a relative path, seek time, source dimensions and sample aspect ratio, the selected safe tone-map mode and settings, and PNG compression level. The endpoint never receives a command, raw FFmpeg arguments, filter expression, or output path. A successful worker returns raw `image/png` bytes. Upload Assistant validates the PNG structure, checksums, dimensions, completeness, content type, and size before atomically replacing the screenshot file.

With `UA_REMOTE_FFMPEG_FALLBACK=true`, configuration, network, timeout, worker, and invalid-image errors run the unchanged local FFmpeg operation. With fallback disabled, the screenshot follows Upload Assistant's existing capture-failure handling.

The intentionally narrow worker contract matches the main, non-overlay video screenshot renderer. Disc/DVD screenshots and frame-overlay screenshots remain local because their special seek and draw-text behavior is not represented by the endpoint. A libplacebo HDR first pass also remains local; if Upload Assistant reaches its existing zscale fallback, that fallback is eligible for remote rendering. Non-libplacebo zscale HDR screenshots retain their active tone-map algorithm and desaturation settings remotely.

Keep the worker on a trusted network, use a unique high-entropy token, and do not publish the worker port directly to the internet.
