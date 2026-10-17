/* Fixed-capacity native storage trees under an existing Host admission.
 * Immutable purposes separate effects; unknown closes retain native owners. */
#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <wchar.h>
#define API __declspec(dllexport)
#define GRP_OWNERS 4
/* Purpose is immutable from reservation; phases may never select authority. */
enum { TREE_PIN=0, TREE_INSTALL=1, TREE_INSTALLED=2, TREE_ABSENCE=3,
       TREE_BOOTSTRAP=4, TREE_BOOTSTRAPPED=5, TREE_RETIRE=6, TREE_RETIRED=7 };
#define GRP_NODES 1024
#define GRP_BYTES (UINT64_C(256)*1024*1024)
typedef NTSTATUS (NTAPI *CreateFn)(PHANDLE,ACCESS_MASK,POBJECT_ATTRIBUTES,PIO_STATUS_BLOCK,
    PLARGE_INTEGER,ULONG,ULONG,ULONG,ULONG,PVOID,ULONG);
typedef ULONG (WINAPI *ErrorFn)(NTSTATUS);
typedef NTSTATUS (NTAPI *SecurityFn)(HANDLE,SECURITY_INFORMATION,PSECURITY_DESCRIPTOR,ULONG,PULONG);
typedef struct Node {HANDLE handle;int phase,directory,writable,flushed;FILE_ID_INFO id;uint64_t length,written;} Node;
typedef struct Runtime {
    SRWLOCK lock;uint64_t generation,bytes;DWORD pid,count;int occupied,sealed,installing,purpose;
    Node nodes[GRP_NODES],ancestors[64];DWORD ancestor_count;
    IO_STATUS_BLOCK io;unsigned char security[65536],user[SECURITY_MAX_SID_SIZE];
    DWORD user_length;
    unsigned char inventory[65536];
} Runtime;
static Runtime records[GRP_OWNERS];static SRWLOCK pool;
static DWORD error(void){DWORD e=GetLastError();return e?e:ERROR_GEN_FAILURE;}
static Runtime *get(uint64_t cookie){
    unsigned i=(unsigned)(cookie&255);Runtime *r;
    if(!i||i>GRP_OWNERS)return 0;r=&records[i-1];AcquireSRWLockExclusive(&r->lock);
    if(!r->occupied||r->generation!=(cookie>>8)||r->pid!=GetCurrentProcessId()){
        ReleaseSRWLockExclusive(&r->lock);return 0;
    }return r;
}
static DWORD done(Runtime *r,DWORD e){if(e)r->sealed=1;ReleaseSRWLockExclusive(&r->lock);return e;}
static DWORD metadata(Node *n){
    BY_HANDLE_FILE_INFORMATION info;FILE_ID_INFO id;uint64_t length;
    if(GetFileType(n->handle)!=FILE_TYPE_DISK)return ERROR_INVALID_DATA;
    if(!GetFileInformationByHandle(n->handle,&info)||
       !GetFileInformationByHandleEx(n->handle,FileIdInfo,&id,sizeof(id)))return error();
    length=((uint64_t)info.nFileSizeHigh<<32)|info.nFileSizeLow;
    if((info.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT)||
       (!!(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY))!=n->directory||
       (!n->directory&&(info.nNumberOfLinks!=1||length!=n->length)))return ERROR_INVALID_DATA;
    if(n->phase==2&&memcmp(&id,&n->id,sizeof(id)))return ERROR_INVALID_DATA;
    n->id=id;return 0;
}
static int trusted_sid(Runtime *r,PSID sid){
    return IsValidSid(sid)&&(EqualSid(sid,r->user)||IsWellKnownSid(sid,WinLocalSystemSid)||
        IsWellKnownSid(sid,WinBuiltinAdministratorsSid));
}
static DWORD security(Runtime *r,Node *n,int root){
    SecurityFn query;ErrorFn convert;NTSTATUS status;ULONG length=0;PACL acl=0;PSID owner=0;
    BOOL present=FALSE,defaulted=FALSE;SECURITY_DESCRIPTOR_CONTROL control;DWORD revision;unsigned i;
    HMODULE nt=GetModuleHandleW(L"ntdll.dll");if(!nt)return error();
    query=(SecurityFn)(void*)GetProcAddress(nt,"NtQuerySecurityObject");
    convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");
    if(!query||!convert)return ERROR_PROC_NOT_FOUND;
    status=query(n->handle,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,
        r->security,sizeof(r->security),&length);
    if(status<0)return convert(status);
    if(length>sizeof(r->security)||!IsValidSecurityDescriptor(r->security)||
       !GetSecurityDescriptorControl(r->security,&control,&revision)||
       (root&&!(control&SE_DACL_PROTECTED))||
       !GetSecurityDescriptorOwner(r->security,&owner,&defaulted)||!trusted_sid(r,owner)||
       !GetSecurityDescriptorDacl(r->security,&present,&acl,&defaulted)||!present||!acl||!IsValidAcl(acl)||
       !acl->AceCount||acl->AceCount>16)return ERROR_ACCESS_DENIED;
    for(i=0;i<acl->AceCount;i++){
        ACCESS_ALLOWED_ACE *ace=0;
        if(!GetAce(acl,i,(void**)&ace)||ace->Header.AceType!=ACCESS_ALLOWED_ACE_TYPE||
           !trusted_sid(r,(PSID)&ace->SidStart))return ERROR_ACCESS_DENIED;
    }return 0;
}
static int leaf_ok(const wchar_t *leaf){
    unsigned i,n;if(!leaf)return 0;n=(unsigned)wcsnlen(leaf,129);
    if(!n||n>128||!wcscmp(leaf,L".")||!wcscmp(leaf,L"..")||leaf[n-1]==L'.')return 0;
    for(i=0;i<n;i++)if(!((leaf[i]>=L'a'&&leaf[i]<=L'z')||(leaf[i]>=L'A'&&leaf[i]<=L'Z')||
        (leaf[i]>=L'0'&&leaf[i]<=L'9')||leaf[i]==L'_'||leaf[i]==L'-'||leaf[i]==L'.'))return 0;
    return 1;
}
API DWORD gitgo_tree_reserve(uint64_t *cookie,const void *user,DWORD length,DWORD purpose){
    Runtime *r=0;uint64_t generation;unsigned i;
    if((purpose!=TREE_PIN&&purpose!=TREE_INSTALL&&purpose!=TREE_ABSENCE&&purpose!=TREE_BOOTSTRAP&&purpose!=TREE_RETIRE)||
       !cookie||!user||length>SECURITY_MAX_SID_SIZE||!IsValidSid((PSID)user)||GetLengthSid((PSID)user)!=length)
        return ERROR_INVALID_PARAMETER;
    *cookie=0;AcquireSRWLockExclusive(&pool);
    for(i=0;i<GRP_OWNERS;i++)if(!records[i].occupied&&records[i].generation<(UINT64_MAX>>8)){r=&records[i];break;}
    if(!r){ReleaseSRWLockExclusive(&pool);return ERROR_NOT_ENOUGH_QUOTA;}
    AcquireSRWLockExclusive(&r->lock);generation=r->generation+1;
    memset((char*)r+offsetof(Runtime,generation),0,sizeof(*r)-offsetof(Runtime,generation));
    r->generation=generation;r->occupied=1;r->pid=GetCurrentProcessId();r->purpose=(int)purpose;
    memcpy(r->user,user,length);r->user_length=length;*cookie=(generation<<8)|(i+1);
    ReleaseSRWLockExclusive(&r->lock);ReleaseSRWLockExclusive(&pool);return 0;
}
API DWORD gitgo_tree_root(uint64_t cookie,HANDLE root,const uint64_t *identity,DWORD count){
    Runtime *r=get(cookie);Node *n;DWORD e;HMODULE nt;CreateFn create;ErrorFn convert;
    UNICODE_STRING name;OBJECT_ATTRIBUTES attrs;NTSTATUS status;wchar_t empty[]=L"";
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_PIN)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->count||!root||!identity||count!=3)return done(r,ERROR_INVALID_PARAMETER);
    r->sealed=1;r->count=1;n=&r->nodes[0];n->phase=1;n->directory=1;
    nt=GetModuleHandleW(L"ntdll.dll");if(!nt)return done(r,error());
    create=(CreateFn)(void*)GetProcAddress(nt,"NtCreateFile");convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");
    if(!create||!convert)return done(r,ERROR_PROC_NOT_FOUND);
    name.Buffer=empty;name.Length=0;name.MaximumLength=sizeof(empty);
    memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=root;attrs.ObjectName=&name;attrs.Attributes=0x40|0x1000;
    status=create(&n->handle,SYNCHRONIZE|READ_CONTROL|FILE_READ_ATTRIBUTES|FILE_LIST_DIRECTORY,
        &attrs,&r->io,0,0,FILE_SHARE_READ|FILE_SHARE_WRITE,1,0x200000|0x20|1,0,0);
    if(status<0)return done(r,convert(status));if(!n->handle)return done(r,ERROR_INVALID_HANDLE);
    e=metadata(n);if(e)return done(r,e);
    if(n->id.VolumeSerialNumber!=identity[0]||memcmp(n->id.FileId.Identifier,identity+1,16))return done(r,ERROR_INVALID_DATA);
    e=security(r,n,1);if(e)return done(r,e);
    n->phase=2;r->sealed=0;return done(r,0);
}
API DWORD gitgo_tree_path(uint64_t cookie,const wchar_t *path,const uint64_t *identity,DWORD count){
    Runtime *r=get(cookie);wchar_t component[513],drive[8]=L"\\??\\C:\\";unsigned cursor=3,start,length,depth=0;
    Node *n;HANDLE parent=0;DWORD e;HMODULE nt;CreateFn create;ErrorFn convert;
    UNICODE_STRING name;OBJECT_ATTRIBUTES attrs;NTSTATUS status;int final=0;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_PIN)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->count||r->ancestor_count||!path||!identity||count!=3||
       (length=(unsigned)wcsnlen(path,513))<4||length>512||path[1]!=L':'||path[2]!=L'\\'||
       !((path[0]>=L'a'&&path[0]<=L'z')||(path[0]>=L'A'&&path[0]<=L'Z')))
        return done(r,ERROR_INVALID_PARAMETER);
    nt=GetModuleHandleW(L"ntdll.dll");if(!nt)return done(r,error());
    create=(CreateFn)(void*)GetProcAddress(nt,"NtCreateFile");convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");
    if(!create||!convert)return done(r,ERROR_PROC_NOT_FOUND);r->sealed=1;drive[4]=path[0];
    for(;;){
        if(!depth){name.Buffer=drive;name.Length=7*sizeof(wchar_t);name.MaximumLength=sizeof(drive);}
        else{
            start=cursor;
            while(cursor<length&&path[cursor]!=L'\\'){
                if(path[cursor]==L':'||path[cursor]==L'/'||path[cursor]<32)return done(r,ERROR_INVALID_PARAMETER);cursor++;
            }
            if(cursor==start||cursor-start>255)return done(r,ERROR_INVALID_PARAMETER);
            memcpy(component,path+start,(cursor-start)*sizeof(wchar_t));component[cursor-start]=0;
            if(!wcscmp(component,L".")||!wcscmp(component,L"..")||component[cursor-start-1]==L'.'||component[cursor-start-1]==L' ')
                return done(r,ERROR_INVALID_PARAMETER);
            final=cursor==length;cursor++;
            name.Buffer=component;name.Length=(USHORT)(wcslen(component)*sizeof(wchar_t));name.MaximumLength=name.Length;
        }
        if(final){r->count=1;n=&r->nodes[0];}
        else{
            if(r->ancestor_count>=64)return done(r,ERROR_NOT_ENOUGH_QUOTA);
            n=&r->ancestors[r->ancestor_count++];
        }
        n->phase=1;n->directory=1;
        memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=parent;
        attrs.ObjectName=&name;attrs.Attributes=0x40|0x1000;
        status=create(&n->handle,SYNCHRONIZE|FILE_READ_ATTRIBUTES|(final?READ_CONTROL|FILE_LIST_DIRECTORY:FILE_TRAVERSE),
            &attrs,&r->io,0,0,FILE_SHARE_READ|FILE_SHARE_WRITE,1,0x200000|0x20|1,0,0);
        if(status<0)return done(r,convert(status));if(!n->handle)return done(r,ERROR_INVALID_HANDLE);
        e=metadata(n);if(e)return done(r,e);n->phase=2;
        if(final){
            /* Ancestors are independently pinned objects. Windows may expose
             * distinct native volumes along the Host's virtualized profile.
             * The installation root and all installed members must match the
             * registered volume; no ancestor is substituted for that root. */
            if(n->id.VolumeSerialNumber!=identity[0]||memcmp(n->id.FileId.Identifier,identity+1,16))return done(r,ERROR_INVALID_DATA);
            e=security(r,n,1);if(e)return done(r,e);break;
        }
        parent=n->handle;depth++;
    }
    r->sealed=0;return done(r,0);
}
API DWORD gitgo_tree_add(uint64_t cookie,DWORD parent,const wchar_t *leaf,int directory,uint64_t length,DWORD *index){
    Runtime *r=get(cookie);Node *n,*p;DWORD e;unsigned i;HMODULE nt;CreateFn create;ErrorFn convert;
    UNICODE_STRING name;OBJECT_ATTRIBUTES attrs;NTSTATUS status;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_PIN)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->installing||!index||r->count>=GRP_NODES||parent>=r->count||!r->nodes[parent].directory||
       !leaf_ok(leaf)||(directory!=0&&directory!=1)||(directory&&length)||length>64*1024*1024||
       length>GRP_BYTES-r->bytes)return done(r,ERROR_INVALID_PARAMETER);
    *index=UINT32_MAX;r->sealed=1;p=&r->nodes[parent];e=metadata(p);if(e)return done(r,e);
    e=security(r,p,parent==0);if(e)return done(r,e);
    nt=GetModuleHandleW(L"ntdll.dll");if(!nt)return done(r,error());
    create=(CreateFn)(void*)GetProcAddress(nt,"NtCreateFile");convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");
    if(!create||!convert)return done(r,ERROR_PROC_NOT_FOUND);
    n=&r->nodes[r->count++];n->phase=1;n->directory=directory;n->length=length;r->bytes+=length;
    name.Buffer=(PWSTR)leaf;name.Length=(USHORT)(wcslen(leaf)*sizeof(wchar_t));name.MaximumLength=name.Length;
    memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=p->handle;attrs.ObjectName=&name;attrs.Attributes=0x40|0x1000;
    status=create(&n->handle,SYNCHRONIZE|READ_CONTROL|FILE_READ_ATTRIBUTES|FILE_READ_DATA,
        &attrs,&r->io,0,0,FILE_SHARE_READ|(directory?FILE_SHARE_WRITE:0),1,
        0x200000|0x20|(directory?1:0x40),0,0);
    if(status<0)return done(r,convert(status));if(!n->handle)return done(r,ERROR_INVALID_HANDLE);
    e=metadata(n);if(e)return done(r,e);
    if(n->id.VolumeSerialNumber!=r->nodes[0].id.VolumeSerialNumber)return done(r,ERROR_INVALID_DATA);
    for(i=0;i<r->count-1;i++)if(!memcmp(&n->id,&r->nodes[i].id,sizeof(n->id)))return done(r,ERROR_INVALID_DATA);
    e=security(r,n,0);if(e)return done(r,e);
    n->phase=2;r->sealed=0;*index=r->count-1;return done(r,0);
}
API DWORD gitgo_tree_read(uint64_t cookie,DWORD index,uint64_t offset,void *data,DWORD size){
    Runtime *r=get(cookie);Node *n;DWORD e,count=0;LARGE_INTEGER where;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->sealed||index>=r->count||!data||!size||size>65536)return done(r,ERROR_INVALID_PARAMETER);
    n=&r->nodes[index];if(n->directory||(n->writable&&!n->flushed)||offset>n->length||size>n->length-offset)return done(r,ERROR_INVALID_PARAMETER);
    r->sealed=1;e=metadata(n);if(e)return done(r,e);
    where.QuadPart=(LONGLONG)offset;
    if(!SetFilePointerEx(n->handle,where,0,FILE_BEGIN)||!ReadFile(n->handle,data,size,&count,0))return done(r,error());
    if(count!=size)return done(r,ERROR_HANDLE_EOF);r->sealed=0;return done(r,0);
}
API DWORD gitgo_tree_identity(uint64_t cookie,DWORD index,uint64_t *out,DWORD count){
    Runtime *r=get(cookie);Node *n;DWORD e;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->sealed||index>=r->count||!out||count!=4)return done(r,ERROR_INVALID_PARAMETER);
    n=&r->nodes[index];e=metadata(n);if(e)return done(r,e);
    out[0]=n->id.VolumeSerialNumber;memcpy(out+1,n->id.FileId.Identifier,16);out[3]=n->length;return done(r,0);
}
API DWORD gitgo_tree_status(uint64_t cookie,uint64_t *out,DWORD count){
    Runtime *r=get(cookie);Node *last;
    if(!r)return ERROR_INVALID_HANDLE;
    if(!out||count!=8)return done(r,ERROR_INVALID_PARAMETER);
    out[0]=r->count;out[1]=r->ancestor_count;out[2]=r->sealed;out[3]=r->bytes;
    last=r->count?&r->nodes[r->count-1]:(r->ancestor_count?&r->ancestors[r->ancestor_count-1]:0);
    out[4]=last?last->phase:0;out[5]=last?last->id.VolumeSerialNumber:0;
    memset(out+6,0,16);if(last)memcpy(out+6,last->id.FileId.Identifier,16);
    ReleaseSRWLockExclusive(&r->lock);return 0;
}
API DWORD gitgo_tree_retire(uint64_t cookie){
    Runtime *r;unsigned i,index=(unsigned)(cookie&255);uint64_t generation=cookie>>8;
    if(!index||index>GRP_OWNERS||!generation)return ERROR_INVALID_HANDLE;
    AcquireSRWLockExclusive(&pool);r=&records[index-1];AcquireSRWLockExclusive(&r->lock);
    if(r->pid!=GetCurrentProcessId()){
        ReleaseSRWLockExclusive(&pool);return done(r,ERROR_INVALID_HANDLE);
    }
    if(generation<r->generation||(generation==r->generation&&!r->occupied)){
        ReleaseSRWLockExclusive(&pool);return done(r,0);
    }
    if(generation!=r->generation){
        ReleaseSRWLockExclusive(&pool);return done(r,ERROR_INVALID_HANDLE);
    }
    r->sealed=1;
    for(i=r->count;i;i--){Node *n=&r->nodes[i-1];
        if(n->phase==3){ReleaseSRWLockExclusive(&pool);return done(r,ERROR_INVALID_STATE);}
        if(n->handle){n->phase=3;if(!CloseHandle(n->handle)){ReleaseSRWLockExclusive(&pool);return done(r,error());}
            n->handle=0;n->phase=0;}
    }
    for(i=r->ancestor_count;i;i--){Node *n=&r->ancestors[i-1];
        if(n->phase==3){ReleaseSRWLockExclusive(&pool);return done(r,ERROR_INVALID_STATE);}
        if(n->handle){n->phase=3;if(!CloseHandle(n->handle)){ReleaseSRWLockExclusive(&pool);return done(r,error());}
            n->handle=0;n->phase=0;}
    }
    r->occupied=0;ReleaseSRWLockExclusive(&pool);return done(r,0);
}

/* Exact-tree runtime installer. The caller must already hold the existing Host
 * gate, durable immutable intent, pinned source and both runtime budget slots.
 * This code supplies bounded native effects, not installation authorization. */

#define GRI_NODES 256
#define GRI_CHARGE (UINT64_C(64)*1024*1024)
static DWORD native_create(Runtime *r,Node *node,HANDLE parent,const wchar_t *leaf,int directory,PSECURITY_DESCRIPTOR sd,ULONG disposition,NTSTATUS *native_status){
    HMODULE nt=GetModuleHandleW(L"ntdll.dll");CreateFn create;ErrorFn convert;UNICODE_STRING name;
    OBJECT_ATTRIBUTES attrs;NTSTATUS status;
    if(!nt)return error();create=(CreateFn)(void*)GetProcAddress(nt,"NtCreateFile");
    convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");
    if(!create||!convert)return ERROR_PROC_NOT_FOUND;
    name.Buffer=(PWSTR)leaf;name.Length=(USHORT)(wcslen(leaf)*sizeof(wchar_t));name.MaximumLength=name.Length;
    memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=parent;
    attrs.ObjectName=&name;attrs.Attributes=0x40|0x1000;attrs.SecurityDescriptor=sd;
    node->phase=1;node->directory=directory;
    status=create(&node->handle,SYNCHRONIZE|READ_CONTROL|FILE_READ_ATTRIBUTES|FILE_READ_DATA|(directory?0:FILE_WRITE_DATA),
        &attrs,&r->io,0,0,FILE_SHARE_READ|(directory?FILE_SHARE_WRITE:0),disposition,0x200000|0x20|(directory?1:0x40),0,0);
    if(native_status)*native_status=status;
    if(status<0)return convert(status);return node->handle?0:ERROR_INVALID_HANDLE;
}
static DWORD exact_parent(Runtime *r,DWORD index){
    DWORD e;if(index>=r->count||!r->nodes[index].directory)return ERROR_INVALID_PARAMETER;
    e=metadata(&r->nodes[index]);if(e)return e;return security(r,&r->nodes[index],index==0);
}
API DWORD gitgo_install_begin(uint64_t cookie,HANDLE parent,const uint64_t *identity,DWORD words,const wchar_t *leaf){
    Runtime *r=get(cookie);Node *p,*n;DWORD e;PACL acl=0;BOOL present,defaulted;SECURITY_DESCRIPTOR sd;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_INSTALL)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->count||r->ancestor_count||!parent||!identity||words!=3||!leaf_ok(leaf)||wcslen(leaf)!=36)
        return done(r,ERROR_INVALID_PARAMETER);
    /* A fixed fresh generation name only, with canonical UUID positions. */
    for(unsigned i=0;i<36;i++){
        if(i==8||i==13||i==18||i==23){if(leaf[i]!=L'-')return done(r,ERROR_INVALID_PARAMETER);}
        else if(!((leaf[i]>=L'0'&&leaf[i]<=L'9')||(leaf[i]>=L'a'&&leaf[i]<=L'f')))return done(r,ERROR_INVALID_PARAMETER);
    }
    r->sealed=1;r->installing=1;r->ancestor_count=1;p=&r->ancestors[0];p->phase=1;p->directory=1;
    e=native_create(r,p,parent,L"",1,0,1,0);if(e)return done(r,e);
    e=metadata(p);if(e)return done(r,e);
    if(p->id.VolumeSerialNumber!=identity[0]||memcmp(p->id.FileId.Identifier,identity+1,16))return done(r,ERROR_INVALID_DATA);
    e=security(r,p,1);if(e)return done(r,e);p->phase=2;
    if(!GetSecurityDescriptorDacl(r->security,&present,&acl,&defaulted)||!present||!acl||
       !InitializeSecurityDescriptor(&sd,SECURITY_DESCRIPTOR_REVISION)||
       !SetSecurityDescriptorDacl(&sd,TRUE,acl,FALSE)||
       !SetSecurityDescriptorControl(&sd,SE_DACL_PROTECTED,SE_DACL_PROTECTED))return done(r,error());
    r->count=1;n=&r->nodes[0];e=native_create(r,n,p->handle,leaf,1,&sd,2,0);if(e)return done(r,e);
    e=metadata(n);if(e)return done(r,e);
    if(n->id.VolumeSerialNumber!=p->id.VolumeSerialNumber)return done(r,ERROR_INVALID_DATA);
    e=security(r,n,1);if(e)return done(r,e);n->phase=2;r->sealed=0;return done(r,0);
}
API DWORD gitgo_install_add(uint64_t cookie,DWORD parent,const wchar_t *leaf,int directory,uint64_t length,DWORD *index){
    Runtime *r=get(cookie);Node *n;DWORD e;uint64_t charge=4096;unsigned i;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_INSTALL)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->installing!=1||!index||r->count>=GRI_NODES||!leaf_ok(leaf)||
       (directory!=0&&directory!=1)||(directory&&length)||length>GRI_CHARGE)
        return done(r,ERROR_INVALID_PARAMETER);
    while(charge<length)charge*=2;
    if(!directory&&charge>GRI_CHARGE-r->bytes)return done(r,ERROR_NOT_ENOUGH_QUOTA);
    *index=UINT32_MAX;r->sealed=1;e=exact_parent(r,parent);if(e)return done(r,e);
    n=&r->nodes[r->count++];n->length=0;n->writable=!directory;
    if(!directory)r->bytes+=charge;
    e=native_create(r,n,r->nodes[parent].handle,leaf,directory,0,2,0);if(e)return done(r,e);
    e=metadata(n);if(e)return done(r,e);n->phase=2;n->length=length;
    if(n->id.VolumeSerialNumber!=r->nodes[0].id.VolumeSerialNumber)return done(r,ERROR_INVALID_DATA);
    for(i=0;i<r->count-1;i++)if(!memcmp(&n->id,&r->nodes[i].id,sizeof(n->id)))return done(r,ERROR_INVALID_DATA);
    e=security(r,n,0);if(e)return done(r,e);*index=r->count-1;r->sealed=0;return done(r,0);
}
API DWORD gitgo_install_write(uint64_t cookie,DWORD index,const void *data,DWORD size){
    Runtime *r=get(cookie);Node *n,observed;DWORD e,count=0;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_INSTALL)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->installing!=1||index>=r->count||!data||!size||size>65536)return done(r,ERROR_INVALID_PARAMETER);
    n=&r->nodes[index];
    if(!n->writable||n->flushed||n->written>n->length||size>n->length-n->written)return done(r,ERROR_INVALID_STATE);
    r->sealed=1;observed=*n;observed.length=n->written;e=metadata(&observed);if(e)return done(r,e);
    if(!WriteFile(n->handle,data,size,&count,0))return done(r,error());
    if(count!=size)return done(r,ERROR_WRITE_FAULT);n->written+=size;r->sealed=0;return done(r,0);
}
API DWORD gitgo_install_seal_file(uint64_t cookie,DWORD index){
    Runtime *r=get(cookie);Node *n;DWORD e;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_INSTALL)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->installing!=1||index>=r->count)return done(r,ERROR_INVALID_PARAMETER);
    n=&r->nodes[index];if(!n->writable||n->flushed||n->written!=n->length)return done(r,ERROR_INVALID_STATE);
    r->sealed=1;if(!FlushFileBuffers(n->handle))return done(r,error());e=metadata(n);if(e)return done(r,e);
    n->flushed=1;r->sealed=0;return done(r,0);
}
API DWORD gitgo_install_finish(uint64_t cookie,DWORD expected_nodes,uint64_t expected_charge){
    Runtime *r=get(cookie);DWORD e;unsigned i;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_INSTALL)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->installing!=1||r->count!=expected_nodes||r->bytes!=expected_charge)return done(r,ERROR_INVALID_PARAMETER);
    r->sealed=1;
    for(i=0;i<r->count;i++){
        Node *n=&r->nodes[i];if(n->writable&&!n->flushed)return done(r,ERROR_INVALID_STATE);
        e=metadata(n);if(e)return done(r,e);e=security(r,n,i==0);if(e)return done(r,e);
    }
    r->installing=2;r->sealed=0;return done(r,0);
}
API DWORD gitgo_install_absent(uint64_t cookie,HANDLE parent,const uint64_t *identity,DWORD words,const wchar_t *leaf,int *absent){
    Runtime *r=get(cookie);Node *p,*n;DWORD e;NTSTATUS status=(NTSTATUS)0xc0000001;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_ABSENCE)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->count||r->ancestor_count||!parent||!identity||words!=3||!leaf_ok(leaf)||!absent)
        return done(r,ERROR_INVALID_PARAMETER);
    *absent=0;r->sealed=1;r->installing=3;r->ancestor_count=1;p=&r->ancestors[0];
    e=native_create(r,p,parent,L"",1,0,1,0);if(e)return done(r,e);
    e=metadata(p);if(e)return done(r,e);
    if(p->id.VolumeSerialNumber!=identity[0]||memcmp(p->id.FileId.Identifier,identity+1,16))return done(r,ERROR_INVALID_DATA);
    e=security(r,p,1);if(e)return done(r,e);p->phase=2;
    r->count=1;n=&r->nodes[0];e=native_create(r,n,p->handle,leaf,1,0,1,&status);
    if(status==(NTSTATUS)0xc0000034&&!n->handle){*absent=1;return done(r,0);}
    return done(r,e);
}

/* Fresh bounded metadata trees; no open-or-create, overwrite or recovery API.
 * Shares the already tested native installer owner and writer primitives. */

#define GBI_NODES 32
#define GBI_CHARGE (UINT64_C(256)*1024)
typedef struct BootstrapGate {uint64_t generation;int attempted;OVERLAPPED overlapped;} BootstrapGate;
static BootstrapGate gates[GRP_OWNERS];
static DWORD parent_security(Runtime *r,Node *n){
    SecurityFn query;ErrorFn convert;HMODULE nt=GetModuleHandleW(L"ntdll.dll");NTSTATUS status;ULONG length=0;
    PACL acl=0;PSID owner=0;BOOL present,defaulted;
    const DWORD safe=FILE_READ_DATA|FILE_READ_EA|FILE_EXECUTE|FILE_READ_ATTRIBUTES|READ_CONTROL|SYNCHRONIZE|GENERIC_READ|GENERIC_EXECUTE;
    if(!nt)return error();query=(SecurityFn)(void*)GetProcAddress(nt,"NtQuerySecurityObject");
    convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");if(!query||!convert)return ERROR_PROC_NOT_FOUND;
    status=query(n->handle,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,r->security,sizeof(r->security),&length);
    if(status<0)return convert(status);
    if(length>sizeof(r->security)||!IsValidSecurityDescriptor(r->security)||
       !GetSecurityDescriptorOwner(r->security,&owner,&defaulted)||!trusted_sid(r,owner)||
       !GetSecurityDescriptorDacl(r->security,&present,&acl,&defaulted)||!present||!acl||!IsValidAcl(acl)||
       !acl->AceCount||acl->AceCount>16)return ERROR_ACCESS_DENIED;
    for(unsigned i=0;i<acl->AceCount;i++){
        ACCESS_ALLOWED_ACE *ace=0;
        if(!GetAce(acl,i,(void**)&ace)||ace->Header.AceType!=ACCESS_ALLOWED_ACE_TYPE||
           (!trusted_sid(r,(PSID)&ace->SidStart)&&(ace->Mask&~safe)))return ERROR_ACCESS_DENIED;
    }
    return 0;
}
static DWORD host_account(Runtime *r){
    Node *n=&r->ancestors[1];DWORD code,length,container=0;PSID user;
    r->ancestor_count=2;n->phase=1;
    if(OpenThreadToken(GetCurrentThread(),TOKEN_QUERY,TRUE,&n->handle))return ERROR_ACCESS_DENIED;
    code=GetLastError();if(code!=ERROR_NO_TOKEN||n->handle)return code?code:ERROR_INVALID_DATA;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&n->handle))return error();
    if(!GetTokenInformation(n->handle,TokenIsAppContainer,&container,sizeof(container),&length))return error();
    if(length!=sizeof(container)||container)return ERROR_ACCESS_DENIED;
    if(!GetTokenInformation(n->handle,TokenUser,r->security,sizeof(r->security),&length))return error();
    if(length>sizeof(r->security)||length<sizeof(TOKEN_USER))return ERROR_INVALID_DATA;
    user=((TOKEN_USER*)r->security)->User.Sid;
    if((uintptr_t)user<(uintptr_t)r->security||(uintptr_t)user>(uintptr_t)r->security+length-8||
       !IsValidSid(user)||GetLengthSid(user)>(uintptr_t)r->security+length-(uintptr_t)user||
       !EqualSid(user,r->user))return ERROR_ACCESS_DENIED;
    n->phase=3;if(!CloseHandle(n->handle))return error();n->handle=0;n->phase=0;return 0;
}
static DWORD begin_tree(uint64_t cookie,HANDLE parent,const uint64_t *identity,DWORD words,const wchar_t *leaf,int fresh){
    Runtime *r=get(cookie);Node *p,*n;DWORD code,system_size=SECURITY_MAX_SID_SIZE,admin_size=SECURITY_MAX_SID_SIZE;
    unsigned char system_sid[SECURITY_MAX_SID_SIZE],admin_sid[SECURITY_MAX_SID_SIZE],acl_bytes[1024];
    PACL acl=(PACL)acl_bytes;SECURITY_DESCRIPTOR sd;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_BOOTSTRAP)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->count||r->ancestor_count||!parent||!identity||words!=3||
       (fresh?!leaf_ok(leaf):!leaf||leaf[0]))
        return done(r,ERROR_INVALID_PARAMETER);
    r->sealed=1;r->installing=4;
    memset(&gates[(cookie&255)-1],0,sizeof(BootstrapGate));gates[(cookie&255)-1].generation=cookie>>8;
    code=host_account(r);if(code)return done(r,code);
    p=&r->ancestors[0];p->phase=1;p->directory=1;
    code=native_create(r,p,parent,L"",1,0,1,0);if(code)return done(r,code);
    code=metadata(p);if(code)return done(r,code);
    if(p->id.VolumeSerialNumber!=identity[0]||memcmp(p->id.FileId.Identifier,identity+1,16))return done(r,ERROR_INVALID_DATA);
    /* A protected child cannot repair a hostile parent's DELETE_CHILD right.
     * Read/traverse-only parent access is harmless; mutation by an untrusted
     * principal is refused before any creation. Children inherit none of it. */
    code=parent_security(r,p);if(code)return done(r,code);p->phase=2;
    if(!CreateWellKnownSid(WinLocalSystemSid,0,system_sid,&system_size)||
       !CreateWellKnownSid(WinBuiltinAdministratorsSid,0,admin_sid,&admin_size)||
       !InitializeAcl(acl,sizeof(acl_bytes),ACL_REVISION)||
       !AddAccessAllowedAceEx(acl,ACL_REVISION,OBJECT_INHERIT_ACE|CONTAINER_INHERIT_ACE,FILE_ALL_ACCESS,r->user)||
       !AddAccessAllowedAceEx(acl,ACL_REVISION,OBJECT_INHERIT_ACE|CONTAINER_INHERIT_ACE,FILE_ALL_ACCESS,system_sid)||
       !AddAccessAllowedAceEx(acl,ACL_REVISION,OBJECT_INHERIT_ACE|CONTAINER_INHERIT_ACE,FILE_ALL_ACCESS,admin_sid)||
       !InitializeSecurityDescriptor(&sd,SECURITY_DESCRIPTOR_REVISION)||
       !SetSecurityDescriptorOwner(&sd,r->user,FALSE)||!SetSecurityDescriptorDacl(&sd,TRUE,acl,FALSE)||
       !SetSecurityDescriptorControl(&sd,SE_DACL_PROTECTED,SE_DACL_PROTECTED))return done(r,error());
    r->count=1;n=&r->nodes[0];code=native_create(r,n,p->handle,leaf,1,fresh?&sd:0,fresh?2:1,0);if(code)return done(r,code);
    code=metadata(n);if(code)return done(r,code);
    if(n->id.VolumeSerialNumber!=p->id.VolumeSerialNumber)return done(r,ERROR_INVALID_DATA);
    code=security(r,n,1);if(code)return done(r,code);n->phase=2;r->sealed=0;return done(r,0);
}
API DWORD gitgo_bootstrap_begin(uint64_t cookie,HANDLE parent,const uint64_t *identity,DWORD words,const wchar_t *leaf){
    return begin_tree(cookie,parent,identity,words,leaf,1);
}
API DWORD gitgo_bootstrap_attach(uint64_t cookie,HANDLE parent,const uint64_t *identity,DWORD words){
    return begin_tree(cookie,parent,identity,words,L"",0);
}
API DWORD gitgo_bootstrap_add(uint64_t cookie,DWORD parent,const wchar_t *leaf,int directory,uint64_t ceiling,DWORD *index){
    Runtime *r=get(cookie);Node *n;uint64_t charge=4096;DWORD code;unsigned i;
    SECURITY_DESCRIPTOR sd;PACL acl=0;BOOL present,defaulted;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_BOOTSTRAP)return done(r,ERROR_INVALID_STATE);
    while(charge<ceiling&&charge<=65536)charge*=2;
    if(r->sealed||r->installing!=4||!index||!leaf_ok(leaf)||(directory!=0&&directory!=1)||
       (directory&&ceiling))return done(r,ERROR_INVALID_PARAMETER);
    if(r->count>=GBI_NODES||ceiling>65536||(!directory&&charge>GBI_CHARGE-r->bytes))
        return done(r,ERROR_NOT_ENOUGH_QUOTA);
    *index=UINT32_MAX;r->sealed=1;code=exact_parent(r,parent);if(code)return done(r,code);
    if(directory&&(!GetSecurityDescriptorDacl(r->security,&present,&acl,&defaulted)||!present||!acl||
       !InitializeSecurityDescriptor(&sd,SECURITY_DESCRIPTOR_REVISION)||!SetSecurityDescriptorOwner(&sd,r->user,FALSE)||
       !SetSecurityDescriptorDacl(&sd,TRUE,acl,FALSE)||
       !SetSecurityDescriptorControl(&sd,SE_DACL_PROTECTED,SE_DACL_PROTECTED)))return done(r,error());
    n=&r->nodes[r->count++];n->writable=!directory;if(!directory)r->bytes+=charge;
    code=native_create(r,n,r->nodes[parent].handle,leaf,directory,directory?&sd:0,2,0);if(code)return done(r,code);
    code=metadata(n);if(code)return done(r,code);n->phase=2;n->length=ceiling;
    if(n->id.VolumeSerialNumber!=r->nodes[0].id.VolumeSerialNumber)return done(r,ERROR_INVALID_DATA);
    for(i=0;i<r->count-1;i++)if(!memcmp(&n->id,&r->nodes[i].id,sizeof(n->id)))return done(r,ERROR_INVALID_DATA);
    code=security(r,n,0);if(code)return done(r,code);*index=r->count-1;r->sealed=0;return done(r,0);
}
API DWORD gitgo_bootstrap_write_file(uint64_t cookie,DWORD index,const void *data,DWORD size){
    Runtime *r=get(cookie);Node *n;DWORD code,written=0;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_BOOTSTRAP)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->installing!=4||index>=r->count||(!data&&size)||size>65536)
        return done(r,ERROR_INVALID_PARAMETER);
    n=&r->nodes[index];
    if(!n->writable||n->flushed||n->written||size>n->length)return done(r,ERROR_INVALID_STATE);
    r->sealed=1;n->length=0;code=metadata(n);if(code)return done(r,code);n->length=size;
    if(size){
        if(!WriteFile(n->handle,data,size,&written,0))return done(r,error());
        if(written!=size)return done(r,ERROR_WRITE_FAULT);n->written=size;
    }
    if(!FlushFileBuffers(n->handle))return done(r,error());
    code=metadata(n);if(code)return done(r,code);n->flushed=1;r->sealed=0;return done(r,0);
}
API DWORD gitgo_bootstrap_identity(uint64_t cookie,DWORD index,uint64_t *out,DWORD count){
    Runtime *r=get(cookie);Node n;DWORD code;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_BOOTSTRAP)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||index>=r->count||!out||count!=4)return done(r,ERROR_INVALID_PARAMETER);
    n=r->nodes[index];if(n.writable&&!n.flushed)n.length=n.written;
    code=metadata(&n);if(code)return done(r,code);
    out[0]=n.id.VolumeSerialNumber;memcpy(out+1,n.id.FileId.Identifier,16);out[3]=n.length;return done(r,0);
}
API DWORD gitgo_bootstrap_finish(uint64_t cookie,DWORD nodes,uint64_t charge){
    Runtime *r=get(cookie);DWORD code;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_BOOTSTRAP)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->installing!=4||r->count!=nodes||r->bytes!=charge)return done(r,ERROR_INVALID_PARAMETER);
    r->sealed=1;
    for(unsigned i=0;i<r->count;i++){
        Node *n=&r->nodes[i];if(n->writable&&!n->flushed)return done(r,ERROR_INVALID_STATE);
        code=metadata(n);if(code)return done(r,code);code=security(r,n,i==0);if(code)return done(r,code);
    }
    r->installing=5;r->sealed=0;return done(r,0);
}
API DWORD gitgo_bootstrap_lock_gate(uint64_t cookie,DWORD index){
    Runtime *r=get(cookie);Node *n;BootstrapGate *gate;DWORD code;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_BOOTSTRAP)return done(r,ERROR_INVALID_STATE);gate=&gates[(cookie&255)-1];
    if(r->sealed||r->installing!=4||index>=r->count||gate->generation!=(cookie>>8)||gate->attempted)
        return done(r,ERROR_INVALID_PARAMETER);
    n=&r->nodes[index];if(n->directory||!n->flushed||n->length)return done(r,ERROR_INVALID_PARAMETER);
    r->sealed=1;gate->attempted=1;code=metadata(n);if(code)return done(r,code);
    if(!LockFileEx(n->handle,LOCKFILE_EXCLUSIVE_LOCK|LOCKFILE_FAIL_IMMEDIATELY,0,1,0,&gate->overlapped))return done(r,error());
    r->sealed=0;return done(r,0);
}

/* Bounded runtime retirement, subordinate to a committed Host intent.
 * No traversal deletion: callers supply the closed installation member set.
 * Each target is held by a relative native handle before disposition. */

#define GRT_NODES 256
typedef struct Tree {
    uint64_t cookie,charge;DWORD parent[GRT_NODES];wchar_t leaf[GRT_NODES][129];
    int absent[GRT_NODES],attempt[GRT_NODES],ready;
} Tree;
static Tree trees[GRP_OWNERS];
static Tree *tree(Runtime *r,uint64_t cookie){Tree *t=&trees[r-records];return t->cookie==cookie?t:0;}
static DWORD gitgo_retirement_open(Runtime *r,Node *n,HANDLE parent,const wchar_t *leaf,int dir,int deleting,int *absent){
    HMODULE nt=GetModuleHandleW(L"ntdll.dll");CreateFn create;ErrorFn convert;NTSTATUS status;
    UNICODE_STRING name;OBJECT_ATTRIBUTES attrs;
    *absent=0;if(!nt)return error();create=(CreateFn)(void*)GetProcAddress(nt,"NtCreateFile");
    convert=(ErrorFn)(void*)GetProcAddress(nt,"RtlNtStatusToDosError");if(!create||!convert)return ERROR_PROC_NOT_FOUND;
    name.Buffer=(PWSTR)leaf;name.Length=(USHORT)(wcslen(leaf)*sizeof(wchar_t));name.MaximumLength=name.Length;
    memset(&attrs,0,sizeof(attrs));attrs.Length=sizeof(attrs);attrs.RootDirectory=parent;attrs.ObjectName=&name;attrs.Attributes=0x40|0x1000;
    n->phase=1;n->directory=dir;
    status=create(&n->handle,SYNCHRONIZE|READ_CONTROL|FILE_READ_ATTRIBUTES|FILE_READ_DATA|(deleting?DELETE:0),
        &attrs,&r->io,0,0,FILE_SHARE_READ|(dir?FILE_SHARE_WRITE:0),1,0x200000|0x20|(dir?1:0x40),0,0);
    if(status==(NTSTATUS)0xc0000034&&!n->handle){*absent=1;n->phase=0;return 0;}
    if(status<0)return convert(status);return n->handle?0:ERROR_INVALID_HANDLE;
}
static DWORD gitgo_retirement_metadata(Runtime *r,Node *n,uint64_t ceiling,int root){
    LARGE_INTEGER size;DWORD e;
    if(!n->directory){if(!GetFileSizeEx(n->handle,&size))return error();
        if(size.QuadPart<0||(uint64_t)size.QuadPart>ceiling)return ERROR_INVALID_DATA;n->length=(uint64_t)size.QuadPart;}
    e=metadata(n);if(e)return e;e=security(r,n,root);if(e)return e;n->phase=2;return 0;
}
API DWORD gitgo_retirement_begin(uint64_t cookie,HANDLE parent,const uint64_t *parent_id,const wchar_t *leaf,const uint64_t *root_id,DWORD root_words){
    Runtime *r=get(cookie);Tree *t;Node *n;DWORD e;int absent;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_RETIRE)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||r->count||r->ancestor_count||!parent||!parent_id||!leaf_ok(leaf)||wcslen(leaf)!=36||
       (root_words!=0&&root_words!=3)||(root_words&&!root_id))return done(r,ERROR_INVALID_PARAMETER);
    for(unsigned i=0;i<36;i++){
        if(i==8||i==13||i==18||i==23){if(leaf[i]!=L'-')return done(r,ERROR_INVALID_PARAMETER);}
        else if(!((leaf[i]>=L'0'&&leaf[i]<=L'9')||(leaf[i]>=L'a'&&leaf[i]<=L'f')))return done(r,ERROR_INVALID_PARAMETER);
    }
    r->sealed=1;r->installing=TREE_RETIRE;t=&trees[r-records];memset(t,0,sizeof(*t));t->cookie=cookie;
    r->ancestor_count=1;n=&r->ancestors[0];e=gitgo_retirement_open(r,n,parent,L"",1,0,&absent);if(e)return done(r,e);
    if(absent)return done(r,ERROR_INVALID_DATA);e=gitgo_retirement_metadata(r,n,0,1);if(e)return done(r,e);
    if(n->id.VolumeSerialNumber!=parent_id[0]||memcmp(n->id.FileId.Identifier,parent_id+1,16))return done(r,ERROR_INVALID_DATA);
    r->count=1;n=&r->nodes[0];memcpy(t->leaf[0],leaf,37*sizeof(wchar_t));t->parent[0]=UINT32_MAX;
    e=gitgo_retirement_open(r,n,r->ancestors[0].handle,leaf,1,1,&absent);if(e)return done(r,e);t->absent[0]=absent;
    if(!absent){
        /* An unrecorded root may only be proved absent. Never adopt its identity. */
        if(!root_words)return done(r,ERROR_INVALID_DATA);e=gitgo_retirement_metadata(r,n,0,1);if(e)return done(r,e);
        if(n->id.VolumeSerialNumber!=parent_id[0]||n->id.VolumeSerialNumber!=root_id[0]||
           memcmp(n->id.FileId.Identifier,root_id+1,16))return done(r,ERROR_INVALID_DATA);
    }
    r->sealed=0;return done(r,0);
}
API DWORD gitgo_retirement_add(uint64_t cookie,DWORD parent,const wchar_t *leaf,int directory,uint64_t ceiling,DWORD *out){
    Runtime *r=get(cookie);Tree *t;Node *n;DWORD e,index;uint64_t charge=4096;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_RETIRE)return done(r,ERROR_INVALID_STATE);t=tree(r,cookie);
    if(!t||r->sealed||r->installing!=TREE_RETIRE||t->ready||!out||r->count>=GRT_NODES||parent>=r->count||
       !r->nodes[parent].directory||!leaf_ok(leaf)||(directory!=0&&directory!=1)||(directory&&ceiling)||ceiling>64*1024*1024)
        return done(r,ERROR_INVALID_PARAMETER);
    *out=UINT32_MAX;while(charge<ceiling)charge*=2;
    if(!directory&&charge>UINT64_C(64)*1024*1024-t->charge)return done(r,ERROR_NOT_ENOUGH_QUOTA);
    for(unsigned i=1;i<r->count;i++)if(t->parent[i]==parent&&!_wcsicmp(t->leaf[i],leaf))return done(r,ERROR_INVALID_DATA);
    r->sealed=1;index=r->count++;n=&r->nodes[index];n->directory=directory;
    t->parent[index]=parent;memcpy(t->leaf[index],leaf,(wcslen(leaf)+1)*sizeof(wchar_t));if(!directory)t->charge+=charge;
    if(t->absent[parent]){t->absent[index]=1;*out=index;r->sealed=0;return done(r,0);}
    e=metadata(&r->nodes[parent]);if(e)return done(r,e);
    e=gitgo_retirement_open(r,n,r->nodes[parent].handle,leaf,directory,1,&t->absent[index]);if(e)return done(r,e);
    if(!t->absent[index]){
        e=gitgo_retirement_metadata(r,n,ceiling,0);if(e)return done(r,e);
        if(n->id.VolumeSerialNumber!=r->nodes[0].id.VolumeSerialNumber)return done(r,ERROR_INVALID_DATA);
        for(unsigned i=0;i<index;i++)if(!t->absent[i]&&!memcmp(&n->id,&r->nodes[i].id,sizeof(n->id)))return done(r,ERROR_INVALID_DATA);
    }
    *out=index;r->sealed=0;return done(r,0);
}
API DWORD gitgo_retirement_info(uint64_t cookie,DWORD index,uint64_t *out,DWORD words){
    Runtime *r=get(cookie);Tree *t;DWORD e;if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_RETIRE)return done(r,ERROR_INVALID_STATE);t=tree(r,cookie);
    if(!t||r->sealed||index>=r->count||!out||words!=2)return done(r,ERROR_INVALID_PARAMETER);
    if(!t->absent[index]){e=metadata(&r->nodes[index]);if(e)return done(r,e);}
    out[0]=(uint64_t)t->absent[index];out[1]=r->nodes[index].length;return done(r,0);
}
API DWORD gitgo_retirement_prepare(uint64_t cookie,DWORD nodes){
    Runtime *r=get(cookie);Tree *t;if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_RETIRE)return done(r,ERROR_INVALID_STATE);t=tree(r,cookie);
    if(!t||r->sealed||t->ready||nodes!=r->count)return done(r,ERROR_INVALID_PARAMETER);
    t->ready=1;return done(r,0);
}
static DWORD gitgo_retirement_probe(Runtime *r,HANDLE parent,const wchar_t *leaf,int directory){
    Node *probe=&r->ancestors[1];DWORD e;int absent=0;
    if(probe->handle||probe->phase==3)return ERROR_INVALID_STATE;r->ancestor_count=2;
    e=gitgo_retirement_open(r,probe,parent,leaf,directory,0,&absent);if(e)return e;
    return absent?0:ERROR_INVALID_DATA;
}
API DWORD gitgo_retirement_delete(uint64_t cookie,DWORD index){
    Runtime *r=get(cookie);Tree *t;Node *n;HANDLE parent;DWORD e;FILE_DISPOSITION_INFO disposition;
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_RETIRE)return done(r,ERROR_INVALID_STATE);t=tree(r,cookie);
    if(!t||r->sealed||!t->ready||index>=r->count||t->attempt[index])return done(r,ERROR_INVALID_PARAMETER);
    for(unsigned i=1;i<r->count;i++)if(t->parent[i]==index&&!t->attempt[i])return done(r,ERROR_INVALID_STATE);
    t->attempt[index]=1;r->sealed=1;n=&r->nodes[index];
    if(t->absent[index]){r->sealed=0;return done(r,0);}
    e=metadata(n);if(e)return done(r,e);e=security(r,n,index==0);if(e)return done(r,e);
    disposition.DeleteFile=TRUE;
    if(!SetFileInformationByHandle(n->handle,FileDispositionInfo,&disposition,sizeof(disposition)))return done(r,error());
    n->phase=3;if(!CloseHandle(n->handle))return done(r,error());n->handle=0;n->phase=0;
    parent=index?r->nodes[t->parent[index]].handle:r->ancestors[0].handle;
    e=gitgo_retirement_probe(r,parent,t->leaf[index],n->directory);if(e)return done(r,e);
    t->absent[index]=1;r->sealed=0;return done(r,0);
}
API DWORD gitgo_retirement_finish(uint64_t cookie){
    Runtime *r=get(cookie);Tree *t;DWORD e;if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_RETIRE)return done(r,ERROR_INVALID_STATE);t=tree(r,cookie);
    if(!t||r->sealed||!t->ready)return done(r,ERROR_INVALID_PARAMETER);
    for(unsigned i=0;i<r->count;i++)if(!t->attempt[i]||!t->absent[i])return done(r,ERROR_INVALID_STATE);
    r->sealed=1;e=metadata(&r->ancestors[0]);if(e)return done(r,e);
    e=gitgo_retirement_probe(r,r->ancestors[0].handle,t->leaf[0],1);if(e)return done(r,e);
    r->installing=TREE_RETIRED;r->sealed=0;return done(r,0);
}

/* Enumerate through an already owned directory handle. No FindFirstFile or
 * Python-owned search handle can escape before the native record owns it. */
API DWORD gitgo_tree_inventory(uint64_t cookie,DWORD index,wchar_t *out,DWORD capacity,DWORD *used,DWORD *count){
    Runtime *r=get(cookie);Node *node;DWORD code,position,total=0,names=0,records_seen=0,offset;
    FILE_INFO_BY_HANDLE_CLASS query=FileIdBothDirectoryRestartInfo;
    FILE_ID_BOTH_DIR_INFO *entry;wchar_t leaf[129];
    if(!r)return ERROR_INVALID_HANDLE;
    if(r->purpose!=TREE_PIN)return done(r,ERROR_INVALID_STATE);
    if(r->sealed||index>=r->count||!r->nodes[index].directory||!out||capacity!=33025||!used||!count)
        return done(r,ERROR_INVALID_PARAMETER);
    *used=0;*count=0;out[0]=0;r->sealed=1;node=&r->nodes[index];
    code=metadata(node);if(code)return done(r,code);
    code=security(r,node,index==0);if(code)return done(r,code);
    for(;;){
        memset(r->inventory,0,sizeof(r->inventory));
        if(!GetFileInformationByHandleEx(node->handle,query,r->inventory,sizeof(r->inventory))){
            code=GetLastError();if(code==ERROR_NO_MORE_FILES)break;return done(r,code?code:ERROR_GEN_FAILURE);
        }
        query=FileIdBothDirectoryInfo;position=0;
        for(;;){
            if(position>sizeof(r->inventory)-offsetof(FILE_ID_BOTH_DIR_INFO,FileName))return done(r,ERROR_INVALID_DATA);
            entry=(FILE_ID_BOTH_DIR_INFO*)(void*)(r->inventory+position);
            if(++records_seen>258||!entry->FileNameLength||entry->FileNameLength%sizeof(wchar_t)||
               entry->FileNameLength>128*sizeof(wchar_t)||
               entry->FileNameLength>sizeof(r->inventory)-position-offsetof(FILE_ID_BOTH_DIR_INFO,FileName))
                return done(r,ERROR_INVALID_DATA);
            memcpy(leaf,entry->FileName,entry->FileNameLength);leaf[entry->FileNameLength/sizeof(wchar_t)]=0;
            if(wcscmp(leaf,L".")&&wcscmp(leaf,L"..")){
                DWORD length=entry->FileNameLength/sizeof(wchar_t);
                if(wcslen(leaf)!=length||!leaf_ok(leaf)||names>=256||total+length+2>capacity)return done(r,ERROR_INVALID_DATA);
                memcpy(out+total,leaf,(length+1)*sizeof(wchar_t));total+=length+1;names++;
            }
            offset=entry->NextEntryOffset;
            if(!offset)break;
            if(offset%8||offset<offsetof(FILE_ID_BOTH_DIR_INFO,FileName)+entry->FileNameLength||
               offset>sizeof(r->inventory)-position)return done(r,ERROR_INVALID_DATA);
            position+=offset;
        }
    }
    out[total]=0;*used=total;*count=names;r->sealed=0;return done(r,0);
}

API DWORD gitgo_tree_abi(void){return 1;}
