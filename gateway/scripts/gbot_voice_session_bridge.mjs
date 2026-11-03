#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 Joseph Zhou
// SPDX-License-Identifier: MIT
/** Local validation bridge for Grok Bot 0.71.2's existing macOS login.
 * Never persists credentials or refreshes/modifies the client's account state.
 * Internal APIs are version dependent; expired sessions must be renewed in the app.
 */
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { pbkdf2Sync, createDecipheriv, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createServer } from 'node:http';

const BACKEND = 'https://api2.cursor.sh/aiserver.v1.GrokBotService/';
export const RPC_ROUTES = Object.freeze({
  'GET /credential': 'MintSandVoiceCallSecret',
  'POST /session': 'VoiceCallHarnessSession',
  'POST /tool': 'VoiceCallHarnessTool',
});

// Local patch: cache Safe Storage password so gateway retries don't re-hit Keychain UI.
let cachedSafeStoragePassword;
function readSafeStoragePassword() {
  if (cachedSafeStoragePassword) return cachedSafeStoragePassword;
  cachedSafeStoragePassword = execFileSync('/usr/bin/security', ['find-generic-password', '-w', '-s', 'Grok Bot Safe Storage'], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 10000,
  }).trimEnd();
  return cachedSafeStoragePassword;
}

export function loadAccount() {
  const data = JSON.parse(readFileSync(join(homedir(), 'Library/Application Support/Grok Bot/sand-secrets.json'), 'utf8'));
  const accounts = JSON.parse(data['cursor-accounts']);
  const active = accounts.accounts[accounts.active];
  if (!active) throw new Error('login_unavailable');
  const password = readSafeStoragePassword();
  const key = pbkdf2Sync(password, 'saltysalt', 1003, 16, 'sha1');
  const decrypt = (value) => {
    const blob = Buffer.from(value, 'base64');
    if (blob.subarray(0, 3).toString() !== 'v10') throw new Error('unsupported_storage');
    const cipher = createDecipheriv('aes-128-cbc', key, Buffer.alloc(16, 32));
    return Buffer.concat([cipher.update(blob.subarray(3)), cipher.final()]).toString('utf8');
  };
  const token = decrypt(active['cursor-access-token']);
  const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
  if (!Number.isFinite(claims.exp) || claims.exp * 1000 < Date.now() + 30000) throw new Error('login_expired_open_grok_bot');
  const machineId = decrypt(data['cursor-machine-id']);
  const teamId = active['cursor-selected-team-id'] ? decrypt(active['cursor-selected-team-id']) : undefined;
  return { token, machineId, teamId, authId: claims.sub };
}

function checksum(machineId) {
  const t = Math.floor(Date.now() / 1e6);
  const bytes = Buffer.from([t >> 40 & 255, t >> 32 & 255, t >> 24 & 255, t >> 16 & 255, t >> 8 & 255, t & 255]);
  let previous = 165;
  for (let i = 0; i < bytes.length; i++) { bytes[i] = (bytes[i] ^ previous) + i % 256; previous = bytes[i]; }
  return bytes.toString('base64url') + machineId;
}

async function accountRequest(method, body, { accountLoader = loadAccount, fetcher = fetch, signal, streaming = false } = {}) {
  if (!Object.values(RPC_ROUTES).includes(method) && method !== 'EnsureSandBox' && method !== 'WatchGrokBotTranscripts') throw new Error('rpc_forbidden');
  if (method === 'EnsureSandBox' && ![JSON.stringify({wake: false, supportsAsleepBox: true}), '{}'].includes(JSON.stringify(body))) throw new Error('rpc_forbidden');
  if (method === 'WatchGrokBotTranscripts' && (!streaming || body.includeUnlistedAgents !== false || body.inlineBodyMaxBytes !== 65536 || body.cursors?.length !== 1 || typeof body.cursors[0].agentId !== 'string' || Object.keys(body.cursors[0]).some(k => !['agentId', 'generation', 'afterUpdatedSeq'].includes(k)) || body.cursors[0].generation !== 0 || body.cursors[0].afterUpdatedSeq !== '0')) throw new Error('rpc_forbidden');
  const account = accountLoader();
  const headers = {
    authorization: `Bearer ${account.token}`, 'content-type': 'application/json',
    'connect-protocol-version': '1', 'x-cursor-client-type': 'sand',
    'x-cursor-client-version': '0.71.2', 'x-cursor-client-source': 'sand-desktop',
    'x-cursor-client-os': 'darwin', 'x-sand-box-namespace': 'prod',
    'x-ghost-mode': 'true', 'x-cursor-checksum': checksum(account.machineId),
    'x-request-id': randomUUID(), 'x-cursor-timezone': Intl.DateTimeFormat().resolvedOptions().timeZone,
  };
  if (account.teamId) headers['x-cursor-team-id'] = account.teamId;
  let payload = Buffer.from(JSON.stringify(body));
  if (streaming) {
    headers['content-type'] = 'application/connect+json';
    const prefix = Buffer.alloc(5); prefix.writeUInt32BE(payload.length, 1);
    payload = Buffer.concat([prefix, payload]);
  }
  const response = await fetcher(BACKEND + method, { method: 'POST', headers,
    body: payload, redirect: 'error', signal: signal ?? AbortSignal.timeout(20000) });
  if (!response.ok) {
    // Upstream bodies can contain user content or credentials; never expose them.
    throw new Error(`upstream_http_${response.status}`);
  }
  return response;
}

export async function accountRpc(method, body, options = {}) {
  if (method === 'WatchGrokBotTranscripts') throw new Error('rpc_forbidden');
  return (await accountRequest(method, body, options)).json();
}

export function validateSandboxUrl(raw) {
  const url = new URL(raw);
  const host = url.hostname.toLowerCase();
  const allowed = host === 'cursor.sh' || host === 'cursor.com' || host.endsWith('.cursor.sh') || host.endsWith('.cursor.com') || host.endsWith('.cursorvm.com');
  if (url.protocol !== 'https:' || !allowed || url.username || url.password || url.search || url.hash) throw new Error('sandbox_url_forbidden');
  return url.toString().replace(/\/$/, '');
}

// Request-shape errors (400/409/422...) are not fixed by a new sandbox connection.
function isStaleSandboxStatus(status) {
  return [401, 403, 404, 408, 410, 421].includes(status) || status >= 500;
}

export class NativeSandboxClient {
  #connection;
  #loading;
  constructor({rpc = accountRpc, fetcher = fetch, accountLoader = loadAccount, connectMode = 'locate'} = {}) {
    if (!['locate', 'normal'].includes(connectMode)) throw new Error('invalid_connect_mode');
    this.connectMode = connectMode;
    this.rpc = rpc; this.fetcher = fetcher; this.accountLoader = accountLoader;
  }
  async #connect() {
    if (this.#connection) return this.#connection;
    if (!this.#loading) this.#loading = (async () => {
      const account = this.accountLoader();
      const raw = await this.rpc('EnsureSandBox', this.connectMode === 'normal' ? {} : {wake: false, supportsAsleepBox: true}, {accountLoader: () => account, fetcher: this.fetcher});
      if (typeof raw.gatewayToken !== 'string' || !raw.gatewayToken) throw new Error('sandbox_connection_unavailable');
      const headers = {authorization: `Bearer ${raw.gatewayToken}`};
      const network = raw.networkToken ?? raw.gatewayHeaders?.['x-anyrun-network-token'];
      if (typeof network === 'string' && network) headers['x-anyrun-network-token'] = network;
      this.#connection = {baseUrl: validateSandboxUrl(raw.gatewayUrl), headers, authId: account.authId, responseFields: Object.keys(raw).sort()};
      return this.#connection;
    })().finally(() => { this.#loading = undefined; });
    return this.#loading;
  }
  #invalidate(connection) {
    if (this.#connection === connection) this.#connection = undefined;
  }
  // The sandbox gatewayUrl/token can go stale (pod replaced overnight) while the
  // account login stays valid. Drop the cached connection on stale-looking
  // failures and re-run EnsureSandBox once per request; never loop.
  async #fetch(path, options = {}, accept = response => response.ok) {
    for (let attempt = 0; ; attempt++) {
      const connection = await this.#connect();
      let response;
      try {
        response = await this.fetcher(connection.baseUrl + path, {...options, redirect: 'error', headers: {...connection.headers, ...options.headers}});
      } catch (error) {
        const signal = options.signal;
        if (signal?.aborted && signal.reason?.name !== 'TimeoutError') throw error;
        this.#invalidate(connection);
        if (attempt > 0 || signal?.aborted) throw error;
        continue;
      }
      if (accept(response)) return response;
      if (!response.ok && !isStaleSandboxStatus(response.status)) return response;
      this.#invalidate(connection);
      if (attempt > 0 || options.signal?.aborted) return response;
      await response.body?.cancel();
    }
  }
  async openEvents(signal) {
    const deadline = new AbortController();
    const timer = setTimeout(() => deadline.abort(new DOMException('native_events_timeout', 'TimeoutError')), 20000);
    const isEventStream = response => response.ok && response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream') === true;
    try {
      const response = await this.#fetch('/events', {headers: {accept: 'text/event-stream'}, signal: signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal}, isEventStream);
      if (!isEventStream(response)) {
        await response.body?.cancel(); throw new Error(`upstream_http_${response.status}`);
      }
      return response;
    } finally { clearTimeout(timer); }
  }
  async probe() {
    const health = await this.#fetch('/health', {signal: AbortSignal.timeout(20000)});
    const healthStatus = health.status;
    await health.body?.cancel();
    if (!health.ok) throw new Error(`upstream_http_${healthStatus}`);
    const events = await this.openEvents();
    const connection = await this.#connect();
    const result = {ok: true, responseFields: connection.responseFields, https: true, hostnameAllowed: true, gatewayTokenPresent: true, networkHeaderPresent: Boolean(connection.headers['x-anyrun-network-token']), healthStatus, eventsStatus: events.status, eventStreamContentTypeOk: true};
    // Handshake only. Do not parse/read the initial snapshot or any messages.
    await events.body?.cancel();
    return result;
  }
  async presence({agentId, callId, isOnTheLine, acceptsOverheard}) {
    const response = await this.#fetch('/api/setVoiceCallPresence', {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({id: agentId, callId, isOnTheLine, acceptsOverheard}), signal: AbortSignal.timeout(20000),
    });
    await response.body?.cancel();
    if (!response.ok) throw new Error(`upstream_http_${response.status}`);
  }
  async callerAuthId() { return (await this.#connect()).authId; }
}

export class NativeTranscriptClient {
  constructor({accountLoader = loadAccount, fetcher = fetch} = {}) { this.accountLoader = accountLoader; this.fetcher = fetcher; }
  async probe(agentId) {
    const response = await accountRequest('WatchGrokBotTranscripts', {cursors: [{agentId, generation: 0, afterUpdatedSeq: '0'}], includeUnlistedAgents: false, inlineBodyMaxBytes: 65536}, {accountLoader: this.accountLoader, fetcher: this.fetcher, signal: AbortSignal.timeout(20000), streaming: true});
    const ok = response.headers.get('content-type')?.startsWith('application/connect+json') === true;
    await response.body?.cancel();
    return {ok, status: response.status, connectJson: ok};
  }
  async *events(context, signal, onOpen = () => {}) {
    // Empty session is the native owner slot. Non-owner sessions require a
    // separately verified metadata binding and are deliberately unsupported.
    if (context.sessionId) throw new Error('session_binding_unavailable');
    const response = await accountRequest('WatchGrokBotTranscripts', {cursors: [{agentId: context.agentId, generation: 0, afterUpdatedSeq: '0'}], includeUnlistedAgents: false, inlineBodyMaxBytes: 65536}, {accountLoader: this.accountLoader, fetcher: this.fetcher, signal, streaming: true});
    if (!response.headers.get('content-type')?.startsWith('application/connect+json')) { await response.body?.cancel(); throw new Error('native_stream_format'); }
    onOpen(response.status);
    let pending = Buffer.alloc(0);
    const state = {connected: false, sinceMs: context.sinceMs, generation: null};
    try {
      for await (const bytes of response.body) {
        pending = Buffer.concat([pending, bytes]);
        if (pending.length > 2 * 1024 * 1024) throw new Error('event_too_large');
        while (pending.length >= 5) {
          const size = pending.readUInt32BE(1), flags = pending[0];
          if (size > 1024 * 1024) throw new Error('event_too_large');
          if (pending.length < size + 5) break;
          const data = pending.subarray(5, size + 5); pending = pending.subarray(size + 5);
          if (flags === 2) return; // End-stream envelope; never expose its text.
          if (flags !== 0) throw new Error('native_stream_format');
          const event = filterTranscriptFrame(JSON.parse(data.toString('utf8')), context, state);
          for (const item of event) yield item;
        }
      }
    } finally { await response.body?.cancel().catch(() => {}); }
  }
}

export function filterTranscriptFrame(frame, context, state) {
  if (frame.connected) {
    const at = Number(frame.connected.serverTimeMs);
    state.connected = Number.isFinite(at); state.sinceMs = Math.max(context.sinceMs, at); state.generation = null;
    return [];
  }
  const reset = frame.cleared ?? frame.cursorTooOld;
  if (reset && reset.agentId === context.agentId && !reset.sessionId) {
    state.connected = false; state.generation = null;
    // Fail closed: caller reconnects and establishes a new server-time baseline.
    throw new Error('transcript_rebaseline_required');
  }
  const rows = frame.rows;
  if (!state.connected || !rows || rows.agentId !== context.agentId || (rows.sessionId || '') !== context.sessionId || rows.replay === true || !Number.isSafeInteger(rows.generation)) return [];
  if (state.generation !== null && rows.generation !== state.generation) throw new Error('transcript_rebaseline_required');
  state.generation = rows.generation;
  const result = [];
  for (const row of rows.entries ?? []) {
    // No blob reads, omitted-body reads or history snapshots are performed.
    if (typeof row.body !== 'string' || row.bodyOmitted) continue;
    let entry;
    try { entry = JSON.parse(Buffer.from(row.body, 'base64').toString('utf8')); } catch { continue; }
    const event = filterNativeEvent({channel: 'transcript', payload: {type: 'appended', entry, ordered: {replicaKey: 'transcript:' + context.agentId, sequence: 0}}}, {...context, sinceMs: state.sinceMs});
    if (event) result.push(event);
  }
  return result;
}

export function filterNativeEvent(event, context) {
  const p = event?.payload;
  if (!p || typeof p !== 'object') return null;
  if (event.channel === 'voice-call-nudge') {
    if (p.agentId !== context.agentId || p.callId !== context.callId || !['outcome', 'overheard'].includes(p.kind) || typeof p.update !== 'string' || !p.update.trim()) return null;
    if (p.kind === 'overheard' && !context.acceptsOverheard) return null;
    // Equal text is not an event identity: separate tasks can return exactly
    // the same result. Only an explicit upstream event ID can identify replay.
    const eventId = typeof event.eventId === 'string' && event.eventId.length > 0 && event.eventId.length <= 256
      ? event.eventId : undefined;
    if (eventId !== undefined) {
      const key = 'nudge-id:' + eventId;
      if (context.seen.has(key)) return null;
      context.seen.add(key);
    }
    return {channel: 'voice-call-nudge', payload: {agentId: p.agentId, callId: p.callId, kind: p.kind, update: p.update.slice(0, 16000)}};
  }
  if (event.channel !== 'transcript' || p.type !== 'appended') return null;
  // Ordered replica keys are the authoritative agent/session scope in 0.71.2.
  const replica = p.ordered?.replicaKey;
  const expected = 'transcript:' + context.agentId + (context.sessionId ? '\0' + context.sessionId : '');
  if (replica !== expected || !Number.isSafeInteger(p.ordered?.sequence)) return null;
  const entry = p.entry;
  if (!entry || entry.kind !== 'message' || entry.role !== 'user' || typeof entry.id !== 'string' || !entry.id || typeof entry.content !== 'string') return null;
  if ('channel' in entry || 'fromAgent' in entry || 'channelSender' in entry || 'fromUser' in entry || entry.author?.kind !== 'cursor_user' || entry.author.id !== context.authId) return null;
  // No baseline/snapshot is ever forwarded. Timestamp bounds also reject replay
  // of old appended frames after a transport reconnect; missing time is closed.
  if (!Number.isFinite(entry.timestampMs) || entry.timestampMs < context.sinceMs || !entry.content.trim()) return null;
  const key = 'entry:' + entry.id;
  if (context.seen.has(key)) return null;
  context.seen.add(key);
  return {channel: 'typed-message', payload: {agentId: context.agentId, callId: context.callId, entryId: entry.id, text: entry.content.slice(0, 16000), atMs: entry.timestampMs}};
}

export function validateBody(route, body) {
  if (route === 'GET /credential') return {};
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('invalid_body');
  const fields = route === 'POST /session'
    ? ['agentId', 'agentName', 'userName', 'description', 'spokenLanguage']
    : ['agentId', 'callId', 'name', 'input', 'line'];
  if (Object.keys(body).some(k => !fields.includes(k))) throw new Error('invalid_body');
  if (typeof body.agentId !== 'string' || !body.agentId || body.agentId.length > 200) throw new Error('invalid_agent');
  if (route === 'POST /session') {
    if (typeof body.agentName !== 'string') throw new Error('invalid_name');
    for (const k of fields.slice(2)) if (body[k] !== undefined && typeof body[k] !== 'string') throw new Error('invalid_body');
  } else {
    if (typeof body.callId !== 'string' || !body.callId || typeof body.name !== 'string' || !body.name) throw new Error('invalid_tool');
    const lineKeys = ['theyJustTalked', 'newsOwed', 'receiptOwed', 'alreadySpokeThisTurn'];
    if (!body.line || Object.keys(body.line).some(k => !lineKeys.includes(k)) || lineKeys.some(k => typeof body.line[k] !== 'boolean')) throw new Error('invalid_line');
  }
  return body;
}

function streamFailure(error) {
  const status = /^upstream_http_(\d{3})$/.exec(error?.message ?? '');
  const name = ['Error', 'TypeError', 'AbortError', 'TimeoutError'].includes(error?.name) ? error.name : 'other';
  return {http_status: status ? Number(status[1]) : null, error_class: name};
}

function waitForNativeRetry(milliseconds, signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    signal.addEventListener('abort', finish, {once: true});
  });
}

export function createBridge({ port = 18771, rpc = accountRpc, agentId = process.env.STACKCHAN_GBOT_VOICE_BOT_ID || process.env.STACKCHAN_GBOT_VOICE_AGENT_ID || process.env.STACKCHAN_TOOL_BOT_ID, toolsEnabled = process.env.STACKCHAN_GBOT_VOICE_TOOLS === '1', desktopEnabled = Boolean(process.env.STACKCHAN_TOOL_BOT?.trim()) || process.env.STACKCHAN_GBOT_VOICE_DESKTOP === '1', sandbox = new NativeSandboxClient({connectMode: 'normal'}), transcript = new NativeTranscriptClient(), nativeRetryDelayMs = 5000, nativeLog = event => console.log(JSON.stringify({event: 'gbot_native_stream', ...event})), sessionId = process.env.STACKCHAN_GBOT_VOICE_SESSION_ID || '' } = {}) {
  if (!Number.isFinite(nativeRetryDelayMs) || nativeRetryDelayMs < 10 || nativeRetryDelayMs > 30000) throw new Error('invalid_native_retry_delay');
  const logStream = (source, state, extra = {}) => nativeLog({source, state, ...extra});
  let activeCall = null;
  let presenceBusy = false;
  const streams = new Set();
  return createServer(async (req, res) => {
    const send = (status, value) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' }); res.end(JSON.stringify(value)); };
    const address = req.socket.remoteAddress;
    if (address !== '127.0.0.1' || req.headers.origin !== undefined || req.headers['sec-fetch-site'] && req.headers['sec-fetch-site'] !== 'none' || req.headers.host !== `127.0.0.1:${port}`) return send(403, { ok: false, error: 'local_client_only' });
    const route = `${req.method} ${req.url}`;
    if (route === 'GET /health') return send(200, { ok: true, source: 'grok-bot-existing-login', clientVersion: '0.71.2', agentId: agentId || null, toolsEnabled, desktopEnabled });
    if (route === 'GET /native/health') {
      if (!desktopEnabled && !toolsEnabled) return send(403, {ok: false, error: 'desktop_disabled'});
      try { return send(200, await sandbox.probe()); }
      catch { return send(502, {ok: false, error: 'native_handshake_failed'}); }
    }
    const eventUrl = new URL(req.url, 'http://127.0.0.1');
    if (req.method === 'GET' && eventUrl.pathname === '/native/events') {
      if (!desktopEnabled && !toolsEnabled) return send(403, {ok: false, error: 'desktop_disabled'});
      if ([...eventUrl.searchParams.keys()].some(k => !['agentId', 'callId'].includes(k)) || !activeCall || eventUrl.searchParams.get('agentId') !== agentId || eventUrl.searchParams.get('callId') !== activeCall.callId) return send(403, {ok: false, error: 'call_not_allowed'});
      const context = activeCall, controller = new AbortController();
      streams.add(controller); res.on('close', () => controller.abort());
      let typedTask;
      try {
        const upstream = await sandbox.openEvents(controller.signal);
        logStream('box', 'open', {http_status: upstream.status, error_class: null});
        res.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
        res.write(': connected\n\n');
        typedTask = (async () => {
          while (activeCall === context && !controller.signal.aborted) {
            try {
              for await (const event of transcript.events(context, controller.signal, status => logStream('typed', 'open', {http_status: status, error_class: null}))) {
                if (activeCall !== context || controller.signal.aborted) break;
                if (!res.write('data: ' + JSON.stringify(event) + '\n\n')) throw new Error('slow_consumer');
              }
              if (!controller.signal.aborted) logStream('typed', 'eof', {http_status: null, error_class: null});
            } catch (error) {
              if (controller.signal.aborted) break;
              if (error?.message === 'slow_consumer') {
                logStream('downstream', 'slow_consumer', {http_status: null, error_class: 'Error'});
                controller.abort();
                break;
              }
              logStream('typed', 'error', streamFailure(error));
            }
            await waitForNativeRetry(nativeRetryDelayMs, controller.signal);
          }
        })();
        let pending = '';
        const decoder = new TextDecoder();
        for await (const bytes of upstream.body) {
          if (activeCall !== context) break;
          pending += decoder.decode(bytes, {stream: true}).replace(/\r\n/g, '\n');
          if (pending.length > 2 * 1024 * 1024) throw new Error('event_too_large');
          let end;
          while ((end = pending.indexOf('\n\n')) !== -1) {
            const block = pending.slice(0, end); pending = pending.slice(end + 2);
            const data = block.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('\n');
            if (!data) {
              // Preserve upstream heartbeat liveness without forwarding contents.
              if (block.split('\n').some(line => line.startsWith(':')) && !res.write(': upstream\n\n')) throw new Error('slow_consumer');
              continue;
            }
            let event;
            try {
              const parsed = JSON.parse(data);
              const id = block.split('\n').find(line => line.startsWith('id:'))?.slice(3).trim();
              event = filterNativeEvent(id ? {...parsed, eventId: id} : parsed, context);
            } catch { continue; }
            if (event?.channel === 'typed-message') event = null;
            if (event && !res.write('data: ' + JSON.stringify(event) + '\n\n')) throw new Error('slow_consumer');
            if (context.seen.size > 2000) context.seen.delete(context.seen.values().next().value);
          }
        }
        if (!controller.signal.aborted) logStream('box', 'eof', {http_status: null, error_class: null});
      } catch (error) {
        if (!controller.signal.aborted) logStream('box', 'error', streamFailure(error));
        if (!res.headersSent) send(502, {ok: false, error: 'native_stream_failed'});
      } finally {
        controller.abort();
        await typedTask;
        streams.delete(controller);
        res.end();
      }
      return;
    }
    const nativePresence = route === 'POST /native/presence';
    const method = nativePresence ? 'setVoiceCallPresence' : RPC_ROUTES[route];
    if (!method) return send(404, { ok: false, error: 'route_not_found' });
    if ((route === 'POST /tool' || nativePresence) && !toolsEnabled && !desktopEnabled) return send(403, { ok: false, error: 'tools_disabled' });
    try {
      let raw = '';
      for await (const chunk of req) { raw += chunk; if (Buffer.byteLength(raw) > 65536) return send(413, { ok: false, error: 'body_too_large' }); }
      if (req.method === 'POST' && req.headers['content-type']?.split(';')[0] !== 'application/json') return send(415, { ok: false, error: 'json_required' });
      let body;
      try {
        const parsed = raw ? JSON.parse(raw) : {};
        if (nativePresence) {
          if (Object.keys(parsed).some(k => !['agentId', 'callId', 'isOnTheLine', 'acceptsOverheard', 'sessionId'].includes(k)) || typeof parsed.agentId !== 'string' || typeof parsed.callId !== 'string' || !/^[A-Za-z0-9_-]{1,120}$/.test(parsed.callId) || typeof parsed.isOnTheLine !== 'boolean' || typeof parsed.acceptsOverheard !== 'boolean' || (parsed.sessionId !== undefined && parsed.sessionId !== sessionId)) throw new Error('invalid_body');
          body = parsed;
        } else body = validateBody(route, parsed);
      } catch { return send(400, { ok: false, error: 'invalid_body' }); }
      if (req.method === 'POST' && (!agentId || body.agentId !== agentId)) return send(403, { ok: false, error: 'agent_not_allowed' });
      if (route === 'POST /tool') {
        if (!toolsEnabled && !['send_task', 'search_conversations'].includes(body.name)) return send(403, {ok: false, error: 'tools_disabled'});
        if (!activeCall || body.callId !== activeCall.callId) return send(403, {ok: false, error: 'call_not_allowed'});
      }
      if (nativePresence) {
        if (presenceBusy) return send(409, {ok: false, error: 'presence_busy'});
        if (!activeCall && !body.isOnTheLine) return send(403, {ok: false, error: 'call_not_allowed'});
        // Gateway hard-restarts often skip presence(false), leaving a stale activeCall.
        // Localhost event streams can also linger half-open after the gateway dies, so
        // streams.size>0 is not proof another live owner exists. Force-takeover on a new
        // on-the-line callId: abort leftover consumers, then claim the line.
        if (activeCall && body.callId !== activeCall.callId) {
          if (!body.isOnTheLine) return send(409, {ok: false, error: 'call_conflict'});
          for (const controller of [...streams]) controller.abort();
          streams.clear();
          activeCall = null;
        }
        presenceBusy = true;
        try {
        const authId = await sandbox.callerAuthId();
        await sandbox.presence(body);
        if (body.isOnTheLine) {
          if (!activeCall) activeCall = {agentId, callId: body.callId, acceptsOverheard: body.acceptsOverheard, sessionId, authId, sinceMs: Date.now(), seen: new Set()};
          else activeCall.acceptsOverheard = body.acceptsOverheard;
        } else {
          activeCall = null; for (const controller of streams) controller.abort();
        }
        return send(200, {ok: true});
        } finally { presenceBusy = false; }
      }
      const result = await rpc(method, body);
      if (route === 'GET /credential') return send(200, { ok: true, credential: {
        clientSecret: result.clientSecret, expiresAtUnixSeconds: Number(result.expiresAtUnixSeconds),
        model: result.model, websocketUrl: result.websocketUrl,
      } });
      if (route === 'POST /tool') {
        const kinds = { VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_SERVED: 'served', VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_UNKNOWN_TOOL: 'unknown_tool', VOICE_CALL_HARNESS_TOOL_OUTCOME_KIND_INVALID_INPUT: 'invalid_input', 1: 'served', 2: 'unknown_tool', 3: 'invalid_input' };
        return send(200, { ...result, kind: kinds[result.kind] ?? result.kind });
      }
      return send(200, result);
    } catch (error) {
      const safe = /^(upstream_http_\d{3}|login_unavailable|login_expired_open_grok_bot|unsupported_storage)$/.test(error.message) ? error.message : 'bridge_request_failed';
      return send(502, { ok: false, error: safe });
    }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const port = Number(process.env.GBOT_VOICE_BRIDGE_PORT || 18771);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('invalid_port');
  const server = createBridge({ port });
  server.requestTimeout = 25000;
  server.listen(port, '127.0.0.1', () => console.log(`Grok Bot voice session bridge listening on 127.0.0.1:${port}`));
}
