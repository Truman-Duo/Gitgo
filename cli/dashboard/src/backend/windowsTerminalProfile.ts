import {existsSync,mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';

export const DASHBOARD_TERMINAL_PROFILE='{8cb789e0-60a5-4c4b-bd67-635352e91f85}';
const profile={guid:DASHBOARD_TERMINAL_PROFILE,name:'Gitgo Dashboard',hidden:true,
 commandline:'cmd.exe',scrollbarState:'hidden',cursorShape:'bar'} as const;

/** An application-owned fragment; never edits settings.json or other profiles.
 * The launcher supplies the real child command; this hidden profile owns only
 * its terminal appearance. File conflicts fail without replacing user data. */
export function ensureDashboardTerminalProfile(localAppData=process.env.LOCALAPPDATA){
 if(!localAppData)throw new Error('Windows Terminal profile location is unavailable');
 const directory=join(localAppData,'Microsoft','Windows Terminal','Fragments','Gitgo');
 const path=join(directory,'dashboard.json');
 const desired={profiles:[profile]};
 if(existsSync(path)){
  const saved=JSON.parse(readFileSync(path,'utf8').replace(/^\uFEFF/,''));
  if(JSON.stringify(saved)!==JSON.stringify(desired))throw new Error('Gitgo terminal profile differs; inspect '+path);
  return path;
 }
 mkdirSync(directory,{recursive:true});
 try{writeFileSync(path,JSON.stringify(desired,null,2)+'\n',{encoding:'utf8',flag:'wx'});}
 catch(error){
  if(!existsSync(path)||JSON.stringify(JSON.parse(readFileSync(path,'utf8')))!==JSON.stringify(desired))throw error;
 }
 return path;
}

export function dashboardTerminalArgs(args:readonly string[]){
 const existing=[...args];
 if(existing.some(arg=>arg==='-p'||arg==='--profile'||arg.startsWith('--profile=')))return existing;
 const delimiter=existing.indexOf('--');
 existing.splice(delimiter<0?existing.length:delimiter,0,'--profile',DASHBOARD_TERMINAL_PROFILE);
 return existing;
}
