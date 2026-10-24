/* Actual I/O failures in a supplied private fixture, no Python callbacks. */
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <wchar.h>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
static int fail_write_after,short_write,fail_flush_after,fail_close_after,fail_read_number;
static int writes,flushes,closes,reads;
static BOOL (WINAPI *real_write)(HANDLE,LPCVOID,DWORD,LPDWORD,LPOVERLAPPED)=WriteFile;
static BOOL (WINAPI *real_read)(HANDLE,LPVOID,DWORD,LPDWORD,LPOVERLAPPED)=ReadFile;
static BOOL (WINAPI *real_flush)(HANDLE)=FlushFileBuffers;
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static BOOL WINAPI injected_write(HANDLE h,LPCVOID p,DWORD n,LPDWORD count,LPOVERLAPPED o){
    BOOL ok;writes++;if(short_write){n/=2;short_write=0;}
    ok=real_write(h,p,n,count,o);
    if(ok&&fail_write_after){fail_write_after=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    return ok;
}
static BOOL WINAPI injected_read(HANDLE h,LPVOID p,DWORD n,LPDWORD count,LPOVERLAPPED o){
    BOOL ok=real_read(h,p,n,count,o);reads++;
    if(ok&&reads==fail_read_number){SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    return ok;
}
static BOOL WINAPI injected_flush(HANDLE h){
    BOOL ok=real_flush(h);flushes++;
    if(ok&&fail_flush_after){fail_flush_after=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    return ok;
}
static BOOL WINAPI injected_close(HANDLE h){
    BOOL ok=real_close(h);closes++;
    if(ok&&fail_close_after){fail_close_after=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    return ok;
}
#define WriteFile injected_write
#define ReadFile injected_read
#define FlushFileBuffers injected_flush
#define CloseHandle injected_close
#include "../../backend/native/storage_journal_windows.c"
static uint64_t open_journal(HANDLE parent,uint64_t *ids,int init){
    uint64_t cookie=0;assert(!gitgo_journal_reserve(&cookie));
    assert(!gitgo_journal_open(cookie,parent,0,ids,6,init));return cookie;
}
int wmain(int argc,wchar_t **argv){
    wchar_t path[32768];HANDLE parent,file;FILE_ID_INFO id;
    uint64_t ids[6],cookie,other[GSJ_COUNT];int before,i;
    unsigned char prior[GSJ_BYTES],data[GSJ_CELL],after[GSJ_BYTES];
    assert(argc==2&&wcslen(argv[1])<32000);
    assert(swprintf(path,32768,L"%ls\\slot-0000.json",argv[1])>0);
    parent=CreateFileW(argv[1],FILE_TRAVERSE|FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,0,
        OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,0);
    assert(parent!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(parent,FileIdInfo,&id,sizeof(id)));
    ids[0]=id.VolumeSerialNumber;memcpy(ids+1,id.FileId.Identifier,16);
    file=CreateFileW(path,GENERIC_WRITE|FILE_READ_ATTRIBUTES,0,0,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,0);
    assert(file!=INVALID_HANDLE_VALUE&&GetFileInformationByHandleEx(file,FileIdInfo,&id,sizeof(id)));
    ids[3]=id.VolumeSerialNumber;memcpy(ids+4,id.FileId.Identifier,16);assert(real_close(file));
    cookie=open_journal(parent,ids,1);assert(!gitgo_journal_initialize(cookie));
    assert(gitgo_journal_initialize(cookie)==ERROR_INVALID_STATE);
    assert(!gitgo_journal_read(cookie,prior,sizeof(prior)));memset(data,0x41,sizeof(data));
    before=writes;fail_write_after=1;
    assert(gitgo_journal_update(cookie,prior,sizeof(prior),0,data,sizeof(data))==ERROR_GEN_FAILURE);
    assert(gitgo_journal_update(cookie,prior,sizeof(prior),0,data,sizeof(data))==ERROR_INVALID_STATE&&writes==before+1);
    assert(!gitgo_journal_retire(cookie));cookie=open_journal(parent,ids,0);
    assert(!gitgo_journal_read(cookie,prior,sizeof(prior))&&!memcmp(prior,data,sizeof(data)));
    before=flushes;fail_flush_after=1;memset(data,0x42,sizeof(data));
    assert(gitgo_journal_update(cookie,prior,sizeof(prior),1,data,sizeof(data))==ERROR_GEN_FAILURE);
    assert(gitgo_journal_update(cookie,prior,sizeof(prior),1,data,sizeof(data))==ERROR_INVALID_STATE&&flushes==before+1);
    assert(!gitgo_journal_retire(cookie));cookie=open_journal(parent,ids,0);
    assert(!gitgo_journal_read(cookie,prior,sizeof(prior))&&!memcmp(prior+GSJ_CELL,data,sizeof(data)));
    short_write=1;memset(data,0x43,sizeof(data));before=writes;
    assert(gitgo_journal_update(cookie,prior,sizeof(prior),0,data,sizeof(data))==ERROR_WRITE_FAULT);
    assert(gitgo_journal_update(cookie,prior,sizeof(prior),0,data,sizeof(data))==ERROR_INVALID_STATE&&writes==before+1);
    assert(!gitgo_journal_retire(cookie));cookie=open_journal(parent,ids,0);
    assert(!gitgo_journal_read(cookie,after,sizeof(after))&&memcmp(after,prior,GSJ_CELL)&&memcmp(after,data,GSJ_CELL));
    memcpy(prior,after,sizeof(prior));fail_read_number=reads+2;before=writes;
    assert(gitgo_journal_update(cookie,prior,sizeof(prior),1,data,sizeof(data))==ERROR_GEN_FAILURE);
    assert(gitgo_journal_update(cookie,prior,sizeof(prior),1,data,sizeof(data))==ERROR_INVALID_STATE&&writes==before+1);
    assert(!gitgo_journal_retire(cookie));cookie=open_journal(parent,ids,0);
    fail_close_after=1;before=closes;assert(gitgo_journal_retire(cookie)==ERROR_INVALID_STATE);
    for(i=0;i<3;i++)assert(gitgo_journal_retire(cookie)==ERROR_INVALID_STATE);
    assert(closes==before+1);
    for(i=0;i<GSJ_COUNT-1;i++)assert(!gitgo_journal_reserve(other+i));
    {uint64_t denied=1;assert(gitgo_journal_reserve(&denied)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
    for(i=0;i<GSJ_COUNT-1;i++)assert(!gitgo_journal_retire(other[i]));
    assert(real_close(parent));
    printf("slot native faults passed: write-flush-readback-unknown-sealed, short-write-retained, close-not-retried; quarantined=1\n");
    return 0;
}
