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
static NativeCreate real_create;static int fail_open,fail_delete,fail_close,fail_read,opens,deletes,closes,reads;
static FARPROC (WINAPI *real_proc)(HMODULE,LPCSTR)=GetProcAddress;
static BOOL (WINAPI *real_dispose)(HANDLE,FILE_INFO_BY_HANDLE_CLASS,LPVOID,DWORD)=SetFileInformationByHandle;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static BOOL (WINAPI *real_read)(HANDLE,LPVOID,DWORD,LPDWORD,LPOVERLAPPED)=ReadFile;
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
static BOOL WINAPI injected_dispose(HANDLE h,FILE_INFO_BY_HANDLE_CLASS c,LPVOID p,DWORD n){
    BOOL ok=real_dispose(h,c,p,n);deletes++;
    if(ok&&fail_delete){fail_delete=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_close(HANDLE h){
    BOOL ok=real_close(h);closes++;
    if(ok&&fail_close){fail_close=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
static BOOL WINAPI injected_read(HANDLE h,LPVOID p,DWORD n,LPDWORD out,LPOVERLAPPED o){
    BOOL ok=real_read(h,p,n,out,o);reads++;
    if(ok&&fail_read){fail_read=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}return ok;
}
#define GetProcAddress injected_proc
#define SetFileInformationByHandle injected_dispose
#define CloseHandle injected_close
#define ReadFile injected_read
#include "../../backend/native/storage_tree_windows.c"
int wmain(int argc,wchar_t **argv){
    HANDLE token=0,parent,root;FILE_ID_INFO id;unsigned char buffer[1024];DWORD length,index;
    uint64_t parent_id[3],root_id[3],cookie,others[GRP_OWNERS];PSID sid;wchar_t path[1024];int before,mode;char out[4];
    assert(argc==4);mode=_wtoi(argv[3]);assert(mode>=1&&mode<=5);
    assert(OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token));
    assert(GetTokenInformation(token,TokenUser,buffer,sizeof(buffer),&length));sid=((TOKEN_USER*)buffer)->User.Sid;
    assert(real_close(token));length=GetLengthSid(sid);
    parent=CreateFileW(argv[1],FILE_TRAVERSE|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(parent,FileIdInfo,&id,sizeof(id)));
    parent_id[0]=id.VolumeSerialNumber;memcpy(parent_id+1,id.FileId.Identifier,16);
    assert(swprintf(path,1024,L"%ls\\%ls",argv[1],argv[2])>0);
    root=CreateFileW(path,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(root!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(root,FileIdInfo,&id,sizeof(id)));
    root_id[0]=id.VolumeSerialNumber;memcpy(root_id+1,id.FileId.Identifier,16);assert(real_close(root));
    assert(!gitgo_tree_reserve(&cookie,sid,length,TREE_RETIRE));
    if(mode==1){
        fail_open=opens+2;before=closes;
        assert(gitgo_retirement_begin(cookie,parent,parent_id,argv[2],root_id,3)!=0);
        assert(gitgo_retirement_begin(cookie,parent,parent_id,argv[2],root_id,3)==ERROR_INVALID_PARAMETER);
        assert(!gitgo_tree_retire(cookie)&&closes==before+2);
    }else{
        assert(!gitgo_retirement_begin(cookie,parent,parent_id,argv[2],root_id,3));
        assert(!gitgo_retirement_add(cookie,0,L"code.py",0,4,&index));assert(!gitgo_retirement_prepare(cookie,2));
        if(mode==2){
            fail_delete=1;before=deletes;assert(gitgo_retirement_delete(cookie,index)==ERROR_GEN_FAILURE);
            assert(gitgo_retirement_delete(cookie,index)==ERROR_INVALID_PARAMETER&&deletes==before+1);
            assert(gitgo_retirement_finish(cookie)==ERROR_INVALID_PARAMETER);assert(!gitgo_tree_retire(cookie));
        }else if(mode==3){
            fail_close=1;before=closes;assert(gitgo_retirement_delete(cookie,index)==ERROR_GEN_FAILURE);
            for(int i=0;i<3;i++)assert(gitgo_tree_retire(cookie)==ERROR_INVALID_STATE);assert(closes==before+1);
            for(int i=0;i<GRP_OWNERS-1;i++)assert(!gitgo_tree_reserve(others+i,sid,length,TREE_RETIRE));
            {uint64_t denied=1;assert(gitgo_tree_reserve(&denied,sid,length,TREE_RETIRE)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
            for(int i=0;i<GRP_OWNERS-1;i++)assert(!gitgo_tree_retire(others[i]));
        }else if(mode==4){
            assert(!gitgo_retirement_delete(cookie,index)&&!gitgo_retirement_delete(cookie,0)&&!gitgo_retirement_finish(cookie));assert(!gitgo_tree_retire(cookie));
        }else{
            fail_read=1;before=reads;assert(gitgo_tree_read(cookie,index,0,out,4)==ERROR_GEN_FAILURE);
            assert(gitgo_tree_read(cookie,index,0,out,4)==ERROR_INVALID_PARAMETER&&reads==before+1);
            assert(gitgo_retirement_delete(cookie,index)==ERROR_INVALID_PARAMETER);assert(!gitgo_tree_retire(cookie));
        }
    }
    assert(real_close(parent));printf("native runtime retirement fault mode %d passed; quarantined=%d\n",mode,mode==3?1:0);return 0;
}
