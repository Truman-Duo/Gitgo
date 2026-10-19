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
typedef NTSTATUS (NTAPI *NativeCreate)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,
    PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
static NativeCreate real_create;static int fail_open,fail_lock,fail_close,fail_read,fail_token,fail_dup,fail_flush;
static int opens,locks,closes,reads,tokens,dups,flushes;
static FARPROC (WINAPI *real_proc)(HMODULE,LPCSTR)=GetProcAddress;
static BOOL (WINAPI *real_lock)(HANDLE,DWORD,DWORD,DWORD,DWORD,LPOVERLAPPED)=LockFileEx;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static BOOL (WINAPI *real_read)(HANDLE,LPVOID,DWORD,LPDWORD,LPOVERLAPPED)=ReadFile;
static BOOL (WINAPI *real_token)(HANDLE,DWORD,PHANDLE)=OpenProcessToken;
static BOOL (WINAPI *real_dup)(HANDLE,HANDLE,HANDLE,LPHANDLE,DWORD,BOOL,DWORD)=DuplicateHandle;
static BOOL (WINAPI *real_flush)(HANDLE)=FlushFileBuffers;
static NTSTATUS NTAPI injected_create(PHANDLE h,ACCESS_MASK a,POBJECT_ATTRIBUTES b,PIO_STATUS_BLOCK c,
    PLARGE_INTEGER d,ULONG e,ULONG f,ULONG g,ULONG i,PVOID j,ULONG k){
    NTSTATUS result=real_create(h,a,b,c,d,e,f,g,i,j,k);opens++;
    if(result>=0&&opens==fail_open){fail_open=0;return (NTSTATUS)0xc0000001;}return result;
}
static FARPROC WINAPI injected_proc(HMODULE module,LPCSTR name){
    if(!strcmp(name,"NtCreateFile")){
        real_create=(NativeCreate)(void*)real_proc(module,name);return (FARPROC)(void*)injected_create;
    }return real_proc(module,name);
}
static BOOL WINAPI injected_lock(HANDLE h,DWORD a,DWORD b,DWORD c,DWORD d,LPOVERLAPPED o){
    BOOL ok=real_lock(h,a,b,c,d,o);locks++;
    if(ok&&fail_lock){fail_lock=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_close(HANDLE h){
    BOOL ok=real_close(h);closes++;
    if(ok&&fail_close){fail_close=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_read(HANDLE h,LPVOID p,DWORD n,LPDWORD out,LPOVERLAPPED o){
    BOOL ok=real_read(h,p,n,out,o);reads++;
    if(ok&&fail_read){fail_read=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_token(HANDLE p,DWORD a,PHANDLE out){
    BOOL ok=real_token(p,a,out);tokens++;
    if(ok&&fail_token){fail_token=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_dup(HANDLE a,HANDLE b,HANDLE c,LPHANDLE d,DWORD e,BOOL f,DWORD g){
    BOOL ok=real_dup(a,b,c,d,e,f,g);dups++;
    if(ok&&fail_dup){fail_dup=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_flush(HANDLE h){
    BOOL ok=real_flush(h);flushes++;
    if(ok&&fail_flush){fail_flush=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
#define GetProcAddress injected_proc
#define LockFileEx injected_lock
#define CloseHandle injected_close
#define ReadFile injected_read
#define OpenProcessToken injected_token
#define DuplicateHandle injected_dup
#define FlushFileBuffers injected_flush
#include "../../backend/native/storage_scope_windows.c"
static uint64_t root(uint64_t cookie,const wchar_t *path){
    wchar_t part[256],drive[8]=L"\\??\\C:\\";uint64_t current=0,next=0;unsigned cursor=3,start;
    drive[4]=path[0];assert(!gitgo_scope_open(cookie,0,drive,1,1,&current));
    while(path[cursor]){
        start=cursor;while(path[cursor]&&path[cursor]!=L'\\')cursor++;
        assert(cursor-start<256);memcpy(part,path+start,(cursor-start)*sizeof(wchar_t));part[cursor-start]=0;
        assert(!gitgo_scope_open(cookie,current,part,1,1,&next));current=next;if(path[cursor])cursor++;
    }return current;
}
int wmain(int argc,wchar_t **argv){
    uint64_t cookie=0,parent=0,file=0,info[4]={0},other=0,borrowed_token=0,full[GSO_OWNERS]={0},status[4]={0};uintptr_t raw=0;
    wchar_t sid[256],profile[513];DWORD count;char data[16];int mode,before;
    assert(argc==3);mode=_wtoi(argv[2]);assert(mode>=1&&mode<=9);assert(!gitgo_scope_reserve(&cookie));
    if(mode==6||mode==7){
        if(mode==6){
            fail_token=1;before=closes;assert(gitgo_scope_account(cookie,sid,256,profile,513)==ERROR_GEN_FAILURE);
            assert(!sid[0]&&!profile[0]);assert(!gitgo_scope_retire(cookie)&&closes==before+1);
        }else{
            fail_close=1;before=closes;assert(gitgo_scope_account(cookie,sid,256,profile,513)==ERROR_GEN_FAILURE);
            assert(!sid[0]&&!profile[0]);assert(gitgo_scope_retire(cookie)==ERROR_INVALID_STATE&&closes==before+1);
        }
    }else{
        parent=root(cookie,argv[1]);assert(!gitgo_scope_identity(cookie,parent,info,4));
        if(mode==1){
            fail_open=opens+1;before=opens;assert(gitgo_scope_open(cookie,parent,L"gate",0,1,&file)!=0);
            assert(gitgo_scope_open(cookie,parent,L"gate",0,1,&file)==ERROR_INVALID_PARAMETER&&opens==before+1);
            assert(!gitgo_scope_retire(cookie));
        }else if(mode==5){
            assert(!gitgo_scope_borrow(cookie,parent,&raw)&&!gitgo_scope_reserve(&other));fail_dup=1;before=closes;
            assert(gitgo_scope_import_parent(other,(HANDLE)raw,info,&borrowed_token)==ERROR_GEN_FAILURE);
            assert(!gitgo_scope_retire(other)&&closes==before+1);assert(!gitgo_scope_retire(cookie));
        }else if(mode==8||mode==9){
            assert(!gitgo_scope_open(cookie,parent,L"gate",0,1,&file)&&!gitgo_scope_identity(cookie,file,info,4));
            if(mode==8)fail_flush=1;else fail_close=1;
            before=flushes;assert(gitgo_scope_flush_bound(cookie,parent,L"gate",info,16)==ERROR_GEN_FAILURE);
            assert(gitgo_scope_flush_bound(cookie,parent,L"gate",info,16)==ERROR_INVALID_PARAMETER&&flushes==before+1);
            if(mode==8)assert(!gitgo_scope_retire(cookie));else assert(gitgo_scope_retire(cookie)==ERROR_INVALID_STATE);
        }else{
            assert(!gitgo_scope_open(cookie,parent,L"gate",0,1,&file)&&!gitgo_scope_identity(cookie,file,info,4));
            if(mode==2){
                fail_lock=1;before=locks;assert(gitgo_scope_lock(cookie,file,info,1)==ERROR_GEN_FAILURE);
                assert(gitgo_scope_lock(cookie,file,info,1)==ERROR_INVALID_PARAMETER&&locks==before+1);
                assert(!gitgo_scope_reserve(&other));parent=root(other,argv[1]);assert(!gitgo_scope_open(other,parent,L"gate",0,1,&borrowed_token));
                assert(gitgo_scope_lock(other,borrowed_token,info,1)==ERROR_LOCK_VIOLATION);assert(!gitgo_scope_retire(other));assert(!gitgo_scope_retire(cookie));
            }else if(mode==3){
                fail_read=1;before=reads;assert(gitgo_scope_read(cookie,file,data,sizeof(data),&count)==ERROR_GEN_FAILURE);
                assert(gitgo_scope_read(cookie,file,data,sizeof(data),&count)==ERROR_INVALID_PARAMETER&&reads==before+1);assert(!gitgo_scope_retire(cookie));
            }else{
                fail_close=1;before=closes;assert(gitgo_scope_close(cookie,file)==ERROR_GEN_FAILURE);
                for(int i=0;i<3;i++)assert(gitgo_scope_close(cookie,file)==ERROR_INVALID_STATE);
                assert(gitgo_scope_retire(cookie)==ERROR_INVALID_STATE&&closes==before+1);
                assert(!gitgo_scope_status(cookie,status,4)&&status[0]&&status[2]==1);
            }
        }
    }
    if(mode==4||mode==7||mode==9){
        for(int i=0;i<GSO_OWNERS-1;i++)assert(!gitgo_scope_reserve(full+i));
        {uint64_t denied=1;assert(gitgo_scope_reserve(&denied)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
        for(int i=0;i<GSO_OWNERS-1;i++)assert(!gitgo_scope_retire(full[i]));
    }
    printf("native Host fault mode %d passed; quarantined=%d\n",mode,(mode==4||mode==7||mode==9)?1:0);return 0;
}
