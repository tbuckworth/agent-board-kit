// The service worker's STATIC_ASSETS must name every file the app serves, and only those.
// Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const SKIP = new Set(['sw.js', 'package.json', 'fonts/LICENSE.txt']);

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === 'tests' ? [] : walk(path);
    return [relative(ROOT, path).split('\\').join('/')];
  });
}

test('the service worker caches exactly the app\'s files', () => {
  const sw = readFileSync(join(ROOT, 'sw.js'), 'utf8');
  const listed = [...sw.matchAll(/'\.\/([^']*)'/g)].map(m => m[1]).filter(Boolean);
  const files = walk(ROOT).filter(f => !SKIP.has(f));
  assert.deepEqual([...new Set(listed)].sort(), files.sort());
});
