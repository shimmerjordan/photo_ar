# photo-ar

**English** · [简体中文](README.zh-CN.md)

Point your phone at a **printed** photo and a video plays, anchored to the paper
and tracking your viewpoint.

The photo is a **trigger and a canvas**: it does not have to be a frame from the
video. Any printed photo can carry any video.

Self-hosted: a small server runs on your NAS and holds the recognition index; an
Android app does the camera work and the AR tracking. Nothing leaves your
network — no cloud service, no third-party recognition API. Who may see which
photos is configured in the built-in web admin panel.

**Recognition and AR fitting run entirely on the phone; the server is not in the
hot path.** The server does three things: index resources (pre-build the ARCore
target database), transport (serve that database and the videos), and manage
(users / grants / config).

```
┌── Android app ──────────────┐    ┌── NAS ─────────────────────────┐
│ ARCore local recognition    │    │ Index: arcoreimg pre-builds a  │
│ + 6DoF world tracking       │←db─┤   whole-library target DB      │
│   ↑ loads the server-built  │←mf─┤   GET /v1/targets/db (ETag)    │
│     DB (a few MB, once)     │    │   GET /v1/targets/manifest     │
│ + GLES video quad           │←mp4┤ Transport: /v1/photo/<id>/media│
│   no network to recognise   │    │ Manage:   /admin web panel     │
└─────────────────────────────┘    └────────────────────────────────┘
     └── fallback (>1000 photos / DB won't load) → POST /v1/recognize ┘
```

## How recognition works

1. **Ingest** — for each photo the server extracts local descriptors, quantises
   them against a vocabulary tree, and stores the word sequence in an inverted
   index. It also asks ARCore's `arcoreimg` for a quality score and builds the
   `.imgdb` the phone needs for tracking. The feature backend is switchable:
   **ORB** (default; the measured baseline) or **XFeat** (CVPR 2024 pretrained
   weights, Apache-2.0). The trade-offs and measured numbers are in
   [docs/decisions.md](docs/decisions.md).
2. **Query** — a camera frame goes through the same pipeline; the inverted index
   returns a shortlist, then each candidate is geometrically verified with a
   RANSAC homography. A match needs **≥ 40 inliers**.
3. **Why 40** — measured, not guessed. Over 29,740 queries the false-positive
   inlier counts topped out at 39 while true positives had a 5th percentile of
   69. The two distributions barely overlap; 40 sits in the gap and takes the
   real-world false-positive rate to 0. See `bench/`.

Two things the recogniser deliberately refuses: photos whose texture is too
sparse for ARCore to track (**about 65% of real family photos**, measured), and
near-duplicates of something already in the library — keeping both would make
*both* permanently unrecognisable.

## Deploy

The server ships as a container image on GHCR. On the NAS you need four config
files and two of your own files — no source checkout, no build:

```bash
cp .env.example .env      # only PHOTOAR_ROOTS needs a look
docker compose up -d
```

No hand-written config file, no pre-trained vocabulary, no pre-staged model —
whatever is missing is explained in the startup log and the service still comes
up. The bootstrap admin password is printed to the log once.

Full walkthrough with a verification step after each command:
**[docs/deploy.md](docs/deploy.md)**.

The image intentionally contains neither `arcoreimg` (closed-source ARCore
binary, not redistributable) nor `vocab.npz` (a vocabulary tree trained on your
own photos). Both are bind-mounted at runtime.

## Documentation

| Document | What's in it |
|---|---|
| [docs/deploy.md](docs/deploy.md) | Step-by-step deployment: SSH → compose → hardware encoding → ingest → Tailscale → Cloudflare → APK → phone. Every step says what "it worked" looks like |
| [docs/decisions.md](docs/decisions.md) | **Decision record**: why XFeat, why no global descriptor, how the thresholds were measured, how users and permissions are designed, measured latencies, and the **known risks plus what still has to be measured on real hardware** |
| [docs/deploy-details.md](docs/deploy-details.md) | The reasoning and the numbers: why media never goes through Cloudflare, why VAAPI instead of QuickSync, why batch ingest is serial, measured baselines, troubleshooting table |
| [deploy/README.md](deploy/README.md) | Command cheat sheet, maintenance commands, what each file under `data/` is worth |
| [bench/README.md](bench/README.md) | The measurement scripts behind every number quoted above |

## Repository layout

```
src/photoar/          Recognition, ingest, transcode, and the HTTP server (Python)
  server/             /v1/* endpoints, path whitelist, media resolution
android/
  arview/             ARCore + GLES scanning view, endpoint resolver, offline cache
  app/                Compose shell: library, detail, browse, settings
  server/webui/       Zero-build web admin panel (users, grants, config, photos)
tools/                batch_ingest.py (stdlib only), export_models.py, fetch_models.py
bench/                Phase 0 measurement scripts
deploy/               config.example.json + command cheat sheet
docs/                 Deployment docs
```

## Development

```bash
pip install -e ".[dev]" && pytest        # server + recognition
cd android && ./gradlew test             # arview + shell unit tests
```

Publishing a new server image is a deliberate act, not a side effect of pushing:

```bash
git tag v0.2.0 && git push origin v0.2.0    # builds and pushes to GHCR
```

Two notes for anyone reading the source:

- `§N` references in comments point to an internal design document that is not
  published with the repository. The surrounding comment always states the
  actual reason, so nothing is lost.
- The Android release build is signed with the **debug** key on purpose — it is
  installed on a handful of known phones, not shipped to a store. Consequence:
  a build from a different machine has a different signature and cannot upgrade
  in place.

## Status

Recognition feasibility, the NAS server, the ARCore scanning view, the app
shell, on-device caching for offline scanning, the user/permission system and
the web admin panel are all done. One-click `docker compose` deployment is
verified locally under the NAS resource budget (3 CPU / 3 GiB).

Not yet verified on real hardware: the XFeat backend's latency on the target
N5095 (measured 800 ms p50 under a 3-CPU budget on a faster machine — likely
too slow there), and on-device feature extraction on a real phone. Both are
off by default. See [docs/decisions.md](docs/decisions.md) §11.
