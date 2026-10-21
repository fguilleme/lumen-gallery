const test = require('node:test');
const assert = require('node:assert/strict');
const {
  buildPhotoQuery,
  formatBytes,
  getPhotoAspectRatio,
  formatExifValue,
  sessionPresentation,
  buildExifRows,
} = require('../app.js');

test('buildPhotoQuery serializes active filters and preserves API names', () => {
  const params = buildPhotoQuery({
    q: 'mont blanc',
    camera: 'Leica M11',
    lens: '',
    format: 'RAW',
    dateFrom: '2026-01-02',
    dateTo: '2026-01-31',
    isoMin: '100',
    isoMax: '800',
    sort: 'iso_desc',
    offset: 24,
    limit: 24,
  });

  assert.equal(
    params,
    'q=mont%20blanc&camera=Leica%20M11&format=RAW&date_from=2026-01-02&date_to=2026-01-31&iso_min=100&iso_max=800&sort=iso_desc&offset=24&limit=24',
  );
});

test('buildPhotoQuery does not leak empty values or unsupported sort names', () => {
  const params = buildPhotoQuery({
    q: '  ',
    camera: null,
    sort: 'newest',
    offset: 0,
    limit: 0,
  });

  assert.equal(params, 'sort=date_desc&offset=0&limit=0');
});

test('formatBytes chooses compact French units without inventing a size', () => {
  assert.equal(formatBytes(0), '0 o');
  assert.equal(formatBytes(1536), '1,5 Ko');
  assert.equal(formatBytes(2500000), '2,5 Mo');
  assert.equal(formatBytes(null), '—');
});

test('getPhotoAspectRatio preserves source dimensions and guards malformed input', () => {
  assert.equal(getPhotoAspectRatio({ width: 600, height: 400 }), 1.5);
  assert.equal(getPhotoAspectRatio({ width: 0, height: 800 }), 1.5);
  assert.equal(getPhotoAspectRatio({ width: 100, height: 0 }), 1.5);
});

test('formatExifValue renders missing metadata as an em dash', () => {
  assert.equal(formatExifValue(undefined), '—');
  assert.equal(formatExifValue(null), '—');
  assert.equal(formatExifValue(''), '—');
  assert.equal(formatExifValue(400), '400');
});

test('sessionPresentation identifies password-free Tailscale access', () => {
  assert.deepEqual(
    sessionPresentation({ authenticated: true, auth_required: false }),
    { label: 'Accès Tailscale', hideLogout: true },
  );
});

test('buildExifRows keeps the lightbox metadata compact and photographic', () => {
  const rows = buildExifRows(
    {
      camera: 'Canon EOS 6D Mark II',
      lens: '50mm',
      date: '2026-10-16T20:14:33',
      iso: 640,
      aperture: 9,
      shutter: '1/200',
      focal: 50,
      width: 6240,
      height: 4160,
      size: 32409901,
      format: 'CR2',
    },
    {
      exif: {
        InternalSerialNumber: 'must not be shown',
        GPSLatitude: 'must not be shown',
        FilePermissions: 100644,
        ExposureCompensation: 0,
      },
    },
  );

  assert.ok(rows.length <= 12);
  assert.deepEqual(rows.map(({ label }) => label), [
    'Prise de vue',
    'Appareil',
    'Objectif',
    'Focale',
    'Ouverture',
    'Vitesse',
    'ISO',
    'Dimensions',
    'Taille',
    'Format',
    'Correction d’exposition',
  ]);
  assert.equal(rows.some(({ label, value }) => /serial|gps|permission/i.test(`${label} ${value}`)), false);
});

test('sessionPresentation preserves the private session presentation by default', () => {
  assert.deepEqual(
    sessionPresentation({ authenticated: true }),
    { label: 'connecté', hideLogout: false },
  );
});
