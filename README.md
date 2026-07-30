# photo-ar

**English** · [简体中文](README.zh-CN.md)

Point your phone at a **printed** photo and the video from that moment plays,
anchored to the paper.

Self-hosted: a small server runs on your NAS and holds the recognition index; an
Android app does the camera work and the AR tracking. Nothing leaves your
network — no cloud service, no account, no third-party recognition API.

```
┌── Android app ──────────┐        ┌── NAS ─────────────────────────┐
│ camera frame ──────────────POST──→ /v1/recognize                  │
│                         │        │   ORB → vocabulary tree →      │
│                         │        │   inverted index → RANSAC      │
│ ARCore Augmented Image  │←──id───┤                                │
│ + GLES video quad       │        │ /v1/photo/<id>/media           │
│                         │←─mp4───┤   (Range-capable streaming)    │
└─────────────────────────┘        └────────────────────────────────┘
```

## How recognition works

1. **Ingest** — for each photo the server extracts ORB descriptors, quantises
   them against a pre-trained vocabulary tree, and stores the word sequence in
   an inverted index. It also asks ARCore's `arcoreimg` for a quality score and
   builds the `.imgdb` the phone needs for tracking.
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
docker compose pull && docker compose up -d
```

Full walkthrough with a verification step after each command:
**[docs/deploy.md](docs/deploy.md)**.

The image intentionally contains neither `arcoreimg` (closed-source ARCore
binary, not redistributable) nor `vocab.npz` (a vocabulary tree trained on your
own photos). Both are bind-mounted at runtime.

## Documentation

| Document | What's in it |
|---|---|
| [docs/deploy.md](docs/deploy.md) | Step-by-step deployment: SSH → compose → hardware encoding → ingest → Tailscale → Cloudflare → APK → phone. Every step says what "it worked" looks like |
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
tools/                batch_ingest.py (stdlib only), cf_edge_probe.py
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
shell, and on-device caching for offline scanning are all done and running. A
read-only web version — so relatives can see the effect without installing an
app — is designed but not built.
