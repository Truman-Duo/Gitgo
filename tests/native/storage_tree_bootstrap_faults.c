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
static NativeCreate real_create;
static int fail_create,fail_write,fail_flush,fail_read,fail_close,fail_token,fail_lock,short_write;
static int creates,writes,flushes,reads,closes,locks;
static FARPROC (WINAPI *real_proc)(HMODULE,LPCSTR)=GetProcAddress;
static BOOL (WINAPI *real_write)(HANDLE,LPCVOID,DWORD,LPDWORD,LPOVERLAPPED)=WriteFile;
static BOOL (WINAPI *real_flush)(HANDLE)=FlushFileBuffers;
static BOOL (WINAPI *real_read)(HANDLE,LPVOID,DWORD,LPDWORD,LPOVERLAPPED)=ReadFile;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static BOOL (WINAPI *real_token)(HANDLE,DWORD,PHANDLE)=OpenProcessToken;
static BOOL (WINAPI *real_lock)(HANDLE,DWORD,DWORD,DWORD,DWORD,LPOVERLAPPED)=LockFileEx;
static NTSTATUS NTAPI injected_create(PHANDLE h,ACCESS_MASK a,POBJECT_ATTRIBUTES b,PIO_STATUS_BLOCK c,
    PLARGE_INTEGER d,ULONG e,ULONG f,ULONG g,ULONG i,PVOID j,ULONG k){
    NTSTATUS result=real_create(h,a,b,c,d,e,f,g,i,j,k);creates++;
    if(result>=0&&creates==fail_create){fail_create=0;return (NTSTATUS)0xc0000001;}return result;
}
static FARPROC WINAPI injected_proc(HMODULE module,LPCSTR name){
    if(!strcmp(name,"NtCreateFile")){
        real_create=(NativeCreate)(void*)real_proc(module,name);return (FARPROC)(void*)injected_create;
    }return real_proc(module,name);
}
static BOOL WINAPI injected_write(HANDLE h,LPCVOID p,DWORD n,LPDWORD out,LPOVERLAPPED o){
    BOOL ok;writes++;if(short_write){short_write=0;n/=2;}ok=real_write(h,p,n,out,o);
    if(ok&&fail_write){fail_write=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_flush(HANDLE h){
    BOOL ok=real_flush(h);flushes++;if(ok&&fail_flush){fail_flush=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_read(HANDLE h,LPVOID p,DWORD n,LPDWORD out,LPOVERLAPPED o){
    BOOL ok=real_read(h,p,n,out,o);reads++;if(ok&&fail_read){fail_read=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_close(HANDLE h){
    BOOL ok=real_close(h);closes++;if(ok&&fail_close){fail_close=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_token(HANDLE p,DWORD a,PHANDLE t){
    BOOL ok=real_token(p,a,t);if(ok&&fail_token){fail_token=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_lock(HANDLE h,DWORD a,DWORD b,DWORD c,DWORD d,LPOVERLAPPED o){
    BOOL ok=real_lock(h,a,b,c,d,o);locks++;if(ok&&fail_lock){fail_lock=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
#define GetProcAddress injected_proc
#define WriteFile injected_write
#define FlushFileBuffers injected_flush
#define ReadFile injected_read
#define CloseHandle injected_close
#define OpenProcessToken injected_token
#define LockFileEx injected_lock
#include "../../backend/native/storage_tree_windows.c"
int wmain(int argc,wchar_t **argv){
    HANDLE token=0,parent;FILE_ID_INFO id;unsigned char user[1024];DWORD length,index=0;
    uint64_t cookie=0,identity[3],others[GRP_OWNERS]={0};PSID sid;char out[4];int mode,before,result;
    assert(argc==3);mode=_wtoi(argv[2]);assert(mode>=1&&mode<=10);
    assert(real_token(GetCurrentProcess(),TOKEN_QUERY,&token));
    assert(GetTokenInformation(token,TokenUser,user,sizeof(user),&length));sid=((TOKEN_USER*)user)->User.Sid;
    assert(real_close(token));length=GetLengthSid(sid);
    parent=CreateFileW(argv[1],FILE_TRAVERSE|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(parent,FileIdInfo,&id,sizeof(id)));
    identity[0]=id.VolumeSerialNumber;memcpy(identity+1,id.FileId.Identifier,16);
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_BOOTSTRAP));
    if(mode==1)fail_create=creates+2;
    if(mode==6)fail_token=1;
    if(mode==7)fail_close=1;
    if(mode==1||mode==6||mode==7){
        before=creates;assert(gitgo_bootstrap_begin(cookie,parent,identity,3,L"registry")!=0);result=creates;
        assert(result==before+(mode==1?2:0));
        assert(gitgo_bootstrap_begin(cookie,parent,identity,3,L"registry")==ERROR_INVALID_PARAMETER&&creates==result);
        before=closes;
        if(mode==7){assert(gitgo_tree_retire(cookie)==ERROR_INVALID_STATE&&closes==before);}
        else assert(!gitgo_tree_retire(cookie));
    }else{
        assert(!gitgo_bootstrap_begin(cookie,parent,identity,3,L"registry"));
        if(mode==2){
            fail_create=creates+1;before=creates;assert(gitgo_bootstrap_add(cookie,0,L"intent",0,4096,&index)!=0);
            assert(gitgo_bootstrap_add(cookie,0,L"retry",0,4096,&index)==ERROR_INVALID_PARAMETER&&creates==before+1);
            assert(!gitgo_tree_retire(cookie));
        }else{
            assert(!gitgo_bootstrap_add(cookie,0,L"intent",0,4096,&index));
            if(mode==3||mode==4||mode==10){
                if(mode==3)fail_write=1;if(mode==4)fail_flush=1;if(mode==10)short_write=1;
                before=writes;assert(gitgo_bootstrap_write_file(cookie,index,"data",4)==(mode==10?ERROR_WRITE_FAULT:ERROR_GEN_FAILURE));
                assert(gitgo_bootstrap_write_file(cookie,index,"data",4)==ERROR_INVALID_PARAMETER&&writes==before+1);
                assert(!gitgo_tree_retire(cookie));
            }else{
                assert(!gitgo_bootstrap_write_file(cookie,index,"data",4));
                if(mode==5){
                    assert(!gitgo_bootstrap_finish(cookie,2,4096));fail_close=1;before=closes;
                    assert(gitgo_tree_retire(cookie)==ERROR_GEN_FAILURE);
                    for(int i=0;i<3;i++)assert(gitgo_tree_retire(cookie)==ERROR_INVALID_STATE);
                    assert(closes==before+1);
                }else if(mode==8){
                    assert(!gitgo_bootstrap_add(cookie,0,L"gate",0,0,&index));assert(!gitgo_bootstrap_write_file(cookie,index,0,0));
                    fail_lock=1;before=locks;assert(gitgo_bootstrap_lock_gate(cookie,index)==ERROR_GEN_FAILURE);
                    assert(gitgo_bootstrap_lock_gate(cookie,index)==ERROR_INVALID_PARAMETER&&locks==before+1);assert(!gitgo_tree_retire(cookie));
                }else{
                    fail_read=1;before=reads;assert(gitgo_tree_read(cookie,index,0,out,4)==ERROR_GEN_FAILURE);
                    assert(gitgo_tree_read(cookie,index,0,out,4)==ERROR_INVALID_PARAMETER&&reads==before+1);assert(!gitgo_tree_retire(cookie));
                }
            }
        }
    }
    if(mode==5||mode==7){
        for(int i=0;i<GRP_OWNERS-1;i++)assert(!gitgo_tree_reserve(others+i,sid,length,TREE_BOOTSTRAP));
        {uint64_t denied=1;assert(gitgo_tree_reserve(&denied,sid,length,TREE_BOOTSTRAP)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
        for(int i=0;i<GRP_OWNERS-1;i++)assert(!gitgo_tree_retire(others[i]));
    }
    assert(real_close(parent));printf("native bootstrap fault %d passed; quarantined=%d\n",mode,mode==5||mode==7);return 0;
}
