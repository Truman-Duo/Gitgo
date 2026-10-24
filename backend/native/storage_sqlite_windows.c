/* Owned bounded SQLite VFS construction dependency. Not default activation.
 * All enforcement callbacks are C; no callback enters Python or allocates here.
 * Parent SQLite/OS allocations remain subject to a separate runtime profile.
 * Caller owns namespace qualification, connection leases and durable budgets.
 */
#include "sqlite3.h"
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <limits.h>
#include <stdatomic.h>
#ifndef _WIN32
#error This native SQLite binding profile requires Windows x64
#endif
#include <windows.h>
#define API __declspec(dllexport)
typedef SRWLOCK Mutex;
static void lock(Mutex *m) { AcquireSRWLockExclusive(m); }
static void unlock(Mutex *m) { ReleaseSRWLockExclusive(m); }
#include "storage_sqlite_binding_windows.c"
#define OWNERS 8
#define FILES 16
#define PATH_BYTES 4096
#define PARENT_BYTES 4096
#define NAME_BYTES 96
#define CAP_MAX ((sqlite3_int64)1 << 40)
enum { FREE=0, LIVE=1, UNKNOWN=2 };
typedef struct Owner Owner;
typedef struct Entry Entry;
typedef struct Proxy { sqlite3_file base; Entry *entry; uint64_t serial; } Proxy;
typedef union NativeStorage { max_align_t alignment; unsigned char bytes[PARENT_BYTES]; } NativeStorage;
struct Entry { Owner *owner; uint64_t serial; int state, role, cleanup_unknown; NativeStorage native; };
struct Owner {
    Mutex mutex;
    uint64_t generation, next_serial, pid, thread, leases, starts, refusals, closes, retired_generation;
    uint64_t max_write[3], max_shm;
    int occupied, sealed, registered, unregister_entered;
    sqlite3_vfs vfs, *parent;
    SqliteImage image;
    int (*unregister_vfs)(sqlite3_vfs *);
    sqlite3_int64 hard[3], write[3], shm;
    char paths[3][PATH_BYTES], name[NAME_BYTES];
    Entry entries[FILES];
};
static Owner owners[OWNERS];
static Mutex global_mutex;
static _Atomic uint64_t module_pid;
static INIT_ONCE once=INIT_ONCE_STATIC_INIT;
static uint64_t process_id(void) { return (uint64_t)GetCurrentProcessId(); }
static BOOL CALLBACK initialize_once(PINIT_ONCE init,PVOID arg,PVOID *context) {
    (void)init;(void)arg;(void)context;
    /* Static SRWLOCK zero initialization is valid. */
    atomic_store(&module_pid,process_id());return TRUE;
}
static void initialize(void) { InitOnceExecuteOnce(&once,initialize_once,0,0); }
static size_t bounded_len(const char *s, size_t cap) {
    size_t i; if(!s) return cap;
    for(i=0;i<cap;i++) if(!s[i]) return i;
    return cap;
}
static int same_path(const char *a,const char *b) {
    size_t i;
    if(!a||!b) return 0;
    for(i=0;i<PATH_BYTES;i++) {
        unsigned char x=(unsigned char)a[i],y=(unsigned char)b[i];
        if(x=='/')x='\\'; if(y=='/')y='\\';
        if(x>='A'&&x<='Z')x+=32; if(y>='A'&&y<='Z')y+=32;
        if(x!=y)return 0; if(!x)return 1;
    }
    return 0;
}
static int role_for(Owner *o,const char *name) {
    int i; for(i=0;i<3;i++)if(same_path(name,o->paths[i]))return i;
    return -1;
}
/* Read-only native metadata check. Namespace identity and exclusive ownership
 * are caller prerequisites; this check alone is not a path-race defense. */
static int size_fits(const char *path,sqlite3_int64 cap) {
    wchar_t wide[PATH_BYTES];WIN32_FILE_ATTRIBUTE_DATA data;DWORD error;
    int n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path,-1,wide,PATH_BYTES);
    uint64_t size;
    if(!n)return 0;
    if(!GetFileAttributesExW(wide,GetFileExInfoStandard,&data)) {
        error=GetLastError();return error==ERROR_FILE_NOT_FOUND;
    }
    if(data.dwFileAttributes&(FILE_ATTRIBUTE_DIRECTORY|FILE_ATTRIBUTE_REPARSE_POINT))return 0;
    size=((uint64_t)data.nFileSizeHigh<<32)|data.nFileSizeLow;
    return size<=(uint64_t)cap;
}
static int shm_fits(Owner *o) {
    char path[PATH_BYTES];size_t n=strlen(o->paths[0]);
    memcpy(path,o->paths[0],n);memcpy(path+n,"-shm",5);
    return size_fits(path,o->shm);
}
static int family_fits(Owner *o) {
    int i;for(i=0;i<3;i++)if(!size_fits(o->paths[i],o->hard[i]))return 0;
    return shm_fits(o);
}
static sqlite3_file *native(Entry *e) { return (sqlite3_file*)e->native.bytes; }
static Owner *from_vfs(sqlite3_vfs *v) { return (Owner*)v->pAppData; }
/* Valid proxy memory is owned by SQLite until xClose. Parent storage is ours. */
static Entry *begin_file(sqlite3_file *p, int allow_sealed) {
    Proxy *proxy=(Proxy*)p; Entry *e=proxy->entry; Owner *o;
    if(!e)return 0; o=e->owner;
    if(atomic_load(&module_pid)!=process_id())return 0; /* Never acquire inherited locks. */
    lock(&o->mutex);
    if(!o->occupied||o->thread!=(uint64_t)GetCurrentThreadId()||e->state!=LIVE||proxy->serial!=e->serial||
       (!allow_sealed&&o->sealed)||(e->cleanup_unknown&&allow_sealed!=2)||!native(e)->pMethods) {
        unlock(&o->mutex); return 0;
    }
    return e;
}
static int finish(Entry *e,int rc) { unlock(&e->owner->mutex); return rc; }
/* A refused bound and ordinary lock contention are known results. Native I/O
 * errors can follow an effect and seal further work, while known close remains
 * available. Short reads retain SQLite's documented zero-fill behavior. */
static int io_finish(Entry *e,int rc) {
    if((rc&255)==SQLITE_IOERR && rc!=SQLITE_IOERR_SHORT_READ)e->owner->sealed=1;
    return finish(e,rc);
}
static int fail(Entry *e,int rc) { e->owner->refusals++; return finish(e,rc); }
static int within(sqlite3_int64 offset,int amount,sqlite3_int64 cap) {
    return offset>=0&&amount>=0&&offset<=cap&&(sqlite3_int64)amount<=cap-offset;
}
static int file_close(sqlite3_file *p) {
    Entry *e=begin_file(p,2); int rc; Owner *o;
    if(!e)return SQLITE_IOERR_CLOSE;
    o=e->owner;
    /* Mark unknown BEFORE crossing native close. No repeat, even after error. */
    e->state=UNKNOWN; o->closes++;
    rc=e->cleanup_unknown?SQLITE_IOERR_CLOSE:native(e)->pMethods->xClose(native(e));
    p->pMethods=0; ((Proxy*)p)->entry=0;
    if(rc==SQLITE_OK) { e->state=FREE; memset(&e->native,0,sizeof(e->native)); }
    else o->sealed=1;
    return io_finish(e,rc);
}
static int file_read(sqlite3_file *p,void *b,int n,sqlite3_int64 off) {
    Entry *e=begin_file(p,0); if(!e)return SQLITE_IOERR_READ;
    if(!within(off,n,e->owner->hard[e->role]))return fail(e,SQLITE_IOERR_READ);
    return io_finish(e,native(e)->pMethods->xRead(native(e),b,n,off));
}
static int file_write(sqlite3_file *p,const void *b,int n,sqlite3_int64 off) {
    Entry *e=begin_file(p,0); int rc; uint64_t end;
    if(!e)return SQLITE_IOERR_WRITE;
    if(!within(off,n,e->owner->write[e->role]))return fail(e,SQLITE_FULL);
    rc=native(e)->pMethods->xWrite(native(e),b,n,off); end=(uint64_t)off+(uint64_t)n;
    if(rc==SQLITE_OK&&end>e->owner->max_write[e->role])e->owner->max_write[e->role]=end;
    return io_finish(e,rc);
}
static int file_truncate(sqlite3_file *p,sqlite3_int64 size) {
    Entry *e=begin_file(p,0); if(!e)return SQLITE_IOERR_TRUNCATE;
    if(size<0||size>e->owner->write[e->role])return fail(e,SQLITE_FULL);
    return io_finish(e,native(e)->pMethods->xTruncate(native(e),size));
}
static int file_sync(sqlite3_file *p,int flags) {
    Entry *e=begin_file(p,0); if(!e)return SQLITE_IOERR_FSYNC;
    return io_finish(e,native(e)->pMethods->xSync(native(e),flags));
}
static int file_size(sqlite3_file *p,sqlite3_int64 *out) {
    Entry *e=begin_file(p,0); int rc; *out=0; if(!e)return SQLITE_IOERR_FSTAT;
    rc=native(e)->pMethods->xFileSize(native(e),out);
    if(rc==SQLITE_OK&&(*out<0||*out>e->owner->hard[e->role])) {
        e->owner->sealed=1; rc=SQLITE_IOERR_FSTAT;
    }
    return io_finish(e,rc);
}
static int file_lock(sqlite3_file *p,int flags) {
    Entry *e=begin_file(p,0); if(!e)return SQLITE_IOERR_LOCK;
    return io_finish(e,native(e)->pMethods->xLock(native(e),flags));
}
static int file_unlock(sqlite3_file *p,int flags) {
    Entry *e=begin_file(p,1); if(!e)return SQLITE_IOERR_UNLOCK;
    return io_finish(e,native(e)->pMethods->xUnlock(native(e),flags));
}
static int file_reserved(sqlite3_file *p,int *out) {
    Entry *e=begin_file(p,0); *out=0; if(!e)return SQLITE_IOERR_CHECKRESERVEDLOCK;
    return io_finish(e,native(e)->pMethods->xCheckReservedLock(native(e),out));
}
static int file_control(sqlite3_file *p,int op,void *arg) {
    Entry *e=begin_file(p,0); if(!e)return SQLITE_IOERR;
    if(op==SQLITE_FCNTL_SIZE_HINT) {
        sqlite3_int64 size=arg?*(sqlite3_int64*)arg:-1;
        if(size<0||size>e->owner->write[e->role])return fail(e,SQLITE_FULL);
        return finish(e,SQLITE_OK); /* Suppress optional allocation. */
    }
    /* Includes raw handles, chunking, mmap and atomic-write controls. */
    return finish(e,SQLITE_NOTFOUND);
}
static int file_sector(sqlite3_file *p) {
    Entry *e=begin_file(p,0); int n; if(!e)return 65536;
    n=native(e)->pMethods->xSectorSize(native(e));
    return finish(e,n);
}
static int file_device(sqlite3_file *p) { (void)p; return 0; }
static int file_shm_map(sqlite3_file *p,int region,int size,int extend,void volatile **out) {
    Entry *e=begin_file(p,0); sqlite3_int64 end; int rc; *out=0;
    if(!e)return SQLITE_IOERR_SHMMAP;
    if(e->role!=0||region<0||size!=32768)return fail(e,SQLITE_IOERR_SHMSIZE);
    end=((sqlite3_int64)region+1)*size;
    if(end>e->owner->shm)return fail(e,SQLITE_IOERR_SHMSIZE);
    if(!shm_fits(e->owner)){e->owner->sealed=1;return fail(e,SQLITE_IOERR_SHMSIZE);}
    rc=native(e)->pMethods->xShmMap(native(e),region,size,extend,out);
    if(rc==SQLITE_OK&&*out&&(uint64_t)end>e->owner->max_shm)e->owner->max_shm=(uint64_t)end;
    return io_finish(e,rc);
}
static int file_shm_lock(sqlite3_file *p,int off,int n,int flags) {
    Entry *e=begin_file(p,1); if(!e)return SQLITE_IOERR_SHMLOCK;
    if(off<0||n<=0||off>SQLITE_SHM_NLOCK||n>SQLITE_SHM_NLOCK-off)return fail(e,SQLITE_IOERR_SHMLOCK);
    if(e->owner->sealed&&(flags&SQLITE_SHM_LOCK))return finish(e,SQLITE_IOERR_SHMLOCK);
    return io_finish(e,native(e)->pMethods->xShmLock(native(e),off,n,flags));
}
static void file_shm_barrier(sqlite3_file *p) {
    Entry *e=begin_file(p,1); if(!e)return;
    native(e)->pMethods->xShmBarrier(native(e)); unlock(&e->owner->mutex);
}
static int file_shm_unmap(sqlite3_file *p,int remove) {
    Entry *e=begin_file(p,1); if(!e)return SQLITE_IOERR_SHMMAP;
    if(remove!=0&&remove!=1)return fail(e,SQLITE_IOERR_SHMMAP);
    int rc=native(e)->pMethods->xShmUnmap(native(e),remove);
    if(rc!=SQLITE_OK){e->cleanup_unknown=1;e->owner->sealed=1;}
    return io_finish(e,rc);
}
static int file_fetch(sqlite3_file *p,sqlite3_int64 off,int n,void **out) {
    (void)p;(void)off;(void)n;*out=0;return SQLITE_OK;
}
static int file_unfetch(sqlite3_file *p,sqlite3_int64 off,void *address) {
    (void)p;(void)off;return address?SQLITE_IOERR:SQLITE_OK;
}
static const sqlite3_io_methods methods={3,file_close,file_read,file_write,file_truncate,
    file_sync,file_size,file_lock,file_unlock,file_reserved,file_control,file_sector,
    file_device,file_shm_map,file_shm_lock,file_shm_barrier,file_shm_unmap,file_fetch,file_unfetch};
static int valid_methods(Owner *o,const sqlite3_io_methods *m) {
    const SqliteImage *image=&o->image;
    if(!m || (uintptr_t)m<(uintptr_t)image->base
       || !sqlite_image_range(image,(uintptr_t)m-(uintptr_t)image->base,sizeof(*m))
       || m->iVersion!=3)return 0;
#define METHOD(name) if(!sqlite_image_function(image,(uintptr_t)m->name))return 0
    METHOD(xClose);METHOD(xRead);METHOD(xWrite);METHOD(xTruncate);METHOD(xSync);
    METHOD(xFileSize);METHOD(xLock);METHOD(xUnlock);METHOD(xCheckReservedLock);
    METHOD(xFileControl);METHOD(xSectorSize);METHOD(xDeviceCharacteristics);
    METHOD(xShmMap);METHOD(xShmLock);METHOD(xShmBarrier);METHOD(xShmUnmap);
#undef METHOD
    return 1;
}
static int vfs_open(sqlite3_vfs *v,const char *name,sqlite3_file *p,int flags,int *out) {
    Owner *o=from_vfs(v); Entry *e=0; Proxy *proxy=(Proxy*)p; int i,role,rc,sector;
    sqlite3_int64 actual=0;
    memset(proxy,0,sizeof(*proxy)); if(out)*out=0;
    if(atomic_load(&module_pid)!=process_id())return SQLITE_CANTOPEN;
    lock(&o->mutex); role=role_for(o,name);
    if(!o->occupied||o->thread!=(uint64_t)GetCurrentThreadId()||!o->registered||o->sealed||!o->leases||role<0||
       (flags&(SQLITE_OPEN_DELETEONCLOSE|SQLITE_OPEN_TEMP_DB|SQLITE_OPEN_TRANSIENT_DB|
               SQLITE_OPEN_TEMP_JOURNAL|SQLITE_OPEN_SUBJOURNAL|SQLITE_OPEN_SUPER_JOURNAL))||
       (flags&(SQLITE_OPEN_MAIN_DB|SQLITE_OPEN_MAIN_JOURNAL|SQLITE_OPEN_WAL))!=
         (role==0?SQLITE_OPEN_MAIN_DB:role==1?SQLITE_OPEN_WAL:SQLITE_OPEN_MAIN_JOURNAL)) {
        o->refusals++; unlock(&o->mutex);return SQLITE_CANTOPEN;
    }
    if(role==0&&!family_fits(o)){o->refusals++;unlock(&o->mutex);return SQLITE_CANTOPEN;}
    for(i=0;i<FILES;i++)if(o->entries[i].state==FREE){e=&o->entries[i];break;}
    if(!e||o->next_serial>=FILES){o->sealed=1;o->refusals++;unlock(&o->mutex);return SQLITE_CANTOPEN;}
    memset(&e->native,0,sizeof(e->native)); e->owner=o;e->role=role;
    e->serial=++o->next_serial;e->state=LIVE;
    proxy->entry=e;proxy->serial=e->serial;proxy->base.pMethods=&methods;
    /* Record independent ownership BEFORE invoking the parent open. */
    rc=o->parent->xOpen(o->parent,name,native(e),flags,out);
    if(!native(e)->pMethods) {
        /* Null methods after failure are no OS-close certificate. Preserve
         * independent storage and prohibit another open until process exit. */
        e->state=UNKNOWN;o->sealed=1;proxy->base.pMethods=0;proxy->entry=0;
        unlock(&o->mutex);return rc?rc:SQLITE_CANTOPEN;
    }
    if(!valid_methods(o,native(e)->pMethods)) {
        e->state=UNKNOWN;o->sealed=1;proxy->base.pMethods=0;proxy->entry=0;
        unlock(&o->mutex);return SQLITE_CANTOPEN;
    }
    /* SQLite will call our xClose after a failed open with non-null methods. */
    if(rc==SQLITE_OK) {
        rc=native(e)->pMethods->xFileSize(native(e),&actual);
        if(rc==SQLITE_OK&&(actual<0||actual>o->hard[role]))rc=SQLITE_CANTOPEN;
    }
    if(rc==SQLITE_OK) {
        sector=native(e)->pMethods->xSectorSize(native(e));
        if(sector<512||sector>65536||(sector&(sector-1)))rc=SQLITE_CANTOPEN;
    }
    if(rc!=SQLITE_OK)o->sealed=1;
    unlock(&o->mutex);return rc;
}
static int vfs_delete(sqlite3_vfs *v,const char *name,int syncdir) {
    Owner *o=from_vfs(v);int role,rc;
    if(atomic_load(&module_pid)!=process_id())return SQLITE_IOERR_DELETE;
    lock(&o->mutex);role=role_for(o,name);
    if(!o->occupied||o->thread!=(uint64_t)GetCurrentThreadId()||o->sealed||!o->leases||role<1){o->refusals++;unlock(&o->mutex);return SQLITE_IOERR_DELETE;}
    rc=o->parent->xDelete(o->parent,name,syncdir);if(rc!=SQLITE_OK)o->sealed=1;unlock(&o->mutex);return rc;
}
static int vfs_access(sqlite3_vfs *v,const char *name,int flags,int *out) {
    Owner *o=from_vfs(v);int rc;*out=0;
    if(atomic_load(&module_pid)!=process_id())return SQLITE_IOERR_ACCESS;
    lock(&o->mutex);
    if(!o->occupied||o->thread!=(uint64_t)GetCurrentThreadId()||o->sealed||!o->leases||role_for(o,name)<0){o->refusals++;unlock(&o->mutex);return SQLITE_IOERR_ACCESS;}
    if(flags!=SQLITE_ACCESS_EXISTS&&flags!=SQLITE_ACCESS_READ&&flags!=SQLITE_ACCESS_READWRITE){unlock(&o->mutex);return SQLITE_MISUSE;}
    rc=o->parent->xAccess(o->parent,name,flags,out);if(rc!=SQLITE_OK)o->sealed=1;unlock(&o->mutex);return rc;
}
static int vfs_fullpath(sqlite3_vfs *v,const char *name,int count,char *out) {
    Owner *o=from_vfs(v);int rc;
    if(count>0)*out=0;if(atomic_load(&module_pid)!=process_id())return SQLITE_CANTOPEN;
    lock(&o->mutex);
    if(!o->occupied||o->thread!=(uint64_t)GetCurrentThreadId()||o->sealed||!o->leases){unlock(&o->mutex);return SQLITE_CANTOPEN;}
    if(!name||!out||count<=0||count>PATH_BYTES){unlock(&o->mutex);return SQLITE_CANTOPEN;}
    rc=o->parent->xFullPathname(o->parent,name,count,out);
    if(rc==SQLITE_OK&&role_for(o,out)<0){*out=0;o->refusals++;rc=SQLITE_CANTOPEN;}
    unlock(&o->mutex);return rc;
}
static void *vfs_dlopen(sqlite3_vfs *v,const char *n){(void)v;(void)n;return 0;}
static void vfs_dlerror(sqlite3_vfs *v,int n,char *out){(void)v;if(n>0)*out=0;}
static void (*vfs_dlsym(sqlite3_vfs *v,void *h,const char *n))(void){(void)v;(void)h;(void)n;return 0;}
static void vfs_dlclose(sqlite3_vfs *v,void *h){(void)v;(void)h;}
static Owner *begin_vfs(sqlite3_vfs *v) {
    Owner *o=from_vfs(v);
    if(atomic_load(&module_pid)!=process_id())return 0;
    lock(&o->mutex);
    if(!o->occupied||o->thread!=(uint64_t)GetCurrentThreadId()||o->sealed||!o->leases){
        unlock(&o->mutex);return 0;
    }
    return o;
}
static int vfs_random(sqlite3_vfs *v,int n,char *out){
    Owner *o=begin_vfs(v);int rc;if(!o)return 0;
    if(n<0||n>4096||!out){unlock(&o->mutex);return 0;}
    rc=o->parent->xRandomness(o->parent,n,out);unlock(&o->mutex);return rc;
}
static int vfs_sleep(sqlite3_vfs *v,int n){
    Owner *o=begin_vfs(v);int rc;if(!o)return 0;
    if(n<0||n>100000){unlock(&o->mutex);return 0;}
    rc=o->parent->xSleep(o->parent,n);unlock(&o->mutex);return rc;
}
static int vfs_time(sqlite3_vfs *v,double *out){
    Owner *o=begin_vfs(v);int rc;*out=0;if(!o)return SQLITE_IOERR;
    rc=o->parent->xCurrentTime(o->parent,out);unlock(&o->mutex);return rc;
}
static int vfs_error(sqlite3_vfs *v,int n,char *out){
    Owner *o=begin_vfs(v);int rc;if(n>0&&out)*out=0;if(!o)return ERROR_INVALID_STATE;
    if(n<0||n>PATH_BYTES||!out){unlock(&o->mutex);return ERROR_INVALID_PARAMETER;}
    rc=o->parent->xGetLastError?o->parent->xGetLastError(o->parent,n,out):0;
    unlock(&o->mutex);return rc;
}
static int vfs_time64(sqlite3_vfs *v,sqlite3_int64 *out){
    Owner *o=begin_vfs(v);int rc;*out=0;if(!o)return SQLITE_IOERR;
    rc=o->parent->xCurrentTimeInt64(o->parent,out);unlock(&o->mutex);return rc;
}
static Owner *get(uint64_t handle) {
    unsigned slot=(unsigned)(handle&255);Owner *o;
    if(!slot||slot>OWNERS)return 0;
    o=&owners[slot-1];
    if(atomic_load(&module_pid)!=process_id())return 0;
    lock(&o->mutex);
    if(!o->occupied||o->generation!=(handle>>8)||o->thread!=(uint64_t)GetCurrentThreadId()){unlock(&o->mutex);return 0;}
    return o;
}
static int sqlite_vfs_create_bound(const SqliteImage *image,sqlite3_vfs *(*find)(const char*),
    sqlite3_vfs *parent,int (*reg)(sqlite3_vfs*,int),
    int (*unreg)(sqlite3_vfs*),const char *name,const char *path,
    sqlite3_int64 main_cap,sqlite3_int64 wal_cap,sqlite3_int64 journal_cap,
    sqlite3_int64 shm_cap,uint64_t *out) {
    Owner *o=0;int i,rc;size_t len;uint64_t gen,serial;
    if(!out)return SQLITE_MISUSE;
    *out=0;initialize();
    if(atomic_load(&module_pid)!=process_id())return SQLITE_MISUSE;
    if(!image||!find||!parent||!reg||!unreg||parent->iVersion!=3||parent->szOsFile<=0||
       parent->szOsFile>PARENT_BYTES||parent->mxPathname>=PATH_BYTES||
       !parent->xOpen||!parent->xDelete||!parent->xAccess||!parent->xFullPathname||
       !parent->xRandomness||!parent->xSleep||!parent->xCurrentTime||!parent->xCurrentTimeInt64||
       bounded_len(name,NAME_BYTES)<16||bounded_len(name,NAME_BYTES)>=NAME_BYTES||
       bounded_len(path,PATH_BYTES-9)>=PATH_BYTES-9||
       main_cap<=0||main_cap>CAP_MAX||wal_cap<=0||wal_cap>CAP_MAX||
       journal_cap<=0||journal_cap>CAP_MAX||shm_cap<32768||shm_cap>CAP_MAX||shm_cap%32768)
        return SQLITE_MISUSE;
    lock(&global_mutex);
    if(find(name)){unlock(&global_mutex);return SQLITE_MISUSE;}
    /* Registrations and parent-file opens are bounded over the entire worker
     * lifetime. Nominal SQLite close cannot refund hidden OS SHM resources. */
    for(i=0;i<OWNERS;i++)if(!owners[i].occupied&&!owners[i].generation) {o=&owners[i];break;}
    if(!o){unlock(&global_mutex);return SQLITE_FULL;}
    lock(&o->mutex);gen=o->generation+1;serial=o->next_serial;
    /* mutex is kept outside the reset region and never moved. */
    memset((char*)o+offsetof(Owner,generation),0,sizeof(*o)-offsetof(Owner,generation));
    o->generation=gen;o->next_serial=serial;o->pid=process_id();o->thread=GetCurrentThreadId();o->occupied=1;
    o->parent=parent;o->unregister_vfs=unreg;o->image=*image;
    memcpy(o->name,name,bounded_len(name,NAME_BYTES)+1);
    rc=parent->xFullPathname(parent,path,PATH_BYTES,o->paths[0]);
    len=bounded_len(o->paths[0],PATH_BYTES-9);
    if(rc||!len||len>=PATH_BYTES-9){o->occupied=0;unlock(&o->mutex);unlock(&global_mutex);return SQLITE_CANTOPEN;}
    memcpy(o->paths[1],o->paths[0],len);memcpy(o->paths[1]+len,"-wal",5);
    memcpy(o->paths[2],o->paths[0],len);memcpy(o->paths[2]+len,"-journal",9);
    o->hard[0]=o->write[0]=main_cap;o->hard[1]=o->write[1]=wal_cap;
    o->hard[2]=o->write[2]=journal_cap;o->shm=shm_cap;
    o->vfs=(sqlite3_vfs){3,sizeof(Proxy),parent->mxPathname,0,o->name,o,
        vfs_open,vfs_delete,vfs_access,vfs_fullpath,vfs_dlopen,vfs_dlerror,vfs_dlsym,vfs_dlclose,
        vfs_random,vfs_sleep,vfs_time,vfs_error,vfs_time64,0,0,0};
    /* Even a registration error retains a handle until exact unregister. */
    o->registered=1;*out=(gen<<8)|(uint64_t)(i+1);
    rc=reg(&o->vfs,0);
    if(rc!=SQLITE_OK)o->sealed=1;
    unlock(&o->mutex);unlock(&global_mutex);return rc;
}
API int gitgo_vfs_create(HMODULE extension,HMODULE selected,const char *name,const char *path,
    sqlite3_int64 main_cap,sqlite3_int64 wal_cap,sqlite3_int64 journal_cap,sqlite3_int64 shm_cap,uint64_t *out) {
    SqliteImage image;uint64_t api[5];sqlite3_vfs *(*find)(const char *);int (*reg)(sqlite3_vfs*,int),(*unreg)(sqlite3_vfs*);
    size_t length;unsigned i;
    if(!out)return SQLITE_MISUSE;*out=0;
    length=bounded_len(name,NAME_BYTES);
    if(length!=46 || memcmp(name,"gitgo-storage-",14))return SQLITE_MISUSE;
    for(i=14;i<46;i++)if(!((name[i]>='0'&&name[i]<='9')||(name[i]>='a'&&name[i]<='f')))return SQLITE_MISUSE;
    if(gitgo_sqlite_bind(extension,selected,api,5) || api[3]!=SQLITE_VERSION_NUMBER || !sqlite_image(selected,&image))return SQLITE_MISUSE;
    find=(sqlite3_vfs *(*)(const char *))(uintptr_t)GetProcAddress(selected,"sqlite3_vfs_find");
    reg=(int (*)(sqlite3_vfs*,int))(uintptr_t)api[1];unreg=(int (*)(sqlite3_vfs*))(uintptr_t)api[2];
    return sqlite_vfs_create_bound(&image,find,(sqlite3_vfs*)(uintptr_t)api[0],reg,unreg,name,path,
                                  main_cap,wal_cap,journal_cap,shm_cap,out);
}

API int gitgo_vfs_lease(uint64_t handle,int acquire) {
    Owner *o;if(acquire!=0&&acquire!=1)return SQLITE_MISUSE;o=get(handle);if(!o)return SQLITE_MISUSE;
    if(acquire) {
        if(o->sealed||o->leases>=FILES||o->starts>=FILES){unlock(&o->mutex);return SQLITE_BUSY;}
        o->leases++;o->starts++;
    } else {
        if(!o->leases){unlock(&o->mutex);return SQLITE_MISUSE;}
        o->leases--;
    }
    unlock(&o->mutex);return SQLITE_OK;
}
API int gitgo_vfs_limit(uint64_t handle,int role,sqlite3_int64 cap) {
    Owner *o=get(handle);if(!o)return SQLITE_MISUSE;
    if(o->sealed||role<0||role>=3||cap<=0||cap>o->hard[role]){unlock(&o->mutex);return SQLITE_MISUSE;}
    o->write[role]=cap;unlock(&o->mutex);return SQLITE_OK;
}
API int gitgo_vfs_seal(uint64_t handle) {
    Owner *o=get(handle);if(!o)return SQLITE_MISUSE;o->sealed=1;unlock(&o->mutex);return SQLITE_OK;
}
/* Fixed-width observational snapshot; never an independent close certificate. */
API int gitgo_vfs_stats(uint64_t handle,uint64_t *out,int count) {
    Owner *o=get(handle);int i;if(!o)return SQLITE_MISUSE;
    if(count!=12||!out){unlock(&o->mutex);return SQLITE_MISUSE;}
    memset(out,0,12*sizeof(*out));out[0]=o->sealed;out[3]=o->leases;
    for(i=0;i<FILES;i++){if(o->entries[i].state==LIVE)out[1]++;if(o->entries[i].state==UNKNOWN)out[2]++;}
    for(i=0;i<3;i++)out[4+i]=o->max_write[i];out[7]=o->max_shm;
    out[8]=o->refusals;out[9]=o->closes;out[10]=o->next_serial;out[11]=sizeof(owners);
    unlock(&o->mutex);return SQLITE_OK;
}
API int gitgo_vfs_destroy(uint64_t handle) {
    Owner *o;int i,rc;unsigned slot=(unsigned)(handle&255);uint64_t generation=handle>>8;
    initialize();
    if(atomic_load(&module_pid)!=process_id()||!slot||slot>OWNERS||!generation)return SQLITE_MISUSE;
    lock(&global_mutex);o=&owners[slot-1];lock(&o->mutex);
    if(o->generation!=generation||o->thread!=(uint64_t)GetCurrentThreadId()){
        unlock(&o->mutex);unlock(&global_mutex);return SQLITE_MISUSE;
    }
    if(o->retired_generation==generation){unlock(&o->mutex);unlock(&global_mutex);return SQLITE_OK;}
    if(!o->occupied||o->unregister_entered){unlock(&o->mutex);unlock(&global_mutex);return SQLITE_MISUSE;}
    if(o->leases){unlock(&o->mutex);unlock(&global_mutex);return SQLITE_BUSY;}
    for(i=0;i<FILES;i++)if(o->entries[i].state!=FREE){unlock(&o->mutex);unlock(&global_mutex);return SQLITE_BUSY;}
    o->sealed=1;o->unregister_entered=1;
    rc=o->unregister_vfs(&o->vfs);
    if(rc==SQLITE_OK){o->registered=0;o->occupied=0;o->retired_generation=generation;}
    unlock(&o->mutex);unlock(&global_mutex);return rc;
}
API unsigned gitgo_vfs_abi(void) { return 1; }

API int gitgo_vfs_extent(uint64_t handle,int role,sqlite3_int64 *out){
    Owner *o=get(handle);Entry *found=0;int i,rc;
    if(!o)return SQLITE_MISUSE;
    if(!out||role<0||role>2||o->sealed||!o->leases){unlock(&o->mutex);return SQLITE_MISUSE;}
    *out=-1;
    for(i=0;i<FILES;i++)if(o->entries[i].role==role&&o->entries[i].state!=FREE){
        if(found||o->entries[i].state!=LIVE){unlock(&o->mutex);return SQLITE_BUSY;}
        found=&o->entries[i];
    }
    if(!found||!native(found)->pMethods){unlock(&o->mutex);return SQLITE_NOTFOUND;}
    rc=native(found)->pMethods->xFileSize(native(found),out);
    if(rc==SQLITE_OK&&(*out<0||*out>o->hard[role]))rc=SQLITE_IOERR_FSTAT;
    if(rc!=SQLITE_OK)o->sealed=1;
    unlock(&o->mutex);return rc;
}

API unsigned gitgo_vfs_header_version(void){return SQLITE_VERSION_NUMBER;}
