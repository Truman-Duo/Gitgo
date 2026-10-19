/* Descriptor-bound CAS generation construction. No default activation.
 * The caller must own the native exclusive data/collection domain, persist an
 * exact deletion intent before disposition, and retain debt through actual
 * worker termination. This module neither grants access nor refunds capacity.
 */
#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <wchar.h>
#define API __declspec(dllexport)
#define GCF_COUNT 32
enum { GCF_EMPTY,GCF_LIVE,GCF_UNKNOWN };
typedef NTSTATUS (NTAPI *CreateFn)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,
    PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
typedef ULONG (WINAPI *ErrorFn)(NTSTATUS);
typedef struct Slot { HANDLE handle;int phase; } Slot;
typedef struct Generation {
    SRWLOCK lock;uint64_t generation,retired_generation;int occupied,opened,delete_attempted,sealed,absent,purpose;
    DWORD pid,thread,last_error;Slot parent,file,probe;
    IO_STATUS_BLOCK io;wchar_t leaf[107];
    FILE_ID_INFO parent_id;
    uint64_t identity[4];
} Generation;
static Generation records[GCF_COUNT];
static SRWLOCK pool;
static DWORD error(void){DWORD e=GetLastError();return e?e:ERROR_GEN_FAILURE;}
static Generation *get(uint64_t cookie){
    unsigned i=(unsigned)(cookie&255);Generation *g;
    if(!i||i>GCF_COUNT)return 0;
    g=&records[i-1];AcquireSRWLockExclusive(&g->lock);
    if(!g->occupied||g->generation!=(cookie>>8)||g->pid!=GetCurrentProcessId()||g->thread!=GetCurrentThreadId()){
        ReleaseSRWLockExclusive(&g->lock);return 0;
    }
    return g;
}
static DWORD done(Generation *g,DWORD e){
    if(e)g->last_error=e;ReleaseSRWLockExclusive(&g->lock);return e;
}
static int close_slot(Generation *g,Slot *slot){
    if(slot->phase==GCF_UNKNOWN)return 0;
    if(!slot->handle)return 1;
    slot->phase=GCF_UNKNOWN;
    if(!CloseHandle(slot->handle)){g->sealed=1;g->last_error=error();return 0;}
    slot->handle=0;slot->phase=GCF_EMPTY;return 1;
}
static int valid_name(const wchar_t *name){
    unsigned i;
    if(!name||wcsnlen(name,107)!=106)return 0;
    for(i=0;i<64;i++)if(!((name[i]>=L'0'&&name[i]<=L'9')||(name[i]>=L'a'&&name[i]<=L'f')))return 0;
    if(name[64]!=L'.'||wcscmp(name+101,L".blob"))return 0;
    for(i=65;i<101;i++){
        if(i==73||i==78||i==83||i==88){if(name[i]!=L'-')return 0;}
        else if(!((name[i]>=L'0'&&name[i]<=L'9')||(name[i]>=L'a'&&name[i]<=L'f')))return 0;
    }
    return 1;
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
static DWORD parent_valid(Generation *g){
    FILE_ID_INFO id;DWORD e=metadata(g->parent.handle,1,&id,0);
    if(e)return e;
    return memcmp(&id,&g->parent_id,sizeof(id))?ERROR_INVALID_DATA:0;
}
static DWORD relative_open(Generation *g,Slot *slot,int deleting,int *absent){
    HMODULE ntdll=GetModuleHandleW(L"ntdll.dll");CreateFn create;ErrorFn convert;
    UNICODE_STRING name;OBJECT_ATTRIBUTES attrs;NTSTATUS status;DWORD e;
    if(slot->phase==GCF_UNKNOWN||slot->handle)return ERROR_INVALID_STATE;
    *absent=0;e=parent_valid(g);if(e)return e;
    if(!ntdll)return error();
    create=(CreateFn)(void*)GetProcAddress(ntdll,"NtCreateFile");
    convert=(ErrorFn)(void*)GetProcAddress(ntdll,"RtlNtStatusToDosError");
    if(!create||!convert)return ERROR_PROC_NOT_FOUND;
    name.Buffer=g->leaf;name.Length=106*sizeof(wchar_t);name.MaximumLength=sizeof(g->leaf);
    memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=g->parent.handle;
    attrs.ObjectName=&name;attrs.Attributes=0x40|0x1000; /* case insensitive, no reparse, no inherit */
    memset(&g->io,0,sizeof(g->io));slot->phase=GCF_LIVE;
    status=create(&slot->handle,SYNCHRONIZE|FILE_READ_ATTRIBUTES|FILE_READ_DATA|(deleting?DELETE:0),
        &attrs,&g->io,0,0,FILE_SHARE_READ,1,0x200000|0x20|0x40,0,0);
    /* A missing final name under the retained native parent is the only
     * absence result. Permission/type/path/deletion-pending errors are unknown. */
    if(status==(NTSTATUS)0xc0000034&&!slot->handle){*absent=1;slot->phase=GCF_EMPTY;return 0;}
    if(status<0)return convert(status);
    if(!slot->handle)return ERROR_INVALID_HANDLE;
    return 0;
}
API DWORD gitgo_cas_generation_reserve(uint64_t *cookie,int purpose){
    Generation *g=0;uint64_t generation;unsigned i;
    if(!cookie)return ERROR_INVALID_PARAMETER;*cookie=0;
    /* Immutable 0=reader, 1=exact committed retirement, 2=known unpublished
     * partial-file retirement. The last requires the coordinator's persisted
     * creation identity, unpublished-batch proof and exact input prefix check. */
    if(purpose<0||purpose>2)return ERROR_INVALID_PARAMETER;
    AcquireSRWLockExclusive(&pool);
    for(i=0;i<GCF_COUNT;i++)if(!records[i].occupied&&!records[i].generation){g=&records[i];break;}
    if(!g){ReleaseSRWLockExclusive(&pool);return ERROR_NOT_ENOUGH_QUOTA;}
    AcquireSRWLockExclusive(&g->lock);generation=g->generation+1;
    memset((char*)g+offsetof(Generation,generation),0,sizeof(*g)-offsetof(Generation,generation));
    g->generation=generation;g->occupied=1;g->pid=GetCurrentProcessId();
    g->thread=GetCurrentThreadId();g->purpose=purpose;*cookie=(generation<<8)|(i+1);
    ReleaseSRWLockExclusive(&g->lock);ReleaseSRWLockExclusive(&pool);return 0;
}
API DWORD gitgo_cas_generation_open(uint64_t cookie,HANDLE parent,const uint64_t *parent_id,DWORD parent_count,
    const wchar_t *name,const uint64_t *expected,DWORD expected_count){
    Generation *g=get(cookie);DWORD e;FILE_ID_INFO id;uint64_t size;int absent;
    if(!g)return ERROR_INVALID_HANDLE;
    if(g->opened||g->sealed||!parent||!parent_id||parent_count!=3||!expected||expected_count!=4||!valid_name(name)||expected[3]>65536)return done(g,ERROR_INVALID_PARAMETER);
    g->opened=1;memcpy(g->leaf,name,sizeof(g->leaf));
    g->parent.phase=GCF_LIVE;
    if(!DuplicateHandle(GetCurrentProcess(),parent,GetCurrentProcess(),&g->parent.handle,0,FALSE,DUPLICATE_SAME_ACCESS)){
        g->sealed=1;return done(g,error());
    }
    e=metadata(g->parent.handle,1,&g->parent_id,0);if(e)goto failed;
    if(g->parent_id.VolumeSerialNumber!=parent_id[0]||memcmp(g->parent_id.FileId.Identifier,parent_id+1,16)){
        e=ERROR_INVALID_DATA;goto failed;
    }
    e=relative_open(g,&g->file,g->purpose!=0,&absent);if(e)goto failed;
    if(absent){g->absent=1;return done(g,0);}
    e=metadata(g->file.handle,0,&id,&size);if(e)goto failed;
    if(id.VolumeSerialNumber!=g->parent_id.VolumeSerialNumber||(g->purpose==2?size>expected[3]:size!=expected[3])){e=ERROR_INVALID_DATA;goto failed;}
    g->identity[0]=id.VolumeSerialNumber;
    memcpy(&g->identity[1],id.FileId.Identifier,16);g->identity[3]=size;
    if(memcmp(g->identity,expected,(g->purpose==2?3:4)*sizeof(uint64_t))){e=ERROR_INVALID_DATA;goto failed;}
    return done(g,0);
failed:g->sealed=1;return done(g,e);
}
static DWORD file_valid(Generation *g){
    FILE_ID_INFO id;uint64_t size;DWORD e=parent_valid(g);
    if(e)return e;
    e=metadata(g->file.handle,0,&id,&size);if(e)return e;
    if(id.VolumeSerialNumber!=g->identity[0]||memcmp(id.FileId.Identifier,g->identity+1,16)
       ||size!=g->identity[3])return ERROR_INVALID_DATA;
    return 0;
}
API DWORD gitgo_cas_generation_identity(uint64_t cookie,uint64_t *values,DWORD count){
    Generation *g=get(cookie);
    if(!g)return ERROR_INVALID_HANDLE;
    if(!values||count!=5||!g->opened||g->sealed)return done(g,ERROR_INVALID_PARAMETER);
    memcpy(values,g->identity,sizeof(g->identity));values[4]=g->absent;return done(g,0);
}
API DWORD gitgo_cas_generation_read(uint64_t cookie,uint64_t offset,void *buffer,DWORD size){
    Generation *g=get(cookie);DWORD count=0,e;LARGE_INTEGER position;
    if(!g)return ERROR_INVALID_HANDLE;
    if(!buffer||!size||size>4096||g->sealed||g->delete_attempted||g->file.phase!=GCF_LIVE
       ||offset>g->identity[3]||size>g->identity[3]-offset)return done(g,ERROR_INVALID_PARAMETER);
    e=file_valid(g);if(e){g->sealed=1;return done(g,e);}
    position.QuadPart=(LONGLONG)offset;
    if(!SetFilePointerEx(g->file.handle,position,0,FILE_BEGIN)
       ||!ReadFile(g->file.handle,buffer,size,&count,0)){
        g->sealed=1;return done(g,error());
    }
    if(count!=size){g->sealed=1;return done(g,ERROR_HANDLE_EOF);}
    return done(g,0);
}
API DWORD gitgo_cas_generation_delete(uint64_t cookie,const uint64_t *expected,DWORD count){
    Generation *g=get(cookie);FILE_DISPOSITION_INFO info;DWORD e;
    if(!g)return ERROR_INVALID_HANDLE;
    if(!expected||count!=4||g->sealed||g->delete_attempted||g->absent||!g->purpose||g->file.phase!=GCF_LIVE)
        return done(g,ERROR_INVALID_STATE);
    if(memcmp(expected,g->identity,sizeof(g->identity)))return done(g,ERROR_INVALID_DATA);
    e=file_valid(g);if(e){g->sealed=1;return done(g,e);}
    /* Consumed before effect. Unknown disposition is never repeated. */
    g->delete_attempted=1;info.DeleteFile=TRUE;
    if(!SetFileInformationByHandle(g->file.handle,FileDispositionInfo,&info,sizeof(info))){
        g->sealed=1;return done(g,error());
    }
    if(!close_slot(g,&g->file))return done(g,ERROR_INVALID_STATE);
    return done(g,0);
}
API DWORD gitgo_cas_generation_absent(uint64_t cookie,int *out){
    Generation *g=get(cookie);DWORD e;int absent;
    if(!g)return ERROR_INVALID_HANDLE;
    if(!out||!g->opened||g->sealed||g->file.phase==GCF_UNKNOWN||g->file.handle)return done(g,ERROR_INVALID_STATE);
    *out=0;e=relative_open(g,&g->probe,0,&absent);if(e)return done(g,e);
    if(!close_slot(g,&g->probe))return done(g,ERROR_INVALID_STATE);
    *out=absent;return done(g,0);
}
API DWORD gitgo_cas_generation_retire(uint64_t cookie){
    Generation *g;unsigned slot=(unsigned)(cookie&255);uint64_t generation=cookie>>8;
    if(!slot||slot>GCF_COUNT||!generation)return ERROR_INVALID_HANDLE;
    AcquireSRWLockExclusive(&pool);g=&records[slot-1];AcquireSRWLockExclusive(&g->lock);
    if(g->generation!=generation||g->pid!=GetCurrentProcessId()||g->thread!=GetCurrentThreadId()){
        ReleaseSRWLockExclusive(&pool);return done(g,ERROR_INVALID_HANDLE);
    }
    if(g->retired_generation==generation){ReleaseSRWLockExclusive(&pool);return done(g,0);}
    if(!g->occupied){ReleaseSRWLockExclusive(&pool);return done(g,ERROR_INVALID_STATE);}
    g->sealed=1;
    if(!close_slot(g,&g->file)||!close_slot(g,&g->probe)||!close_slot(g,&g->parent)){
        ReleaseSRWLockExclusive(&pool);return done(g,ERROR_INVALID_STATE);
    }
    g->occupied=0;g->retired_generation=generation;
    ReleaseSRWLockExclusive(&pool);return done(g,0);
}
API DWORD gitgo_cas_generation_abi(void){return 1;}
