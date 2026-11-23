import {expect,test} from 'bun:test';
import {mkdtempSync,readFileSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,dirname,resolve,basename} from 'node:path';
import {ensureDashboardTerminalProfile,dashboardTerminalArgs,DASHBOARD_TERMINAL_PROFILE} from './windowsTerminalProfile.js';

test('Gitgo appearance is an owned fragment; existing terminal profiles and explicit selection remain untouched',()=>{
 const root=mkdtempSync(join(tmpdir(),'gitgo-profile-'));
 try{
  const path=ensureDashboardTerminalProfile(root),content=readFileSync(path,'utf8');
  const profile=JSON.parse(content).profiles[0];
  expect(profile).toMatchObject({guid:DASHBOARD_TERMINAL_PROFILE,hidden:true,scrollbarState:'hidden',cursorShape:'bar'});
  expect(ensureDashboardTerminalProfile(root)).toBe(path);
  expect(readFileSync(path,'utf8')).toBe(content);
  writeFileSync(path,JSON.stringify({profiles:[{guid:'user-owned-profile'}]}));
  expect(()=>ensureDashboardTerminalProfile(root)).toThrow('differs');
  expect(JSON.parse(readFileSync(path,'utf8')).profiles[0].guid).toBe('user-owned-profile');
  expect(dashboardTerminalArgs(['new-tab','--'])).toEqual(['new-tab','--profile',DASHBOARD_TERMINAL_PROFILE,'--']);
  expect(dashboardTerminalArgs(['new-tab','-p','custom','--'])).toEqual(['new-tab','-p','custom','--']);
 }finally{
  if(dirname(resolve(root))!==resolve(tmpdir())||!basename(root).startsWith('gitgo-profile-'))throw new Error('unexpected test directory');
  rmSync(root,{recursive:true,force:true});
 }
});
