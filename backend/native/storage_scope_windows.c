/* Windows storage handle/gate ownership. No creation or admission authority.
 * Native records precede OS effects. Every exposed handle is only a borrow;
 * owner/slot generation tokens, not raw HANDLE values, authorize closure. */
#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <wchar.h>
#include <stdio.h>
#include <userenv.h>
#define API __declspec(dllexport)
#define GSO_OWNERS 16
#define GSO_SLOTS 128
#define GSO_ABSENT ((DWORD)0xe0010001)
typedef NTSTATUS (NTAPI *CreateFn)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,
    PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
typedef ULONG (WINAPI *ErrorFn)(NTSTATUS);
typedef struct HandleSlot {
    HANDLE handle;uint64_t generation;int phase,directory,readable,lock_attempted;
    FILE_ID_INFO id;OVERLAPPED lock;IO_STATUS_BLOCK io;
} HandleSlot;
typedef struct Scope {
    SRWLOCK lock;uint64_t generation;DWORD pid,thread_id;int occupied,sealed,account_attempted;
    HandleSlot slots[GSO_SLOTS];
    unsigned char token_user[65536];wchar_t sid[256],profile[513];
} Scope;
static Scope scopes[GSO_OWNERS];static SRWLOCK pool;
static DWORD error(void){DWORD code=GetLastError();return code?code:ERROR_GEN_FAILURE;}
static Scope *get(uint64_t cookie){
    unsigned index=(unsigned)(cookie&255);Scope *s;
    if(!index||index>GSO_OWNERS)return 0;s=&scopes[index-1];AcquireSRWLockExclusive(&s->lock);
    if(!s->occupied||s->generation!=(cookie>>8)||s->pid!=GetCurrentProcessId()||s->thread_id!=GetCurrentThreadId()){
        ReleaseSRWLockExclusive(&s->lock);return 0;
    }return s;
}
static DWORD done(Scope *s,DWORD code){if(code&&code!=GSO_ABSENT)s->sealed=1;ReleaseSRWLockExclusive(&s->lock);return code;}
static HandleSlot *slot(Scope *s,uint64_t token){
    unsigned index=(unsigned)(token&255);HandleSlot *n;
    if(!index||index>GSO_SLOTS)return 0;n=&s->slots[index-1];
    return n->generation==(token>>8)&&n->phase?n:0;
}
static HandleSlot *allocate(Scope *s,uint64_t *token){
    HandleSlot *n;uint64_t next;unsigned i;*token=0;
    for(i=0;i<GSO_SLOTS;i++)if(!s->slots[i].phase&&s->slots[i].generation<(UINT64_MAX>>8))break;
    if(i==GSO_SLOTS)return 0;n=&s->slots[i];next=n->generation+1;memset(n,0,sizeof(*n));
    n->generation=next;n->phase=1;*token=(next<<8)|(i+1);return n;
}
static int leaf_ok(const wchar_t *name){
    unsigned count;if(!name)return 0;count=(unsigned)wcsnlen(name,256);
    if(!count||count>255||!wcscmp(name,L".")||!wcscmp(name,L"..")||name[count-1]==L'.'||name[count-1]==L' ')return 0;
    for(unsigned i=0;i<count;i++)if(name[i]<32||wcschr(L"\\/:*?\"<>|",name[i]))return 0;
    return 1;
}
static int drive_ok(const wchar_t *name){
    return name&&wcslen(name)==7&&!wcsncmp(name,L"\\??\\",4)&&name[5]==L':'&&name[6]==L'\\'&&
        ((name[4]>=L'A'&&name[4]<=L'Z')||(name[4]>=L'a'&&name[4]<=L'z'));
}
static DWORD metadata(HandleSlot *n){
    BY_HANDLE_FILE_INFORMATION info;FILE_ID_INFO id;
    if(!n||!n->handle||GetFileType(n->handle)!=FILE_TYPE_DISK)return ERROR_INVALID_DATA;
    if(!GetFileInformationByHandle(n->handle,&info)||!GetFileInformationByHandleEx(n->handle,FileIdInfo,&id,sizeof(id)))return error();
    if((info.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT)||
       (!!(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY))!=n->directory||(!n->directory&&info.nNumberOfLinks!=1))return ERROR_INVALID_DATA;
    if(n->phase==2&&memcmp(&id,&n->id,sizeof(id)))return ERROR_INVALID_DATA;
    n->id=id;return 0;
}
static DWORD identity(HandleSlot *n,const uint64_t *expected){
    DWORD e=metadata(n);if(e)return e;
    return !expected||n->id.VolumeSerialNumber!=expected[0]||memcmp(n->id.FileId.Identifier,expected+1,16)?ERROR_INVALID_DATA:0;
}
static DWORD opened(HandleSlot *n,HANDLE parent,const wchar_t *name,int directory,int readable,DWORD extra_access){
    HMODULE nt=GetModuleHandleW(L"ntdll.dll");CreateFn create;ErrorFn convert;
    UNICODE_STRING string;OBJECT_ATTRIBUTES attrs;NTSTATUS status;DWORD e;
    if(!nt)return error();create=(CreateFn)(void*)GetProcAddress(nt,"NtCreateFile");
    convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");if(!create||!convert)return ERROR_PROC_NOT_FOUND;
    string.Buffer=(PWSTR)name;string.Length=(USHORT)(wcslen(name)*sizeof(wchar_t));string.MaximumLength=string.Length;
    memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=parent;attrs.ObjectName=&string;attrs.Attributes=0x40|0x1000;
    n->directory=directory;n->readable=readable;
    status=create(&n->handle,SYNCHRONIZE|FILE_READ_ATTRIBUTES|extra_access|(directory?FILE_TRAVERSE:(readable?FILE_READ_DATA:0)),
        &attrs,&n->io,0,0,FILE_SHARE_READ|FILE_SHARE_WRITE,1,0x200000|0x20|(directory?1:0x40),0,0);
    if(status==(NTSTATUS)0xc0000034&&!n->handle){n->phase=0;return GSO_ABSENT;}
    if(status<0)return convert(status);if(!n->handle)return ERROR_INVALID_HANDLE;
    e=metadata(n);if(e)return e;n->phase=2;return 0;
}
API DWORD gitgo_scope_reserve(uint64_t *cookie){
    Scope *s;uint64_t next;unsigned i;if(!cookie)return ERROR_INVALID_PARAMETER;*cookie=0;
    AcquireSRWLockExclusive(&pool);
    for(i=0;i<GSO_OWNERS;i++)if(!scopes[i].occupied&&scopes[i].generation<(UINT64_MAX>>8))break;
    if(i==GSO_OWNERS){ReleaseSRWLockExclusive(&pool);return ERROR_NOT_ENOUGH_QUOTA;}
    s=&scopes[i];AcquireSRWLockExclusive(&s->lock);next=s->generation+1;
    memset((char*)s+offsetof(Scope,generation),0,sizeof(*s)-offsetof(Scope,generation));
    s->generation=next;s->occupied=1;s->pid=GetCurrentProcessId();s->thread_id=GetCurrentThreadId();*cookie=(next<<8)|(i+1);
    ReleaseSRWLockExclusive(&s->lock);ReleaseSRWLockExclusive(&pool);return 0;
}
API DWORD gitgo_scope_open(uint64_t cookie,uint64_t parent_token,const wchar_t *name,int directory,int readable,uint64_t *token){
    Scope *s=get(cookie);HandleSlot *parent=0,*n;DWORD e;
    if(!s)return ERROR_INVALID_HANDLE;
    if(s->sealed||!token||(directory!=0&&directory!=1)||(readable!=0&&readable!=1))return done(s,ERROR_INVALID_PARAMETER);
    *token=0;
    if(parent_token){
        parent=slot(s,parent_token);if(!parent||parent->phase!=2||!parent->directory||!leaf_ok(name))return done(s,ERROR_INVALID_PARAMETER);
        e=metadata(parent);if(e)return done(s,e);
    }else if(!directory||!drive_ok(name))return done(s,ERROR_INVALID_PARAMETER);
    n=allocate(s,token);if(!n)return done(s,ERROR_NOT_ENOUGH_QUOTA);
    e=opened(n,parent?parent->handle:0,name,directory,readable,0);return done(s,e);
}
API DWORD gitgo_scope_import_parent(uint64_t cookie,HANDLE parent,const uint64_t *expected,uint64_t *token){
    Scope *s=get(cookie);HandleSlot *n;DWORD e;if(!s)return ERROR_INVALID_HANDLE;
    if(s->sealed||!parent||!expected||!token)return done(s,ERROR_INVALID_PARAMETER);
    n=allocate(s,token);if(!n)return done(s,ERROR_NOT_ENOUGH_QUOTA);n->directory=1;
    if(!DuplicateHandle(GetCurrentProcess(),parent,GetCurrentProcess(),&n->handle,0,FALSE,DUPLICATE_SAME_ACCESS))return done(s,error());
    e=identity(n,expected);if(e)return done(s,e);n->phase=2;return done(s,0);
}
API DWORD gitgo_scope_borrow(uint64_t cookie,uint64_t token,uintptr_t *raw){
    Scope *s=get(cookie);HandleSlot *n;DWORD e;if(!s)return ERROR_INVALID_HANDLE;
    if(s->sealed||!raw||(n=slot(s,token))==0||n->phase!=2)return done(s,ERROR_INVALID_HANDLE);
    *raw=0;e=metadata(n);if(e)return done(s,e);*raw=(uintptr_t)n->handle;return done(s,0);
}
API DWORD gitgo_scope_identity(uint64_t cookie,uint64_t token,uint64_t *out,DWORD words){
    Scope *s=get(cookie);HandleSlot *n;DWORD e;if(!s)return ERROR_INVALID_HANDLE;
    if(s->sealed||!out||words!=4||(n=slot(s,token))==0||n->phase!=2)return done(s,ERROR_INVALID_HANDLE);
    e=metadata(n);if(e)return done(s,e);out[0]=n->id.VolumeSerialNumber;memcpy(out+1,n->id.FileId.Identifier,16);
    out[3]=(uint64_t)n->directory;return done(s,0);
}
API DWORD gitgo_scope_lock(uint64_t cookie,uint64_t token,const uint64_t *expected,int exclusive){
    Scope *s=get(cookie);HandleSlot *n;DWORD e;if(!s)return ERROR_INVALID_HANDLE;
    if(s->sealed||!expected||(exclusive!=0&&exclusive!=1)||(n=slot(s,token))==0||n->phase!=2||
       n->directory||!n->readable||n->lock_attempted)return done(s,ERROR_INVALID_PARAMETER);
    e=identity(n,expected);if(e)return done(s,e);n->lock_attempted=1;
    if(!LockFileEx(n->handle,LOCKFILE_FAIL_IMMEDIATELY|(exclusive?LOCKFILE_EXCLUSIVE_LOCK:0),0,1,0,&n->lock))return done(s,error());
    n->lock_attempted=2;return done(s,0);
}
API DWORD gitgo_scope_read(uint64_t cookie,uint64_t token,void *out,DWORD capacity,DWORD *count){
    Scope *s=get(cookie);HandleSlot *n;LARGE_INTEGER size,end,offset;DWORD e,read=0;if(!s)return ERROR_INVALID_HANDLE;
    if(s->sealed||!out||!count||!capacity||capacity>65536||(n=slot(s,token))==0||n->phase!=2||n->directory||!n->readable)
        return done(s,ERROR_INVALID_PARAMETER);
    *count=0;e=metadata(n);if(e)return done(s,e);
    if(!GetFileSizeEx(n->handle,&size))return done(s,error());
    if(size.QuadPart<0||(uint64_t)size.QuadPart>capacity)return done(s,ERROR_NOT_ENOUGH_QUOTA);
    offset.QuadPart=0;if(!SetFilePointerEx(n->handle,offset,0,FILE_BEGIN)||
        !ReadFile(n->handle,out,(DWORD)size.QuadPart,&read,0)||!GetFileSizeEx(n->handle,&end))return done(s,error());
    if(read!=(DWORD)size.QuadPart||end.QuadPart!=size.QuadPart)return done(s,ERROR_INVALID_DATA);
    *count=read;return done(s,0);
}
static DWORD close_slot(HandleSlot *n);
API DWORD gitgo_scope_flush_bound(uint64_t cookie,uint64_t parent_token,const wchar_t *leaf,const uint64_t *expected,uint64_t maximum){
    Scope *s=get(cookie);HandleSlot *parent,*n;uint64_t token;DWORD code;LARGE_INTEGER size;
    if(!s)return ERROR_INVALID_HANDLE;
    if(s->sealed||!leaf_ok(leaf)||!expected||maximum>65536||(parent=slot(s,parent_token))==0||
       parent->phase!=2||!parent->directory)return done(s,ERROR_INVALID_PARAMETER);
    code=metadata(parent);if(code)return done(s,code);n=allocate(s,&token);if(!n)return done(s,ERROR_NOT_ENOUGH_QUOTA);
    code=opened(n,parent->handle,leaf,0,1,FILE_WRITE_DATA);if(code)return done(s,code);
    code=identity(n,expected);if(code)return done(s,code);
    if(!GetFileSizeEx(n->handle,&size))return done(s,error());
    if(size.QuadPart<0||(uint64_t)size.QuadPart>maximum)return done(s,ERROR_NOT_ENOUGH_QUOTA);
    if(!FlushFileBuffers(n->handle))return done(s,error());code=close_slot(n);return done(s,code);
}
API DWORD gitgo_scope_account(uint64_t cookie,wchar_t *sid,DWORD sid_count,wchar_t *profile,DWORD profile_count){
    Scope *s=get(cookie);HandleSlot *thread,*process;uint64_t token;DWORD code,length,appcontainer=0;PSID user;
    SID_IDENTIFIER_AUTHORITY *authority;unsigned count;int used,written;
    if(!s)return ERROR_INVALID_HANDLE;
    if(s->sealed||s->account_attempted||!sid||sid_count<256||!profile||profile_count<513)return done(s,ERROR_INVALID_PARAMETER);
    s->account_attempted=1;sid[0]=0;profile[0]=0;
    thread=allocate(s,&token);if(!thread)return done(s,ERROR_NOT_ENOUGH_QUOTA);thread->directory=-1;
    if(OpenThreadToken(GetCurrentThread(),TOKEN_QUERY,TRUE,&thread->handle))return done(s,ERROR_ACCESS_DENIED);
    code=GetLastError();if(code!=ERROR_NO_TOKEN||thread->handle)return done(s,code?code:ERROR_INVALID_DATA);thread->phase=0;
    process=allocate(s,&token);if(!process)return done(s,ERROR_NOT_ENOUGH_QUOTA);process->directory=-1;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&process->handle))return done(s,error());process->phase=2;
    if(!GetTokenInformation(process->handle,TokenIsAppContainer,&appcontainer,sizeof(appcontainer),&length))return done(s,error());
    if(length!=sizeof(appcontainer)||appcontainer)return done(s,ERROR_ACCESS_DENIED);
    if(!GetTokenInformation(process->handle,TokenUser,s->token_user,sizeof(s->token_user),&length))return done(s,error());
    if(length>sizeof(s->token_user)||length<sizeof(TOKEN_USER))return done(s,ERROR_INVALID_DATA);
    user=((TOKEN_USER*)s->token_user)->User.Sid;
    if((uintptr_t)user<(uintptr_t)s->token_user||(uintptr_t)user>(uintptr_t)s->token_user+length-8||!IsValidSid(user)||
        GetLengthSid(user)>(uintptr_t)s->token_user+length-(uintptr_t)user)return done(s,ERROR_INVALID_DATA);
    authority=GetSidIdentifierAuthority(user);count=*GetSidSubAuthorityCount(user);
    /* This Windows user-profile contract is limited to NT-authority users.
     * Unknown authority representations are refused, never reformatted. */
    if(memcmp(authority->Value,(unsigned char[]){0,0,0,0,0,5},6))return done(s,ERROR_NOT_SUPPORTED);
    used=swprintf(s->sid,256,L"S-1-5");if(used!=5)return done(s,ERROR_INVALID_DATA);
    for(unsigned i=0;i<count;i++){
        written=swprintf(s->sid+used,256-(size_t)used,L"-%lu",(unsigned long)*GetSidSubAuthority(user,i));
        if(written<1)return done(s,ERROR_INVALID_DATA);used+=written;
    }
    length=513;if(!GetUserProfileDirectoryW(process->handle,s->profile,&length))return done(s,error());
    if(!length||length>513||wcslen(s->profile)<3||s->profile[1]!=L':'||s->profile[2]!=L'\\')return done(s,ERROR_NOT_SUPPORTED);
    code=close_slot(process);if(code)return done(s,code);
    /* No token, string allocation or ambiguous close escapes into Python. */
    memcpy(sid,s->sid,((size_t)used+1)*sizeof(wchar_t));
    memcpy(profile,s->profile,(wcslen(s->profile)+1)*sizeof(wchar_t));return done(s,0);
}
static DWORD close_slot(HandleSlot *n){
    if(n->phase==3)return ERROR_INVALID_STATE;
    if(n->handle){n->phase=3;if(!CloseHandle(n->handle))return error();n->handle=0;}
    n->phase=0;return 0;
}
API DWORD gitgo_scope_close(uint64_t cookie,uint64_t token){
    Scope *s=get(cookie);HandleSlot *n;DWORD e;unsigned index=(unsigned)(token&255);if(!s)return ERROR_INVALID_HANDLE;
    if(!index||index>GSO_SLOTS)return done(s,ERROR_INVALID_HANDLE);n=&s->slots[index-1];
    if(n->generation!=(token>>8))return done(s,ERROR_INVALID_HANDLE);
    /* A lost caller return may query an already recorded successful close.
     * Never use the raw HANDLE or retry an unknown CloseHandle call. */
    e=close_slot(n);return done(s,e);
}
API DWORD gitgo_scope_status(uint64_t cookie,uint64_t *out,DWORD words){
    Scope *s=get(cookie);if(!s)return ERROR_INVALID_HANDLE;
    if(!out||words!=4)return done(s,ERROR_INVALID_PARAMETER);memset(out,0,4*sizeof(uint64_t));out[0]=(uint64_t)s->sealed;
    for(unsigned i=0;i<GSO_SLOTS;i++)if(s->slots[i].phase){out[1]++;if(s->slots[i].phase==3)out[2]++;
        if(s->slots[i].lock_attempted)out[3]++;}
    ReleaseSRWLockExclusive(&s->lock);return 0;
}
API DWORD gitgo_scope_retire(uint64_t cookie){
    Scope *s;DWORD e;unsigned index=(unsigned)(cookie&255);uint64_t generation=cookie>>8;
    if(!index||index>GSO_OWNERS||!generation)return ERROR_INVALID_HANDLE;
    AcquireSRWLockExclusive(&pool);s=&scopes[index-1];AcquireSRWLockExclusive(&s->lock);
    /* A slot can advance only after every prior handle has a known close.
     * Returning that recorded terminal fact never retries an OS close and
     * never touches resources belonging to a later scope generation. */
    if(generation<s->generation||(generation==s->generation&&!s->occupied)){
        ReleaseSRWLockExclusive(&s->lock);ReleaseSRWLockExclusive(&pool);return 0;
    }
    if(generation!=s->generation||s->pid!=GetCurrentProcessId()||s->thread_id!=GetCurrentThreadId()){
        ReleaseSRWLockExclusive(&s->lock);ReleaseSRWLockExclusive(&pool);return ERROR_INVALID_HANDLE;
    }s->sealed=1;
    for(unsigned i=GSO_SLOTS;i;i--){e=close_slot(&s->slots[i-1]);if(e){ReleaseSRWLockExclusive(&pool);return done(s,e);}}
    s->occupied=0;ReleaseSRWLockExclusive(&pool);return done(s,0);
}

API DWORD gitgo_scope_abi(void){return 1;}
