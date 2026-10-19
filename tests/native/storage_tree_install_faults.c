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
static NativeCreate real_create;static int fail_at,fail_write,fail_flush,fail_read,fail_close,short_write;
static int creates,writes,flushes,reads,closes;
static FARPROC (WINAPI *real_proc)(HMODULE,LPCSTR)=GetProcAddress;
static BOOL (WINAPI *real_write)(HANDLE,LPCVOID,DWORD,LPDWORD,LPOVERLAPPED)=WriteFile;
static BOOL (WINAPI *real_flush)(HANDLE)=FlushFileBuffers;
static BOOL (WINAPI *real_read)(HANDLE,LPVOID,DWORD,LPDWORD,LPOVERLAPPED)=ReadFile;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static NTSTATUS NTAPI injected_create(PHANDLE h,ACCESS_MASK a,POBJECT_ATTRIBUTES b,PIO_STATUS_BLOCK c,
    PLARGE_INTEGER d,ULONG e,ULONG f,ULONG g,ULONG i,PVOID j,ULONG k){
    NTSTATUS result=real_create(h,a,b,c,d,e,f,g,i,j,k);creates++;
    if(result>=0&&creates==fail_at){fail_at=0;return (NTSTATUS)0xc0000001;}return result;
}
static FARPROC WINAPI injected_proc(HMODULE module,LPCSTR name){
    if(!strcmp(name,"NtCreateFile")){
        real_create=(NativeCreate)(void*)real_proc(module,name);return (FARPROC)(void*)injected_create;
    }return real_proc(module,name);
}
static BOOL WINAPI injected_write(HANDLE h,LPCVOID p,DWORD n,LPDWORD out,LPOVERLAPPED o){
    BOOL ok;writes++;if(short_write){short_write=0;n/=2;}
    ok=real_write(h,p,n,out,o);if(ok&&fail_write){fail_write=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_flush(HANDLE h){
    BOOL ok=real_flush(h);flushes++;
    if(ok&&fail_flush){fail_flush=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_read(HANDLE h,LPVOID p,DWORD n,LPDWORD out,LPOVERLAPPED o){
    BOOL ok=real_read(h,p,n,out,o);reads++;
    if(ok&&fail_read){fail_read=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_close(HANDLE h){
    BOOL ok=real_close(h);closes++;
    if(ok&&fail_close){fail_close=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
#define GetProcAddress injected_proc
#define WriteFile injected_write
#define FlushFileBuffers injected_flush
#define ReadFile injected_read
#define CloseHandle injected_close
#include "../../backend/native/storage_tree_windows.c"
static uint64_t begin(HANDLE parent,const uint64_t *identity,PSID sid,DWORD length,unsigned number){
    uint64_t cookie;wchar_t generation[37];assert(swprintf(generation,37,L"00000000-0000-0000-0000-%012u",number)==36);
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_INSTALL)&&!gitgo_install_begin(cookie,parent,identity,3,generation));return cookie;
}
int wmain(int argc,wchar_t **argv){
    HANDLE token=0,parent;FILE_ID_INFO id;unsigned char buffer[1024];DWORD length,index;
    uint64_t identity[3],cookie,other[GRP_OWNERS];PSID sid;int before,i;char out[4];
    assert(argc==2&&OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token));
    assert(GetTokenInformation(token,TokenUser,buffer,sizeof(buffer),&length));sid=((TOKEN_USER*)buffer)->User.Sid;
    assert(real_close(token));length=GetLengthSid(sid);
    parent=CreateFileW(argv[1],FILE_TRAVERSE|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(parent,FileIdInfo,&id,sizeof(id)));
    identity[0]=id.VolumeSerialNumber;memcpy(identity+1,id.FileId.Identifier,16);
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_INSTALL));fail_at=creates+2;before=closes;
    assert(gitgo_install_begin(cookie,parent,identity,3,L"00000000-0000-0000-0000-000000000000")!=0);
    assert(gitgo_install_begin(cookie,parent,identity,3,L"00000000-0000-0000-0000-000000000000")==ERROR_INVALID_PARAMETER);
    assert(!gitgo_tree_retire(cookie)&&closes==before+2);
    cookie=begin(parent,identity,sid,length,1);fail_at=creates+1;
    assert(gitgo_install_add(cookie,0,L"code.py",0,4,&index)!=0);
    assert(gitgo_install_add(cookie,0,L"again.py",0,4,&index)==ERROR_INVALID_PARAMETER);assert(!gitgo_tree_retire(cookie));
    cookie=begin(parent,identity,sid,length,2);assert(!gitgo_install_add(cookie,0,L"code.py",0,4,&index));fail_write=1;before=writes;
    assert(gitgo_install_write(cookie,index,"code",4)==ERROR_GEN_FAILURE);
    assert(gitgo_install_write(cookie,index,"code",4)==ERROR_INVALID_PARAMETER&&writes==before+1);
    assert(gitgo_install_finish(cookie,2,4096)==ERROR_INVALID_PARAMETER);assert(!gitgo_tree_retire(cookie));
    cookie=begin(parent,identity,sid,length,3);assert(!gitgo_install_add(cookie,0,L"code.py",0,4,&index));short_write=1;
    assert(gitgo_install_write(cookie,index,"code",4)==ERROR_WRITE_FAULT);assert(!gitgo_tree_retire(cookie));
    cookie=begin(parent,identity,sid,length,4);assert(!gitgo_install_add(cookie,0,L"code.py",0,4,&index)&&!gitgo_install_write(cookie,index,"code",4));
    fail_flush=1;before=flushes;assert(gitgo_install_seal_file(cookie,index)==ERROR_GEN_FAILURE);
    assert(gitgo_install_seal_file(cookie,index)==ERROR_INVALID_PARAMETER&&flushes==before+1);assert(!gitgo_tree_retire(cookie));
    cookie=begin(parent,identity,sid,length,5);assert(!gitgo_install_add(cookie,0,L"code.py",0,4,&index)&&!gitgo_install_write(cookie,index,"code",4));
    assert(!gitgo_install_seal_file(cookie,index));fail_read=1;before=reads;
    assert(gitgo_tree_read(cookie,index,0,out,4)==ERROR_GEN_FAILURE);
    assert(gitgo_tree_read(cookie,index,0,out,4)==ERROR_INVALID_PARAMETER&&reads==before+1);assert(!gitgo_tree_retire(cookie));
    cookie=begin(parent,identity,sid,length,6);assert(!gitgo_install_add(cookie,0,L"code.py",0,4,&index)&&!gitgo_install_write(cookie,index,"code",4));
    assert(!gitgo_install_seal_file(cookie,index)&&!gitgo_install_finish(cookie,2,4096));fail_close=1;before=closes;
    assert(gitgo_tree_retire(cookie)==ERROR_GEN_FAILURE);
    for(i=0;i<3;i++)assert(gitgo_tree_retire(cookie)==ERROR_INVALID_STATE);assert(closes==before+1);
    for(i=0;i<GRP_OWNERS-1;i++)assert(!gitgo_tree_reserve(other+i,sid,length,TREE_INSTALL));
    {uint64_t denied=1;assert(gitgo_tree_reserve(&denied,sid,length,TREE_INSTALL)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
    for(i=0;i<GRP_OWNERS-1;i++)assert(!gitgo_tree_retire(other[i]));assert(real_close(parent));
    printf("native installer faults passed: root-file-create-owned, bounded-write-flush-read-unknown-sealed, partial-write-retained, unknown-close-not-retried; quarantined=1\n");
    return 0;
}
