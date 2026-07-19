const assert = require('node:assert/strict');
const test = require('node:test');

const { commandVisibility } = require(
  '../lib/extensions/searchEnhancement/commandVisibility'
);

test('shows enhance only for eligible PPT states', () => {
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

test('hides commands without edit permission or for non-PPT files', () => {
  assert.deepEqual(commandVisibility('report.pdf', 'Ready', true), {
    enhance: false,
    remove: false
  });
  assert.deepEqual(commandVisibility('report.pptx', 'Ready', false), {
    enhance: false,
    remove: false
  });
});
