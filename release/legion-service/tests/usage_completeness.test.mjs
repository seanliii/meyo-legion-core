import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/index_v2.html', import.meta.url), 'utf8');
const scope = {};
vm.createContext(scope);
for (const name of ['_num', '_usageBrief']) {
  const line = source.split('\n').find(line => line.startsWith(`function ${name}(`));
  assert.ok(line, `${name} must be the shipped page function`);
  vm.runInContext(line, scope);
}

test('partial usage is presented as known subtotal, not a full cost total', () => {
  const text = scope._usageBrief({
    available: true, input_tokens: 7, output_tokens: 3, total_tokens: 10,
    complete: false, coverage: 'partial', attempts_unreported: 1,
  });
  assert.match(text, /已知部分 10/);
  assert.match(text, /1 次执行用量未上报/);
  assert.doesNotMatch(text, /合计 10/);
});

test('missing token dimensions are unavailable, not zero', () => {
  const text = scope._usageBrief({
    available: true, input_tokens: null, output_tokens: 3, total_tokens: 3, complete: false,
  });
  assert.match(text, /输入 未上报/);
  assert.doesNotMatch(text, /输入 0/);
});

test('complete native accounting still displays the actual total', () => {
  assert.match(scope._usageBrief({
    available: true, input_tokens: 7, output_tokens: 3, total_tokens: 10, complete: true,
  }), /合计 10/);
});
