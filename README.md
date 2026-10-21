# LUMEN — galerie photographique privée

Galerie responsive en français pour photographes : planche-contact, visionneuse avec navigation clavier/tactile, EXIF, recherche, filtres et téléchargement des originaux. FastAPI, SQLite, Pillow et ExifTool ; aucun service cloud ni développement RAW. Les JPEG embarqués des RAW servent aux aperçus.

## Installation active

- URL recommandée : https://arcturus.tail38ed6c.ts.net:9443 via Tailscale Serve (tailnet uniquement). La connexion et les miniatures ont été vérifiées via HTTPS dans Chromium mobile. Accès direct historique : http://100.90.227.52:8093.
- Tailscale Serve 9443 relaie vers http://100.90.227.52:8093 ; les autres ports Serve restent inchangés. Uvicorn accepte les en-têtes proxy uniquement depuis 100.90.227.52 pour conserver schéma HTTPS, contrôle Origin et cookies Secure. Ne pas utiliser --no-proxy-headers derrière ce proxy.
- Écoute limitée à l’adresse Tailscale, pas au LAN ni à Internet. HTTP est transporté dans le tunnel chiffré Tailscale.
- Source en lecture seule : /home/francois/Salomé ; 3 882 photos découvertes (3 558 CR2, 324 JPEG). Les vidéos sont ignorées.
- Accès sans mot de passe applicatif : PHOTO_AUTH_REQUIRED=false. Tailscale constitue la frontière d’accès ; ne pas exposer cette instance sur Internet ou sur toutes les interfaces. Les protections Origin des actions restent actives. L’authentification reste activée par défaut si ce paramètre est absent.
- Configuration : /home/francois/.config/lumen-gallery/gallery.env.
- Service utilisateur : lumen-gallery.service, activé au démarrage de la session utilisateur. Linger est déjà actif sur cet hôte.
- Commandes : `systemctl --user status lumen-gallery`, `systemctl --user restart lumen-gallery`, `journalctl --user -u lumen-gallery -n 50`.
- L’index SQLite et les aperçus sont dans data/, séparés des originaux. Le premier scan est déclenché ; les scans suivants se lancent avec « Réindexer » et ignorent les fichiers inchangés.
- Pas de déplacement, renommage, suppression ou écriture des originaux. Le service impose aussi ReadOnlyPaths sur le dossier source.

## Features

Filtres : recherche (nom/dossier/boîtier/objectif), boîtier, objectif, format, période de prise de vue, plage ISO. Tri : date croissante/décroissante, nom, ISO, ouverture et focale. Import optionnel depuis le navigateur ; les fichiers importés sont stockés dans data/uploads, jamais dans le dossier source.

RAW : extraction JpgFromRaw, puis PreviewImage ; les fichiers sans aperçu JPEG exploitable sont signalés comme erreurs de scan. Aucun dématriçage RAW. Miniatures limitées à 480 px et aperçus à 2048 px ; téléchargement = fichier original intégral.

## Requirements and setup

Python 3.12 is the tested runtime. `uv` is recommended:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

The repository vendors the official Phil Harvey ExifTool source (13.59, downloaded from `https://github.com/exiftool/exiftool`) under `vendor/exiftool/`. It is invoked as `perl vendor/exiftool/exiftool`; no system ExifTool installation or sudo is required.

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

The parent deployment can bind or expose the service through Tailscale. `GET /health` is public. The browser uses `/api/login` and then `/api/scan`; all photo, scan, upload, facet, status, and media routes require the signed HttpOnly SameSite-Strict session cookie.

A scan is asynchronous and single-flight:

```bash
curl -c /tmp/lumen.cookies -H 'Content-Type: application/json' \
  -d '{"password":"..."}' http://127.0.0.1:8093/api/login
curl -b /tmp/lumen.cookies -X POST http://127.0.0.1:8093/api/scan
curl -b /tmp/lumen.cookies http://127.0.0.1:8093/api/status
```

State-changing requests with an `Origin` header must match the request host origin. Cross-origin state changes are rejected.

## Test

```bash
.venv/bin/python -m pytest -q
```

The test suite creates real EXIF JPEGs, exercises filtering/sorting/download/auth/upload/path containment/deletion, and scans the real rawpy Canon CR2 fixtures in `tests/fixtures/`. `RAW_CANON_40D_SRAW_V103.CR2` has SHA-256 `152382ce4dbf644899d12b41b4c577f07638aa3ec5745ac344c36bad93826125`; its embedded `PreviewImage` fallback is verified as a 1936x1288 JPEG. `M0054341_01_00005.cr2` has SHA-256 `add8183e643d98d5b07479d50bf5591838451d43449fd3399e2f146cf19beb8d` and verifies source EXIF orientation `3` is applied to the preview.
