// SPDX-FileCopyrightText: 2026 Joseph Zhou
// SPDX-License-Identifier: MIT
import test from 'node:test';
import assert from 'node:assert/strict';
import { createBridge, accountRpc, validateBody } from './gbot_voice_session_bridge.mjs';
import { createServer } from 'node:net';
import { request } from 'node:http';

async function freePort() {
  const s = createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r));
  const port = s.address().port; await new Promise(r => s.close(r)); return port;
}

test('loopback bridge rejects cross origin, wrong host, unknown RPC without consulting login', async () => {
  let calls = 0; const port = await freePort();
  const server = createBridge({port, agentId: 'a', rpc: async (method, body) => { calls++; return { instructions: 'test', method, body }; }});
  await new Promise(r => server.listen(port, '127.0.0.1', r));
  try {
    const url = `http://127.0.0.1:${port}`;
    assert.equal((await fetch(url + '/credential', {headers:{Origin:'https://example.com'}})).status, 403);
    const wrongHostStatus = await new Promise((resolve, reject) => { const req = request(url + '/credential', {headers:{Host:`localhost:${port}`}}, res => {res.resume(); resolve(res.statusCode);}); req.on('error', reject); req.end(); });
    assert.equal(wrongHostStatus, 403);
    assert.equal((await fetch(url + '/api/sendPrompt', {method:'POST'})).status, 404);
    assert.equal((await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"a","agentName":"b","rpc":"DeleteAgent"}'})).status, 400);
    assert.equal((await fetch(url + '/tool', {method:'POST'})).status, 403);
    assert.equal((await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"other","agentName":"b"}'})).status, 403);
    assert.equal(calls, 0);
    const response = await fetch(url + '/session', {method:'POST',headers:{'Content-Type':'application/json'},body:'{"agentId":"a","agentName":"b"}'});
    assert.equal(response.status, 200); assert.equal((await response.json()).method, 'VoiceCallHarnessSession'); assert.equal(calls, 1);
  } finally { await new Promise(r => server.close(r)); }
});

test('account RPC admits only fixed methods and never leaks upstream response body', async () => {
  let loads = 0; const accountLoader = () => {loads++; return {token:'FAKE_TEST',machineId:'FAKE_MACHINE'};};
  await assert.rejects(accountRpc('SendPrompt', {}, {accountLoader}), /rpc_forbidden/); assert.equal(loads, 0);
  const fetcher = async (url, options) => {
    assert.equal(url, 'https://api2.cursor.sh/aiserver.v1.GrokBotService/MintSandVoiceCallSecret');
    assert.equal(options.redirect, 'error'); return {ok:false,status:403,json:async()=>({token:'DO_NOT_LEAK'})};
  };
  await assert.rejects(accountRpc('MintSandVoiceCallSecret', {}, {accountLoader,fetcher}), /^Error: upstream_http_403$/);
});

test('tool invocation requires complete bounded line state', () => {
  assert.throws(() => validateBody('POST /tool', {agentId:'a',callId:'c',name:'x',input:{},line:{}}), /invalid_line/);
});

test('native bootstrap uses fixed read-only request and cancels bodies without reading snapshots', async () => {
  const {NativeSandboxClient, validateSandboxUrl} = await import('./gbot_voice_session_bridge.mjs');
  for (const url of ['http://a.cursor.sh', 'https://cursor.sh.evil.test', 'https://u@a.cursor.sh', 'https://a.cursor.sh/?token=x']) assert.throws(() => validateSandboxUrl(url));
  let cancelled = 0; const paths = [];
  const client = new NativeSandboxClient({accountLoader:()=>({authId:'owner'}), rpc:async(method,body)=>{
    assert.equal(method,'EnsureSandBox'); assert.deepEqual(body,{wake:false,supportsAsleepBox:true});
    return {gatewayUrl:'https://pod.cursorvm.com',gatewayToken:'FAKE',networkToken:'FAKE_NETWORK'};
  },fetcher:async(url,options)=>{
    paths.push(new URL(url).pathname); assert.equal(options.redirect,'error');
    return {ok:true,status:200,headers:new Headers({'content-type':'text/event-stream'}),body:{cancel:async()=>{cancelled++;}},json:()=>assert.fail('must not read body')};
  }});
  const result = await client.probe(); assert.equal(result.ok,true); assert.deepEqual(paths,['/health','/events']); assert.equal(cancelled,2);
  assert.equal(JSON.stringify(result).includes('FAKE'),false);
});

test('native event filtering excludes history, other callers, sessions and duplicate entries', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',sessionId:'',authId:'owner',sinceMs:100,seen:new Set()};
  const entry = {id:'e',kind:'message',role:'user',content:'FAKE TEXT',timestampMs:101,author:{kind:'cursor_user',id:'owner'}};
  const event = {channel:'transcript',payload:{type:'appended',entry,ordered:{replicaKey:'transcript:a',sequence:1}}};
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,type:'snapshot'}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,entry:{...entry,author:{kind:'cursor_user',id:'other'}}}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,entry:{...entry,timestampMs:99}}},context),null);
  assert.equal(filterNativeEvent({...event,payload:{...event.payload,ordered:{replicaKey:'transcript:a\0other',sequence:1}}},context),null);
  assert.equal(filterNativeEvent(event,context).channel,'typed-message'); assert.equal(filterNativeEvent(event,context),null);
  assert.equal(filterNativeEvent({channel:'voice-call-nudge',payload:{agentId:'a',callId:'other',kind:'outcome',update:'fake'}},context),null);
});


test('stale activeCall can be taken over after gateway restart even with leftover event streams', async () => {
  const port = await freePort(); let presenceCalls=0;
  const sandbox={
    callerAuthId:async()=> 'owner',
    presence:async()=>{presenceCalls++;},
    openEvents: async signal => new Response(new ReadableStream({start(controller) {
      signal.addEventListener('abort', () => controller.close(), {once:true});
    }}), {headers:{'content-type':'text/event-stream'}}),
  };
  const transcript = {async *events(_context, signal, onOpen) {
    onOpen(200);
    await new Promise(resolve => signal.addEventListener('abort', resolve, {once:true}));
  }};
  const server=createBridge({port,agentId:'a',desktopEnabled:true,toolsEnabled:false,sandbox,transcript});
  await new Promise(r=>server.listen(port,'127.0.0.1',r));
  const post=async(path,body)=>fetch(`http://127.0.0.1:${port}`+path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  let reader;
  try {
    assert.equal((await post('/native/presence',{agentId:'a',callId:'old-call',isOnTheLine:true,acceptsOverheard:false})).status,200);
    const response = await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=old-call`);
    reader = response.body.getReader();
    await reader.read();
    // Simulate gateway hard restart while the old SSE consumer is still half-open.
    assert.equal((await post('/native/presence',{agentId:'a',callId:'new-call',isOnTheLine:true,acceptsOverheard:false})).status,200);
    assert.equal(presenceCalls,2);
    assert.equal((await post('/native/presence',{agentId:'a',callId:'new-call',isOnTheLine:false,acceptsOverheard:false})).status,200);
  } finally {
    await reader?.cancel().catch(()=>{});
    server.closeAllConnections();
    await new Promise(r=>server.close(r));
  }
});

test('desktop opt-in admits native send and search scoped to active call while unknown tools remain off', async () => {
  const port = await freePort(); let toolCalls=0, presenceCalls=0;
  const sandbox={callerAuthId:async()=> 'owner', presence:async()=>{presenceCalls++;}};
  const server=createBridge({port,agentId:'a',desktopEnabled:true,toolsEnabled:false,sandbox,rpc:async()=>{toolCalls++;return {kind:1};}});
  await new Promise(r=>server.listen(port,'127.0.0.1',r));
  const post=async(path,body)=>fetch(`http://127.0.0.1:${port}`+path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const tool={agentId:'a',callId:'call-c',name:'send_task',input:{request:'FAKE'},line:{theyJustTalked:false,newsOwed:false,receiptOwed:false,alreadySpokeThisTurn:false}};
  try {
    assert.equal((await post('/tool',tool)).status,403);
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-c',isOnTheLine:true,acceptsOverheard:false})).status,200);
    assert.equal((await post('/tool',{...tool,name:'unknown_native_tool'})).status,403);
    assert.equal((await post('/tool',{...tool,name:'search_conversations',input:{query:'fixture'}})).status,200);
    assert.equal((await post('/tool',{...tool,callId:'other'})).status,403);
    assert.equal((await post('/tool',tool)).status,200); assert.equal(toolCalls,2);
    assert.equal((await post('/native/presence',{agentId:'a',callId:'call-c',isOnTheLine:false,acceptsOverheard:false})).status,200);
    assert.equal((await post('/tool',tool)).status,403); assert.equal(presenceCalls,2);
  } finally {await new Promise(r=>server.close(r));}
});

test('server transcript stream drops replay and pre-connection entries and requires rebaseline after clear', async()=>{
  const {filterTranscriptFrame,NativeTranscriptClient}=await import('./gbot_voice_session_bridge.mjs');
  const context={agentId:'a',callId:'call-c',sessionId:'',authId:'owner',sinceMs:100,seen:new Set()};
  const state={};
  assert.deepEqual(filterTranscriptFrame({connected:{serverTimeMs:'110'}},context,state),[]);
  const row=(time,id='e')=>({body:Buffer.from(JSON.stringify({id,kind:'message',role:'user',content:'FAKE',timestampMs:time,author:{kind:'cursor_user',id:'owner'}})).toString('base64')});
  const rows={agentId:'a',sessionId:'',generation:1,entries:[row(120)]};
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,replay:true}},context,state),[]);
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,entries:[row(109)]}},context,state),[]);
  assert.equal(filterTranscriptFrame({rows},context,state).length,1);
  assert.deepEqual(filterTranscriptFrame({rows},context,state),[]);
  assert.throws(()=>filterTranscriptFrame({cleared:{agentId:'a',newGeneration:2}},context,state),/rebaseline/);
  assert.deepEqual(filterTranscriptFrame({rows:{...rows,entries:[row(130,'e2')]}},context,state),[]);
  let fetched=false;
  const client=new NativeTranscriptClient({fetcher:async()=>{fetched=true;},accountLoader:()=>({})});
  await assert.rejects(async()=>{for await(const ignored of client.events({...context,sessionId:'unverified'})) {}},/session_binding/);
  assert.equal(fetched,false);
});

test('separate native results with identical text are both delivered without event IDs', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',seen:new Set()};
  const event = {channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'SAME FIXTURE RESULT'}};
  assert.equal(filterNativeEvent(event,context)?.channel,'voice-call-nudge');
  assert.equal(filterNativeEvent(event,context)?.channel,'voice-call-nudge');
});

test('native event IDs deduplicate replays without conflating separate same-text results', async () => {
  const {filterNativeEvent} = await import('./gbot_voice_session_bridge.mjs');
  const context = {agentId:'a',callId:'call-c',seen:new Set()};
  const event = {eventId:'event-1',channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'SAME FIXTURE RESULT'}};
  assert.ok(filterNativeEvent(event,context));
  assert.equal(filterNativeEvent(event,context),null);
  assert.ok(filterNativeEvent({...event,eventId:'event-2'},context));
});

test('HTTP: typed EOF or error reconnects independently and box results still arrive', async () => {
  for (const failure of ['eof', 'error']) {
    const port = await freePort();
    let box, attempts = 0, typedCancelled = false;
    const logs = [];
    const sandbox = {
      presence: async () => {}, callerAuthId: async () => 'owner',
      openEvents: async signal => new Response(new ReadableStream({start(controller) {
        box = controller;
        signal.addEventListener('abort', () => controller.close(), {once:true});
      }}), {headers:{'content-type':'text/event-stream'}}),
    };
    const transcript = {async *events(_context, signal, onOpen) {
      attempts++; onOpen(200);
      if (attempts === 1) {
        if (failure === 'error') throw new TypeError('SECRET_FIXTURE');
        return;
      }
      await new Promise(resolve => signal.addEventListener('abort', resolve, {once:true}));
      typedCancelled = true;
    }};
    const server = createBridge({port, agentId:'a', desktopEnabled:true, toolsEnabled:false, sandbox, transcript, nativeRetryDelayMs:10, nativeLog:event=>logs.push(event)});
    await new Promise(resolve=>server.listen(port,'127.0.0.1',resolve));
    let reader;
    try {
      await fetch(`http://127.0.0.1:${port}/native/presence`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({agentId:'a',callId:'call-c',isOnTheLine:true,acceptsOverheard:false})});
      const response=await fetch(`http://127.0.0.1:${port}/native/events?agentId=a&callId=call-c`);
      reader=response.body.getReader(); await reader.read();
      await new Promise(resolve=>setTimeout(resolve,30));
      assert.ok(attempts>=2);
      box.enqueue(new TextEncoder().encode('data: '+JSON.stringify({channel:'voice-call-nudge',payload:{agentId:'a',callId:'call-c',kind:'outcome',update:'FIXTURE RESULT'}})+'\n\n'));
      const result=await reader.read();
      assert.equal(result.done,false); assert.match(new TextDecoder().decode(result.value),/FIXTURE RESULT/);
      assert.ok(logs.some(event=>event.source==='typed'&&event.state===failure));
      assert.equal(JSON.stringify(logs).includes('SECRET_FIXTURE'),false);
      await reader.cancel(); reader=null;
      await new Promise(resolve=>setTimeout(resolve,20));
      assert.equal(typedCancelled,true);
    } finally {await reader?.cancel();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
  }
});

function fakeSandboxPods(handlers) {
  let ensures = 0; const hits = [];
  const rpc = async method => {
    assert.equal(method, 'EnsureSandBox');
    ensures++;
    return {gatewayUrl: `https://pod${ensures}.cursorvm.com`, gatewayToken: 'FAKE_TOKEN'};
  };
  const fetcher = async (url, options) => {
    const {hostname, pathname} = new URL(url);
    hits.push(hostname.split('.')[0] + pathname);
    return handlers(hostname.split('.')[0], pathname, options);
  };
  return {rpc, fetcher, hits, get ensures() { return ensures; }};
}
const fakeResponse = (status, type = 'application/json') => ({ok: status >= 200 && status < 300, status, headers: new Headers({'content-type': type}), body: {cancel: async () => {}}});

test('stale cached sandbox connection reconnects once and presence/probe recover without restart', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  let pod1Alive = true;
  const pods = fakeSandboxPods((pod, path) => {
    if (pod === 'pod1' && !pod1Alive) return fakeResponse(502);
    return fakeResponse(200, path === '/events' ? 'text/event-stream' : 'application/json');
  });
  const client = new NativeSandboxClient({accountLoader: () => ({authId: 'owner'}), rpc: pods.rpc, fetcher: pods.fetcher, connectMode: 'normal'});
  const presence = {agentId: 'a', callId: 'c', isOnTheLine: true, acceptsOverheard: false};
  await client.presence(presence); await client.presence(presence);
  assert.equal((await client.probe()).ok, true);
  assert.equal(pods.ensures, 1, 'successful responses must keep the cached connection');
  pod1Alive = false;
  await client.presence(presence);
  assert.equal(pods.ensures, 2);
  assert.deepEqual(pods.hits.slice(-2), ['pod1/api/setVoiceCallPresence', 'pod2/api/setVoiceCallPresence']);
  assert.equal((await client.probe()).ok, true);
  assert.equal(pods.ensures, 2);
});

test('sandbox network errors and non-SSE event responses also reconnect', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  const pods = fakeSandboxPods((pod, path) => {
    if (pod === 'pod1' && path === '/health') throw new TypeError('fetch failed');
    if (pod === 'pod2' && path === '/events') return fakeResponse(200, 'text/html');
    return fakeResponse(200, path === '/events' ? 'text/event-stream' : 'application/json');
  });
  const client = new NativeSandboxClient({accountLoader: () => ({authId: 'owner'}), rpc: pods.rpc, fetcher: pods.fetcher});
  assert.equal((await client.probe()).ok, true);
  assert.equal(pods.ensures, 3);
  assert.deepEqual(pods.hits, ['pod1/health', 'pod2/health', 'pod2/events', 'pod3/events']);
});

test('sandbox reconnect is bounded and request errors do not drop the connection', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  let status = 502;
  const pods = fakeSandboxPods(() => fakeResponse(status));
  const client = new NativeSandboxClient({accountLoader: () => ({authId: 'owner'}), rpc: pods.rpc, fetcher: pods.fetcher});
  const presence = {agentId: 'a', callId: 'c', isOnTheLine: true, acceptsOverheard: false};
  await assert.rejects(client.presence(presence), /^Error: upstream_http_502$/);
  assert.equal(pods.ensures, 2); assert.equal(pods.hits.length, 2);
  await assert.rejects(client.probe(), /^Error: upstream_http_502$/);
  assert.equal(pods.ensures, 4); assert.equal(pods.hits.length, 4);
  status = 400;
  await assert.rejects(client.presence(presence), /^Error: upstream_http_400$/);
  await assert.rejects(client.presence(presence), /^Error: upstream_http_400$/);
  assert.equal(pods.ensures, 5); assert.equal(pods.hits.length, 6);
});

test('HTTP /native/health recovers after the cached sandbox connection goes stale', async () => {
  const {NativeSandboxClient} = await import('./gbot_voice_session_bridge.mjs');
  let pod1Alive = true;
  const pods = fakeSandboxPods((pod, path) => {
    if (pod === 'pod1' && !pod1Alive) return fakeResponse(502);
    return fakeResponse(200, path === '/events' ? 'text/event-stream' : 'application/json');
  });
  const sandbox = new NativeSandboxClient({accountLoader: () => ({authId: 'owner'}), rpc: pods.rpc, fetcher: pods.fetcher, connectMode: 'normal'});
  const port = await freePort();
  const server = createBridge({port, agentId: 'a', desktopEnabled: true, toolsEnabled: false, sandbox});
  await new Promise(r => server.listen(port, '127.0.0.1', r));
  try {
    const health = () => fetch(`http://127.0.0.1:${port}/native/health`);
    assert.equal((await health()).status, 200);
    pod1Alive = false;
    const response = await health();
    assert.equal(response.status, 200);
    assert.equal(JSON.stringify(await response.json()).includes('FAKE_TOKEN'), false);
    assert.equal(pods.ensures, 2);
  } finally { await new Promise(r => server.close(r)); }
});
