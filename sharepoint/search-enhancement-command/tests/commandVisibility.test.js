const assert = require('node:assert/strict');
const test = require('node:test');

const { commandVisibility, isSupportedFile, SUPPORTED_EXTENSIONS } = require(
  '../lib/extensions/searchEnhancement/commandVisibility'
);

test('shows enhance only for eligible states', () => {
  assert.deepEqual(commandVisibility('report.pptx', '', true), {
    enhance: true,
    remove: false
  });
  assert.deepEqual(commandVisibility('report.pptx', 'Failed', true), {
    enhance: true,
    remove: false
  });
  assert.deepEqual(commandVisibility('report.pptx', 'Ready', true), {
    enhance: false,
    remove: true
  });
});

test('offers the commands for every format the worker supports', () => {
  for (const extension of SUPPORTED_EXTENSIONS) {
    assert.deepEqual(
      commandVisibility(`report${extension}`, 'Ready', true),
      { enhance: false, remove: true },
      `expected ${extension} to be eligible`
    );
  }
  assert.ok(SUPPORTED_EXTENSIONS.includes('.docx'));
  assert.ok(SUPPORTED_EXTENSIONS.includes('.pdf'));
  assert.ok(SUPPORTED_EXTENSIONS.includes('.hwp'));
});

test('matches extensions case-insensitively', () => {
  assert.deepEqual(commandVisibility('REPORT.DOCX', 'Ready', true), {
    enhance: false,
    remove: true
  });
});

test('hides commands without edit permission or for unsupported files', () => {
  assert.deepEqual(commandVisibility('report.txt', 'Ready', true), {
    enhance: false,
    remove: false
  });
  assert.deepEqual(commandVisibility('report.ppt', 'Ready', true), {
    enhance: false,
    remove: false
  });
  assert.deepEqual(commandVisibility('report.pptx', 'Ready', false), {
    enhance: false,
    remove: false
  });
});

test('rejects names that are only an extension', () => {
  assert.equal(isSupportedFile('.docx'), false);
  assert.equal(isSupportedFile(undefined), false);
  assert.equal(isSupportedFile(''), false);
});
