#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
typedef NTSTATUS (NTAPI *VolumeFn)(HANDLE,PIO_STATUS_BLOCK,PVOID,ULONG,int);
static VolumeFn actual_query;
static FARPROC (WINAPI *actual_proc)(HMODULE,LPCSTR)=GetProcAddress;
static BOOL (WINAPI *actual_fs)(HANDLE,LPWSTR,DWORD,LPDWORD,LPDWORD,LPDWORD,LPWSTR,DWORD)=GetVolumeInformationByHandleW;
static int mode,queries;
static NTSTATUS NTAPI injected_query(HANDLE h,PIO_STATUS_BLOCK io,PVOID data,ULONG length,int kind){
    NTSTATUS result=actual_query(h,io,data,length,kind);queries++;
    if(result>=0){
        if(mode==1)return (NTSTATUS)0xc0000001;
        if(mode==2&&kind==4)((ULONG*)data)[1]|=0x10;
        if(mode==3&&kind==3)((ULONG*)((unsigned char*)data+16))[0]*=2;
        if(mode==5)io->Information=0;
    }return result;
}
static FARPROC WINAPI injected_proc(HMODULE module,LPCSTR name){
    if(!strcmp(name,"NtQueryVolumeInformationFile")){
        actual_query=(VolumeFn)(void*)actual_proc(module,name);return (FARPROC)(void*)injected_query;
    }return actual_proc(module,name);
}
static BOOL WINAPI injected_fs(HANDLE h,LPWSTR volume,DWORD a,LPDWORD serial,LPDWORD b,LPDWORD flags,LPWSTR fs,DWORD c){
    BOOL result=actual_fs(h,volume,a,serial,b,flags,fs,c);
    if(result&&mode==4){assert(c>=5);memcpy(fs,L"ReFS",5*sizeof(wchar_t));}return result;
}
#define GetProcAddress injected_proc
#define GetVolumeInformationByHandleW injected_fs
#include "../../backend/native/storage_scope_windows.c"
int wmain(int argc,wchar_t **argv){
    uint64_t cookie,token,values[2],snapshot[4];int before;
    assert(argc==3);mode=_wtoi(argv[2]);assert(mode>=1&&mode<=5);
    assert(!gitgo_scope_reserve(&cookie));
    /* The fixture supplies its already validated native drive spelling. */
    assert(!gitgo_scope_open(cookie,0,argv[1],1,1,&token));
    assert(gitgo_scope_volume(cookie,token,values,2)!=0);
    before=queries;assert(before>0);
    assert(gitgo_scope_volume(cookie,token,values,2)==ERROR_INVALID_PARAMETER&&queries==before);
    assert(!gitgo_scope_status(cookie,snapshot,4)&&snapshot[0]);
    assert(!gitgo_scope_retire(cookie));
    printf("native volume fault %d passed; unqualified or unknown volume refused\n",mode);
    return 0;
}
