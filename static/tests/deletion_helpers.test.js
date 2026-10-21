const test = require('node:test');
const assert = require('node:assert/strict');
const {
  buildPhotoQuery,
  buildZeroCountQuery,
  confirmationMessageForZeroSchedule,
  deletionDueAt,
  isDeletionScheduled,
  normaliseDeletionStatus,
  zeroCountFromPayload,
  deletionStatusCountFromPayload,
  formatDeletionDueDate,
} = require('../app.js');

test('buildPhotoQuery composes the scheduled deletion filter with existing filters', () => {
  assert.equal(
    buildPhotoQuery({
      deletionStatus: 'scheduled',
      rating: 0,
      mediaType: 'photo',
      importedOnly: true,
      q: 'lake',
      sort: 'rating_desc',
      offset: 24,
      limit: 24,
    }),
    'media_type=photo&imported_only=true&rating=0&deletion_status=scheduled&q=lake&sort=rating_desc&offset=24&limit=24',
  );
  assert.equal(
    buildPhotoQuery({ deletion_status: 'scheduled', sort: 'date_desc' }),
    'deletion_status=scheduled&sort=date_desc',
  );
});

test('buildZeroCountQuery asks only for the total number of zero-star media', () => {
  assert.equal(buildZeroCountQuery(), 'rating=0&limit=1');
});

test('deletion helpers keep scheduled state nullable and parse the backend count contract', () => {
  assert.equal(normaliseDeletionStatus('scheduled'), 'scheduled');
  assert.equal(normaliseDeletionStatus('trashed'), '');
  assert.equal(normaliseDeletionStatus(null), '');
  assert.equal(zeroCountFromPayload({ total: 37 }), 37);
  assert.equal(zeroCountFromPayload({ total: '37' }), 37);
  assert.equal(zeroCountFromPayload({ total: -1 }), null);
  assert.equal(zeroCountFromPayload({ items: [] }), null);
  assert.equal(deletionStatusCountFromPayload({ scheduled: 4 }), 4);
  assert.equal(deletionStatusCountFromPayload({ scheduled: '4' }), 4);
  assert.equal(deletionStatusCountFromPayload({ scheduled: null }), null);
});

test('scheduled deletion helpers prefer the due date and produce the exact warning scope', () => {
  const photo = {
    deletion_scheduled_at: '2026-10-21T10:00:00Z',
    delete_after: '2026-11-20T10:00:00Z',
  };
  assert.equal(deletionDueAt(photo), photo.delete_after);
  assert.equal(isDeletionScheduled(photo), true);
  assert.equal(isDeletionScheduled({ deletion_scheduled_at: null, delete_after: null }), false);
  assert.equal(
    confirmationMessageForZeroSchedule(37),
    'Tous les 37 médias à 0 étoile, y compris hors des filtres actuels, seront placés en corbeille dans un mois. Remettre une étoile annule la programmation. Aucun fichier supprimé définitivement.',
  );
});

test('formatDeletionDueDate renders a compact French date and omits nulls', () => {
  assert.match(formatDeletionDueDate('2026-11-20T10:00:00Z'), /20 nov\. 2026/);
  assert.equal(formatDeletionDueDate(null), '');
  assert.equal(formatDeletionDueDate('not-a-date'), 'not-a-date');
});
