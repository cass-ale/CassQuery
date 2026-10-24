'use strict';
// Supplemental native Electron assurance. Never claims packaged Mac, CAD, ZIP or DMG proof.
// Run with Electron, not Node: electron tools/native_lifecycle_probe.cjs --output=/outside/source/path
const {app,BrowserWindow,screen}=require('electron');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),crypto=require('node:crypto'),zlib=require('node:zlib');
const assert=require('node:assert/strict');
const {bounded,closeWindow,waitFor,evaluate,capture,checkpoint}=require('../shell/smoke.cjs');
const {configureRenderer,readRendererContext,recordRendererConsole}=require('../shell/renderer_config.cjs');
const output=path.resolve(process.argv.find(s=>s.startsWith('--output='))?.slice(9)||'evidence/native-lifecycle');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'user'));
const requested=configureRenderer(app.commandLine,process.platform,process.env);
const source=()=>['main.js','renderer_config.cjs','runtime.cjs','smoke.cjs'].map(name=>{const bytes=fs.readFileSync(path.join(__dirname,'../shell',name));return{bytes:bytes.length,path:'shell/'+name,sha256:crypto.createHash('sha256').update(bytes).digest('hex')};});
const files=source(),fingerprint=crypto.createHash('sha256').update(JSON.stringify({files})).digest('hex');
const probeBytes=fs.readFileSync(__filename);
const report={schema:1,pass:false,probe_source:{bytes:probeBytes.length,path:'tools/native_lifecycle_probe.cjs',sha256:crypto.createHash('sha256').update(probeBytes).digest('hex')},app_pid:process.pid,sandbox_requested:true,started:checkpoint('probe-start'),source_fingerprint:fingerprint,source_files:files,
 runtime:{electron:process.versions.electron,chromium:process.versions.chrome,node:process.versions.node,platform:process.platform,architecture:process.arch},
 packaged:app.isPackaged,backend_requested:requested,checks:[],images:[],errors:[],
 not_checked:['packaged Mac app','warm worker/Dock activation','full CAD/viewer matrix','final ZIP/DMG','spontaneous CDP transport failure (ack-loss case is injected)']};
const windows=new Set(),servers=new Set();let destroyAttempts=0;
const html='data:text/html,'+encodeURIComponent('<!doctype html><style>body{margin:0;background:linear-gradient(90deg,#123,#abc)}canvas{display:block}</style><main id="stage"><canvas width="500" height="360"></canvas></main><script>const gl=document.querySelector("canvas").getContext("webgl2");if(!gl)throw Error("WebGL2 unavailable");gl.clearColor(.1,.3,.5,1);gl.clear(gl.COLOR_BUFFER_BIT);</script>');
function window(errors=report.errors){const win=new BrowserWindow({width:640,height:480,show:true,webPreferences:{sandbox:true,nodeIntegration:false,contextIsolation:true}});
 windows.add(win);win.once('closed',()=>windows.delete(win));win.destroy=()=>{destroyAttempts++;throw Error('destroy bypass forbidden');};
 win.cassqueryRendererErrors=errors;win.webContents.on('console-message',e=>recordRendererConsole(errors,e));
 win.webContents.on('render-process-gone',(_e,d)=>errors.push('renderer gone: '+d.reason));return win;}
async function check(name,kind,run){const started=checkpoint(name+'-start');try{const observations=await run();report.checks.push({name,kind,pass:true,started,finished:checkpoint(name+'-complete'),observations});}catch(e){report.checks.push({name,kind,pass:false,started,error:e.stack});throw e;}}
function verifyPNG(file){const bytes=fs.readFileSync(file);assert.equal(bytes.subarray(0,8).toString('hex'),'89504e470d0a1a0a');let offset=8,idat=[];
 while(offset<bytes.length){const count=bytes.readUInt32BE(offset),end=offset+12+count;assert.ok(end<=bytes.length);const kind=bytes.toString('ascii',offset+4,offset+8);assert.equal(zlib.crc32(bytes.subarray(offset+4,end-4)),bytes.readUInt32BE(end-4));if(kind==='IDAT')idat.push(bytes.subarray(offset+8,end-4));offset=end;}
 const width=bytes.readUInt32BE(16),height=bytes.readUInt32BE(20),channels=bytes[25]===6?4:bytes[25]===2?3:0;assert.equal(bytes[24],8);assert.ok(channels&&idat.length);assert.equal(bytes[28],0);
 assert.equal(zlib.inflateSync(Buffer.concat(idat),{maxOutputLength:height*(width*channels+1)}).length,height*(width*channels+1));return{width,height};}
app.on('window-all-closed',()=>{});
app.whenReady().then(async()=>{
 assert.equal(process.versions.electron,'45.0.0','probe requires the pinned Electron runtime');
 assert.equal(app.commandLine.hasSwitch('no-sandbox'),false,'native probe cannot disable sandboxing');
 await check('actual graphics and native screenshot','native',async()=>{const win=window();await win.loadURL(html);const display=screen.getDisplayMatching(win.getBounds());
  report.graphics={runtime:report.runtime,display:{bounds:display.bounds,work_area:display.workArea,scale_factor:display.scaleFactor,window_bounds:win.getBounds()},gpu:await bounded(()=>app.getGPUInfo('complete'),5000,'GPU info'),webgl:await evaluate(win,`(${readRendererContext.toString()})()`)};
  if(requested==='swiftshader-requested')assert.match(report.graphics.webgl.unmasked_renderer||'',/SwiftShader/i);
  const image=await capture(win,path.join(output,'native-frame.png'));assert.deepEqual(verifyPNG(image.path),{width:image.width,height:image.height});report.images.push(image);
  await closeWindow(win,2000);return{actual_renderer:report.graphics.webgl.unmasked_renderer,image};});
 await check('normal asynchronous close completes without destroy','native',async()=>{const win=window();await win.loadURL(html);const events=[];win.on('close',()=>events.push('close'));win.on('closed',()=>events.push('closed'));
  const nativeClose=win.close.bind(win);let destroyed_immediately_after_close;
  win.close=()=>{nativeClose();destroyed_immediately_after_close=win.isDestroyed();};
  await closeWindow(win,2000);assert.equal(win.isDestroyed(),true);assert.equal(typeof destroyed_immediately_after_close,'boolean');return{destroyed_immediately_after_close,destroyed_after_closed:true,events};});
 await check('veto remains failed and recovery is normal close','native',async()=>{const win=window();await win.loadURL(html);const veto=e=>e.preventDefault();win.on('close',veto);win.probeVeto=veto;
  await assert.rejects(closeWindow(win,100),/timed out/);const destroyed_after_veto=win.isDestroyed();assert.equal(destroyed_after_veto,false);win.removeListener('close',veto);await closeWindow(win,2000);return{destroyed_after_veto,recovered_by_normal_close:true};});
 await check('beforeunload veto remains failed','native',async()=>{const win=window();await win.loadURL(html);win.focus();win.webContents.sendInputEvent({type:'mouseDown',x:100,y:100,button:'left',clickCount:1});win.webContents.sendInputEvent({type:'mouseUp',x:100,y:100,button:'left',clickCount:1});
  await waitFor(win,'navigator.userActivation.hasBeenActive',2000);await evaluate(win,'window.__probeBeforeUnload=e=>{e.preventDefault();e.returnValue=false;return false;};window.addEventListener("beforeunload",window.__probeBeforeUnload)');
  await assert.rejects(closeWindow(win,100),/timed out/);const destroyed_after_veto=win.isDestroyed();assert.equal(destroyed_after_veto,false);await evaluate(win,'window.removeEventListener("beforeunload",window.__probeBeforeUnload)');await closeWindow(win,2000);return{destroyed_after_veto,recovered_by_normal_close:true};});
 await check('held main load bounds execution and capture; native debugger detaches','native',async()=>{let response,arrived;const received=new Promise(r=>{arrived=r;});
  const server=http.createServer((req,res)=>{if(req.url!=='/'){res.writeHead(404);res.end();return;}response=res;res.writeHead(200,{'Content-Type':'text/html'});res.write('<html>');arrived();});servers.add(server);await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const win=window(),loading=win.loadURL('http://127.0.0.1:'+server.address().port+'/').catch(()=>{});await bounded(()=>received,2000,'held response request');
  const started=Date.now();await assert.rejects(waitFor(win,'true',100),/timed out|deadline/);const elapsed_ms=Date.now()-started;assert.ok(elapsed_ms<1000);
  await assert.rejects(capture(win,path.join(output,'never-written.png'),100),/timed out|deadline/);assert.equal(win.webContents.debugger.isAttached(),false);assert.equal(fs.existsSync(path.join(output,'never-written.png')),false);
  response.end('</html>');await bounded(()=>loading,3000,'released main load');await closeWindow(win,2000);server.closeAllConnections();await new Promise(r=>server.close(r));servers.delete(server);return{requested_ms:100,elapsed_ms,debugger_attached_after_failure:false};});
 await check('lost screenshot acknowledgment releases actual debugger ownership','native-with-injected-ack-loss',async()=>{const win=window();await win.loadURL(html);const debug=win.webContents.debugger,original=debug.sendCommand,send=original.bind(debug);let completed=false;
  debug.sendCommand=(command,args)=>send(command,args).then(result=>{if(command==='Page.captureScreenshot'){completed=true;return new Promise(()=>{});}return result;});
  try{await assert.rejects(capture(win,path.join(output,'lost-ack.png'),500),/timed out|deadline/);assert.equal(debug.isAttached(),false);assert.equal(completed,true);assert.equal(fs.existsSync(path.join(output,'lost-ack.png')),false);}
  finally{debug.sendCommand=original;await closeWindow(win,2000);}return{injection:'suppress promise acknowledgment after real native Page.captureScreenshot',underlying_native_command_completed:completed,debugger_attached_after_failure:false};});
 await check('error persists once page regenerates and readiness remains failed','native',async()=>{const errors=[],win=window(errors);await win.loadURL(html);await evaluate(win,'console.error("probe diagnostic sentinel")');
  await bounded(async()=>{while(!errors.length)await new Promise(r=>setTimeout(r,10));},1000,'native console event');await win.loadURL(html);await assert.rejects(waitFor(win,'true',100),/probe diagnostic sentinel/);assert.deepEqual(errors,['probe diagnostic sentinel']);await closeWindow(win,2000);return{errors_after_regeneration:errors};});
 assert.deepEqual(report.errors,[]);assert.equal(destroyAttempts,0);assert.deepEqual(source(),files);report.pass=true;
}).catch(e=>{report.pass=false;report.error=e.stack||String(e);}).finally(async()=>{
 for(const server of servers){server.closeAllConnections();await new Promise(r=>server.close(r));}
 for(const win of windows){try{if(win.probeVeto)win.removeListener('close',win.probeVeto);await evaluate(win,'if(window.__probeBeforeUnload)window.removeEventListener("beforeunload",window.__probeBeforeUnload)',500);await closeWindow(win,2000);}catch(e){report.pass=false;(report.cleanup_errors||=[]).push(e.message);}}
 report.destroy_attempts=destroyAttempts;report.remaining_windows=windows.size;report.finished=checkpoint('probe-complete');if(windows.size)report.pass=false;
 fs.writeFileSync(path.join(output,'native-lifecycle.json'),JSON.stringify(report,null,2)+'\n');if(report.pass)app.quit();else app.exit(1);
});
