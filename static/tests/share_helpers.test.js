const test = require('node:test');
const assert = require('node:assert/strict');
const {
  shareFileName,
  shareFileType,
  isRawMedia,
} = require('../app.js');

test('shareFileName preserves the original response filename before item fallback', () => {
  const photo = { name: 'fallback.CR2' };

  assert.equal(
    shareFileName(photo, 'attachment; filename="Original Name.CR2"', '/api/photos/7/download'),
    'Original Name.CR2',
  );
  assert.equal(
    shareFileName(photo, "attachment; filename*=UTF-8''Original%20Name.CR2", '/api/photos/7/download'),
    'Original Name.CR2',
  );
  assert.equal(shareFileName(photo, '', '/api/photos/7/download'), 'fallback.CR2');
});

test('shareFileName uses a media URL basename only when no original name exists', () => {
  assert.equal(
    shareFileName({}, '', 'https://gallery.test/api/photos/7/download?raw=1'),
    'download',
  );
  assert.equal(shareFileName({}, '', ''), 'media');
});

test('shareFileType prefers the response blob MIME and falls back to its header', () => {
  assert.equal(shareFileType('image/jpeg', 'application/octet-stream'), 'image/jpeg');
  assert.equal(shareFileType('', 'video/quicktime; charset=binary'), 'video/quicktime');
  assert.equal(shareFileType('', ''), 'application/octet-stream');
});

test('isRawMedia identifies original RAW formats without treating JPEG or MOV as RAW', () => {
  assert.equal(isRawMedia({ name: 'frame.CR2', format: 'CR2' }), true);
  assert.equal(isRawMedia({ name: 'frame.nef', format: 'NEF' }), true);
  assert.equal(isRawMedia({ name: 'frame.jpg', format: 'JPEG' }), false);
  assert.equal(isRawMedia({ name: 'clip.MOV', format: 'MOV', media_type: 'video' }), false);
});
