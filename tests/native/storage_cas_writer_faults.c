#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stdio.h>
#include <wchar.h>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
typedef NTSTATUS (NTAPI *NativeCreate)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,
    PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
static NativeCreate real_create;
static int fail_create,fail_write,fail_flush,fail_read,fail_close,short_write;
static int creates,writes,flushes,reads,closes;
static FARPROC (WINAPI *real_proc)(HMODULE,LPCSTR)=GetProcAddress;
static BOOL (WINAPI *real_write)(HANDLE,LPCVOID,DWORD,LPDWORD,LPOVERLAPPED)=WriteFile;
static BOOL (WINAPI *real_flush)(HANDLE)=FlushFileBuffers;
static BOOL (WINAPI *real_read)(HANDLE,LPVOID,DWORD,LPDWORD,LPOVERLAPPED)=ReadFile;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static NTSTATUS NTAPI injected_create(PHANDLE h,ACCESS_MASK a,POBJECT_ATTRIBUTES b,PIO_STATUS_BLOCK c,
    PLARGE_INTEGER d,ULONG e,ULONG f,ULONG g,ULONG i,PVOID j,ULONG k){
    NTSTATUS result=real_create(h,a,b,c,d,e,f,g,i,j,k);creates++;
    if(result>=0&&fail_create){fail_create=0;return (NTSTATUS)0xc0000001;}
    return result;
}
static FARPROC WINAPI injected_proc(HMODULE module,LPCSTR name){
    if(!strcmp(name,"NtCreateFile")){
        real_create=(NativeCreate)(void*)real_proc(module,name);return (FARPROC)(void*)injected_create;
    }
    return real_proc(module,name);
}
static BOOL WINAPI injected_write(HANDLE h,LPCVOID p,DWORD n,LPDWORD out,LPOVERLAPPED o){
    BOOL ok;writes++;if(short_write){n/=2;short_write=0;}
    ok=real_write(h,p,n,out,o);
    if(ok&&fail_write){fail_write=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
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
#include "../../backend/native/storage_cas_writer_windows.c"
static uint64_t created(HANDLE parent,const uint64_t *identity,unsigned number){
    wchar_t name[107];uint64_t cookie;
    assert(swprintf(name,107,L"%064u.00000000-0000-0000-0000-%012u.blob",0u,number)==106);
    assert(!gitgo_cas_writer_reserve(&cookie)&&!gitgo_cas_writer_create(cookie,parent,identity,3,name,4));return cookie;
}
int wmain(int argc,wchar_t **argv){
    HANDLE parent;FILE_ID_INFO id;uint64_t identity[3],cookie,values[4],other[GCW_COUNT];
    wchar_t name[107];char data[4]={1,2,3,4},output[4];int before,i;
    assert(argc==2);
    parent=CreateFileW(argv[1],FILE_TRAVERSE|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(parent,FileIdInfo,&id,sizeof(id)));
    identity[0]=id.VolumeSerialNumber;memcpy(identity+1,id.FileId.Identifier,16);
    assert(swprintf(name,107,L"%064u.00000000-0000-0000-0000-%012u.blob",0u,0u)==106);
    assert(!gitgo_cas_writer_reserve(&cookie));fail_create=1;before=creates;
    assert(gitgo_cas_writer_create(cookie,parent,identity,3,name,4)!=0);
    assert(gitgo_cas_writer_create(cookie,parent,identity,3,name,4)==ERROR_INVALID_PARAMETER&&creates==before+1);
    assert(gitgo_cas_writer_write(cookie,data,4)==ERROR_INVALID_PARAMETER);assert(!gitgo_cas_writer_retire(cookie));
    cookie=created(parent,identity,1);fail_write=1;before=writes;
    assert(gitgo_cas_writer_write(cookie,data,4)==ERROR_GEN_FAILURE);
    assert(gitgo_cas_writer_write(cookie,data,4)==ERROR_INVALID_PARAMETER&&writes==before+1);
    assert(gitgo_cas_writer_seal(cookie)==ERROR_INVALID_STATE);assert(!gitgo_cas_writer_retire(cookie));
    cookie=created(parent,identity,2);short_write=1;
    assert(gitgo_cas_writer_write(cookie,data,4)==ERROR_WRITE_FAULT);assert(gitgo_cas_writer_seal(cookie)==ERROR_INVALID_STATE);assert(!gitgo_cas_writer_retire(cookie));
    cookie=created(parent,identity,3);assert(!gitgo_cas_writer_write(cookie,data,4));fail_flush=1;before=flushes;
    assert(gitgo_cas_writer_seal(cookie)==ERROR_GEN_FAILURE);assert(gitgo_cas_writer_seal(cookie)==ERROR_INVALID_STATE&&flushes==before+1);
    assert(gitgo_cas_writer_identity(cookie,values,4)==ERROR_INVALID_STATE);assert(!gitgo_cas_writer_retire(cookie));
    cookie=created(parent,identity,4);assert(!gitgo_cas_writer_write(cookie,data,4)&&!gitgo_cas_writer_seal(cookie));fail_read=1;before=reads;
    assert(gitgo_cas_writer_read(cookie,0,output,4)==ERROR_GEN_FAILURE);
    assert(gitgo_cas_writer_read(cookie,0,output,4)==ERROR_INVALID_PARAMETER&&reads==before+1);assert(!gitgo_cas_writer_retire(cookie));
    cookie=created(parent,identity,5);assert(!gitgo_cas_writer_write(cookie,data,4)&&!gitgo_cas_writer_seal(cookie));
    fail_close=1;before=closes;assert(gitgo_cas_writer_retire(cookie)==ERROR_INVALID_STATE);
    for(i=0;i<3;i++)assert(gitgo_cas_writer_retire(cookie)==ERROR_INVALID_STATE);
    assert(closes==before+1);
    for(i=0;i<GCW_COUNT-6;i++)assert(!gitgo_cas_writer_reserve(other+i));
    {uint64_t denied=1;assert(gitgo_cas_writer_reserve(&denied)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
    for(i=0;i<GCW_COUNT-6;i++)assert(!gitgo_cas_writer_retire(other[i]));assert(real_close(parent));
    printf("native CAS writer faults passed: create-write-flush-read-unknown-sealed, partial-write-retained, unknown-close-not-retried; quarantined=1\n");
    return 0;
}
