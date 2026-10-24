/* Standalone fault fixture. The only file target is below its supplied fresh
 * temporary directory; one unknown native record is quarantined until exit. */
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <wchar.h>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
static int fail_disposition_after,fail_close_after,dispositions,closes;
static BOOL (WINAPI *real_set)(HANDLE,FILE_INFO_BY_HANDLE_CLASS,LPVOID,DWORD)=SetFileInformationByHandle;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static BOOL WINAPI injected_set(HANDLE file,FILE_INFO_BY_HANDLE_CLASS kind,LPVOID value,DWORD size){
    BOOL ok=real_set(file,kind,value,size);
    if(kind==FileDispositionInfo){
        dispositions++;
        if(ok&&fail_disposition_after){fail_disposition_after=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    }
    return ok;
}
static BOOL WINAPI injected_close(HANDLE handle){
    BOOL ok=real_close(handle);closes++;
    if(ok&&fail_close_after){fail_close_after=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    return ok;
}
#define SetFileInformationByHandle injected_set
#define CloseHandle injected_close
#include "../../backend/native/storage_cas_generation_windows.c"

static uint64_t parent_words[3],expected[4];
static void capture(HANDLE file){FILE_ID_INFO id;assert(GetFileInformationByHandleEx(file,FileIdInfo,&id,sizeof(id)));
    expected[0]=id.VolumeSerialNumber;memcpy(expected+1,id.FileId.Identifier,16);expected[3]=0;}
int wmain(int argc,wchar_t **argv){
    wchar_t full[32768];HANDLE parent,file;uint64_t cookie,values[5],other[GCF_COUNT];
    const wchar_t *leaf=L"0000000000000000000000000000000000000000000000000000000000000000.00000000-0000-0000-0000-000000000001.blob";
    int absent,i,before;
    assert(argc==2&&wcslen(argv[1])<32000);
    assert(swprintf(full,32768,L"%ls\\%ls",argv[1],leaf)>0);
    parent=CreateFileW(argv[1],FILE_TRAVERSE|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE);
    {FILE_ID_INFO id;assert(GetFileInformationByHandleEx(parent,FileIdInfo,&id,sizeof(id)));
     parent_words[0]=id.VolumeSerialNumber;memcpy(parent_words+1,id.FileId.Identifier,16);}
    file=CreateFileW(full,GENERIC_WRITE|FILE_READ_ATTRIBUTES,0,0,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,0);
    assert(file!=INVALID_HANDLE_VALUE);capture(file);assert(real_close(file));
    assert(!gitgo_cas_generation_reserve(&cookie,1)&&!gitgo_cas_generation_open(cookie,parent,parent_words,3,leaf,expected,4));
    assert(!gitgo_cas_generation_identity(cookie,values,5)&&!values[4]);
    fail_disposition_after=1;
    assert(gitgo_cas_generation_delete(cookie,values,4)==ERROR_GEN_FAILURE);
    assert(gitgo_cas_generation_delete(cookie,values,4)==ERROR_INVALID_STATE&&dispositions==1);
    assert(!gitgo_cas_generation_retire(cookie));
    assert(!gitgo_cas_generation_reserve(&cookie,1)&&!gitgo_cas_generation_open(cookie,parent,parent_words,3,leaf,expected,4));
    assert(!gitgo_cas_generation_identity(cookie,values,5)&&values[4]);
    assert(!gitgo_cas_generation_absent(cookie,&absent)&&absent);
    assert(!gitgo_cas_generation_retire(cookie));
    file=CreateFileW(full,GENERIC_WRITE|FILE_READ_ATTRIBUTES,0,0,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,0);
    assert(file!=INVALID_HANDLE_VALUE);capture(file);assert(real_close(file));
    assert(!gitgo_cas_generation_reserve(&cookie,1)&&!gitgo_cas_generation_open(cookie,parent,parent_words,3,leaf,expected,4));
    assert(!gitgo_cas_generation_identity(cookie,values,5)&&!values[4]);
    before=closes;fail_close_after=1;
    assert(gitgo_cas_generation_delete(cookie,values,4)==ERROR_INVALID_STATE);
    assert(closes==before+1&&dispositions==2);
    for(i=0;i<3;i++)assert(gitgo_cas_generation_retire(cookie)==ERROR_INVALID_STATE);
    assert(closes==before+1);
    for(i=0;i<GCF_COUNT-3;i++)assert(!gitgo_cas_generation_reserve(&other[i],0));
    {uint64_t denied=1;assert(gitgo_cas_generation_reserve(&denied,0)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
    for(i=0;i<GCF_COUNT-3;i++)assert(!gitgo_cas_generation_retire(other[i]));
    assert(real_close(parent));
    printf("generation native faults passed: disposition-unknown-not-repeated, exact-absence-recovered, unknown-close-not-retried; quarantined=1\n");
    return 0;
}
