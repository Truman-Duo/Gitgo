/* Fresh fixture only: after-effect faults retain actual native resources. */
#include <windows.h>
#include <winternl.h>
#include <sddl.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
typedef NTSTATUS (NTAPI *NativeCreate)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,
    PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
static NativeCreate real_create;
static FARPROC (WINAPI *real_proc)(HMODULE,LPCSTR)=GetProcAddress;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static int fail_open,fail_close,opens,closes;
static NTSTATUS NTAPI after_open(PHANDLE h,ACCESS_MASK a,POBJECT_ATTRIBUTES b,PIO_STATUS_BLOCK c,
    PLARGE_INTEGER d,ULONG e,ULONG f,ULONG g,ULONG i,PVOID j,ULONG k){
    NTSTATUS result=real_create(h,a,b,c,d,e,f,g,i,j,k);opens++;
    if(result>=0&&fail_open){fail_open=0;return (NTSTATUS)0xc0000001;}
    return result;
}
static FARPROC WINAPI injected_proc(HMODULE module,LPCSTR name){
    if(!strcmp(name,"NtCreateFile")){
        real_create=(NativeCreate)(void*)real_proc(module,name);return (FARPROC)(void*)after_open;
    }
    return real_proc(module,name);
}
static BOOL WINAPI after_close(HANDLE handle){
    BOOL result=real_close(handle);closes++;
    if(result&&fail_close){fail_close=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    return result;
}
#define GetProcAddress injected_proc
#define CloseHandle after_close
#include "../../backend/native/storage_tree_windows.c"

static uint64_t words[3],file_words[3];
static uint64_t reserve(HANDLE parent,PSID sid){
    uint64_t cookie;
    assert(!gitgo_tree_reserve(&cookie,sid,GetLengthSid(sid),TREE_DATA));
    assert(!gitgo_tree_root(cookie,parent,words,3));return cookie;
}
int wmain(int argc,wchar_t **argv){
    HANDLE parent,file;PSID sid=0;FILE_ID_INFO id;uint64_t cookie;DWORD index;int before,i;
    wchar_t path[32768];Runtime *record;
    assert(argc==3&&ConvertStringSidToSidW(argv[2],&sid));
    parent=CreateFileW(argv[1],FILE_LIST_DIRECTORY|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(parent,FileIdInfo,&id,sizeof(id)));
    words[0]=id.VolumeSerialNumber;memcpy(words+1,id.FileId.Identifier,16);
    assert(swprintf(path,32768,L"%ls\\state.sqlite3",argv[1])>0);
    file=CreateFileW(path,GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,0,CREATE_NEW,0,0);
    assert(file!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(file,FileIdInfo,&id,sizeof(id)));
    file_words[0]=id.VolumeSerialNumber;memcpy(file_words+1,id.FileId.Identifier,16);assert(real_close(file));
    cookie=reserve(parent,sid);fail_open=1;before=opens;
    assert(gitgo_data_add(cookie,0,L"state.sqlite3",0,4096,file_words,3,&index)!=0);
    assert(gitgo_data_add(cookie,0,L"state.sqlite3",0,4096,file_words,3,&index)!=0&&opens==before+1);
    record=&records[(cookie&255)-1];assert(record->nodes[1].handle&&record->nodes[0].handle);
    before=closes;assert(!gitgo_tree_retire(cookie)&&closes==before+2);
    assert(!gitgo_tree_retire(cookie)&&closes==before+2);

    cookie=reserve(parent,sid);
    assert(!gitgo_data_add(cookie,0,L"state.sqlite3",0,4096,file_words,3,&index));
    before=closes;fail_close=1;
    assert(gitgo_data_release(cookie,index)==ERROR_GEN_FAILURE&&closes==before+1);
    record=&records[(cookie&255)-1];assert(record->nodes[index].phase==3&&record->nodes[0].phase==2);
    for(i=0;i<3;i++)assert(gitgo_tree_retire(cookie)==ERROR_INVALID_STATE);
    assert(closes==before+1&&record->nodes[0].handle);
    assert(real_close(parent)&&!LocalFree(sid));
    printf("data native faults passed: after-open-owned, after-close-not-retried, parent-retained; quarantined=1\n");
    return 0;
}
