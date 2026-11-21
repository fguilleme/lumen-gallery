const test = require('node:test');
const assert = require('node:assert/strict');
const { buildPhotoQuery } = require(process.env.LUMEN_DIRECTORY_APP || '../app.js');

test('directory composes with zero rating, video, pagination and directory sorting', () => {
  const query = new URLSearchParams(buildPhotoQuery({directory: 'opaque/token+=', rating: '0', mediaType: 'video', sort: 'directory_asc', offset: 60, limit: 60}));
  assert.equal(query.get('directory'), 'opaque/token+=');
  assert.equal(query.get('rating'), '0');
  assert.equal(query.get('media_type'), 'video');
  assert.equal(query.get('sort'), 'directory_asc');
  assert.equal(query.get('offset'), '60');
});

test('descending directory sort and all folders are supported', () => {
  const query = new URLSearchParams(buildPhotoQuery({directory: '', sort: 'directory_desc'}));
  assert.equal(query.has('directory'), false);
  assert.equal(query.get('sort'), 'directory_desc');
});
