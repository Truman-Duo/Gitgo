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
static NativeCreate real_create;static int fail_create,fail_create_number,fail_read,fail_close,creates,reads,closes;
static FARPROC (WINAPI *real_proc)(HMODULE,LPCSTR)=GetProcAddress;
static BOOL (WINAPI *real_read)(HANDLE,LPVOID,DWORD,LPDWORD,LPOVERLAPPED)=ReadFile;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static NTSTATUS NTAPI injected_create(PHANDLE h,ACCESS_MASK a,POBJECT_ATTRIBUTES b,PIO_STATUS_BLOCK c,
    PLARGE_INTEGER d,ULONG e,ULONG f,ULONG g,ULONG i,PVOID j,ULONG k){
    NTSTATUS result=real_create(h,a,b,c,d,e,f,g,i,j,k);creates++;
    if(result>=0&&(fail_create||creates==fail_create_number)){fail_create=0;fail_create_number=0;return (NTSTATUS)0xc0000001;}return result;
}
static FARPROC WINAPI injected_proc(HMODULE module,LPCSTR name){
    if(!strcmp(name,"NtCreateFile")){
        real_create=(NativeCreate)(void*)real_proc(module,name);return (FARPROC)(void*)injected_create;
    }return real_proc(module,name);
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
#define ReadFile injected_read
#define CloseHandle injected_close
#include "../../backend/native/storage_tree_windows.c"
int wmain(int argc,wchar_t **argv){
    HANDLE token=0,parent,file;FILE_ID_INFO id;uint64_t identity[3],cookie,other[GRP_OWNERS];
    unsigned char buffer[1024];DWORD length,index,actual;PSID sid;wchar_t path[32768];int before,i;char out[4];
    assert(argc==2&&OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token));
    assert(GetTokenInformation(token,TokenUser,buffer,sizeof(buffer),&length));sid=((TOKEN_USER*)buffer)->User.Sid;
    assert(real_close(token));length=GetLengthSid(sid);
    parent=CreateFileW(argv[1],FILE_TRAVERSE|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(parent,FileIdInfo,&id,sizeof(id)));
    identity[0]=id.VolumeSerialNumber;memcpy(identity+1,id.FileId.Identifier,16);
    assert(swprintf(path,32768,L"%ls\\code.py",argv[1])>0);
    file=CreateFileW(path,GENERIC_WRITE,0,0,CREATE_NEW,0,0);assert(file!=INVALID_HANDLE_VALUE);
    assert(WriteFile(file,"code",4,&actual,0)&&actual==4&&real_close(file));
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_PIN));fail_create=1;before=creates;
    assert(gitgo_tree_root(cookie,parent,identity,3)!=0);
    assert(gitgo_tree_root(cookie,parent,identity,3)==ERROR_INVALID_PARAMETER&&creates==before+1);
    assert(!gitgo_tree_retire(cookie));
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_PIN)&&!gitgo_tree_root(cookie,parent,identity,3));fail_create=1;before=creates;
    assert(gitgo_tree_add(cookie,0,L"code.py",0,4,&index)!=0);
    assert(gitgo_tree_add(cookie,0,L"code.py",0,4,&index)==ERROR_INVALID_PARAMETER&&creates==before+1);
    assert(!gitgo_tree_retire(cookie));
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_PIN)&&!gitgo_tree_root(cookie,parent,identity,3));
    assert(!gitgo_tree_add(cookie,0,L"code.py",0,4,&index));fail_read=1;before=reads;
    assert(gitgo_tree_read(cookie,index,0,out,4)==ERROR_GEN_FAILURE);
    assert(gitgo_tree_read(cookie,index,0,out,4)==ERROR_INVALID_PARAMETER&&reads==before+1);assert(!gitgo_tree_retire(cookie));
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_PIN)&&!gitgo_tree_root(cookie,parent,identity,3));
    assert(!gitgo_tree_add(cookie,0,L"code.py",0,4,&index));fail_close=1;before=closes;
    assert(gitgo_tree_retire(cookie)==ERROR_GEN_FAILURE);
    for(i=0;i<3;i++)assert(gitgo_tree_retire(cookie)==ERROR_INVALID_STATE);assert(closes==before+1);
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_PIN));fail_create_number=creates+3;before=closes;
    assert(gitgo_tree_path(cookie,argv[1],identity,3)!=0);assert(!gitgo_tree_retire(cookie)&&closes==before+3);
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_PIN)&&!gitgo_tree_path(cookie,argv[1],identity,3));
    assert(!gitgo_tree_add(cookie,0,L"code.py",0,4,&index));fail_close=1;before=closes;
    assert(gitgo_tree_retire(cookie)==ERROR_GEN_FAILURE&&gitgo_tree_retire(cookie)==ERROR_INVALID_STATE&&closes==before+1);
    for(i=0;i<GRP_OWNERS-2;i++)assert(!gitgo_tree_reserve(other+i,sid,length,TREE_PIN));
    {uint64_t denied=1;assert(gitgo_tree_reserve(&denied,sid,length,TREE_PIN)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
    for(i=0;i<GRP_OWNERS-2;i++)assert(!gitgo_tree_retire(other[i]));assert(real_close(parent));
    printf("native runtime pin faults passed: exact-object-reopen, partial-ancestor-chain-owned, open-read-unknown-sealed, unknown-close-not-retried; quarantined=2\n");
    return 0;
}
