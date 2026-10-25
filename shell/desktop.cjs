'use strict';
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');

const LIMIT = 4 * 1024 * 1024;
const THEMES = ['system','counterform-dark','counterform-light','graphite','midnight','forest','sand','paper','high-contrast'];
const DEFAULTS = Object.freeze({ schema:1, theme:'system', editorFont:'system-mono', editorFontSize:14, tabSize:4,
  lineWrap:false, editorVisible:true, autoBuild:false, reducedMotion:'system', updateCheck:'enabled',
  updateAutoDownload:false, updateChannel:'stable', recentProjects:[] });
function requireValue(value, message, code='INVALID_INPUT') {
  if (!value) { const error = new Error(message); error.code = code; throw error; }
}
function object(value) { return value && typeof value === 'object' && !Array.isArray(value); }
function text(value, max=LIMIT) {
  return typeof value === 'string' && Buffer.byteLength(value,'utf8') <= max && Buffer.from(value,'utf8').toString('utf8') === value;
}
function relative(value) {
  return text(value,4096) && value && !path.isAbsolute(value) && !value.includes('\\') && !value.includes('\0') &&
    value.split('/').every(part => part && part !== '.' && part !== '..');
}
function settings(value) {
  requireValue(object(value) && Object.keys(value).every(key => Object.hasOwn(DEFAULTS,key)), 'Unknown settings field');
  const result = { ...DEFAULTS, ...value };
  requireValue(result.schema===1 && THEMES.includes(result.theme), 'Invalid settings schema/theme');
  requireValue(['system-mono','menlo','sf-mono','consolas','monospace'].includes(result.editorFont), 'Invalid editor font');
  requireValue(Number.isInteger(result.editorFontSize) && result.editorFontSize>=12 && result.editorFontSize<=24 && [2,4].includes(result.tabSize), 'Invalid editor size/tab size');
  for (const key of ['lineWrap','editorVisible','autoBuild','updateAutoDownload']) requireValue(typeof result[key]==='boolean', `Invalid ${key}`);
  requireValue(['system','on'].includes(result.reducedMotion) && ['enabled','manual'].includes(result.updateCheck) && result.updateChannel==='stable', 'Invalid motion/update settings');
  requireValue(Array.isArray(result.recentProjects) && result.recentProjects.length<=10 && new Set(result.recentProjects).size===result.recentProjects.length &&
    result.recentProjects.every(item => text(item,4096) && path.isAbsolute(item) && !item.includes('\0')), 'Invalid recent projects');
  return result;
}
function draft(value) {
  requireValue(object(value) && Object.keys(value).every(key => ['root','project','path','text','base_sha256','selection','scroll','updatedAt'].includes(key)), 'Invalid recovery record');
  requireValue(text(value.root,4096) && path.isAbsolute(value.root) && path.resolve(value.root)===value.root && !value.root.includes('\0'),'Invalid canonical recovery root');
  requireValue(text(value.project,200) && value.project && !value.project.includes('\0') && relative(value.path) && text(value.text,2*1024*1024), 'Invalid recovery document');
  requireValue(value.base_sha256===null || /^[a-f0-9]{64}$/.test(value.base_sha256), 'Invalid recovery base hash');
  const selection = value.selection || {start:0,end:0};
  requireValue(object(selection) && Object.keys(selection).every(key => ['start','end'].includes(key)) &&
    Number.isInteger(selection.start) && Number.isInteger(selection.end) && selection.start>=0 && selection.end>=selection.start && selection.end<=value.text.length, 'Invalid recovery selection');
  const scroll = value.scroll || {top:0,left:0};
  requireValue(object(scroll) && Object.keys(scroll).every(key=>['top','left'].includes(key)) && [scroll.top,scroll.left].every(n=>Number.isFinite(n)&&n>=0&&n<=1e9), 'Invalid recovery scroll');
  return { root:value.root, project:value.project, path:value.path, text:value.text, base_sha256:value.base_sha256, selection, scroll, updatedAt:Date.now() };
}
function createStores(directory) {
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const root = fs.realpathSync(directory), identity=fs.lstatSync(fs.realpathSync(directory));
  function anchored() {
    const now=fs.lstatSync(root);
    requireValue(now.isDirectory()&&!now.isSymbolicLink()&&fs.realpathSync(root)===root&&now.dev===identity.dev&&now.ino===identity.ino,'Desktop state root changed; original and external files retained','INVALID_STATE');
  }
  const names = {settings:path.join(root,'workbench-settings.json'), recovery:path.join(root,'workbench-recovery.json')};
  function read(kind, fallback) {
    anchored();
    const file = names[kind];
    if (!fs.existsSync(file)) return fallback;
    requireValue(!fs.lstatSync(file).isSymbolicLink() && fs.statSync(file).isFile() && fs.statSync(file).size<=LIMIT, 'Unsafe/oversized desktop state','INVALID_STATE');
    try { const value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(fs.readFileSync(file)));anchored();return value; }
    catch { throw Object.assign(new Error('Invalid desktop state; original file retained'),{code:'INVALID_STATE'}); }
  }
  function write(kind, value) {
    anchored();
    const bytes = Buffer.from(JSON.stringify(value));
    requireValue(bytes.length<=LIMIT, 'Desktop state exceeds limit');
    const file = names[kind];
    requireValue(!fs.existsSync(file) || !fs.lstatSync(file).isSymbolicLink(), 'Desktop state symlink refused','INVALID_STATE');
    const tmp = `${file}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
    let fd;
    try { fd=fs.openSync(tmp,'wx',0o600);fs.writeFileSync(fd,bytes);fs.fsyncSync(fd);fs.closeSync(fd);fd=undefined;anchored();fs.renameSync(tmp,file); }
    finally { if(fd!==undefined)fs.closeSync(fd);const now=fs.lstatSync(root);if(!now.isSymbolicLink()&&now.dev===identity.dev&&now.ino===identity.ino&&fs.realpathSync(root)===root)fs.rmSync(tmp,{force:true}); }
    return value;
  }
  function recover() {
    const value = read('recovery',{schema:1,drafts:[]});
    requireValue(object(value) && value.schema===1 && Array.isArray(value.drafts) && value.drafts.length<=10, 'Invalid recovery store','INVALID_STATE');
    return {schema:1,drafts:value.drafts.map(item => ({...draft(item),updatedAt:item.updatedAt}))};
  }
  return {
    getSettings:()=>settings(read('settings',DEFAULTS)),
    patchSettings:patch=>{requireValue(object(patch), 'Invalid settings patch');return write('settings',settings({...settings(read('settings',DEFAULTS)),...patch}));},
    getRecovery:()=>recover(),
    saveRecovery:value=>{const item=draft(value),valueNow=recover();const list=valueNow.drafts.filter(old=>old.root!==item.root || old.project!==item.project || old.path!==item.path);requireValue(list.length<10,'Too many recovered drafts');return write('recovery',{schema:1,drafts:[...list,item]});},
    deleteRecovery:value=>{requireValue(object(value)&&text(value.root,4096)&&path.isAbsolute(value.root)&&text(value.project,200)&&relative(value.path),'Invalid recovery identity');return write('recovery',{schema:1,drafts:recover().drafts.filter(old=>old.root!==value.root||old.project!==value.project||old.path!==value.path)});},
  };
}
function authorize(event, win, port) {
  requireValue(win && !win.isDestroyed() && event.sender===win.webContents && event.senderFrame && event.senderFrame===win.webContents.mainFrame, 'Unowned window/frame','FORBIDDEN');
  requireValue(Number.isInteger(port) && port>0 && port<65536, 'Worker unavailable','WORKER_UNAVAILABLE');
  const expected = `http://127.0.0.1:${port}`;
  try { const frame=new URL(event.senderFrame.url),page=new URL(win.webContents.getURL());requireValue([frame,page].every(url=>url.origin===expected&&!url.username&&!url.password&&['/','/index.html'].includes(url.pathname)), 'Untrusted worker origin','FORBIDDEN'); }
  catch { throw Object.assign(new Error('Untrusted worker origin'),{code:'FORBIDDEN'}); }
}
function conflictDetails(value, request) {
  const nested=object(value.details)?value.details:{}, result={};
  const allowed=['schema','path','phase','active_file_changed','active_revision','draft_sha256','recovery_paths','journal_error','output_path'];
  for(const key of allowed){if(Object.hasOwn(value,key))result[key]=value[key];else if(Object.hasOwn(nested,key))result[key]=nested[key];}
  if(Object.hasOwn(result,'output_path'))requireValue(request.action==='export'&&text(result.output_path,4096)&&path.isAbsolute(result.output_path)&&!result.output_path.includes('\0')&&result.output_path===request.path,'Invalid native export recovery destination','INVALID_RESPONSE');
  if(!Object.keys(result).length)return null;
  if(Object.hasOwn(result,'schema'))requireValue(result.schema===1,'Invalid conflict schema','INVALID_RESPONSE');
  if(Object.hasOwn(result,'path'))requireValue(result.path===null||relative(result.path),'Invalid conflict source path','INVALID_RESPONSE');
  if(Object.hasOwn(result,'phase'))requireValue(['prepared','exchanged','conflict'].includes(result.phase),'Invalid conflict phase','INVALID_RESPONSE');
  if(Object.hasOwn(result,'active_file_changed'))requireValue(typeof result.active_file_changed==='boolean','Invalid conflict change flag','INVALID_RESPONSE');
  if(Object.hasOwn(result,'draft_sha256'))requireValue(/^[a-f0-9]{64}$/.test(result.draft_sha256),'Invalid conflict draft hash','INVALID_RESPONSE');
  const revision=(record,hasPath)=>{
    requireValue(object(record)&&Object.keys(record).every(key=>['path','bytes','sha256','kind'].includes(key)),'Invalid recovery revision','INVALID_RESPONSE');
    if(hasPath)requireValue(result.output_path?(text(record.path,4096)&&path.isAbsolute(record.path)&&!record.path.includes('\0')&&record.path!==result.output_path&&path.dirname(record.path)===path.dirname(result.output_path)):relative(record.path),'Invalid recovery path','INVALID_RESPONSE');
    requireValue(record.sha256===null||/^[a-f0-9]{64}$/.test(record.sha256),'Invalid recovery hash','INVALID_RESPONSE');
    requireValue(record.bytes===null||(Number.isSafeInteger(record.bytes)&&record.bytes>=0),'Invalid recovery size','INVALID_RESPONSE');
    if(Object.hasOwn(record,'kind'))requireValue(['regular','symlink','missing','nonregular'].includes(record.kind),'Invalid recovery kind','INVALID_RESPONSE');
    return {...record};
  };
  if(Object.hasOwn(result,'active_revision'))result.active_revision=revision(result.active_revision,false);
  if(Object.hasOwn(result,'recovery_paths')){requireValue(Array.isArray(result.recovery_paths)&&result.recovery_paths.length<=100,'Invalid recovery inventory','INVALID_RESPONSE');result.recovery_paths=result.recovery_paths.map(item=>revision(item,true));}
  if(Object.hasOwn(result,'journal_error'))requireValue(text(result.journal_error,4096),'Invalid conflict journal diagnostic','INVALID_RESPONSE');
  return result;
}
function postWorkbench(port, token, payload, timeoutMs=30000) {
  requireValue(Number.isInteger(port)&&port>0&&port<65536 && /^[a-f0-9]{64}$/.test(token), 'Invalid private worker capability');
  const bytes=Buffer.from(JSON.stringify(payload));requireValue(bytes.length<=LIMIT,'Workbench request exceeds limit');
  return new Promise((resolve,reject)=>{
    let timer,settled=false;
    const finish=(error,value)=>{if(settled)return;settled=true;clearTimeout(timer);if(error)reject(error);else resolve(value);};
    const req=http.request({hostname:'127.0.0.1',port,path:'/api/workbench',method:'POST',headers:{'Content-Type':'application/json','Content-Length':bytes.length,'X-CassQuery-Workbench':token}},res=>{
      const chunks=[];let size=0;
      res.on('data',chunk=>{size+=chunk.length;if(size>LIMIT){res.destroy();req.destroy();finish(Object.assign(new Error('Workbench response exceeds limit'),{code:'RESPONSE_LIMIT'}));}else chunks.push(chunk);});
      res.on('error',error=>finish(error));
      res.on('end',()=>{try{const raw=Buffer.concat(chunks).toString('utf8');requireValue(!raw.includes(token),'Worker returned private capability','PRIVATE_CAPABILITY');let value;try{value=JSON.parse(raw);}catch{throw Object.assign(new Error('Invalid worker JSON'),{code:'INVALID_RESPONSE'});}requireValue(object(value),'Invalid worker JSON');requireValue(!JSON.stringify(value).includes(token),'Worker returned private capability','PRIVATE_CAPABILITY');
        if(res.statusCode!==200){const error=new Error(typeof value.error==='string'?value.error:'Workbench request failed');error.code=value.code||'WORKBENCH_ERROR';error.status=res.statusCode;const details=conflictDetails(value,payload);if(details)error.details=details;throw error;}finish(null,value);
      }catch(error){finish(error);}});
    });
    req.on('error',error=>finish(error));timer=setTimeout(()=>{req.destroy();finish(Object.assign(new Error('Workbench request timed out'),{code:'TIMEOUT'}));},timeoutMs);req.end(bytes);
  });
}
const WORKER_ACTIONS = ['list','read','save','transform','parameters','setParameter','inspect'];
function createDesktop({ipcMain,dialog,getWindow,getPort,token,userData,update,onSettings}) {
  const stores=createStores(userData),states=new WeakMap(),roots=new Map();let active=0;
  const state=win=>{if(!states.has(win))states.set(win,{dirty:false,pending:null,decision:null,epoch:0,revision:0});return states.get(win);};
  function command(name,payload={}) {const win=getWindow();if(win&&!win.isDestroyed())win.webContents.send('cassquery:command',{command:name,...payload});}
  function selected(value) {requireValue(text(value,4096)&&path.isAbsolute(value)&&!value.includes('\0'),'Invalid native-selected path');return value;}
  function selectedDestination(value) {
    const chosen=selected(value),parent=fs.realpathSync(path.dirname(chosen));
    requireValue(fs.statSync(parent).isDirectory(),'Native-selected destination parent is not a directory');
    // Resolve only the existing parent: the destination may not exist, and a leaf symlink remains for the worker to reject.
    return path.join(parent,path.basename(chosen));
  }
  function current(win,epoch) {
    requireValue(win&&win===getWindow()&&!win.isDestroyed()&&state(win).epoch===epoch, 'Window changed during desktop action','FORBIDDEN');
    try {requireValue(new URL(win.webContents.getURL()).origin===`http://127.0.0.1:${getPort()}`, 'Worker origin changed','FORBIDDEN');}
    catch {throw Object.assign(new Error('Worker origin changed'),{code:'FORBIDDEN'});}
  }
  async function trust(win) {const choice=await dialog.showMessageBox(win,{type:'warning',buttons:['Trust and Open','Cancel'],defaultId:1,cancelId:1,message:'This project executes local Python.',detail:'Only open Python projects you trust. Imported geometry does not recover parametric history.'});return choice.response===0;}
  async function workbench(action,payload={}) {
    requireValue(text(action,64)&&object(payload)&&!Object.hasOwn(payload,'action'),'Invalid workbench action/payload');
    const win=getWindow(),epoch=state(win).epoch;current(win,epoch);let revision=state(win).revision,body={...payload,action};
    if(WORKER_ACTIONS.includes(action)) {
      const fields={list:['project'],inspect:['project','variant'],read:['project','path'],save:['project','path','text','expected_sha256'],transform:['project','part','translation','rotation','scale','expected_sha256'],parameters:['project','path'],setParameter:['project','path','name','value','expected_sha256']};
      requireValue(Object.keys(payload).every(key=>fields[action].includes(key)), 'Unexpected workbench field');
      requireValue(text(payload.project,200)&&payload.project&&!payload.project.includes('\0'),'Invalid project identity');
      if(['read','save','parameters','setParameter'].includes(action))requireValue(relative(payload.path),'Invalid project-relative file');
      if(action==='save')requireValue(text(payload.text,2*1024*1024),'Invalid source text');
      if(action==='save'||action==='setParameter')body.auto_build=stores.getSettings().autoBuild;
    } else if(action==='resolveConflict') {
      requireValue(Object.keys(payload).every(key=>['project','path','text','expected_sha256'].includes(key))&&relative(payload.path)&&text(payload.text,2*1024*1024), 'Invalid conflict resolution');
      const choice=await dialog.showMessageBox(win,{type:'warning',buttons:['Save selected revision','Cancel'],defaultId:1,cancelId:1,message:'Resolve the source conflict with this text?',detail:'The chosen revision becomes the active file. Retained recovery copies will not be deleted.'});
      if(choice.response!==0)return{cancelled:true};
      body={...payload,action:'save',resolve_conflict:true,auto_build:stores.getSettings().autoBuild};
    } else if(['open','openFile','openFolder','create','import','export','saveAs'].includes(action)) {
      if(['open','openFile','openFolder','create','import'].includes(action) && !await guard(win))return{cancelled:true};
      revision=state(win).revision;
      if(action==='export')requireValue(!state(win).dirty,'Save the draft before exporting','DIRTY_DRAFT');
      const fields={open:['kind'],openFile:[],openFolder:[],create:['name'],import:['name'],export:['project','format','variant','parts'],saveAs:['project','path','text','expected_sha256']};
      requireValue(Object.keys(payload).every(key=>fields[action].includes(key)), 'Unexpected native dialog field');
      if(action==='saveAs')requireValue(relative(payload.path)&&text(payload.text,2*1024*1024),'Invalid Python copy source');
      requireValue(!Object.keys(payload).some(key=>['destination','directory','overwrite','allow_overwrite'].includes(key)) && (action==='saveAs'||!Object.hasOwn(payload,'path')), 'Renderer cannot choose native destination');
      if(['open','openFile','openFolder'].includes(action)) {
        const folder=action==='openFolder'||payload.kind==='folder';const result=await dialog.showOpenDialog(win,{properties:[folder?'openDirectory':'openFile'],filters:folder?[]:[{name:'CadQuery or STEP',extensions:['py','step','stp']}]});
        if(result.canceled||!result.filePaths.length)return{cancelled:true};if(!await trust(win))return{cancelled:true};body={action:'open',path:selected(result.filePaths[0])};
      } else if(action==='create'||action==='import') {
        requireValue(text(payload.name,80)&&/^[\p{L}\p{N} _-]+$/u.test(payload.name),'Invalid project name');
        let input;
        if(action==='import'){const pick=await dialog.showOpenDialog(win,{properties:['openFile'],filters:[{name:'STEP geometry',extensions:['step','stp']}]});if(pick.canceled||!pick.filePaths.length)return{cancelled:true};input=selected(pick.filePaths[0]);}
        const pick=await dialog.showOpenDialog(win,{properties:['openDirectory','createDirectory']});if(pick.canceled||!pick.filePaths.length)return{cancelled:true};if(!await trust(win))return{cancelled:true};body={action,name:payload.name,directory:selected(pick.filePaths[0]),...(input?{path:input}:{})};
      } else {
        if(action==='export')requireValue(['step','stp','stl','brep','zip','py'].includes(payload.format),'Unsupported export format');
        const extension=action==='saveAs'?'py':payload.format;
        const pick=await dialog.showSaveDialog(win,{defaultPath:`CassQuery.${extension}`,filters:[{name:action==='saveAs'?'Python copy':'Export',extensions:[extension]}],properties:['showOverwriteConfirmation','createDirectory']});
        if(pick.canceled||!pick.filePath)return{cancelled:true};const destination=selectedDestination(pick.filePath);body={...payload,action,...(action==='saveAs'?{destination,overwrite:true}:{path:destination,overwrite:true})};
      }
    } else throw Object.assign(new Error('Unsupported workbench action'),{code:'INVALID_ACTION'});
    current(win,epoch);
    if(['open','openFile','openFolder','create','import','export','saveAs','resolveConflict'].includes(action))requireValue(state(win).revision===revision,'Draft changed during native selection','DRAFT_CHANGED');
    if(action==='export')requireValue(!state(win).dirty,'Save the draft before exporting','DIRTY_DRAFT');
    const result=await postWorkbench(getPort(),token,body).catch(async error=>{
      if(body.action!=='open'||error.code!=='ADAPTER_REQUIRED')throw error;
      const choice=await dialog.showMessageBox(win,{type:'warning',buttons:['Create project sidecars','Cancel'],defaultId:1,cancelId:1,message:'Create an editable project beside this Python file?',detail:'A new viewer.json and adapter will be created. Existing source and helper files will not be overwritten.'});
      if(choice.response!==0)return{cancelled:true};
      current(win,epoch);requireValue(state(win).revision===revision,'Draft changed during sidecar confirmation','DRAFT_CHANGED');
      return postWorkbench(getPort(),token,{...body,adapt:true});
    });
    current(win,epoch);
    if(typeof result.project==='string' && typeof result.root==='string'){
      const root=selected(result.root);requireValue(fs.realpathSync(root)===root,'Worker project root is not canonical');roots.set(result.project,root);
    }
    if(['save','transform','setParameter'].includes(body.action) && !result.error){const pending=state(win).pending;if(pending&&pending.kind==='save'&&state(win).revision===revision)pending.savedRevision=revision;}
    if(body.action==='open' && !result.error && !result.cancelled){const prefs=stores.getSettings();const changed=stores.patchSettings({recentProjects:[body.path,...prefs.recentProjects.filter(p=>p!==body.path)].slice(0,10)});if(onSettings)await onSettings(changed);}
    return result;
  }
  async function guard(win) {
    if(!win||win.isDestroyed()||!state(win).dirty)return true;
    const s=state(win),epoch=s.epoch,revision=s.revision;if(s.decision)return s.decision;
    s.decision=(async()=>{const pick=await dialog.showMessageBox(win,{type:'warning',message:'Save unsaved changes?',buttons:['Save','Discard','Cancel'],defaultId:0,cancelId:2});requireValue([0,1,2].includes(pick.response),'Invalid native draft decision');if(pick.response===2)return false;current(win,epoch);if(s.revision!==revision)return false;
      const kind=pick.response===0?'save':'discard';return new Promise(resolve=>{const timer=setTimeout(()=>{s.pending=null;resolve(false);},30000);s.pending={kind,revision:s.revision,savedRevision:null,epoch:s.epoch,resolve:()=>{clearTimeout(timer);s.pending=null;resolve(true);},cancel:()=>{clearTimeout(timer);s.pending=null;resolve(false);}};command(kind,{reason:'close'});});
    })().finally(()=>{s.decision=null;});return s.decision;
  }
  function register() {
    const handlers={
      workbench:async(_event,action,payload)=>{active++;try{return await workbench(action,payload);}finally{active--; }},
      settings:async(_event,action,value)=>{if(action==='get')return stores.getSettings();if(action==='patch'){const prefs=stores.patchSettings(value);if(onSettings)await onSettings(prefs);return prefs;}throw Object.assign(new Error('Unsupported settings action'),{code:'INVALID_ACTION'});},
      recovery:(_event,action,value)=>{if(action==='get')return stores.getRecovery();if(action==='save'){requireValue(object(value)&&roots.get(value.project)===value.root,'Recovery root is not an opened worker project','FORBIDDEN');return stores.saveRecovery(value);}if(action==='delete')return stores.deleteRecovery(value);throw Object.assign(new Error('Unsupported recovery action'),{code:'INVALID_ACTION'});},
      dirty:(_event,value)=>{requireValue(typeof value==='boolean','Invalid dirty state');const s=state(getWindow()),before=s.revision;if(!value&&s.pending)requireValue(s.pending.epoch===s.epoch&&((s.pending.kind==='discard'&&s.pending.revision===before)||(s.pending.kind==='save'&&s.pending.savedRevision===before)),'Dirty acknowledgement lacks a current save/discard decision','INVALID_ACK');s.revision++;s.dirty=value;if(!value&&s.pending)s.pending.resolve();return{dirty:value};},
      update:async(_event,action)=>{requireValue(['state','check','download','reveal','install'].includes(action),'Unsupported update action');requireValue(typeof update==='function','Updater unavailable','UPDATE_UNAVAILABLE');if(action==='install'&&!await guard(getWindow()))return{cancelled:true};return update(action);},
    };
    for(const [name,handler] of Object.entries(handlers))ipcMain.handle(`cassquery:${name}`,async(event,...args)=>{try{authorize(event,getWindow(),getPort());requireValue(Buffer.byteLength(JSON.stringify(args))<=LIMIT,'IPC request exceeds limit');return await handler(event,...args);}catch(error){return{error:error.message,code:error.code||'DESKTOP_ERROR',...(error.status?{status:error.status}:{}),...(error.details?{details:error.details}:{})};}});
  }
  async function openNativeBody(file) {
    const win=getWindow(),epoch=state(win).epoch;current(win,epoch);
    if(!await guard(win))return{cancelled:true};
    const revision=state(win).revision;
    if(!await trust(win))return{cancelled:true};
    requireValue(state(win).revision===revision,'Draft changed during native open','DRAFT_CHANGED');
    let result;
    const body={action:'open',path:selected(file)};
    try{current(win,epoch);result=await postWorkbench(getPort(),token,body);}
    catch(error){if(error.code!=='ADAPTER_REQUIRED')throw error;const choice=await dialog.showMessageBox(win,{type:'warning',buttons:['Create project sidecars','Cancel'],defaultId:1,cancelId:1,message:'Create an editable project beside this Python file?',detail:'Existing Python source and helpers remain unchanged.'});if(choice.response!==0)return{cancelled:true};current(win,epoch);requireValue(state(win).revision===revision,'Draft changed during native open','DRAFT_CHANGED');result=await postWorkbench(getPort(),token,{...body,adapt:true});}
    current(win,epoch);
    if(typeof result.project==='string'&&typeof result.root==='string'){const root=selected(result.root);requireValue(fs.realpathSync(root)===root,'Worker project root is not canonical');roots.set(result.project,root);}
    const prefs=stores.getSettings();const changed=stores.patchSettings({recentProjects:[body.path,...prefs.recentProjects.filter(p=>p!==body.path)].slice(0,10)});if(onSettings)await onSettings(changed);
    command('opened',{result});return result;
  }
  async function openNative(file) {active++;try{return await openNativeBody(file);}finally{active--;}}
  function attachWindow(win) {
    state(win);let allowed=false;
    win.on('close',event=>{if(allowed){allowed=false;return;}if(!state(win).dirty)return;event.preventDefault();guard(win).then(ok=>{if(ok&&!win.isDestroyed()){allowed=true;win.close();}}).catch(error=>dialog.showErrorBox('Unsaved changes retained',error.message));});
    win.webContents.on('did-start-navigation',(_event,_url,_inPlace,isMainFrame)=>{if(isMainFrame){const s=state(win);s.epoch++;if(s.pending)s.pending.cancel();}});
  }
  return {register,attachWindow,requestQuit:()=>guard(getWindow()),command,stores,workbench,openNative,isBusy:()=>active>0,isDirty:()=>{const win=getWindow();return!!(win&&state(win).dirty);}};
}
module.exports={createDesktop,createStores,authorize,postWorkbench,settings,THEMES,DEFAULTS};
