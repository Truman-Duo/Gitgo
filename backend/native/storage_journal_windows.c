/* Bounded native Host journal I/O ABI. The existing Host gate and trusted
 * codec supply authority/state transitions. This module owns every handle
 * before effects, compares the entire prior snapshot, flushes and confirms
 * bytes, and quarantines unknown writes/closes. No compaction/refund API. */
#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <wchar.h>
#define API __declspec(dllexport)
#define GSJ_COUNT 8
#define GSJ_BYTES 8192
#define GSJ_CELL 4096
enum { GSJ_EMPTY,GSJ_LIVE,GSJ_UNKNOWN };
typedef NTSTATUS (NTAPI *CreateFn)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,
    PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
typedef ULONG (WINAPI *ErrorFn)(NTSTATUS);
typedef struct Slot { HANDLE handle;int phase; } Slot;
typedef struct Journal {
    SRWLOCK lock;uint64_t generation;int occupied,opened,sealed,initialized;
    DWORD pid,last_error;Slot parent,file;IO_STATUS_BLOCK io;
    FILE_ID_INFO parent_id,file_id;unsigned char snapshot[GSJ_BYTES];
} Journal;
static Journal records[GSJ_COUNT];
static SRWLOCK pool;
static DWORD error(void){DWORD e=GetLastError();return e?e:ERROR_GEN_FAILURE;}
static Journal *get(uint64_t cookie){
    unsigned i=(unsigned)(cookie&255);Journal *j;
    if(!i||i>GSJ_COUNT)return 0;
    j=&records[i-1];AcquireSRWLockExclusive(&j->lock);
    if(!j->occupied||j->generation!=(cookie>>8)||j->pid!=GetCurrentProcessId()){
        ReleaseSRWLockExclusive(&j->lock);return 0;
    }
    return j;
}
static DWORD done(Journal *j,DWORD e){
    if(e)j->last_error=e;ReleaseSRWLockExclusive(&j->lock);return e;
}
static DWORD unknown(Journal *j,DWORD e){j->sealed=1;return done(j,e);}
static int close_slot(Journal *j,Slot *s){
    if(s->phase==GSJ_UNKNOWN)return 0;
    if(!s->handle)return 1;
    s->phase=GSJ_UNKNOWN;
    if(!CloseHandle(s->handle)){j->last_error=error();return 0;}
    s->handle=0;s->phase=GSJ_EMPTY;return 1;
}
static DWORD metadata(HANDLE handle,int directory,FILE_ID_INFO *id,uint64_t *size){
    BY_HANDLE_FILE_INFORMATION info;
    if(GetFileType(handle)!=FILE_TYPE_DISK)return ERROR_INVALID_DATA;
    if(!GetFileInformationByHandle(handle,&info))return error();
    if((info.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT)||
        (!!(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY))!=directory||
        (!directory&&info.nNumberOfLinks!=1))return ERROR_INVALID_DATA;
    if(!GetFileInformationByHandleEx(handle,FileIdInfo,id,sizeof(*id)))return error();
    if(size)*size=((uint64_t)info.nFileSizeHigh<<32)|info.nFileSizeLow;
    return 0;
}
static int same_identity(const FILE_ID_INFO *id,const uint64_t *expected){
    return id->VolumeSerialNumber==expected[0]&&!memcmp(id->FileId.Identifier,expected+1,16);
}
static DWORD validate(Journal *j,uint64_t size){
    FILE_ID_INFO id;uint64_t actual;DWORD e;
    e=metadata(j->parent.handle,1,&id,0);if(e)return e;
    if(memcmp(&id,&j->parent_id,sizeof(id)))return ERROR_INVALID_DATA;
    e=metadata(j->file.handle,0,&id,&actual);if(e)return e;
    return actual!=size||memcmp(&id,&j->file_id,sizeof(id))?ERROR_INVALID_DATA:0;
}
static DWORD seek(Journal *j,DWORD offset){
    LARGE_INTEGER position;position.QuadPart=offset;
    return SetFilePointerEx(j->file.handle,position,0,FILE_BEGIN)?0:error();
}
static DWORD read_all(Journal *j){
    DWORD e,count=0;e=validate(j,GSJ_BYTES);if(e)return e;
    e=seek(j,0);if(e)return e;
    if(!ReadFile(j->file.handle,j->snapshot,GSJ_BYTES,&count,0))return error();
    return count==GSJ_BYTES?0:ERROR_HANDLE_EOF;
}
API DWORD gitgo_journal_reserve(uint64_t *cookie){
    Journal *j=0;uint64_t generation;unsigned i;
    if(!cookie)return ERROR_INVALID_PARAMETER;*cookie=0;
    AcquireSRWLockExclusive(&pool);
    for(i=0;i<GSJ_COUNT;i++)if(!records[i].occupied&&records[i].generation<(UINT64_MAX>>8)){j=&records[i];break;}
    if(!j){ReleaseSRWLockExclusive(&pool);return ERROR_NOT_ENOUGH_QUOTA;}
    AcquireSRWLockExclusive(&j->lock);generation=j->generation+1;
    memset((char*)j+offsetof(Journal,generation),0,sizeof(*j)-offsetof(Journal,generation));
    j->generation=generation;j->occupied=1;j->pid=GetCurrentProcessId();*cookie=(generation<<8)|(i+1);
    ReleaseSRWLockExclusive(&j->lock);ReleaseSRWLockExclusive(&pool);return 0;
}
API DWORD gitgo_journal_open(uint64_t cookie,HANDLE parent,DWORD index,const uint64_t *expected,
                   DWORD count,int initialize){
    Journal *j=get(cookie);DWORD e;uint64_t size;wchar_t leaf[15];
    HMODULE ntdll;CreateFn create;ErrorFn convert;UNICODE_STRING name;
    OBJECT_ATTRIBUTES attrs;NTSTATUS status;
    if(!j)return ERROR_INVALID_HANDLE;
    if(j->opened||j->sealed||!parent||index>=64||!expected||count!=6||
       (initialize!=0&&initialize!=1))return done(j,ERROR_INVALID_PARAMETER);
    j->opened=1;j->parent.phase=GSJ_LIVE;
    if(!DuplicateHandle(GetCurrentProcess(),parent,GetCurrentProcess(),&j->parent.handle,0,FALSE,DUPLICATE_SAME_ACCESS))
        return unknown(j,error());
    e=metadata(j->parent.handle,1,&j->parent_id,0);if(e)return unknown(j,e);
    if(!same_identity(&j->parent_id,expected))return unknown(j,ERROR_INVALID_DATA);
    ntdll=GetModuleHandleW(L"ntdll.dll");if(!ntdll)return unknown(j,error());
    create=(CreateFn)(void*)GetProcAddress(ntdll,"NtCreateFile");
    convert=(ErrorFn)(void*)GetProcAddress(ntdll,"RtlNtStatusToDosError");
    if(!create||!convert)return unknown(j,ERROR_PROC_NOT_FOUND);
    if(swprintf(leaf,15,L"slot-%04lu.json",(unsigned long)index)!=14)return unknown(j,ERROR_INVALID_DATA);
    name.Buffer=leaf;name.Length=28;name.MaximumLength=30;
    memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=j->parent.handle;
    attrs.ObjectName=&name;attrs.Attributes=0x40|0x1000;
    j->file.phase=GSJ_LIVE;
    status=create(&j->file.handle,SYNCHRONIZE|FILE_READ_ATTRIBUTES|FILE_READ_DATA|FILE_WRITE_DATA,
        &attrs,&j->io,0,0,FILE_SHARE_READ,1,0x200000|0x20|0x40,0,0);
    if(status<0)return unknown(j,convert(status));
    if(!j->file.handle)return unknown(j,ERROR_INVALID_HANDLE);
    e=metadata(j->file.handle,0,&j->file_id,&size);if(e)return unknown(j,e);
    if(!same_identity(&j->file_id,expected+3)||j->file_id.VolumeSerialNumber!=j->parent_id.VolumeSerialNumber||
        size!=(initialize?0:GSJ_BYTES))return unknown(j,ERROR_INVALID_DATA);
    j->initialized=!initialize;return done(j,0);
}
API DWORD gitgo_journal_initialize(uint64_t cookie){
    Journal *j=get(cookie);DWORD e,count=0;
    if(!j)return ERROR_INVALID_HANDLE;
    if(!j->opened||j->sealed||j->initialized)return done(j,ERROR_INVALID_STATE);
    e=validate(j,0);if(e)return unknown(j,e);
    /* Consume before write; an after-effect failure cannot be retried. */
    j->sealed=1;
    memset(j->snapshot,0,sizeof(j->snapshot));
    e=seek(j,0);if(e)return done(j,e);
    if(!WriteFile(j->file.handle,j->snapshot,GSJ_BYTES,&count,0))return done(j,error());
    if(count!=GSJ_BYTES)return done(j,ERROR_WRITE_FAULT);
    if(!FlushFileBuffers(j->file.handle))return done(j,error());
    e=read_all(j);if(e)return done(j,e);
    for(unsigned i=0;i<GSJ_BYTES;i++)if(j->snapshot[i])return done(j,ERROR_INVALID_DATA);
    j->initialized=1;j->sealed=0;return done(j,0);
}
API DWORD gitgo_journal_read(uint64_t cookie,void *output,DWORD capacity){
    Journal *j=get(cookie);DWORD e;
    if(!j)return ERROR_INVALID_HANDLE;
    if(!output||capacity!=GSJ_BYTES||!j->opened||!j->initialized||j->sealed)return done(j,ERROR_INVALID_STATE);
    e=read_all(j);if(e)return unknown(j,e);
    memcpy(output,j->snapshot,GSJ_BYTES);return done(j,0);
}
API DWORD gitgo_journal_update(uint64_t cookie,const void *prior,DWORD prior_size,DWORD cell,
                     const void *value,DWORD size){
    Journal *j=get(cookie);DWORD e,count=0;
    if(!j)return ERROR_INVALID_HANDLE;
    if(!prior||prior_size!=GSJ_BYTES||cell>1||!value||size!=GSJ_CELL||
       !j->opened||!j->initialized||j->sealed)return done(j,ERROR_INVALID_STATE);
    e=read_all(j);if(e)return unknown(j,e);
    if(memcmp(j->snapshot,prior,GSJ_BYTES))return unknown(j,ERROR_INVALID_DATA);
    j->sealed=1;
    e=seek(j,cell*GSJ_CELL);if(e)return done(j,e);
    if(!WriteFile(j->file.handle,value,GSJ_CELL,&count,0))return done(j,error());
    if(count!=GSJ_CELL)return done(j,ERROR_WRITE_FAULT);
    if(!FlushFileBuffers(j->file.handle))return done(j,error());
    e=read_all(j);if(e)return done(j,e);
    if(memcmp(j->snapshot+cell*GSJ_CELL,value,GSJ_CELL)||
       memcmp(j->snapshot+(1-cell)*GSJ_CELL,(const char*)prior+(1-cell)*GSJ_CELL,GSJ_CELL))
        return done(j,ERROR_INVALID_DATA);
    j->sealed=0;return done(j,0);
}
API DWORD gitgo_journal_retire(uint64_t cookie){
    Journal *j;unsigned index=(unsigned)(cookie&255);uint64_t generation=cookie>>8;
    if(!index||index>GSJ_COUNT||!generation)return ERROR_INVALID_HANDLE;
    AcquireSRWLockExclusive(&pool);j=&records[index-1];AcquireSRWLockExclusive(&j->lock);
    if(j->pid!=GetCurrentProcessId()){
        ReleaseSRWLockExclusive(&pool);return done(j,ERROR_INVALID_HANDLE);
    }
    /* Confirm recorded retirement after a lost return, including slot reuse.
     * A failed close never advances this generation or releases its capacity. */
    if(generation<j->generation||(generation==j->generation&&!j->occupied)){
        ReleaseSRWLockExclusive(&pool);return done(j,0);
    }
    if(generation!=j->generation){
        ReleaseSRWLockExclusive(&pool);return done(j,ERROR_INVALID_HANDLE);
    }
    j->sealed=1;
    if(!close_slot(j,&j->file)||!close_slot(j,&j->parent)){
        ReleaseSRWLockExclusive(&pool);return done(j,ERROR_INVALID_STATE);
    }
    j->occupied=0;ReleaseSRWLockExclusive(&pool);return done(j,0);
}

API DWORD gitgo_journal_abi(void){return 1;}
