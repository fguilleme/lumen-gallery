const test = require('node:test');
const assert = require('node:assert/strict');
const {buildPhotoQuery} = require(process.env.LUMEN_STACK_APP || '../app.js');

test('stack overview and full group queries are distinct and preserve filters', () => {
  const overview = new URLSearchParams(buildPhotoQuery({similarity:'content',similarityView:'stacks',rating:'0',offset:24,limit:24}));
  assert.equal(overview.get('similarity_view'),'stacks');
  assert.equal(overview.get('rating'),'0');
  const group = new URLSearchParams(buildPhotoQuery({similarity:'content',similarityGroup:'content:17',similarityView:'stacks',limit:200}));
  assert.equal(group.get('similarity_group'),'content:17');
  assert.equal(group.has('similarity_view'),false);
});

test('invalid or mode-mismatched group tokens never leak into queries', () => {
  for (const filters of [{similarityView:'stacks'},{similarity:'near',similarityGroup:'content:1'},{similarity:'near',similarityGroup:'near:bad'}]) {
    const query = new URLSearchParams(buildPhotoQuery(filters));
    assert.equal(query.has('similarity_group'),false);
    assert.equal(query.has('similarity_view'),false);
  }
});
