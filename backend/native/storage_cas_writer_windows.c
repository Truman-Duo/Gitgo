/* Bounded native CAS creation construction dependency. Durable exact-name intent, exclusive
 * namespace and aggregate capacity are required before calling this primitive.
 * No overwrite, resize, retry after ambiguous effect, or capacity refund. */
#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <wchar.h>
#define API __declspec(dllexport)
#define GCW_COUNT 8
enum { EMPTY,OPEN,SEALED,FAILED };
enum { HANDLE_EMPTY,HANDLE_LIVE,HANDLE_UNKNOWN };
typedef NTSTATUS (NTAPI *CreateFn)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,
    PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
typedef ULONG (WINAPI *ErrorFn)(NTSTATUS);
typedef struct Slot {HANDLE handle;int phase;} Slot;
typedef struct Writer {
    SRWLOCK lock;uint64_t generation,retired_generation;int occupied,phase,attempted;DWORD pid,thread,last_error;
    Slot parent,file;IO_STATUS_BLOCK io;FILE_ID_INFO parent_id,file_id;
    uint64_t length,written;wchar_t leaf[107];
} Writer;
static Writer records[GCW_COUNT];static SRWLOCK pool;
static DWORD error(void){DWORD value=GetLastError();return value?value:ERROR_GEN_FAILURE;}
static Writer *get(uint64_t cookie){
    unsigned i=(unsigned)(cookie&255);Writer *w;
    if(!i||i>GCW_COUNT)return 0;
    w=&records[i-1];AcquireSRWLockExclusive(&w->lock);
    if(!w->occupied||w->generation!=(cookie>>8)||w->pid!=GetCurrentProcessId()||w->thread!=GetCurrentThreadId()){
        ReleaseSRWLockExclusive(&w->lock);return 0;
    }
    return w;
}
static DWORD done(Writer *w,DWORD e){if(e)w->last_error=e;ReleaseSRWLockExclusive(&w->lock);return e;}
static DWORD failed(Writer *w,DWORD e){w->phase=FAILED;return done(w,e);}
static int close_slot(Writer *w,Slot *s){
    if(s->phase==HANDLE_UNKNOWN)return 0;
    if(!s->handle)return 1;
    s->phase=HANDLE_UNKNOWN;
    if(!CloseHandle(s->handle)){w->last_error=error();return 0;}
    s->handle=0;s->phase=HANDLE_EMPTY;return 1;
}
static int valid_name(const wchar_t *name){
    unsigned i;if(!name||wcsnlen(name,107)!=106)return 0;
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
static DWORD valid_file(Writer *w,uint64_t expected_size){
    FILE_ID_INFO id;uint64_t size;DWORD e;
    e=metadata(w->parent.handle,1,&id,0);if(e)return e;
    if(memcmp(&id,&w->parent_id,sizeof(id)))return ERROR_INVALID_DATA;
    e=metadata(w->file.handle,0,&id,&size);if(e)return e;
    return memcmp(&id,&w->file_id,sizeof(id))||size!=expected_size?ERROR_INVALID_DATA:0;
}
API DWORD gitgo_cas_writer_reserve(uint64_t *cookie){
    Writer *w=0;uint64_t generation;unsigned i;
    if(!cookie)return ERROR_INVALID_PARAMETER;*cookie=0;AcquireSRWLockExclusive(&pool);
    for(i=0;i<GCW_COUNT;i++)if(!records[i].occupied&&!records[i].generation){w=&records[i];break;}
    if(!w){ReleaseSRWLockExclusive(&pool);return ERROR_NOT_ENOUGH_QUOTA;}
    AcquireSRWLockExclusive(&w->lock);generation=w->generation+1;
    memset((char*)w+offsetof(Writer,generation),0,sizeof(*w)-offsetof(Writer,generation));
    w->generation=generation;w->occupied=1;w->pid=GetCurrentProcessId();w->thread=GetCurrentThreadId();*cookie=(generation<<8)|(i+1);
    ReleaseSRWLockExclusive(&w->lock);ReleaseSRWLockExclusive(&pool);return 0;
}
API DWORD gitgo_cas_writer_create(uint64_t cookie,HANDLE parent,const uint64_t *parent_id,DWORD count,
                     const wchar_t *leaf,uint64_t length){
    Writer *w=get(cookie);DWORD e;uint64_t size;HMODULE nt;CreateFn create;ErrorFn convert;
    UNICODE_STRING name;OBJECT_ATTRIBUTES attrs;NTSTATUS status;
    if(!w)return ERROR_INVALID_HANDLE;
    if(w->attempted||w->phase!=EMPTY||!parent||!parent_id||count!=3||!valid_name(leaf)||length>65536)
        return done(w,ERROR_INVALID_PARAMETER);
    w->attempted=1;w->phase=FAILED;w->length=length;memcpy(w->leaf,leaf,sizeof(w->leaf));
    w->parent.phase=HANDLE_LIVE;
    if(!DuplicateHandle(GetCurrentProcess(),parent,GetCurrentProcess(),&w->parent.handle,0,FALSE,DUPLICATE_SAME_ACCESS))
        return done(w,error());
    e=metadata(w->parent.handle,1,&w->parent_id,0);if(e)return done(w,e);
    if(w->parent_id.VolumeSerialNumber!=parent_id[0]||memcmp(w->parent_id.FileId.Identifier,parent_id+1,16))
        return done(w,ERROR_INVALID_DATA);
    nt=GetModuleHandleW(L"ntdll.dll");if(!nt)return done(w,error());
    create=(CreateFn)(void*)GetProcAddress(nt,"NtCreateFile");convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");
    if(!create||!convert)return done(w,ERROR_PROC_NOT_FOUND);
    name.Buffer=w->leaf;name.Length=106*sizeof(wchar_t);name.MaximumLength=sizeof(w->leaf);
    memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=w->parent.handle;
    attrs.ObjectName=&name;attrs.Attributes=0x40|0x1000;
    w->file.phase=HANDLE_LIVE;
    /* FILE_CREATE only: collision never opens, overwrites or adopts a file. */
    status=create(&w->file.handle,SYNCHRONIZE|FILE_READ_ATTRIBUTES|FILE_READ_DATA|FILE_WRITE_DATA,
        &attrs,&w->io,0,0,FILE_SHARE_READ,2,0x200000|0x20|0x40,0,0);
    if(status<0)return done(w,convert(status));
    if(!w->file.handle)return done(w,ERROR_INVALID_HANDLE);
    e=metadata(w->file.handle,0,&w->file_id,&size);if(e)return done(w,e);
    if(size||w->file_id.VolumeSerialNumber!=w->parent_id.VolumeSerialNumber)return done(w,ERROR_INVALID_DATA);
    w->phase=OPEN;return done(w,0);
}
API DWORD gitgo_cas_writer_write(uint64_t cookie,const void *data,DWORD size){
    Writer *w=get(cookie);DWORD count=0,e;
    if(!w)return ERROR_INVALID_HANDLE;
    if(w->phase!=OPEN||!data||!size||size>4096||w->written>w->length||size>w->length-w->written)
        return done(w,ERROR_INVALID_PARAMETER);
    e=valid_file(w,w->written);if(e)return failed(w,e);
    w->phase=FAILED;
    if(!WriteFile(w->file.handle,data,size,&count,0))return done(w,error());
    if(count!=size)return done(w,ERROR_WRITE_FAULT);
    w->written+=size;w->phase=OPEN;return done(w,0);
}
API DWORD gitgo_cas_writer_seal(uint64_t cookie){
    Writer *w=get(cookie);DWORD e;
    if(!w)return ERROR_INVALID_HANDLE;
    if(w->phase!=OPEN||w->written!=w->length)return done(w,ERROR_INVALID_STATE);
    w->phase=FAILED;
    if(!FlushFileBuffers(w->file.handle))return done(w,error());
    e=valid_file(w,w->length);if(e)return done(w,e);
    w->phase=SEALED;return done(w,0);
}
API DWORD gitgo_cas_writer_identity(uint64_t cookie,uint64_t *out,DWORD count){
    Writer *w=get(cookie);DWORD e;
    if(!w)return ERROR_INVALID_HANDLE;
    if(w->phase!=SEALED||!out||count!=4)return done(w,ERROR_INVALID_STATE);
    e=valid_file(w,w->length);if(e)return failed(w,e);
    out[0]=w->file_id.VolumeSerialNumber;memcpy(out+1,w->file_id.FileId.Identifier,16);out[3]=w->length;
    return done(w,0);
}
API DWORD gitgo_cas_writer_read(uint64_t cookie,uint64_t offset,void *buffer,DWORD size){
    Writer *w=get(cookie);DWORD count=0,e;LARGE_INTEGER position;
    if(!w)return ERROR_INVALID_HANDLE;
    if(w->phase!=SEALED||!buffer||!size||size>4096||offset>w->length||size>w->length-offset)
        return done(w,ERROR_INVALID_PARAMETER);
    e=valid_file(w,w->length);if(e)return failed(w,e);
    position.QuadPart=(LONGLONG)offset;
    if(!SetFilePointerEx(w->file.handle,position,0,FILE_BEGIN))return failed(w,error());
    if(!ReadFile(w->file.handle,buffer,size,&count,0))return failed(w,error());
    if(count!=size)return failed(w,ERROR_HANDLE_EOF);
    return done(w,0);
}
/* Exact creation identity is available before writing, so the coordinator can
 * persist the object binding before further effects. This is not a file ticket. */
API DWORD gitgo_cas_writer_created(uint64_t cookie,uint64_t *out,DWORD count){
    Writer *w=get(cookie);DWORD e;
    if(!w)return ERROR_INVALID_HANDLE;
    if((w->phase!=OPEN&&w->phase!=SEALED)||!out||count!=4)return done(w,ERROR_INVALID_STATE);
    e=valid_file(w,w->written);if(e)return failed(w,e);
    out[0]=w->file_id.VolumeSerialNumber;memcpy(out+1,w->file_id.FileId.Identifier,16);out[3]=w->written;
    return done(w,0);
}
API DWORD gitgo_cas_writer_retire(uint64_t cookie){
    Writer *w;unsigned slot=(unsigned)(cookie&255);uint64_t generation=cookie>>8;
    if(!slot||slot>GCW_COUNT||!generation)return ERROR_INVALID_HANDLE;
    AcquireSRWLockExclusive(&pool);w=&records[slot-1];AcquireSRWLockExclusive(&w->lock);
    if(w->generation!=generation||w->pid!=GetCurrentProcessId()||w->thread!=GetCurrentThreadId()){
        ReleaseSRWLockExclusive(&pool);return done(w,ERROR_INVALID_HANDLE);
    }
    if(w->retired_generation==generation){ReleaseSRWLockExclusive(&pool);return done(w,0);}
    if(!w->occupied){ReleaseSRWLockExclusive(&pool);return done(w,ERROR_INVALID_STATE);}
    w->phase=FAILED;
    /* Unknown child close retains the exact parent and never repeats a close. */
    if(!close_slot(w,&w->file)||!close_slot(w,&w->parent)){
        ReleaseSRWLockExclusive(&pool);return done(w,ERROR_INVALID_STATE);
    }
    w->occupied=0;w->retired_generation=generation;
    ReleaseSRWLockExclusive(&pool);return done(w,0);
}
API DWORD gitgo_cas_writer_abi(void){return 1;}
