'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { httpUrl, canonicalWebUrl, pageIdentity, bindingCheck, normalizeTrack, catalogEntries, planLanes, upperBound } = require('../../clients/browser/danmaku-simulator.user.js');

test('sources resolve relative catalog hrefs but reject non-HTTP protocols and URL credentials', () => {
  assert.equal(httpUrl('tracks/a.json', 'https://example.org/catalog/catalog.json'), 'https://example.org/catalog/tracks/a.json');
  for (const value of ['javascript:alert(1)', 'file:///tmp/a.json', 'data:application/json,{}', 'https://name:secret@example.org/a']) assert.throws(() => httpUrl(value));
  assert.equal(httpUrl('http://127.0.0.1:8793/catalog/catalog.json#ignored'), 'http://127.0.0.1:8793/catalog/catalog.json');
});

test('BV, part and public cid determine a Bilibili match; title never does', () => {
  const page = pageIdentity('https://www.bilibili.com/video/BV1iW411j7bn/?p=2&cid=123');
  assert.equal(page.bvid, 'BV1iW411j7bn'); assert.equal(page.page, 2); assert.equal(page.cid, '123');
  assert.equal(bindingCheck([{ site:'bilibili', bvid:'BV1iW411j7bn', page:2, cid:'123' }], page).state, 'match');
  for (const change of [{page:1}, {cid:'456'}, {bvid:'BV1xx411c7mD'}]) {
    assert.equal(bindingCheck([{ site:'bilibili', bvid:'BV1iW411j7bn', page:2, cid:'123', ...change }], page).state, 'mismatch');
  }
  assert.equal(bindingCheck([], page).state, 'manual');
  assert.equal(bindingCheck([{site:'bilibili',bvid:page.bvid,page:2}],pageIdentity('https://example.org/watch/1')).state,'unknown');
  assert.equal(pageIdentity('https://bilibili.com.example.org/video/BV1iW411j7bn/').site, 'web');
  assert.equal(pageIdentity('https://www.bilibili.com/video/BV1iW411j7bn/', '91').cid, '91');
});

test('exact web bindings and any matching declared alternate are accepted', () => {
  const identity=pageIdentity('https://example.org/watch/1?part=2');
  assert.equal(bindingCheck([{site:'web',url:identity.url}],identity).state,'match');
  assert.equal(bindingCheck([{site:'web',url:'https://example.org/watch/2'}],identity).state,'mismatch');
  assert.equal(bindingCheck([{site:'web',url:'https://example.org/watch/2'},{site:'web',url:identity.url}],identity).state,'match');
});

test('web identity ignores tracking query order but preserves hash-routed episode identity', () => {
  const first=canonicalWebUrl('https://EXAMPLE.org:443/watch?z=2&utm_source=x&a=1&share_source=link&spm_id_from=player#/episode/1');
  assert.equal(first,'https://example.org/watch?a=1&z=2#/episode/1');
  const current=pageIdentity('https://example.org/watch?z=2&a=1#/episode/1');
  assert.equal(bindingCheck([{site:'web',url:'https://example.org/watch?a=1&z=2&utm_medium=feed#/episode/1'}],current).state,'match');
  assert.equal(bindingCheck([{site:'web',url:'https://example.org/watch?a=1&z=2#/episode/2'}],current).state,'mismatch');
});

test('track import retains repetitions and safely normalizes ordinary XML modes', () => {
  const result=normalizeTrack({metadata:{synthetic:true,label:'测试轨道'},comments:[
    {time:8,text:'好耶',color:'#ffcc66',mode:1}, {time:7,text:'好耶',mode:5}, {time:7.5,text:'底部',mode:4},
    {time:-1,text:'invalid'}, {time:9,text:''}, {time:'x',text:'invalid'}, {time:10,text:'special',mode:7}
  ]});
  assert.equal(result.comments.length,3); assert.equal(result.rejected,3); assert.equal(result.unsupported,1);
  assert.equal(result.comments[2].color,0xffcc66); assert.equal(result.synthetic,true);
  assert.deepEqual(result.comments.map(c=>c.time),[7,7.5,8]);
  assert.throws(()=>normalizeTrack({comments:[],timeline:{scale:0}}));
  assert.throws(()=>normalizeTrack({comments:[],timeline:{offset:Infinity}}));
});

test('timeline maps start times with positive offset as a delay', () => {
  const track=normalizeTrack({comments:[{id:'a',time:4,text:'测试'}],timeline:{scale:2,offset:3}});
  const settings={fontSize:24,area:.5,density:100,...track.timeline};
  const {items}=planLanes(track.comments,960,540,settings,()=>50);
  assert.equal(items[0].at,11); assert.equal(items[0].end,18);
  assert.equal(upperBound(items,10.99),0); assert.equal(upperBound(items,11),1);
});

test('lane admission prevents later long comments from catching up in the same lane', () => {
  const comments=normalizeTrack([
    {time:0,text:'short',id:'a'}, {time:2,text:'very long',id:'b'}, {time:7,text:'later',id:'c'}
  ]).comments;
  const {items,overflow}=planLanes(comments,960,90,{fontSize:24,area:.5,density:100,scale:1,offset:0},
    (text)=>text==='very long'?1000:70);
  assert.equal(overflow,1); assert.deepEqual(items.map(c=>c.id),['a','c']);
  assert.equal(items[0].lane,items[1].lane);
});

test('render density is deterministic and leaves source comments intact', () => {
  const comments=normalizeTrack(Array.from({length:120},(_,i)=>({id:String(i),time:i*3,text:'内容'}))).comments;
  const settings={fontSize:24,area:1,density:50,scale:1,offset:0};
  const first=planLanes(comments,960,540,settings,()=>50).items.map(c=>c.id);
  assert.deepEqual(planLanes(comments,960,540,settings,()=>50).items.map(c=>c.id),first);
  assert.ok(first.length>30&&first.length<90); assert.equal(comments.length,120);
});

test('negative and target-duration-clipped starts cannot consume a lane or appear at zero', () => {
  const track=normalizeTrack({video:{duration:20},timeline:{scale:1,offset:-2,target_duration:8},comments:[
    {id:'negative',time:1,text:'negative'}, {id:'zero',time:2,text:'zero'}, {id:'edge',time:10,text:'edge'}, {id:'beyond',time:11,text:'beyond'}
  ]});
  assert.equal(track.timeline.target_duration,8);assert.equal(track.sourceDuration,20);
  const settings={fontSize:24,area:.5,density:100,scale:1,offset:-2,targetDuration:8};
  const result=planLanes(track.comments,960,90,settings,()=>60);
  assert.deepEqual(result.items.map(c=>c.id),['zero']);assert.equal(result.overflow,0);
  assert.throws(()=>normalizeTrack({comments:[],timeline:{target_duration:0}}));
});

test('catalog parses explicit JSON/XML entries without requiring any fetch', () => {
  const result=catalogEntries({items:[{video:{title:'Sintel'},tracks:[
    {id:'one',formats:{json:{href:'tracks/one.json',sha256:'a'.repeat(64),bytes:25}}},
    {id:'two',formats:{xml:{href:'https://example.net/two.xml'}}},
    {id:'bad',formats:{json:{href:'javascript:alert(1)'}}}
  ]}]},'https://example.org/catalog/catalog.json');
  assert.equal(result.length,2); assert.equal(result[0].url,'https://example.org/catalog/tracks/one.json');
  assert.equal(result[0].bytes,25); assert.equal(result[0].sha256,'a'.repeat(64));
  assert.throws(()=>catalogEntries({items:[]},'https://example.org/catalog.json'));
});
