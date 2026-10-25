'use strict';
// Production broker/stores and real loopback HTTP; dialogs/window are explicit doubles, not native proof.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),http=require('node:http');
const {EventEmitter}=require('node:events');
const {createDesktop,createStores,authorize,postWorkbench,settings,THEMES}=require('../shell/desktop.cjs');
const TOKEN='a'.repeat(64),BASE='b'.repeat(64);
function windowFor(port){const win=new EventEmitter();win.dead=false;win.isDestroyed=()=>win.dead;win.messages=[];win.webContents=new EventEmitter();win.webContents.mainFrame={url:`http://127.0.0.1:${port}/`};win.webContents.getURL=()=>win.webContents.mainFrame.url;win.webContents.send=(channel,value)=>{win.messages.push({channel,value});win.emit('command',value);};win.close=()=>{const e={prevented:false,preventDefault(){this.prevented=true;}};win.emit('close',e);if(!e.prevented){win.dead=true;win.emit('closed');}};return win;}
async function fixture(t,reply){const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'cq-desktop-test-')));const calls=[];
 const server=http.createServer((request,response)=>{const chunks=[];request.on('data',c=>chunks.push(c));request.on('end',()=>{const body=JSON.parse(Buffer.concat(chunks));calls.push({body,headers:request.headers,method:request.method,url:request.url});const answer=reply?reply(body):{project:'P',root,path:body.path||'entry.py',sha256:BASE,bytes:1};response.writeHead(answer.status||200,{'Content-Type':'application/json'});response.end(JSON.stringify(answer.value||answer));});});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));let port=server.address().port,win=windowFor(port);const handlers=new Map();const dialog={showOpenDialog:async()=>({canceled:true,filePaths:[]}),showSaveDialog:async()=>({canceled:true}),showMessageBox:async()=>({response:2}),showErrorBox(){}};
 const desktop=createDesktop({ipcMain:{handle:(name,fn)=>handlers.set(name,fn)},dialog,getWindow:()=>win,getPort:()=>port,token:TOKEN,userData:path.join(root,'user'),update:async action=>({action})});desktop.register();desktop.attachWindow(win);
 const event=()=>({sender:win.webContents,senderFrame:win.webContents.mainFrame});
 t.after(async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));fs.rmSync(root,{recursive:true,force:true});});
 return{root,calls,desktop,handlers,dialog,event,get win(){return win;},setWindow:value=>{win=value;desktop.attachWindow(value);},setPort:value=>{port=value;},invoke:(channel,...args)=>handlers.get(`cassquery:${channel}`)(event(),...args)};
}

test('IPC rejects unrelated/stale/subframe/non-worker documents before any worker request',async t=>{
 const f=await fixture(t);const handle=f.handlers.get('cassquery:workbench');
 for(const event of [{sender:{},senderFrame:f.win.webContents.mainFrame},{sender:f.win.webContents,senderFrame:{url:f.win.webContents.getURL()}}]){assert.equal((await handle(event,'list',{project:'P'})).code,'FORBIDDEN');}
 for(const url of ['https://example.com/','file:///tmp/file','data:text/html,hello','http://127.0.0.1:1/','http://user@127.0.0.1:'+f.win.webContents.getURL().split(':')[2].split('/')[0]+'/','http://127.0.0.1:'+f.win.webContents.getURL().split(':')[2].split('/')[0]+'/api/status']){f.win.webContents.mainFrame.url=url;assert.equal((await f.invoke('workbench','list',{project:'P'})).code,'FORBIDDEN');}
 assert.equal(f.calls.length,0);
});
test('fixed IPC verbs broker only POST loopback with private header and strict action fields',async t=>{
 const f=await fixture(t);const result=await f.invoke('workbench','list',{project:'P'});assert.equal(result.project,'P');assert.equal(f.calls.length,1);assert.equal(f.calls[0].method,'POST');assert.equal(f.calls[0].url,'/api/workbench');assert.equal(f.calls[0].headers['x-cassquery-workbench'],TOKEN);assert.ok(!JSON.stringify(result).includes(TOKEN));
 for(const [action,payload] of [['exec',{}],['read',{project:'P',path:'../secret'}],['save',{project:'P',path:'entry.py',text:'x',expected_sha256:BASE,auto_build:true}],['open',{path:'/arbitrary'}],['export',{project:'P',format:'step',destination:'/arbitrary'}],['save',{project:'P',path:'entry.py',text:'x',resolve_conflict:true}]])assert.ok((await f.invoke('workbench',action,payload)).error);
 assert.equal(f.calls.length,1);await f.invoke('settings','patch',{autoBuild:true});await f.invoke('workbench','save',{project:'P',path:'entry.py',text:'x',expected_sha256:BASE});assert.equal(f.calls[1].body.auto_build,true);
});
test('settings validate and persist atomically; recovery preserves root identity, text, selection and scroll inertly',()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'cq-state-test-'));try{
  const store=createStores(directory);for(const theme of THEMES)assert.equal(store.patchSettings({theme}).theme,theme);assert.equal(createStores(directory).getSettings().theme,'high-contrast');
  for(const bad of [{schema:2},{theme:'external'},{editorFontSize:11},{editorFontSize:25},{tabSize:3},{lineWrap:'true'},{updateAutoDownload:1},{updateChannel:'beta'},{recentProjects:Array(11).fill('/p')},{url:'https://example.com'}])assert.throws(()=>store.patchSettings(bad));
  const record={root:directory,project:'P',path:'entry.py',text:'print(1)',base_sha256:BASE,selection:{start:2,end:5},scroll:{top:10,left:2}};store.saveRecovery(record);store.saveRecovery({...record,root:path.join(directory,'other'),text:'print(2)'});const recovered=createStores(directory).getRecovery();assert.equal(recovered.drafts.length,2);assert.equal(recovered.drafts[0].text,record.text);assert.deepEqual(recovered.drafts[0].selection,record.selection);assert.deepEqual(recovered.drafts[0].scroll,record.scroll);
  store.deleteRecovery({root:directory,project:'P',path:'entry.py'});assert.equal(store.getRecovery().drafts.length,1);assert.throws(()=>store.saveRecovery({...record,path:'../entry.py'}));assert.throws(()=>store.saveRecovery({...record,text:'\ud800'}));assert.throws(()=>store.saveRecovery({...record,selection:{start:0,end:100}}));assert.throws(()=>store.saveRecovery({...record,text:'x'.repeat(2*1024*1024+1)}));
  for(const file of ['workbench-settings.json','workbench-recovery.json'])assert.equal(fs.statSync(path.join(directory,file)).mode&0o777,0o600);assert.ok(!fs.readdirSync(directory).some(name=>name.endsWith('.tmp')));
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});
test('corrupt or symlink desktop state is retained/refused rather than silently overwritten',()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'cq-corrupt-test-'));try{const store=createStores(directory);const file=path.join(directory,'workbench-recovery.json');fs.writeFileSync(file,'bad JSON');assert.throws(()=>store.getRecovery());assert.equal(fs.readFileSync(file,'utf8'),'bad JSON');fs.unlinkSync(file);fs.symlinkSync('/etc/hosts',file);assert.throws(()=>store.getRecovery());assert.throws(()=>store.saveRecovery({root:directory,project:'P',path:'entry.py',text:'x',base_sha256:BASE}));}finally{fs.rmSync(directory,{recursive:true,force:true});}
});
test('recovery save binds actual broker-known canonical root; delete does not execute/open a stale project',async t=>{
 const f=await fixture(t);const record={root:f.root,project:'P',path:'entry.py',text:'draft',base_sha256:BASE};assert.equal((await f.invoke('recovery','save',record)).code,'FORBIDDEN');await f.invoke('workbench','list',{project:'P'});assert.equal((await f.invoke('recovery','save',record)).drafts.length,1);assert.equal((await f.invoke('recovery','get')).drafts[0].root,f.root);assert.equal((await f.invoke('recovery','save',{...record,root:'/unrelated'})).code,'FORBIDDEN');await f.invoke('recovery','delete',{root:f.root,project:'P',path:'entry.py'});assert.equal((await f.invoke('recovery','get')).drafts.length,0);assert.equal(f.calls.length,1);
});
test('cancelled picker/trust confirmation never invokes Python; SaveCopy destination cannot come from renderer',async t=>{
 const f=await fixture(t);assert.deepEqual(await f.invoke('workbench','openFile',{}),{cancelled:true});f.dialog.showOpenDialog=async()=>({canceled:false,filePaths:[path.join(f.root,'entry.py')]});f.dialog.showMessageBox=async()=>({response:1});assert.deepEqual(await f.invoke('workbench','openFile',{}),{cancelled:true});assert.equal(f.calls.length,0);
 f.dialog.showSaveDialog=async()=>({canceled:false,filePath:path.join(f.root,'copy.py')});await f.invoke('workbench','saveAs',{project:'P',path:'entry.py',text:'draft'});assert.equal(f.calls[0].body.destination,path.join(f.root,'copy.py'));assert.equal(f.calls[0].body.overwrite,true);assert.ok((await f.invoke('workbench','saveAs',{project:'P',path:'entry.py',text:'draft',destination:'/evil'})).error);assert.equal(f.calls.length,1);
});
test('window/origin/draft revision changes during native SaveDialog cancel the old selection before POST',async t=>{
 for(const mode of ['window','origin','draft']){
  const f=await fixture(t);let resolve,entered;const started=new Promise(r=>{entered=r;});f.dialog.showSaveDialog=()=>{entered();return new Promise(r=>{resolve=r;});};const action=f.invoke('workbench','export',{project:'P',format:'step'});await started;
  if(mode==='window')f.setWindow(windowFor(Number(f.win.webContents.getURL().split(':')[2].split('/')[0])));if(mode==='origin')f.win.webContents.mainFrame.url='https://example.com/';if(mode==='draft')await f.invoke('dirty',true);
  resolve({canceled:false,filePath:path.join(f.root,'out.step')});const result=await action;assert.ok(['FORBIDDEN','DRAFT_CHANGED'].includes(result.code));assert.equal(f.calls.length,0);
 }
});
test('dirty close Cancel retains worker/window; Save requires actual CAS plus current-revision ACK',async t=>{
 const f=await fixture(t);await f.invoke('dirty',true);assert.equal(await f.desktop.requestQuit(),false);assert.equal(f.desktop.isDirty(),true);assert.equal(f.win.dead,false);
 f.dialog.showMessageBox=async()=>({response:0});const received=new Promise(resolve=>f.win.once('command',resolve));const quitting=f.desktop.requestQuit();assert.equal((await received).command,'save');assert.equal((await f.invoke('dirty',false)).code,'INVALID_ACK');assert.equal(f.desktop.isDirty(),true,'invalid ACK cannot make a concurrent quit appear clean');let settled=false;quitting.then(()=>{settled=true;});await new Promise(r=>setImmediate(r));assert.equal(settled,false,'bare dirty-false cannot acknowledge unsaved draft');
 await f.invoke('dirty',true);await f.invoke('workbench','save',{project:'P',path:'entry.py',text:'saved',expected_sha256:BASE});await f.invoke('dirty',false);assert.equal(await quitting,true);assert.equal(f.win.dead,false);
});
test('dirty close Discard is explicit and navigation invalidates a pending ACK',async t=>{
 const f=await fixture(t);await f.invoke('dirty',true);f.dialog.showMessageBox=async()=>({response:1});let command=new Promise(r=>f.win.once('command',r));let decision=f.desktop.requestQuit();assert.equal((await command).command,'discard');await f.invoke('dirty',false);assert.equal(await decision,true);
 await f.invoke('dirty',true);command=new Promise(r=>f.win.once('command',r));decision=f.desktop.requestQuit();await command;f.win.webContents.emit('did-start-navigation',{},f.win.webContents.getURL(),false,true);assert.equal(await decision,false);assert.equal(f.win.dead,false);
});
test('worker conflicts preserve structured details; conflict resolution flag requires native confirmation',async t=>{
 const f=await fixture(t,body=>body.resolve_conflict?{project:'P',path:'entry.py',sha256:BASE,bytes:1}:{status:409,value:{error:'Conflict',code:'CONFLICT',path:'entry.py',active_file_changed:true,active_revision:{sha256:BASE,bytes:1,kind:'regular'},draft_sha256:BASE,recovery_paths:[{path:'.recovered.py',sha256:BASE,bytes:1,kind:'regular'}]}});const error=await f.invoke('workbench','save',{project:'P',path:'entry.py',text:'draft',expected_sha256:BASE});assert.equal(error.status,409);assert.equal(error.details.active_file_changed,true);assert.equal(error.details.path,'entry.py');assert.equal(error.details.active_revision.kind,'regular');assert.equal(error.details.draft_sha256,BASE);assert.deepEqual(error.details.recovery_paths,[{path:'.recovered.py',sha256:BASE,bytes:1,kind:'regular'}]);
 f.dialog.showMessageBox=async()=>({response:1});assert.deepEqual(await f.invoke('workbench','resolveConflict',{project:'P',path:'entry.py',text:'chosen',expected_sha256:BASE}),{cancelled:true});assert.equal(f.calls.length,1);f.dialog.showMessageBox=async()=>({response:0});await f.invoke('workbench','resolveConflict',{project:'P',path:'entry.py',text:'chosen',expected_sha256:BASE});assert.equal(f.calls[1].body.resolve_conflict,true);
});
test('flat and nested conflict wire metadata preserve only validated recovery records',async t=>{
 const f=await fixture(t,body=>({status:409,value:body.project==='bad'?{error:'Conflict',code:'CONFLICT',active_file_changed:true,recovery_paths:[{path:'../outside.py',bytes:1,sha256:BASE}]}:{error:'Conflict',code:'CONFLICT',path:'entry.py',active_file_changed:true,details:{recovery_paths:[{path:'.saved.py',bytes:null,sha256:null}]},arbitrary:{destination:'/untrusted'},__proto__:{polluted:true}}}));
 const good=await f.invoke('workbench','save',{project:'P',path:'entry.py',text:'draft',expected_sha256:BASE});assert.equal(good.details.active_file_changed,true);assert.equal(good.details.path,'entry.py');assert.deepEqual(good.details.recovery_paths,[{path:'.saved.py',bytes:null,sha256:null}]);assert.equal(good.details.arbitrary,undefined);assert.equal(good.polluted,undefined);
 const bad=await f.invoke('workbench','save',{project:'bad',path:'entry.py',text:'draft',expected_sha256:BASE});assert.equal(bad.code,'INVALID_RESPONSE');assert.equal(bad.details,undefined);
});
test('CAD export conflict metadata is bound to the native destination and sibling recovery facts only',async t=>{
 const f=await fixture(t,body=>({status:409,value:{error:'Output changed',code:'OUTPUT_CHANGED',active_file_changed:true,output_path:body.path,recovery_paths:[{path:body.project==='bad'?'/different/root/recovered.step':path.join(path.dirname(body.path),'.recovered.step'),bytes:10,sha256:BASE,kind:'regular'}]}}));
 const destination=path.join(f.root,'out.step');f.dialog.showSaveDialog=async()=>({canceled:false,filePath:destination});
 const good=await f.invoke('workbench','export',{project:'P',format:'step'});assert.equal(good.status,409);assert.equal(good.details.active_file_changed,true);assert.equal(good.details.output_path,destination);assert.equal(good.details.recovery_paths[0].path,path.join(f.root,'.recovered.step'));assert.equal(fs.existsSync(path.join(f.root,'.recovered.step')),false,'metadata never grants file read/write authority');
 const bad=await f.invoke('workbench','export',{project:'bad',format:'step'});assert.equal(bad.code,'INVALID_RESPONSE');assert.equal(bad.details,undefined);
});

test('native-selected destination aliases are canonicalized before POST without following the output leaf',async t=>{
 const f=await fixture(t,body=>({status:409,value:{error:'Output changed',code:'OUTPUT_CHANGED',active_file_changed:true,output_path:path.join(f.root,path.basename(body.path)),recovery_paths:[{path:path.join(f.root,'.retained.step'),bytes:1,sha256:BASE}]}}));
 const alias=path.join(f.root,'alias');fs.symlinkSync(f.root,alias,'dir');
 f.dialog.showSaveDialog=async()=>({canceled:false,filePath:path.join(alias,'new.step')});const result=await f.invoke('workbench','export',{project:'P',format:'step'});
 assert.equal(result.code,'OUTPUT_CHANGED');assert.equal(result.details.output_path,path.join(f.root,'new.step'));assert.equal(result.details.recovery_paths.length,1);assert.equal(f.calls[0].body.path,path.join(f.root,'new.step'));
 const target=path.join(f.root,'original.py'),leaf=path.join(f.root,'copy.py');fs.writeFileSync(target,'untouched');fs.symlinkSync(target,leaf);
 f.dialog.showSaveDialog=async()=>({canceled:false,filePath:path.join(alias,'copy.py')});await f.invoke('workbench','saveAs',{project:'P',path:'entry.py',text:'draft'});
 assert.equal(f.calls[1].body.destination,leaf);assert.notEqual(f.calls[1].body.destination,fs.realpathSync(leaf));assert.equal(fs.readFileSync(target,'utf8'),'untouched');
});

test('initialized desktop state root retarget is rejected without changing external or moved originals',()=>{
 for(const moment of ['between-actions','before-commit']){
  const parent=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'cq-retarget-'))),root=path.join(parent,'data'),saved=path.join(parent,'saved'),external=path.join(parent,'external');fs.mkdirSync(external);
  const store=createStores(root);store.patchSettings({theme:'graphite'});const original=fs.readFileSync(path.join(root,'workbench-settings.json'));const target=path.join(external,'workbench-settings.json');fs.writeFileSync(target,JSON.stringify(settings({theme:'forest'})));const outside=fs.readFileSync(target);
  const retarget=()=>{fs.renameSync(root,saved);fs.symlinkSync(external,root);};const fsync=fs.fsyncSync;
  try{
   if(moment==='between-actions')retarget();else fs.fsyncSync=fd=>{fsync(fd);retarget();};
   assert.throws(()=>store.patchSettings({theme:'sand'}),/root changed/);assert.deepEqual(fs.readFileSync(target),outside);assert.deepEqual(fs.readFileSync(path.join(saved,'workbench-settings.json')),original);assert.throws(()=>store.getSettings());
  }finally{fs.fsyncSync=fsync;fs.rmSync(parent,{recursive:true,force:true});}
 }
});

test('response/token leakage, malformed JSON and request deadline fail closed',async t=>{
 const f=await fixture(t,()=>({error:TOKEN,code:'LEAK'}));const result=await f.invoke('workbench','list',{project:'P'});assert.equal(result.code,'PRIVATE_CAPABILITY');assert.ok(!JSON.stringify(result).includes(TOKEN));
 const malformed=http.createServer((_request,response)=>{response.writeHead(200);response.end('not valid JSON');});await new Promise(r=>malformed.listen(0,'127.0.0.1',r));t.after(()=>{malformed.closeAllConnections();malformed.close();});await assert.rejects(postWorkbench(malformed.address().port,TOKEN,{action:'list',project:'P'},1000),/Invalid worker JSON/);
 const server=http.createServer(()=>{});await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>{server.closeAllConnections();server.close();});await assert.rejects(postWorkbench(server.address().port,TOKEN,{action:'list',project:'P'},30),/timed out/);
});
test('owned close listener never treats Cancel or a later unrelated veto as discard consent',async t=>{
 const f=await fixture(t);await f.invoke('dirty',true);f.win.close();await new Promise(r=>setImmediate(r));assert.equal(f.win.dead,false);assert.equal(f.desktop.isDirty(),true);
 f.dialog.showMessageBox=async()=>({response:1});f.win.on('command',command=>{if(command.command==='discard')f.invoke('dirty',false);});
 const veto=event=>event.preventDefault();f.win.on('close',veto);f.win.close();await new Promise(r=>setTimeout(r,10));assert.equal(f.win.dead,false);f.win.removeListener('close',veto);await f.invoke('dirty',true);f.dialog.showMessageBox=async()=>({response:2});f.win.close();await new Promise(r=>setImmediate(r));assert.equal(f.win.dead,false);assert.equal(f.desktop.isDirty(),true);
});
test('fresh application token stays out of parent env, preload and rendered diagnostic page',async()=>{
 const directory=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'cq-main-token-')));try{
  let workerEnv;const child=Object.assign(new EventEmitter(),{pid:123,exitCode:null,signalCode:null});const app=new EventEmitter();Object.assign(app,{commandLine:{appendSwitch(){}},isPackaged:true,getPath:()=>directory,getVersion:()=> '0.2.0',setPath(){},requestSingleInstanceLock:()=>false,quit(){},whenReady:()=>new Promise(()=>{})});
  const electron={app,BrowserWindow:function(){},Menu:{},nativeTheme:{},screen:{},ipcMain:{handle(){}},dialog:{},shell:{}};
  const env={CASSQUERY_USER_DATA:directory,CASSQUERY_WORKBENCH_TOKEN:'untrusted-environment-value'};
  const req=name=>name==='electron'?electron:name==='./runtime.cjs'?{startingPort:()=>3939,workerCommand:()=>({executable:'/bundled/worker',args:[]}),startWorker:async options=>{workerEnv=options.env;return{child,port:3939,diagnostic:{python:'/bundled/worker'}};},stopWorker:async()=>{}}:name.startsWith('./')?require(path.join(__dirname,'../shell',name)):require(name);
  const context=vm.createContext({require:req,__dirname:path.join(__dirname,'../shell'),module:{},console,AbortController,setTimeout,clearTimeout,setImmediate,process:{env,platform:process.platform,arch:process.arch,versions:process.versions,argv:[],pid:999,resourcesPath:'/bundle/Contents/Resources',execPath:'/bundle/Contents/MacOS/CassQuery',hrtime:process.hrtime,on(){}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../shell/main.js'),'utf8')+'\nglobalThis.observer={ensureWorker,secret:workbenchToken,page:()=>{tried.stderr=workbenchToken+" <script>";return errorPage();}};',context);
  await context.observer.ensureWorker();assert.match(context.observer.secret,/^[a-f0-9]{64}$/);assert.equal(workerEnv.CASSQUERY_WORKBENCH_TOKEN,context.observer.secret);assert.equal(env.CASSQUERY_WORKBENCH_TOKEN,'untrusted-environment-value');const html=decodeURIComponent(context.observer.page());assert.ok(!html.includes(context.observer.secret));assert.ok(html.includes('[private capability]'));assert.ok(html.includes('&lt;script>'));
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});

test('actual main constructor records immutable preload bytes for only the current owned window',async()=>{
 const directory=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'cq-main-preload-')));try{
  const app=new EventEmitter();Object.assign(app,{commandLine:{appendSwitch(){}},isPackaged:true,getPath:()=>directory,getVersion:()=> '0.2.0',setPath(){},requestSingleInstanceLock:()=>false,quit(){},whenReady:()=>new Promise(()=>{})});
  let nextId=1;class Window extends EventEmitter{
   constructor(options){super();this.id=nextId++;this.options=options;this.dead=false;this.webContents=new EventEmitter();Object.assign(this.webContents,{id:nextId++,mainFrame:{url:'http://127.0.0.1:3939/'},getURL:()=> 'http://127.0.0.1:3939/',setWindowOpenHandler(){},session:{setPermissionRequestHandler(){}},send(){}});}
   isDestroyed(){return this.dead;}show(){}loadURL(){return Promise.resolve();}close(){this.dead=true;this.emit('closed');}
  }
  const electron={app,BrowserWindow:Window,Menu:{},nativeTheme:{},screen:{},ipcMain:{handle(){}},dialog:{},shell:{}};
  const req=name=>name==='electron'?electron:name==='./runtime.cjs'?{}:name.startsWith('./')?require(path.join(__dirname,'../shell',name)):require(name);
  const context=vm.createContext({require:req,__dirname:path.join(__dirname,'../shell'),module:{},console,AbortController,setTimeout,clearTimeout,setImmediate,process:{env:{},platform:process.platform,arch:process.arch,versions:process.versions,argv:[],pid:999,resourcesPath:'/owned/CassQuery.app/Contents/Resources',execPath:'/owned/CassQuery.app/Contents/MacOS/CassQuery',hrtime:process.hrtime,on(){}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../shell/main.js'),'utf8')+'\nglobalThis.observer={openWindow,getWindow:()=>window,identity:configuredPreloadIdentity,ready:()=>{workerPort=3939;}};',context);
  context.observer.ready();await context.observer.openWindow();const first=context.observer.getWindow(),identity=context.observer.identity(first);
  assert.equal(Object.isFrozen(identity),true);assert.equal(identity.schema,1);assert.equal(identity.method,'owned BrowserWindow constructor');assert.equal(identity.path,first.options.webPreferences.preload);assert.equal(identity.resources_path,'/owned/CassQuery.app/Contents/Resources');assert.equal(identity.web_contents_id,first.webContents.id);
  const bytes=fs.readFileSync(identity.path);assert.equal(identity.bytes,bytes.length);assert.equal(identity.sha256,require('node:crypto').createHash('sha256').update(bytes).digest('hex'));assert.deepEqual(Object.keys(identity).sort(),['schema','method','path','bytes','sha256','web_contents_id','resources_path'].sort());
  const unrelated=new Window(first.options);unrelated.webContents.id=first.webContents.id;assert.throws(()=>context.observer.identity(unrelated),/another window/);
  first.close();assert.throws(()=>context.observer.identity(first),/another window/);await context.observer.openWindow();const second=context.observer.getWindow();assert.notEqual(second,first);assert.notEqual(context.observer.identity(second).web_contents_id,identity.web_contents_id);assert.throws(()=>context.observer.identity(first),/another window/);
  second.webContents.id=0;assert.throws(()=>context.observer.identity(second),/binding/);
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});

test('preload exposes only fixed methods and strips Electron events with unsubscribe cleanup',()=>{
 const listeners=new Map(),calls=[];let exposed;const electron={contextBridge:{exposeInMainWorld:(name,api)=>{assert.equal(name,'cassquery');exposed=api;}},ipcRenderer:{invoke:(...args)=>{calls.push(args);return Promise.resolve({});},on:(name,fn)=>listeners.set(name,fn),removeListener:(name,fn)=>{if(listeners.get(name)===fn)listeners.delete(name);}}};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../shell/preload.cjs'),'utf8'),{require:name=>{assert.equal(name,'electron');return electron;}});assert.deepEqual(Object.keys(exposed).sort(),['onCommand','recovery','setDirty','settings','update','workbench'].sort());assert.equal(Object.isFrozen(exposed),true);exposed.workbench('read',{project:'P',path:'entry.py'});assert.equal(calls[0][0],'cassquery:workbench');let value;const off=exposed.onCommand(command=>{value=command;});listeners.get('cassquery:command')({sender:{private:true}},{command:'build'});assert.equal(value.command,'build');assert.equal(value.sender,undefined);off();assert.equal(listeners.size,0);
});
