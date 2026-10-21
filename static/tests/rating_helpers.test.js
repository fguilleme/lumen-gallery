const test = require('node:test');
const assert = require('node:assert/strict');
const {
  buildPhotoQuery,
  normaliseRating,
  createRatingQueue,
} = require('../app.js');

test('normaliseRating falls back to zero for legacy and malformed values', () => {
  assert.equal(normaliseRating(undefined), 0);
  assert.equal(normaliseRating(null), 0);
  assert.equal(normaliseRating(''), 0);
  assert.equal(normaliseRating('not-a-rating'), 0);
  assert.equal(normaliseRating(-1), 0);
  assert.equal(normaliseRating(4.5), 0);
  assert.equal(normaliseRating(0), 0);
  assert.equal(normaliseRating('0'), 0);
  assert.equal(normaliseRating(4), 4);
  assert.equal(normaliseRating('3'), 3);
  assert.equal(normaliseRating(5), 0);
});

test('buildPhotoQuery preserves an exact zero rating filter and rating sorts', () => {
  assert.equal(
    buildPhotoQuery({ rating: 0, sort: 'rating_asc', offset: 0, limit: 24 }),
    'rating=0&sort=rating_asc&offset=0&limit=24',
  );
  assert.equal(
    buildPhotoQuery({ rating: 4, sort: 'rating_desc' }),
    'rating=4&sort=rating_desc',
  );
  assert.equal(
    buildPhotoQuery({ rating: '', sort: 'rating_desc' }),
    'sort=rating_desc',
  );
});

test('createRatingQueue serializes one id and coalesces to its latest requested rating', async () => {
  const calls = [];
  const deferred = [];
  const events = [];
  const queue = createRatingQueue((id, rating) => {
    calls.push({ id, rating });
    return new Promise((resolve, reject) => deferred.push({ resolve, reject }));
  }, {
    onSettled: (event) => events.push(event),
  });

  queue.enqueue('photo-7', 1);
  queue.enqueue('photo-7', 2);
  queue.enqueue('photo-7', 4);
  assert.deepEqual(calls, [{ id: 'photo-7', rating: 1 }]);

  deferred[0].resolve({ id: 'photo-7', rating: 1 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, [
    { id: 'photo-7', rating: 1 },
    { id: 'photo-7', rating: 4 },
  ]);
  assert.equal(events[0].requestedRating, 1);
  assert.equal(events[0].latest, false);

  deferred[1].resolve({ id: 'photo-7', rating: 4 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(events.length, 2);
  assert.equal(events[1].requestedRating, 4);
  assert.equal(events[1].serverRating, 4);
  assert.equal(events[1].latest, true);
});

test('createRatingQueue ignores stale errors until the latest request settles', async () => {
  const calls = [];
  const deferred = [];
  const events = [];
  const queue = createRatingQueue((id, rating) => {
    calls.push({ id, rating });
    return new Promise((resolve, reject) => deferred.push({ resolve, reject }));
  }, {
    onSettled: (event) => events.push(event),
  });

  queue.enqueue(11, 3);
  queue.enqueue(11, 0);
  deferred[0].reject(new Error('first request failed'));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(calls, [
    { id: '11', rating: 3 },
    { id: '11', rating: 0 },
  ]);
  assert.equal(events[0].error.message, 'first request failed');
  assert.equal(events[0].latest, false);

  deferred[1].reject(new Error('latest request failed'));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(events.length, 2);
  assert.equal(events[1].error.message, 'latest request failed');
  assert.equal(events[1].latest, true);
});
