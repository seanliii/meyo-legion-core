import test from 'node:test'
import assert from 'node:assert/strict'

// Dynamic import makes the first red run report the missing behavior explicitly.
async function parser() {
  const module = await import('../lib/sse.js').catch(() => ({}))
  assert.equal(typeof module.parseSSE, 'function', 'parseSSE must exist')
  return module.parseSSE
}

function stream(text, chunkSize = 1) {
  const bytes = new TextEncoder().encode(text)
  return new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) {
        controller.enqueue(bytes.slice(i, i + chunkSize))
      }
      controller.close()
    },
  })
}

test('SSE retains event id and multibyte text across arbitrary byte boundaries', async () => {
  const parse = await parser()
  const result = []
  for await (const event of parse(stream(': ping\r\nid: 12\r\nevent: meta\r\ndata: {"team_id":"军团"}\r\n\r\n'))) {
    result.push(event)
  }
  assert.deepEqual(result, [{ id: '12', event: 'meta', data: '{"team_id":"军团"}' }])
})

test('SSE admits a CR-only terminating blank line at EOF, whole or split across chunks', async () => {
  const parse = await parser()
  for (const chunkSize of [1, 2, 17, 1024]) {
    const body = stream('id: 3\revent: meta\rdata: {"ok":true,"text":"军团"}\r\r', chunkSize)
    const result = []
    for await (const event of parse(body)) result.push(event)
    assert.deepEqual(result, [{ id: '3', event: 'meta', data: '{"ok":true,"text":"军团"}' }],
      `chunk size ${chunkSize}`)
    assert.equal(body.locked, false)
  }
})

test('SSE joins data lines and discards incomplete events at EOF', async () => {
  const parse = await parser()
  const result = []
  for await (const event of parse(stream('data: first\ndata: second\n\nid: 14\ndata: incomplete'))) {
    result.push(event)
  }
  assert.deepEqual(result, [{ id: '', event: 'message', data: 'first\nsecond' }])
})

test('SSE accepts LF/CR/CRLF without treating a final nonblank line as a complete event', async () => {
  const parse = await parser()
  for (const ending of ['\n', '\r', '\r\n']) {
    for (const chunkSize of [1, 1024]) {
      const body = stream(['id: 5', 'data: first', 'data: 第二行', '', 'data: unfinished', ''].join(ending), chunkSize)
      const result = []
      for await (const event of parse(body)) result.push(event)
      assert.deepEqual(result, [{ id: '5', event: 'message', data: 'first\n第二行' }])
      assert.equal(body.locked, false)
    }
  }
})

test('SSE releases and cancels its reader even when parsing begins with an aborted signal', async () => {
  const parse = await parser()
  const controller = new AbortController()
  const reason = new DOMException('Synthetic pre-abort', 'AbortError')
  controller.abort(reason)
  let cancellations = 0
  const body = new ReadableStream({ cancel() { cancellations++ } })
  await assert.rejects(async () => {
    for await (const event of parse(body, { signal: controller.signal })) {
      assert.fail(`An aborted parser must not yield ${event.event}`)
    }
  }, error => error === reason)
  assert.equal(body.locked, false)
  assert.equal(cancellations, 1)
})

test('SSE stops reading and cancels its reader when the caller aborts', async () => {
  const parse = await parser()
  const controller = new AbortController()
  let cancelled = false
  const body = new ReadableStream({ cancel() { cancelled = true } })
  const iterator = parse(body, { signal: controller.signal })
  const next = iterator.next()
  controller.abort()
  await assert.rejects(next, { name: 'AbortError' })
  assert.equal(cancelled, true)
  assert.equal(body.locked, false)
})

test('SSE releases the reader when the consumer stops after the acknowledgement', async () => {
  const parse = await parser()
  let cancellations = 0
  const body = new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('event: meta\ndata: accepted\n\n')) },
    cancel() { cancellations++ },
  })
  for await (const event of parse(body)) {
    assert.equal(event.event, 'meta')
    break
  }
  assert.equal(cancellations, 1)
  assert.equal(body.locked, false)
})

test('SSE bounds an unterminated event instead of growing without limit', async () => {
  const parse = await parser()
  const body = stream(`data: ${'x'.repeat(1100)}`, 64)
  await assert.rejects(async () => {
    for await (const event of parse(body, { maxEventBytes: 1024 })) {
      void event
    }
  }, /limit/i)
  assert.equal(body.locked, false)
})
