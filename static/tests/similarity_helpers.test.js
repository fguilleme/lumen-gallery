const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const appPath = process.env.LUMEN_SIMILARITY_APP || '../app.js';
const {
  buildPhotoQuery,
  normaliseSimilarity,
  similaritySectionKey,
  similarityGroupBoundary,
  similarityGroupTitle,
} = require(path.resolve(__dirname, appPath));

const stagedIndex = fs.readFileSync(
  path.join(path.dirname(path.resolve(__dirname, appPath)), 'index.html'),
  'utf8',
);

test('buildPhotoQuery composes a valid similarity mode with existing filters and pagination', () => {
  const query = new URLSearchParams(buildPhotoQuery({
    similarity: 'near',
    mediaType: 'photo',
    importedOnly: true,
    rating: '0',
    directory: 'opaque/token+=',
    sort: 'directory_desc',
    offset: 24,
    limit: 24,
  }));

  assert.equal(query.get('similarity'), 'near');
  assert.equal(query.get('media_type'), 'photo');
  assert.equal(query.get('imported_only'), 'true');
  assert.equal(query.get('rating'), '0');
  assert.equal(query.get('directory'), 'opaque/token+=');
  assert.equal(query.get('sort'), 'directory_desc');
  assert.equal(query.get('offset'), '24');
  assert.equal(query.get('limit'), '24');
});

test('buildPhotoQuery omits empty, malformed, and unsupported similarity values', () => {
  for (const similarity of ['', '  ', null, undefined, 'nearby', 'NEAR', 'contentual']) {
    const query = new URLSearchParams(buildPhotoQuery({ similarity, sort: 'date_desc' }));
    assert.equal(query.has('similarity'), false, `unexpected similarity for ${String(similarity)}`);
  }
  assert.equal(normaliseSimilarity('near'), 'near');
  assert.equal(normaliseSimilarity('content'), 'content');
  assert.equal(normaliseSimilarity('nearby'), '');
});

test('similarity grouping keeps videos and singleton or unanalysed media in Autres médias', () => {
  const grouped = { media_type: 'photo', similarity_group: 'near:alpha', similarity_size: 3 };
  const sameGroup = { media_type: 'photo', similarity_group: 'near:alpha', similarity_size: 3 };
  const otherGroup = { media_type: 'photo', similarity_group: 'near:beta', similarity_size: 2 };
  const singleton = { media_type: 'photo', similarity_group: 'near:solo', similarity_size: 1 };
  const unanalysed = { media_type: 'photo', similarity_group: null, similarity_size: 0 };
  const video = { media_type: 'video', similarity_group: 'near:video', similarity_size: 4 };

  assert.equal(similaritySectionKey(grouped, 'near'), 'near:alpha');
  assert.equal(similaritySectionKey(singleton, 'near'), 'ungrouped');
  assert.equal(similaritySectionKey(unanalysed, 'near'), 'ungrouped');
  assert.equal(similaritySectionKey(video, 'near'), 'ungrouped');
  assert.equal(similarityGroupBoundary(null, grouped, 'near'), true);
  assert.equal(similarityGroupBoundary(grouped, sameGroup, 'near'), false);
  assert.equal(similarityGroupBoundary(grouped, otherGroup, 'near'), true);
  assert.equal(similarityGroupBoundary(otherGroup, singleton, 'near'), true);
  assert.equal(similarityGroupBoundary(singleton, video, 'near'), false);
  assert.equal(similarityGroupBoundary(video, unanalysed, 'near'), false);
});

test('similarityGroupTitle uses the mode label and filtered group size', () => {
  assert.equal(
    similarityGroupTitle({ similarity_group: 'near:alpha', similarity_size: 6 }, 'near'),
    'Photos proches · 6 images',
  );
  assert.equal(
    similarityGroupTitle({ similarity_group: 'content:alpha', similarity_size: 1 }, 'content'),
    'Contenu similaire · 1 image',
  );
});

test('staged controls expose both grouping modes without restoring filename sorting', () => {
  assert.match(stagedIndex, /<span[^>]*>Regrouper<\/span>/);
  assert.match(stagedIndex, /<option value="">Sans regroupement<\/option>/);
  assert.match(stagedIndex, /<option value="near">Photos proches<\/option>/);
  assert.match(stagedIndex, /<option value="content">Contenu similaire<\/option>/);
  assert.doesNotMatch(stagedIndex, /<option\s+value="name_asc"/);
  assert.match(stagedIndex, /\/static\/app\.js\?v=[^"\s]+/);
});
