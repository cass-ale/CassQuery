'use strict';
// Synthetic callback/state regressions. These are not native OS or packaged-app proof.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm');
const {EventEmitter}=require('node:events');
const {test}=require('node:test');
const api=require('../shell/smoke.cjs');
const graphics=require('../shell/renderer_config.cjs');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

test('native camera gesture carries coherent state and acknowledges its endpoint after normal coalescing',async()=>{
 const originalSource=fs.readFileSync(path.join(__dirname,'../shell/smoke.cjs'),'utf8');
 const modes=['left','right','superseded','no-down','no-capture','lost-move','untrusted','no-release','covered','cleanup-error','delayed-right','late-down','release-deferred','coalesced-final','foreign-id','wrong-move-mask','missing-final','move-after-up','cancelled','journal-dropped','layout-change','gesture-deadline','no-renderer-frame'];
 for(const mode of modes){
  const healthy=['left','right','superseded','delayed-right','release-deferred','coalesced-final','no-renderer-frame'].includes(mode);
  let source=originalSource.replace('phaseBudget=2000',healthy?'phaseBudget=1000':'phaseBudget=20');
  assert.notEqual(source,originalSource,'Native pointer timeout test seam must replace the production budget');
  if(mode==='gesture-deadline')source=source.replace('gestureBudget=20000','gestureBudget=20');
  const canvasListeners=new Map(),docListeners=new Map(),sent=[];let captured=false,cleaned=false,frames=0,pending=null,stamp=0;
  const canvas={getBoundingClientRect:()=>({left:0,top:0,right:mode==='layout-change'&&frames>=5?399:400,bottom:300,width:400,height:300}),hasPointerCapture:()=>captured,
   addEventListener:(t,f)=>canvasListeners.set(t,f),removeEventListener:t=>canvasListeners.delete(t)};
  const document={querySelector:()=>canvas,elementFromPoint:()=>mode==='covered'?{}:canvas,hasFocus:()=>true,
   addEventListener:(t,f)=>docListeners.set(t,f),removeEventListener:t=>docListeners.delete(t)};
  const renderer=vm.createContext({document,window:{},performance,innerWidth:400,innerHeight:300,scrollX:0,scrollY:0,requestAnimationFrame:callback=>{frames++;if(mode==='gesture-deadline'){const end=performance.now()+35;while(performance.now()<end){}}if(mode==='no-renderer-frame')return;callback(performance.now());}});
  const emit=(type,input,buttons,coalesced=[])=>{const e={type,target:canvas,clientX:input.x,clientY:input.y,buttons,button:type==='pointermove'?-1:input.button==='right'?2:0,pointerId:mode==='foreign-id'&&type==='pointermove'?2:1,isTrusted:mode!=='untrusted',timeStamp:++stamp,movementX:8,movementY:3,getCoalescedEvents:()=>coalesced};docListeners.get(type)?.(e);canvasListeners.get(type)?.(e);};
  const win={focus(){},isFocused:()=>true,isDestroyed:()=>false,webContents:{executeJavaScript:async code=>{
   if(['delayed-right','late-down'].includes(mode)&&code.includes('window.__cqSmokeDown?.trusted')){const end=performance.now()+35;while(performance.now()<end){}}
   if(code.includes("c.removeEventListener('pointermove'")){cleaned=true;if(mode==='cleanup-error')throw Error('listener cleanup sentinel');}
   return vm.runInContext(code,renderer);},sendInputEvent:input=>{
    sent.push(input);const mask=input.modifiers?.includes('rightButtonDown')?2:input.modifiers?.includes('leftButtonDown')?1:0;
    if(input.type==='mouseDown'){assert.equal(mask,input.button==='right'?2:1);if(mode==='no-down')return;captured=mode!=='no-capture';emit('pointerdown',input,mask);if(captured)emit('gotpointercapture',input,mask);}
    if(input.type==='mouseMove'){
     if(mask)frames++;
     assert.equal(input.button,mode==='left'?'left':'right');
     if(mask&&['release-deferred','coalesced-final','move-after-up'].includes(mode)){pending={input,mask};return;}
     if(mask&&(mode==='lost-move'||mode==='missing-final'&&frames===10))return;
     emit('pointermove',input,mode==='wrong-move-mask'&&mask?1:mask);
     if(mode==='superseded'&&mask)emit('pointermove',{...input,x:input.x+1,y:input.y+1},mask);
     if(mode==='cancelled'&&mask)emit('pointercancel',input,mask);
     if(mode==='journal-dropped'&&mask)for(let i=0;i<90;i++)emit('pointermove',input,mask);
    }
    if(input.type==='mouseUp'){
     if(mode==='no-release')return;
     if(pending&&mode!=='move-after-up'){
      if(mode==='coalesced-final'){const p=pending.input,sample={clientX:p.x,clientY:p.y,button:-1,buttons:pending.mask,pointerId:1,isTrusted:true,timeStamp:++stamp};emit('pointermove',{...p,x:p.x+1},pending.mask,[sample]);}
      else emit('pointermove',pending.input,pending.mask);
     }
     captured=false;emit('pointerup',input,0);if(pending&&mode==='move-after-up')emit('pointermove',pending.input,pending.mask);emit('lostpointercapture',input,0);
    }
   }}};
  const main=vm.createContext({require,process,Buffer,setTimeout,clearTimeout,module:{exports:{}}});vm.runInContext(source+'\nglobalThis.drag=dragCamera;',main);
  if(healthy){
   const result=await main.drag(win,mode==='left'?'left':'right');assert.equal(result.status,'checked');assert.equal(result.intermediate_ack_required,false);assert.equal(result.moves_sent,10);assert.equal(result.frames.length,10);assert.ok(result.elapsed_ms<=result.budget_ms);assert.equal(sent.filter(e=>e.type==='mouseDown').length,1);assert.equal(sent.filter(e=>e.type==='mouseMove'&&e.modifiers?.length).length,10);assert.equal(sent.at(-1).type,'mouseUp');assert.equal(result.observations.at(-1).phase,'whole gesture endpoint and release acknowledged');
   if(mode==='superseded')assert.notEqual(result.observations.at(-1).pointer.x,sent.at(-1).x,'Last scalar need not equal the actual observed endpoint event');
   if(mode==='release-deferred')assert.equal(result.observations.at(-1).events.filter(e=>e.type==='pointermove'&&e.buttons).length,1,'Intermediate moves may be coalesced, never claimed individually ACKed');
   if(mode==='coalesced-final')assert.ok(result.observations.at(-1).events.some(e=>e.coalesced&&e.x===sent.at(-1).x));
  }else{await assert.rejects(main.drag(win,'right'),error=>{assert.equal(error.pointer_trace.status,'failed');assert.ok(error.pointer_trace.observations.length||mode==='covered');if(['late-down','gesture-deadline'].includes(mode))assert.match(error.message,/deadline|timed out/);return true;});}
  assert.equal(cleaned,true);if(mode!=='cleanup-error'){assert.equal(canvasListeners.size,0);assert.equal(docListeners.size,0);}
 }
});

test('native Tab probe acknowledges delayed insertion before Undo and rejects missing or wrong indentation',async()=>{
 const source=fs.readFileSync(path.join(__dirname,'../tools/native_workbench_probe.cjs'),'utf8');
 const start=source.indexOf("    await click(win, '#source-editor');\n    await waitFor(win, `document.activeElement"),end=source.indexOf('    const persisted =',start);
 assert.ok(start>0&&end>start);const block=source.slice(start,end);
 for(const mode of ['delayed','missing','wrong']){
  const old='source\n',editor={value:old,selectionStart:4,selectionEnd:4,readOnly:false,disabled:false};let undone=false;
  const document={activeElement:editor,getElementById:()=>editor},window={__workbench:{busy:0,active:{text:old}}};
  const renderer=vm.createContext({document,window});
  const waitFor=async(_win,code)=>{const deadline=Date.now()+1000;while(Date.now()<deadline){if(vm.runInContext(code,renderer))return;await new Promise(r=>setTimeout(r,5));}throw Error('acknowledgement missing');};
  const win={webContents:{selectAll:()=>{editor.selectionStart=0;editor.selectionEnd=old.length;}}};
  const key=code=>setTimeout(()=>{if(code==='Left')editor.selectionStart=editor.selectionEnd=0;
   if(code==='Tab'&&mode!=='missing'){editor.value=(mode==='wrong'?'    ':'  ')+old;window.__workbench.active.text=editor.value;}},30);
  const shortcut=code=>{assert.equal(code,'Z');assert.equal(editor.value,'  '+old);undone=true;setTimeout(()=>{editor.value=old;window.__workbench.active.text=old;},30);};
  const context=vm.createContext({win,waitFor,key,shortcut,external:old,click:async()=>{},js:async code=>vm.runInContext(code,renderer)});
  const pending=vm.runInContext('(async()=>{'+block+'return {oldText,newText};})()',context);
  if(mode==='delayed'){const result=await pending;assert.equal(result.newText,'  '+old);assert.equal(undone,true);assert.equal(editor.value,old);}
  else{await assert.rejects(pending,/acknowledgement missing/);assert.equal(undone,false);}
 }
});

test('failed renderer execution retains bounded exact-operation diagnostics without a retry',async()=>{
 let calls=0;const win={id:11,isDestroyed:()=>false,webContents:{id:22,executeJavaScript:()=>{calls++;return new Promise(()=>{});}}};
 await assert.rejects(api.evaluate(win,'documented operation'.repeat(40),20),error=>{
  assert.match(error.message,/timed out/);assert.equal(error.renderer_execution.timeout_ms,20);assert.equal(error.renderer_execution.expression.length,512);
  assert.equal(error.renderer_execution.window_id,11);assert.equal(error.renderer_execution.web_contents_id,22);return true;
 });assert.equal(calls,1);
});

test('native graphics sampling follows actual certified renderer readiness',()=>{
 const source=fs.readFileSync(path.join(__dirname,'../shell/main.js'),'utf8');
 const ready=source.indexOf("await waitFor(win, 'window.__cq?.model?.tier === \"certified\" && window.__cq.model.gates?.pass')");
 const graphics=source.indexOf('report.graphics.webgl = await evaluate');
 assert.ok(ready>=0&&graphics>ready);
});

test('normal close waits for actual closed; veto/throw/already-dead/false-closed stay red with no destroy bypass',async()=>{
 for(const mode of ['immediate','pending','veto','throw','false-closed','already-dead']){
  const win=new EventEmitter();let dead=mode==='already-dead',destroyCalls=0;
  win.isDestroyed=()=>dead;win.destroy=()=>{destroyCalls++;throw Error('destroy forbidden');};
  win.close=()=>{const closed=()=>{dead=mode!=='false-closed';win.emit('closed');};if(mode==='immediate'||mode==='false-closed')closed();if(mode==='pending')setTimeout(closed,5);if(mode==='throw')throw Error('close sentinel');};
  if(mode==='immediate'||mode==='pending')await api.closeWindow(win,1000);else await assert.rejects(api.closeWindow(win,30));
  assert.equal(win.listenerCount('closed'),0);assert.equal(destroyCalls,0);
 }
});
test('controlled scheduler delay exposes too-tight positive budget without relaxing deadline rejection',async()=>{
 for(const budget of [30,1000]){
  const win=new EventEmitter();const events=[];let dead=false,destroyCalls=0;
  win.isDestroyed=()=>dead;win.destroy=()=>{destroyCalls++;throw Error('destroy forbidden');};
  win.close=()=>{events.push('close');setTimeout(()=>{dead=true;events.push('closed');win.emit('closed');},5);queueMicrotask(()=>{const end=performance.now()+45;while(performance.now()<end){}});};
  if(budget===30)await assert.rejects(api.closeWindow(win,budget),/timed out|deadline/);else await api.closeWindow(win,budget);
  assert.deepEqual(events,['close','closed']);assert.equal(dead,true);assert.equal(win.listenerCount('closed'),0);assert.equal(destroyCalls,0);
 }
});
test('pending execution and late true result cannot exceed the budget and be graded ready',async()=>{
 const pending={isDestroyed:()=>false,webContents:{executeJavaScript:()=>new Promise(()=>{})}};
 await assert.rejects(api.waitFor(pending,'true',20),/timed out/);
 const late={isDestroyed:()=>false,webContents:{executeJavaScript:()=>{const end=performance.now()+30;while(performance.now()<end){}return Promise.resolve(true);}}};
 await assert.rejects(api.waitFor(late,'true',10),/exceeded its deadline/);
 const errors={...pending,cassqueryRendererErrors:['late page error']};await assert.rejects(api.waitFor(errors,'true'),/late page error/);
});
test('capture timeouts/failures release only owned debugger attachment',async()=>{
 let code=fs.readFileSync(path.join(__dirname,'../shell/smoke.cjs'),'utf8').replace('timeoutMs = 10000','timeoutMs = 20');
 code=code.replace("}), 15000, 'Native screenshot capture')","}), 20, 'Native screenshot capture')");
 for(const mode of ['execution','cdp','reject','owned']){
  const context=vm.createContext({require,process,setTimeout,clearTimeout,Buffer,module:{exports:{}}});vm.runInContext(code+'\nglobalThis.capture=capture;',context);
  let attached=mode==='owned',attachments=0,detachments=0;
  const debuggerAPI={isAttached:()=>attached,attach:()=>{attached=true;attachments++;},detach:()=>{attached=false;detachments++;},sendCommand:()=>mode==='reject'?Promise.reject(Error('CDP sentinel')):new Promise(()=>{})};
  const win={webContents:{debugger:debuggerAPI,executeJavaScript:()=>mode==='execution'?new Promise(()=>{}):Promise.resolve({width:100,height:100})}};
  await assert.rejects(context.capture(win,'/never-written.png',20));
  assert.equal(attachments,mode==='owned'?0:1);assert.equal(detachments,mode==='owned'?0:1);assert.equal(attached,mode==='owned');
 }
});

async function mainFixture({closeMode='pending',failSmoke=false,lateError=false,shutdownError=false,assurance=true}={}){
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'cq-lifecycle-unit-'));
 const app=new EventEmitter(),windows=[];let context,doneResolve;const done=new Promise(r=>{doneResolve=r;});
 Object.assign(app,{commandLine:{appendSwitch(){}},isPackaged:true,getVersion:()=> '0.2.0',getPath:()=>directory,getAppPath:()=>path.join(__dirname,'..'),requestSingleInstanceLock:()=>true,whenReady:()=>new Promise(()=>{}),getGPUInfo:async()=>({auxAttributes:{glRenderer:'synthetic'}}),exit:()=>doneResolve()});
 app.quit=()=>{if(lateError)windows[0].webContents.emit('console-message',{level:'error',message:'old page diagnostic'});app.emit('before-quit',{preventDefault(){}});if(vm.runInContext('shutdownComplete',context))doneResolve();};
 class Win extends EventEmitter{
  constructor(options){super();this.dead=false;this.options=options;this.contentSize=[0,0];windows.push(this);this.webContents=new EventEmitter();this.webContents.mainFrame={url:'http://127.0.0.1:3939/'};Object.assign(this.webContents,{getURL:()=>this.webContents.mainFrame.url,send(){},setWindowOpenHandler(){},session:{setPermissionRequestHandler(){}},executeJavaScript:async source=>source==='[innerWidth,innerHeight]'?this.contentSize.slice():source==='__cq.model.hash'?'a'.repeat(16):source.includes('readRendererContext')?{context:'webgl2',is_context_lost:false}:true});}
  // Explicit Mac work-area clamp model (1024x684 minus28px title bar), not native proof.
  setContentSize(width,height){this.contentSize=this.options.enableLargerThanScreen?[width,height]:[Math.min(width,1024),Math.min(height,656)];}
  getContentSize(){return this.contentSize.slice();}
  isDestroyed(){return this.dead;}getBounds(){return{x:0,y:0,width:1320,height:880};}show(){}focus(){}setEnabled(){}loadURL(){return Promise.resolve();}
  close(){if(closeMode==='veto')return;const closed=()=>{this.dead=true;this.emit('closed');};if(closeMode==='pending')setTimeout(closed,5);else closed();}
  destroy(){throw Error('destroy forbidden');}
 }
 const worker=Object.assign(new EventEmitter(),{pid:100,exitCode:null,signalCode:null});
 const req=name=>{
  if(name==='electron')return{app,BrowserWindow:Win,Menu:{},nativeTheme:{},ipcMain:{handle(){}},dialog:{showMessageBox:async()=>({response:2}),showErrorBox(){},showOpenDialog:async()=>({canceled:true,filePaths:[]}),showSaveDialog:async()=>({canceled:true})},shell:{showItemInFolder(){}},screen:{getDisplayMatching:()=>({bounds:{width:1920,height:1080},workArea:{width:1920,height:1080},scaleFactor:1})}};
  if(name==='./desktop.cjs')return require('../shell/desktop.cjs');
  if(name==='./renderer_config.cjs')return graphics;
  if(name==='./runtime.cjs')return{stopWorker:async()=>{worker.exitCode=0;if(shutdownError)throw Error('shutdown sentinel');}};
  if(name==='./smoke.cjs')return{...api,closeWindow:win=>api.closeWindow(win,closeMode==='veto'?30:1000),smoke:async(_w,_d,{evidence})=>{if(failSmoke)throw Error('primary smoke sentinel');evidence.pass=true;return evidence;}};
  if(name==='node:child_process')return{spawn:()=>{const secondary=Object.assign(new EventEmitter(),{pid:101,kill(){}});setImmediate(()=>{app.emit('second-instance');secondary.emit('exit',0);});return secondary;}};
  return require(name);
 };
 context=vm.createContext({require:req,console:{error(){}},AbortController,setTimeout,clearTimeout,setImmediate,process:{env:assurance?{CASSQUERY_SMOKE_DIR:directory}:{},platform:process.platform,arch:process.arch,versions:process.versions,argv:[],on(){},pid:99,resourcesPath:'/synthetic',execPath:'/synthetic/app',hrtime:process.hrtime},__dirname:path.join(__dirname,'../shell')});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../shell/main.js'),'utf8')+'\nglobalThis.audit={runNativeSmoke,openWindow,getWindow:()=>window,setWorker:w=>{worker=w;workerPort=3939;smokeStarted=true;tried.python="/synthetic/worker/cassquery-worker";}};',context);
 try{context.audit.setWorker(worker);await context.audit.openWindow();if(!assurance)return{report:null,windows};await context.audit.runNativeSmoke(context.audit.getWindow());await done;const report=JSON.parse(fs.readFileSync(path.join(directory,'renderer-raw.json'),'utf8'));return{report,windows};}
 finally{fs.rmSync(directory,{recursive:true,force:true});}
}
test('capture uses one absolute budget across dimensions and native CDP phases',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'cq-capture-budget-'));
 try{
  const bytes=Buffer.alloc(1200);Buffer.from('89504e470d0a1a0a','hex').copy(bytes);bytes.writeUInt32BE(100,16);bytes.writeUInt32BE(100,20);
  for(const [phaseMs,pass] of [[30,true],[65,false]]){
   let attached=false,detached=0;const file=path.join(directory,`phase-${phaseMs}.png`);
   const debuggerAPI={isAttached:()=>attached,attach:()=>{attached=true;},detach:()=>{attached=false;detached++;},sendCommand:async()=>{await sleep(phaseMs);return{data:bytes.toString('base64')};}};
    const win={webContents:{debugger:debuggerAPI,executeJavaScript:async expression=>{if(expression.includes('decodeVisibleImages'))return;await sleep(phaseMs);return{width:100,height:100};}}};
   if(pass){const image=await api.capture(win,file,1000);assert.equal(image.width,100);assert.equal(fs.existsSync(file),true);}
   else{await assert.rejects(api.capture(win,file,100),/timed out|deadline/);assert.equal(fs.existsSync(file),false);}
   assert.equal(attached,false);assert.equal(detached,1);
  }
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});

test('capture waits pending image decode but rejects broken rendered images without native screenshot output',async()=>{
 const code=fs.readFileSync(path.join(__dirname,'../shell/smoke.cjs'),'utf8');
 for(const mode of ['healthy','pending','broken','decode-rejected','hidden-broken']){
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'cq-image-capture-'));let decodeCalls=0,sendCalls=0,detached=0,attached=false;
  const image={src:'/brand.svg',currentSrc:'/brand.svg',complete:mode!=='pending',naturalWidth:mode==='broken'||mode==='hidden-broken'?0:100,naturalHeight:mode==='broken'||mode==='hidden-broken'?0:100,parentElement:null,
   getBoundingClientRect:()=>({width:40,height:40}),decode:async()=>{decodeCalls++;if(mode==='decode-rejected')throw Error('bad decoder');if(mode==='pending'){await sleep(15);image.complete=true;image.naturalWidth=image.naturalHeight=100;}}};
  const context=vm.createContext({require,process,setTimeout,clearTimeout,Buffer,module:{exports:{}},document:{images:[image]},getComputedStyle:()=>({visibility:'visible',display:mode==='hidden-broken'?'none':'block'})});
  vm.runInContext(code+'\nglobalThis.decodeVisibleImages=decodeVisibleImages;',context);
  const bytes=Buffer.alloc(1200);Buffer.from('89504e470d0a1a0a','hex').copy(bytes);bytes.writeUInt32BE(100,16);bytes.writeUInt32BE(100,20);
  const debuggerAPI={isAttached:()=>attached,attach:()=>{attached=true;},detach:()=>{attached=false;detached++;},sendCommand:async()=>{sendCalls++;return{data:bytes.toString('base64')};}};
  const win={webContents:{debugger:debuggerAPI,executeJavaScript:expression=>expression.includes('decodeVisibleImages')?context.decodeVisibleImages():Promise.resolve({width:100,height:100})}};
  const file=path.join(directory,'frame.png');
  try{
   if(mode==='broken'||mode==='decode-rejected'){await assert.rejects(context.module.exports.capture(win,file,100),/Image.*brand.svg/);assert.equal(sendCalls,0);assert.equal(fs.existsSync(file),false);}
   else{await context.module.exports.capture(win,file,1000);assert.equal(sendCalls,1);assert.equal(fs.existsSync(file),true);}
   assert.equal(attached,false);assert.equal(detached,1);if(mode==='broken'||mode==='hidden-broken')assert.equal(decodeCalls,0);
  }finally{fs.rmSync(directory,{recursive:true,force:true});}
 }
});

test('native input waits reachable geometry and rejects a persistent overlay/disabled target without sending input',async()=>{
 for(const mode of ['transient','overlay','disabled']){
  let reads=0;const events=[];
  const win={isDestroyed:()=>false,webContents:{executeJavaScript:async()=>({ready:mode==='transient'&&++reads>1,x:10,y:20,reason:mode}),sendInputEvent:e=>events.push(e)}};
  if(mode==='transient'){await api.click(win,'#target',1000);assert.equal(reads,2);assert.equal(events.length,3);}
  else{await assert.rejects(api.click(win,'#target',20),/^(?:Error: )?(?:Unreachable control: #target: |Renderer execution (?:exceeded its deadline|timed out)$)/);assert.equal(events.length,0);}
 }
});
test('renderer readiness waits actual stage/canvas dimensions and camera aspect, not viewport alone',async()=>{
 for(const mode of ['transient','stale']){
  let reads=0;const win={isDestroyed:()=>false,webContents:{executeJavaScript:async source=>{assert.ok(source.includes('canvas.width'));assert.ok(source.includes('camera.aspect'));return{ready:mode==='transient'&&++reads>1,viewport:[450,700],stage:[0,0,450,364],canvas:[0,0,1024,773,1024,773]};}}};
  if(mode==='transient'){await api.waitRendererLayout(win,1000);assert.equal(reads,2);}
  else await assert.rejects(api.waitRendererLayout(win,20),/(?:layout timed out|exceeded its deadline).*1024/);
 }
});

test('executed constructor enables larger-than-screen only for explicit assurance, including reopen',async()=>{
 const{windows}=await mainFixture();assert.equal(windows.length,2);
 for(const win of windows){assert.equal(win.options.enableLargerThanScreen,true);assert.equal(win.options.webPreferences.sandbox,true);assert.equal(win.options.webPreferences.contextIsolation,true);assert.equal(win.options.webPreferences.nodeIntegration,false);}
 const normal=await mainFixture({assurance:false});assert.equal(normal.windows.length,1);assert.equal(normal.windows[0].options.enableLargerThanScreen,false);
});
test('Mac work-area model preserves actual requested matrix content sizes without viewport reduction',async()=>{
 const normal=await mainFixture({assurance:false}),clamped=normal.windows[0];clamped.setContentSize(450,700);
 assert.deepEqual(clamped.getContentSize(),[450,656]);assert.deepEqual(await clamped.webContents.executeJavaScript('[innerWidth,innerHeight]'),[450,656]);
 assert.throws(()=>assert.deepEqual(clamped.getContentSize(),[450,700]),'original clamp must remain a failed700px gate');
 const{windows}=await mainFixture();
 for(const win of windows)for(const width of [450,700,1000,1320]){win.setContentSize(width,700);assert.deepEqual(win.getContentSize(),[width,700]);assert.deepEqual(await win.webContents.executeJavaScript('[innerWidth,innerHeight]'),[width,700]);}
});

test('executed main waits delayed close and activates a new window with the same worker',async()=>{
 const{report,windows}=await mainFixture();assert.equal(report.pass,true);assert.equal(report.lifecycle.pass,true);assert.equal(report.lifecycle.worker_pid_before,100);assert.equal(report.lifecycle.worker_pid_after,100);assert.equal(windows.length,2);assert.equal(windows[0].isDestroyed(),true);assert.equal(report.worker_shutdown,true);assert.ok(report.native_source.fingerprint);
});
test('executed main veto stays failed and cannot be repaired by destroy',async()=>{
 const{report,windows}=await mainFixture({closeMode:'veto'});assert.equal(report.pass,false);assert.match(report.error,/Native window close timed out/);assert.equal(windows.length,1);assert.equal(windows[0].isDestroyed(),false);
});
test('executed main preserves completed lifecycle, first error and diagnostics across page regeneration/shutdown',async()=>{
 const{report,windows}=await mainFixture({failSmoke:true,lateError:true,shutdownError:true});assert.equal(report.pass,false);assert.equal(report.lifecycle.pass,true);assert.match(report.error,/primary smoke sentinel/);assert.deepEqual(report.errors,['old page diagnostic']);assert.match(report.shutdown_error,/shutdown sentinel/);assert.equal(report.worker_shutdown,false);assert.equal(windows[0].cassqueryRendererErrors,windows[1].cassqueryRendererErrors);
});
