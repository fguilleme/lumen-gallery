const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

test('sort menu omits filename sorting but keeps both directory sorts', () => {
  const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
  assert.doesNotMatch(html, /<option\s+value="name_asc"/);
  assert.match(html, /<option\s+value="directory_asc"/);
  assert.match(html, /<option\s+value="directory_desc"/);
});
