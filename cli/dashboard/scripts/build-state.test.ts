import {test,expect} from 'bun:test';
import {mkdtempSync,mkdirSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {currentBuild,recordBuild} from './build-state.mjs';

test('source launch detects missing, changed and corrupt builds; test files do not invalidate it',()=>{
  const root=mkdtempSync(join(tmpdir(),'gitgo-build-state-'));
  try {
    mkdirSync(join(root,'src'));mkdirSync(join(root,'dist'));mkdirSync(join(root,'scripts'));
    writeFileSync(join(root,'src/main.tsx'),'first');
    expect(currentBuild(root)).toBe(false);
    recordBuild(root);expect(currentBuild(root)).toBe(true);
    writeFileSync(join(root,'src/panel.test.tsx'),'test');expect(currentBuild(root)).toBe(true);
    writeFileSync(join(root,'src/main.tsx'),'changed');expect(currentBuild(root)).toBe(false);
    recordBuild(root);writeFileSync(join(root,'scripts/build.ts'),'new compiler');expect(currentBuild(root)).toBe(false);
    recordBuild(root);writeFileSync(join(root,'dist/build-state.json'),'broken');expect(currentBuild(root)).toBe(false);
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('changing the canonical keyboard catalog invalidates the frontend bundle',()=>{
 const base=mkdtempSync(join(tmpdir(),'gitgo-keyboard-build-')),root=join(base,'cli/dashboard'),catalog=join(base,'backend/resources/keyboard');
 try{
  mkdirSync(join(root,'dist'),{recursive:true});mkdirSync(catalog,{recursive:true});
  writeFileSync(join(catalog,'actions.json'),'[]');recordBuild(root);expect(currentBuild(root)).toBe(true);
  writeFileSync(join(catalog,'actions.json'),'[{"id":"new-binding"}]');expect(currentBuild(root)).toBe(false);
 }finally{rmSync(base,{recursive:true,force:true});}
});
