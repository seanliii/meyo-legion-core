/** Read SSE incrementally without dropping ids or splitting UTF-8 characters. */
export async function* parseSSE(body, { signal, maxEventBytes = 1024 * 1024 } = {}) {
  if (!body) throw new Error('Legion returned an empty SSE body')
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let id = ''
  let event = 'message'
  let data = []
  let eventBytes = 0
  const abort = () => { void reader.cancel(signal.reason).catch(() => {}) }
  try {
    signal?.throwIfAborted()
    signal?.addEventListener('abort', abort, { once: true })
    while (true) {
      signal?.throwIfAborted()
      const chunk = await reader.read()
      signal?.throwIfAborted()
      buffer += chunk.done ? decoder.decode() : decoder.decode(chunk.value, { stream: true })
      while (true) {
        const match = /[\r\n]/.exec(buffer)
        if (!match) break
        const index = match.index
        // Wait for a possible split CRLF, but a trailing CR is complete at EOF.
        if (buffer[index] === '\r' && index === buffer.length - 1 && !chunk.done) break
        const width = buffer[index] === '\r' && buffer[index + 1] === '\n' ? 2 : 1
        const line = buffer.slice(0, index)
        buffer = buffer.slice(index + width)
        eventBytes += Buffer.byteLength(line, 'utf8') + width
        if (eventBytes > maxEventBytes) throw new Error('SSE event exceeded the byte limit')
        if (line === '') {
          if (data.length) yield { id, event, data: data.join('\n') }
          event = 'message'
          data = []
          eventBytes = 0
          continue
        }
        if (line.startsWith(':')) continue
        const colon = line.indexOf(':')
        const key = colon < 0 ? line : line.slice(0, colon)
        let value = colon < 0 ? '' : line.slice(colon + 1)
        if (value.startsWith(' ')) value = value.slice(1)
        if (key === 'data') data.push(value)
        else if (key === 'event') event = value || 'message'
        else if (key === 'id' && !value.includes('\0')) id = value
      }
      if (eventBytes + Buffer.byteLength(buffer, 'utf8') > maxEventBytes) {
        throw new Error('SSE event exceeded the byte limit')
      }
      if (chunk.done) break // Never synthesize a missing terminating blank line.
    }
  } finally {
    signal?.removeEventListener('abort', abort)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
