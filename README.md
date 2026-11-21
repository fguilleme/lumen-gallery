# LUMEN — galerie photographique privée

Galerie responsive en français pour photographes : planche-contact photo/vidéo, visionneuse avec navigation clavier/tactile, zoom/pan, métadonnées usuelles, menus de filtrage, notation 0–4 et partage natif. FastAPI, SQLite, Pillow, ExifTool et FFmpeg ; aucun service cloud ni développement RAW. Les JPEG embarqués des RAW servent aux aperçus.

## Installation active

- URL recommandée : https://arcturus.tail38ed6c.ts.net:9443 via Tailscale Serve (tailnet uniquement). La connexion et les miniatures ont été vérifiées via HTTPS dans Chromium mobile. Accès direct historique : http://100.90.227.52:8093.
- Tailscale Serve 9443 relaie vers http://100.90.227.52:8093 ; les autres ports Serve restent inchangés. Uvicorn accepte les en-têtes proxy uniquement depuis 100.90.227.52 pour conserver schéma HTTPS, contrôle Origin et cookies Secure. Ne pas utiliser --no-proxy-headers derrière ce proxy.
- Écoute limitée à l’adresse Tailscale, pas au LAN ni à Internet. HTTP est transporté dans le tunnel chiffré Tailscale.
- Source en lecture seule : /home/francois/Salomé ; 3 882 photos découvertes (3 558 CR2, 324 JPEG) au dernier index. Les vidéos `.mov`, `.mp4`, `.m4v` et `.webm` sont indexées à partir des métadonnées ffprobe ; les aperçus sont extraits à la demande.
- Accès sans mot de passe applicatif : PHOTO_AUTH_REQUIRED=false. Tailscale constitue la frontière d’accès ; ne pas exposer cette instance sur Internet ou sur toutes les interfaces. Les protections Origin des actions restent actives. L’authentification reste activée par défaut si ce paramètre est absent.
- Configuration : /home/francois/.config/lumen-gallery/gallery.env.
- Service utilisateur : lumen-gallery.service, activé au démarrage de la session utilisateur. Linger est déjà actif sur cet hôte.
- Commandes : `systemctl --user status lumen-gallery`, `systemctl --user restart lumen-gallery`, `journalctl --user -u lumen-gallery -n 50`.
- L’index SQLite et les aperçus sont dans data/, séparés des originaux. Le premier scan est déclenché ; les scans suivants se lancent avec « Réindexer » et ignorent les fichiers inchangés.
- Le serveur web n’écrit jamais les originaux : son unité impose ReadOnlyPaths sur le dossier source. Seul le service de maintenance distinct peut déplacer vers la corbeille les fichiers explicitement programmés, à leur échéance, après nouvelle vérification de leur note.
- Maintenance installée : `lumen-trash.timer` vérifie les échéances chaque heure, via `ops/lumen-trash.service`. Les unités utilisateur sont des liens vers `ops/`. Aucun effacement définitif ni vidage automatique de la corbeille.

## Features

Filtres par menus : dossier (sous-dossiers inclus), type photo/vidéo, boîtier, objectif, format, période de prise de vue, plage ISO, note exacte de 0 à 4, imports et suppression programmée. L’API conserve aussi `q` (recherche textuelle) et `rating_min` ; le champ de recherche textuelle est volontairement absent de l’interface. Tri : date, nom, dossier A → Z ou Z → A, ISO, ouverture, focale et note. Les notes sont partagées dans cette galerie privée et persistées dans SQLite, sans écriture EXIF. Les étoiles sélectionnées sont pleines et dorées ; les autres restent creuses, sur une bande translucide. Import depuis le navigateur dans data/uploads, jamais dans le dossier source ; le filtre Importés permet de les retrouver sans conflit avec les anciens filtres.

Visionneuse : photo entière centrée par défaut, pinch-zoom de 1× à 5×, déplacement borné, double-tap et remise à zéro. Les gestes de zoom sont limités aux photos ; les vidéos conservent leurs commandes natives. Pas de téléchargement dans les vignettes ; l’icône de partage de la visionneuse appelle le partage natif de fichiers. Si le navigateur refuse le RAW, le partage d’un JPEG est proposé explicitement. Le partage natif iOS nécessite une confirmation sur appareil réel ; les tests Chromium valident l’appel et les octets transmis, pas l’interface système.

RAW : extraction JpgFromRaw, puis PreviewImage ; les fichiers sans aperçu JPEG exploitable sont signalés comme erreurs de scan. Aucun dématriçage RAW. Miniatures limitées à 480 px et aperçus à 2048 px ; téléchargement = fichier original intégral.

Vidéo : les métadonnées sont lues avec `ffprobe` sans décoder/indexer la vidéo entière. Les aperçus JPEG sont produits par `ffmpeg` sur une image située à `min(1 seconde, durée × 0,1)` et mis en cache par chemin, taille et mtime de la source. Les MP4 H.264/AAC sont servis directement ; les MOV H.264/PCM sont remuxés en MP4 avec copie vidéo et audio AAC. Les codecs vidéo incompatibles nécessitent un encodeur `libx264` disponible. Le flux `/api/photos/{id}/stream` accepte les requêtes HTTP Range.

## Requirements and setup

Python 3.12 is the tested runtime. `uv` is recommended:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

The repository vendors the official Phil Harvey ExifTool source (13.59, downloaded from `https://github.com/exiftool/exiftool`) under `vendor/exiftool/`. It is invoked as `perl vendor/exiftool/exiftool`; no system ExifTool installation or sudo is required. Video support additionally requires the `ffmpeg` and `ffprobe` executables on `PATH` (or explicit `PHOTO_FFMPEG`/`PHOTO_FFPROBE` paths).

## Configuration

The backend reads these environment variables:

- `PHOTO_ROOT`: read-only source directory. The default is `data/uploads`.
- `PHOTO_DATA_DIR`: runtime database, cache, and secret directory. The default is `data`.
- `PHOTO_UPLOAD_DIR`: writable upload directory. The default is `PHOTO_DATA_DIR/uploads`.
- `PHOTO_DB`: optional SQLite path. The default is `PHOTO_DATA_DIR/gallery.sqlite3`.
- `PHOTO_PASSWORD`: optional gallery password. If absent, a random password is generated once in `PHOTO_DATA_DIR/access-password` with mode `0600`.
- `PHOTO_SESSION_SECRET`: optional independent cookie-signing secret. If absent, it is generated in `PHOTO_DATA_DIR/session-secret` with mode `0600`.
- `PHOTO_ALLOWED_HOSTS`: optional comma-separated explicit host allow-list for state-changing requests. Loopback, private/local IPs, Tailscale `100.64.0.0/10`, `*.ts.net`, `localhost`, and `testserver` are accepted by default.
- `PHOTO_MAX_UPLOAD_BYTES`: per-file upload limit; defaults to 250 MiB.
- `PHOTO_FFMPEG`: optional path to the FFmpeg executable; defaults to `ffmpeg` on `PATH`.
- `PHOTO_FFPROBE`: optional path to the FFprobe executable; defaults to `ffprobe` on `PATH`.
- Les notes persistantes utilisent la colonne SQLite `photos.rating` (0 à 4) via `PATCH /api/photos/{id}/rating` ; aucun fichier annexe ni réglage supplémentaire n’est requis.

For the deployment source supplied by the parent worker:

```bash
export PHOTO_ROOT='/home/francois/Salomé'
```

The scanner never writes to `PHOTO_ROOT`. It also rejects files whose resolved path escapes a configured root through a symlink. Uploads are stored under `PHOTO_UPLOAD_DIR` with a random storage name; the original filename is retained as database metadata.

## Run

```bash
PHOTO_ROOT='/home/francois/Salomé' \
  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8093
```

The backend can require signed HttpOnly SameSite-Strict sessions (default). This deployment explicitly uses `PHOTO_AUTH_REQUIRED=false`, so the browser opens directly without a password; Tailscale restricts network access. Cross-origin state-changing requests remain rejected in both modes.

A scan is asynchronous and single-flight:

```bash
curl -c /tmp/lumen.cookies -H 'Content-Type: application/json' \
  -d '{"password":"..."}' http://127.0.0.1:8093/api/login
curl -b /tmp/lumen.cookies -X POST http://127.0.0.1:8093/api/scan
curl -b /tmp/lumen.cookies http://127.0.0.1:8093/api/status
```

State-changing requests with an `Origin` header must match the request host origin. Cross-origin state changes are rejected.

## Recoverable trash maintenance

The web application never moves or deletes source media. `POST /api/deletions/schedule-zero` records one-month, calendar-clamped jobs for every catalogued zero-rated item; rating an item to at least 1 cancels its scheduled job. The scheduled metadata is joined into photo responses as `deletion_scheduled_at` and `delete_after`. `GET /api/deletions/status` and `GET /api/deletions/trash` expose maintenance status and recovery entries.

The maintenance worker is a separate explicit CLI. It defaults to a read-only dry run and processes at most 100 due jobs per invocation:

```bash
.venv/bin/python -m gallery.trash_worker --dry-run
.venv/bin/python -m gallery.trash_worker --apply
.venv/bin/python -m gallery.trash_worker --restore JOBID
```

Applied jobs use an atomic same-filesystem rename into `PHOTO_DATA_DIR/trash/{uuid}/`, with a recovery manifest; source size, mtime, realpath containment, symlink safety, current zero rating, and destination collisions are checked before a move. A changed or cross-filesystem source is recorded as an error without unlinking it. Restore is explicit by job ID and refuses to overwrite an existing original destination. The web app never starts deletion itself.

Installed on this host: `ops/lumen-trash.service` and `ops/lumen-trash.timer`, symlinked under `~/.config/systemd/user/`. The enabled hourly timer runs the separate `--apply` maintenance command with write access restricted to gallery data and the configured source. The web unit keeps read-only access to the source. No permanent purge is configured.

To inspect maintenance: `systemctl --user status lumen-trash.timer`, `journalctl --user -u lumen-trash.service -n 30`. To restore, load the same environment as the worker, then use the job ID from `GET /api/deletions/trash`:

```bash
set -a
source /home/francois/.config/lumen-gallery/gallery.env
set +a
.venv/bin/python -m gallery.trash_worker --restore JOBID
```

## Test

```bash
.venv/bin/python -m pytest -q
```

The test suite creates real EXIF JPEGs and real FFmpeg-generated rotated MP4/MOV fixtures, exercises filtering/sorting/download/auth/upload/path containment/deletion/video Range streaming/source integrity, and scans the real rawpy Canon CR2 fixtures in `tests/fixtures/`. `RAW_CANON_40D_SRAW_V103.CR2` has SHA-256 `152382ce4dbf644899d12b41b4c577f07638aa3ec5745ac344c36bad93826125`; its embedded `PreviewImage` fallback is verified as a 1936x1288 JPEG. `M0054341_01_00005.cr2` has SHA-256 `add8183e643d98d5b07479d50bf5591838451d43449fd3399e2f146cf19beb8d` and verifies source EXIF orientation `3` is applied to the preview.
