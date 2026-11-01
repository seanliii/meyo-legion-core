import { createServer } from 'node:http'
import { createHash } from 'node:crypto'

/** Protocol fixture only. It is deliberately not a Legion engine or model test. */
export async function fixture(t, options = {}) {
  const teams = new Map()
  const requests = []
  const bytes = Buffer.from(options.artifactBytes ?? 'fixture artifact, not a model-generated deliverable\n')
  const sha256 = createHash('sha256').update(bytes).digest('hex')
  let messageCount = 0
  let resumeCount = 0
  const server = createServer(async (req, res) => {
    let raw = ''
    for await (const chunk of req) raw += chunk
    const body = raw ? JSON.parse(raw) : {}
    const url = new URL(req.url, 'http://fixture.test')
    requests.push({ method: req.method, path: url.pathname, search: url.search, body, headers: req.headers })
    const prefix = options.prefix || ''
    const path = prefix && url.pathname.startsWith(`${prefix}/`)
      ? url.pathname.slice(prefix.length) : url.pathname
    const send = (value, status = 200) => {
      res.writeHead(status, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(value))
    }
    if (options.redirect) {
      res.writeHead(302, { Location: options.redirect }); res.end(); return
    }
    if (req.headers.authorization !== 'Bearer fixture-token') {
      send({ error: 'unauthorized', reason: 'Token was fixture-token' }, 401); return
    }
    if (prefix && !url.pathname.startsWith(`${prefix}/`)) {
      send({ error: 'outside service prefix' }, 404); return
    }
    const override = options.responses?.[path]
    if (override) { send(override.body, override.status); return }
    if (path === '/api/legion/dispatch') {
      teams.set(body.team_id, {
        team_id: body.team_id,
        conversation_id: body.session_id,
        tasks: { author: { status: 'running', result: null } },
        events: [{ event_id: 1, event: 'meta', data: { team_id: body.team_id, sid: body.session_id } }],
      })
      if (options.dropDispatch) { res.destroy(); return }
      if (options.dispatchStatus) {
        send({ error: 'Synthetic failure after accepting the request' }, options.dispatchStatus); return
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream' })
      const text = `id: 1\nevent: meta\ndata: ${JSON.stringify({ team_id: body.team_id, sid: body.session_id })}\n\n`
      for (const byte of Buffer.from(text)) res.write(Buffer.from([byte]))
      return // The client must detach; the server-side team survives.
    }
    if (path === '/api/legion/team') {
      const team = teams.get(url.searchParams.get('team_id'))
      send(team ? { ...team, events: undefined } : { error: 'team not found' }, team ? 200 : 404)
      return
    }
    if (path === '/api/legion/events') {
      const team = teams.get(url.searchParams.get('team_id'))
      if (!team) { send({ error: 'team not found' }, 404); return }
      const after = Number(url.searchParams.get('after') || 0)
      const rawEvents = team.events.filter(event => event.event_id > after).slice(0, 500)
      const events = rawEvents.filter(event => !options.hiddenEventIds?.has(event.event_id))
      send({ team_id: team.team_id, after, last_event_id: rawEvents.at(-1)?.event_id ?? after, events })
      return
    }
    if (path === '/api/legion/team/message') {
      messageCount += 1
      send({ id: `message-${messageCount}`, recipient: body.recipient, text: body.text }, 201); return
    }
    if (path === '/api/legion/team/resume') {
      resumeCount += 1
      send({ ok: true, status: 'unchanged', reason: 'no recoverable tasks', team_id: body.team_id }); return
    }
    if (path === '/api/legion/cancel') {
      send({ ok: true, team_id: body.team_id, cancelled: true, reason: 'cancellation requested' }); return
    }
    if (path === (options.artifactPath || '/outputs/result.txt')) {
      const preview = options.previewArtifact && url.searchParams.get('download') !== '1'
      res.writeHead(200, { 'Content-Type': options.previewArtifact ? 'text/html' : 'text/plain' })
      res.end(options.badArtifact ? 'wrong bytes'
        : preview ? Buffer.concat([bytes, Buffer.from('<script>/* preview only */</script>')]) : bytes)
      return
    }
    send({ error: 'not found' }, 404)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => {
    server.closeAllConnections()
    server.close(resolve)
  }))
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}${options.prefix || ''}`,
    teams,
    requests,
    bytes,
    sha256,
    finish(teamId, artifacts = []) {
      const team = teams.get(teamId)
      const result = { status: 'done', final: 'Fixture completed.', artifacts }
      team.tasks.author = { status: 'done', result }
      team.events.push({ event_id: team.events.length + 1, event: 'final', data: {
        status: 'done', final: 'Fixture completed.', artifacts,
        results: [{ key: 'author', ...result }],
      } })
    },
    get messageCount() { return messageCount },
    get resumeCount() { return resumeCount },
  }
}
