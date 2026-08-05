# photo-ar

**English** · [简体中文](README.zh-CN.md)

Point your phone at a **printed** photo and a video plays, anchored to the paper
and tracking your viewpoint. **Just open a web page — nothing to install.**

The photo is a **trigger and a canvas**: it does not have to be a frame from the
video. Any printed photo can carry any video.

Self-hosted: the photos, the videos and the recognition index all live on your
own NAS. No cloud service, no third-party recognition API. Who may see which
photos is configured in the built-in web admin panel.

**Recognition and fitting run entirely in the browser; the server is not in the
hot path.** The page downloads the recognition library once (tens of KB); after
that every frame — feature extraction, matching, homography, fitting — is local.
The server does three things: index resources, transport (the library and the
videos), and manage (users / grants / config).

```
┌── phone browser ────────────┐    ┌── NAS: one container, one port ─┐
│ opencv.js (wasm) → ORB      │    │  /          the web app         │
│ + RANSAC homography         │←lib┤  /api/lib   library pack (ETag)  │
│ + WebGL video quad          │    │  /admin     web admin panel     │
│   zero network per frame    │←mp4┤  /v1/*      API, video streaming│
└─────────────────────────────┘    └─────────────────────────────────┘
```

## How recognition works

1. **Ingest** — for each photo the server extracts local descriptors, quantises
   them against a vocabulary tree, and stores the word sequence in an inverted
   index. The feature backend is switchable: **ORB** (default; the measured
   baseline, and the only one implemented in the browser) or **XFeat** (CVPR 2024
   pretrained weights, Apache-2.0). The trade-offs and measured numbers are in
   [docs/decisions.md](docs/decisions.md).
2. **Query** — a camera frame goes through the same pipeline; the inverted index
   returns a shortlist, then each candidate is geometrically verified with a
   RANSAC homography. A match needs **≥ 40 inliers**.
3. **Why 40** — measured, not guessed. Over 29,740 queries the false-positive
   inlier counts topped out at 39 while true positives had a 5th percentile of
   69. The two distributions barely overlap; 40 sits in the gap and takes the
   real-world false-positive rate to 0. See `bench/`.

Two things the recogniser deliberately refuses: photos whose texture is too
sparse to track (**about 65% of real family photos**, measured), and
near-duplicates of something already in the library — keeping both would make
*both* permanently unrecognisable.

## Deploy

**One container, one port.** The web app, the admin panel and the API share a
single port, split by URI:

| Path | What it is |
|---|---|
| `/` | the page guests point at a photo |
| `/admin` | web admin panel (users, grants, thresholds, photo↔video mapping) |
| `/v1/*` | the API (this is what the batch ingest script talks to) |

No source checkout and no build on the NAS:

```bash
cp .env.example .env      # only PHOTOAR_ROOTS needs a look
docker compose up -d
```

No hand-written config file, no pre-trained vocabulary, no pre-staged model —
whatever is missing is explained in the startup log and the service still comes
up. The bootstrap admin password is printed to the log once.

⚠️ **Guests need https in front of it**: the camera (`getUserMedia`) only exists
in a secure context, and `http://<LAN IP>` is not one. One extra ingress rule on
an existing Cloudflare Tunnel is enough. Plain http is fine if you only use the
admin panel and the API.

Full walkthrough with a verification step after each command:
**[docs/deploy.md](docs/deploy.md)**.

The image intentionally contains neither `vocab.npz` (a vocabulary tree trained
on your own photos) nor `xfeat.onnx`; both arrive at runtime — see the Dockerfile
comments for why and how.

## Documentation

**Start at [docs/README.md](docs/README.md)** — a one-page index that routes you by what you're trying to do.

| Document | What's in it |
|---|---|
| [docs/deploy.md](docs/deploy.md) | Step-by-step deployment: SSH → compose → hardware encoding → ingest → Tailscale → Cloudflare → open the page on a phone. Every step says what "it worked" looks like |
| [docs/decisions.md](docs/decisions.md) | **Decision record**: why XFeat, why no global descriptor, how the thresholds were measured, how users and permissions are designed, measured latencies, and the **known risks plus what still has to be measured on real hardware** |
| [docs/deploy-details.md](docs/deploy-details.md) | The reasoning and the numbers: why the certificate governs both the camera and the disk cache, what the CDN should and must not cache, why VAAPI instead of QuickSync, measured baselines, troubleshooting table |
| [web-front/README.md](web-front/README.md) | The browser half: running ORB in wasm, tracking and fitting, and why there is no ARCore equivalent on the web |
| [deploy/README.md](deploy/README.md) | Command cheat sheet, maintenance commands, what each file under `data/` is worth |
| [bench/README.md](bench/README.md) | The measurement scripts behind every number quoted above |

## Repository layout

```
src/photoar/          Recognition, ingest, transcode, and the HTTP server (Python)
  server/             /v1/* endpoints, path whitelist, media resolution
  server/webui/       Zero-build web admin panel (users, grants, config, photos)
web-front/            The web app (native ES modules + zero-dependency Node, no build step)
  public/             Pages, the recognition pipeline (opencv.js), WebGL rendering
  server/             Static files, /v1 and /admin proxy, library packing, media tickets
docker/               Container entrypoint (two-process supervisor) and healthcheck
tools/                batch_ingest.py (stdlib only), export_models.py, fetch_models.py
bench/                Phase 0 measurement scripts
deploy/               config.example.json, dev-machine compose overlay, ops cheat sheet
docs/                 README.md is the index; deployment, trade-offs, decision log
```

## Development

```bash
pip install -e ".[dev]" && pytest        # server + recognition
cd web-front && npm test                 # the web app (zero deps, plain node --test)
```

The web app also has suites that need a real browser: `npm run test:browser`
(golden cases for the recognition pipeline), plus `npm run test:smoke` and
`npm run test:pages`, which click through every page against a running container.

Publishing a new image is a deliberate act, not a side effect of pushing:

```bash
git tag v0.2.0 && git push origin v0.2.0    # builds and pushes to GHCR
```

One note for anyone reading the source: `§N` references in comments point to an
internal design document that is not published with the repository. The
surrounding comment always states the actual reason, so nothing is lost.

## Status

Recognition feasibility, the NAS server, the web app (recognition, tracking,
fitting, pixel-art UI), users and permissions, and the web admin panel are all
done and running, with the full chain verified on a real Android/Chromium phone.
The one-container, one-port deployment is verified locally under the NAS resource
budget (3 CPU / 3 GiB).

**The native Android client was retired on 2026-08-05** so that all effort goes
into the web version: nothing to install, and it works on iOS and HarmonyOS too,
with recognition and fitting quality that is already good enough. That code lives
on in git history (`android/`); the reasoning is in
[docs/decisions.md](docs/decisions.md).

Not yet verified on the target hardware: the XFeat backend's latency on the N5095
(measured 800 ms p50 under a 3-CPU budget on a faster machine — likely too slow
there). It is off by default. See [docs/decisions.md](docs/decisions.md) §11.
