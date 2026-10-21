const test = require('node:test');
const assert = require('node:assert/strict');
const {
  buildPhotoQuery,
  formatBytes,
  formatDuration,
  formatUploadSelection,
  getPhotoAspectRatio,
  formatExifValue,
  hasMediaTypeContract,
  matchesMediaTypeFilter,
  mediaTypeOf,
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

test('buildPhotoQuery serializes the optional media kind without sending invalid kinds', () => {
  assert.equal(
    buildPhotoQuery({ mediaType: 'video', sort: 'date_desc' }),
    'media_type=video&sort=date_desc',
  );
  assert.equal(
    buildPhotoQuery({
      mediaType: 'photo',
      q: '  lake ',
      format: 'JPEG',
      dateFrom: '2026-01-02',
      sort: 'date_desc',
    }),
    'media_type=photo&q=lake&format=JPEG&date_from=2026-01-02&sort=date_desc',
  );
  assert.equal(
    buildPhotoQuery({ mediaType: 'archive', sort: 'date_desc' }),
    'sort=date_desc',
  );
});

test('buildPhotoQuery sends imported_only only for an active imported filter', () => {
  assert.equal(
    buildPhotoQuery({ importedOnly: true, sort: 'date_desc' }),
    'imported_only=true&sort=date_desc',
  );
  assert.equal(
    buildPhotoQuery({ importedOnly: false, sort: 'date_desc' }),
    'sort=date_desc',
  );
});

test('formatDuration renders compact media durations and omits nullable values', () => {
  assert.equal(formatDuration(0), '0:00');
  assert.equal(formatDuration(5), '0:05');
  assert.equal(formatDuration(83), '1:23');
  assert.equal(formatDuration(83.9), '1:23');
  assert.equal(formatDuration(3661), '1:01:01');
  assert.equal(formatDuration(null), '');
  assert.equal(formatDuration('not-a-duration'), '');
});

test('formatUploadSelection makes the visible import action explicit', () => {
  assert.equal(formatUploadSelection(1), '1 fichier prêt à importer');
  assert.equal(formatUploadSelection(3), '3 fichiers prêts à importer');
  assert.equal(formatUploadSelection(0), '');
});

test('mediaTypeOf keeps the additive contract strict and legacy items photographic', () => {
  assert.equal(mediaTypeOf({ media_type: 'video' }), 'video');
  assert.equal(mediaTypeOf({ media_type: 'photo' }), 'photo');
  assert.equal(mediaTypeOf({ media_type: 'unknown' }), 'photo');
  assert.equal(mediaTypeOf({}), 'photo');
});

test('hasMediaTypeContract rejects legacy or malformed media lists before kind filtering', () => {
  assert.equal(
    hasMediaTypeContract([{ media_type: 'photo' }, { media_type: 'video' }]),
    true,
  );
  assert.equal(hasMediaTypeContract([{ media_type: 'photo' }, {}]), false);
  assert.equal(hasMediaTypeContract([{ media_type: 'photo' }, { media_type: 'audio' }]), false);
  assert.equal(hasMediaTypeContract([]), false);
});

test('matchesMediaTypeFilter refuses legacy responses for an explicit kind', () => {
  assert.equal(matchesMediaTypeFilter([{ media_type: 'video' }], 'video'), true);
  assert.equal(matchesMediaTypeFilter([{ media_type: 'photo' }], 'video'), false);
  assert.equal(matchesMediaTypeFilter([{ id: 1 }], 'video'), false);
  assert.equal(matchesMediaTypeFilter([], 'video'), false);
  assert.equal(matchesMediaTypeFilter([{ id: 1 }], ''), true);
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

test('buildExifRows whitelists video metadata and never shows photographic settings as dashes', () => {
  const rows = buildExifRows(
    {
      media_type: 'video',
      date: '2026-10-16T20:14:33',
      camera: 'Sony FX3',
      width: 3840,
      height: 2160,
      duration: 83,
      size: 125000000,
      format: 'MP4',
      lens: 'must not be shown',
      iso: 800,
      aperture: 2.8,
      shutter: '1/50',
      focal: 35,
    },
    {
      media_type: 'video',
      exif: {
        LensModel: 'must not be shown',
        GPSLatitude: 'must not be shown',
      },
    },
  );

  assert.deepEqual(rows.map(({ label }) => label), [
    'Prise de vue',
    'Appareil',
    'Dimensions',
    'Durée',
    'Taille',
    'Format',
  ]);
  assert.equal(rows.find(({ label }) => label === 'Durée').value, '1:23');
  assert.equal(rows.some(({ label, value }) => /objectif|focale|ouverture|vitesse|iso|gps|dash/i.test(`${label} ${value}`)), false);
});

test('sessionPresentation preserves the private session presentation by default', () => {
  assert.deepEqual(
    sessionPresentation({ authenticated: true }),
    { label: 'connecté', hideLogout: false },
  );
});
