/* Internal protocol fault injection. No production callbacks or switches.
 * Each process owns one fixture; no actual user database or account is used. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "../../backend/native/storage_sqlite_windows.c"
static int effects, answer;
static int fake_close(sqlite3_file *p){(void)p;effects++;return answer;}
static int fake_write(sqlite3_file *p,const void *b,int n,sqlite3_int64 o){(void)p;(void)b;(void)n;(void)o;effects++;return answer;}
static int fake_read(sqlite3_file *p,void *b,int n,sqlite3_int64 o){(void)p;(void)o;effects++;memset(b,0,n);return answer;}
static int fake_sync(sqlite3_file *p,int f){(void)p;(void)f;effects++;return answer;}
static int fake_unmap(sqlite3_file *p,int f){(void)p;(void)f;effects++;return answer;}
static int fake_unlock(sqlite3_file *p,int f){(void)p;(void)f;effects++;return answer;}
static int fake_unregister(sqlite3_vfs *v){(void)v;effects++;return answer;}
static int fake_open(sqlite3_vfs *v,const char *n,sqlite3_file *p,int f,int *o){
    (void)v;(void)n;(void)f;(void)o;effects++;
    p->pMethods=answer==SQLITE_OK?(const sqlite3_io_methods*)(uintptr_t)1:0;
    return answer;
}
static int fake_access(sqlite3_vfs *v,const char *n,int f,int *out){(void)v;(void)n;(void)f;effects++;*out=0;return 0;}
static int fake_delete(sqlite3_vfs *v,const char *n,int f){(void)v;(void)n;(void)f;effects++;return 0;}
static int fake_full(sqlite3_vfs *v,const char *n,int c,char *out){(void)v;(void)n;(void)c;effects++;*out=0;return 0;}
static sqlite3_io_methods fake_methods={.iVersion=3,.xClose=fake_close,.xRead=fake_read,
    .xWrite=fake_write,.xSync=fake_sync,.xShmUnmap=fake_unmap,.xUnlock=fake_unlock};
static sqlite3_vfs fake_parent={.iVersion=3,.xOpen=fake_open,.xAccess=fake_access,
    .xDelete=fake_delete,.xFullPathname=fake_full};
static Proxy proxy;
static Owner *prepare(const char *path) {
    Owner *o=&owners[0];Entry *e=&o->entries[0];size_t n=strlen(path);
    initialize();o->occupied=1;o->generation=1;o->registered=1;o->leases=1;
    o->pid=process_id();o->thread=GetCurrentThreadId();o->parent=&fake_parent;
    o->vfs.pAppData=o;o->unregister_vfs=fake_unregister;
    assert(n+9<PATH_BYTES);
    memcpy(o->paths[0],path,n+1);memcpy(o->paths[1],path,n);memcpy(o->paths[1]+n,"-wal",5);
    memcpy(o->paths[2],path,n);memcpy(o->paths[2]+n,"-journal",9);
    o->hard[0]=o->hard[1]=o->hard[2]=o->write[0]=o->write[1]=o->write[2]=4096;o->shm=32768;
    e->owner=o;e->state=LIVE;e->serial=1;native(e)->pMethods=&fake_methods;
    proxy.base.pMethods=&methods;proxy.entry=e;proxy.serial=1;
    return o;
}
static DWORD WINAPI wrong_thread(void *arg) {
    Owner *o=arg;int out=0;char path[PATH_BYTES];uint64_t stats[12];
    assert(gitgo_vfs_lease(257,1)==SQLITE_MISUSE);
    assert(gitgo_vfs_stats(257,stats,12)==SQLITE_MISUSE);
    assert(vfs_access(&o->vfs,o->paths[0],SQLITE_ACCESS_EXISTS,&out)==SQLITE_IOERR_ACCESS);
    assert(vfs_delete(&o->vfs,o->paths[1],0)==SQLITE_IOERR_DELETE);
    assert(vfs_fullpath(&o->vfs,o->paths[0],sizeof(path),path)==SQLITE_CANTOPEN);
    assert(file_write(&proxy.base,"x",1,0)==SQLITE_IOERR_WRITE);
    assert(file_close(&proxy.base)==SQLITE_IOERR_CLOSE);
    assert(gitgo_vfs_destroy(257)==SQLITE_MISUSE);
    return 0;
}
int main(int argc,char **argv) {
    Owner *o;Entry *e;uint64_t stats[12];int before;char b[16];
    assert(argc==3);o=prepare(argv[2]);e=&o->entries[0];
    if(!strcmp(argv[1],"write")) {
        answer=SQLITE_IOERR_WRITE;
        assert(file_write(&proxy.base,"x",1,0)==answer&&effects==1&&o->sealed);
        assert(file_write(&proxy.base,"x",1,0)==answer&&effects==1);
        answer=SQLITE_OK;assert(file_close(&proxy.base)==0&&effects==2);
    } else if(!strcmp(argv[1],"sync")) {
        answer=SQLITE_IOERR_FSYNC;
        assert(file_sync(&proxy.base,SQLITE_SYNC_FULL)==answer&&effects==1&&o->sealed);
        assert(file_sync(&proxy.base,SQLITE_SYNC_FULL)==answer&&effects==1);
    } else if(!strcmp(argv[1],"short_read")) {
        memset(b,1,sizeof(b));answer=SQLITE_IOERR_SHORT_READ;
        assert(file_read(&proxy.base,b,sizeof(b),0)==answer&&!o->sealed);
        for(unsigned i=0;i<sizeof(b);i++)assert(!b[i]);
        answer=SQLITE_IOERR_READ;assert(file_read(&proxy.base,b,sizeof(b),0)==answer&&o->sealed);
    } else if(!strcmp(argv[1],"close")) {
        answer=SQLITE_IOERR_CLOSE;
        assert(file_close(&proxy.base)==answer&&effects==1&&e->state==UNKNOWN&&!proxy.base.pMethods);
        for(int i=0;i<3;i++)assert(file_close(&proxy.base)==answer&&effects==1);
        assert(gitgo_vfs_lease(257,0)==0&&gitgo_vfs_destroy(257)==SQLITE_BUSY);
    } else if(!strcmp(argv[1],"unmap")) {
        answer=SQLITE_IOERR_SHMMAP;
        assert(file_shm_unmap(&proxy.base,1)==answer&&effects==1&&e->cleanup_unknown);
        assert(file_shm_unmap(&proxy.base,1)==answer&&effects==1);
        assert(file_unlock(&proxy.base,SQLITE_LOCK_NONE)==SQLITE_IOERR_UNLOCK&&effects==1);
        assert(file_close(&proxy.base)==SQLITE_IOERR_CLOSE&&effects==1&&e->state==UNKNOWN);
    } else if(!strcmp(argv[1],"unregister")) {
        e->state=FREE;o->leases=0;answer=SQLITE_ERROR;
        assert(gitgo_vfs_destroy(257)==answer&&effects==1);
        for(int i=0;i<3;i++)assert(gitgo_vfs_destroy(257)==SQLITE_MISUSE&&effects==1);
        assert(o->occupied&&o->sealed);
    } else if(!strcmp(argv[1],"retired")) {
        e->state=FREE;o->leases=0;answer=SQLITE_OK;
        for(int i=0;i<4;i++)assert(gitgo_vfs_destroy(257)==0&&effects==1);
        assert(!o->occupied&&o->generation&&o->retired_generation);
    } else if(!strcmp(argv[1],"thread")) {
        HANDLE thread=CreateThread(0,0,wrong_thread,o,0,0);
        assert(thread&&WaitForSingleObject(thread,10000)==WAIT_OBJECT_0&&CloseHandle(thread));
        assert(effects==0&&!o->sealed&&e->state==LIVE);
    } else if(!strcmp(argv[1],"null_open")||!strcmp(argv[1],"foreign_methods")) {
        e->state=FREE;answer=!strcmp(argv[1],"null_open")?SQLITE_CANTOPEN:SQLITE_OK;
        assert(vfs_open(&o->vfs,o->paths[0],&proxy.base,SQLITE_OPEN_MAIN_DB|SQLITE_OPEN_READWRITE,0)==SQLITE_CANTOPEN);
        assert(effects==1&&e->state==UNKNOWN&&o->sealed&&!proxy.base.pMethods);
    } else if(!strcmp(argv[1],"ceilings")) {
        assert(file_write(&proxy.base,"x",1,4096)==SQLITE_FULL&&!effects&&!o->sealed);
        assert(file_write(&proxy.base,"x",1,INT64_MAX)==SQLITE_FULL&&!effects);
        assert(gitgo_vfs_limit(257,0,4097)==SQLITE_MISUSE&&!effects);
        e->state=FREE;o->next_serial=FILES;
        assert(vfs_open(&o->vfs,o->paths[0],&proxy.base,SQLITE_OPEN_MAIN_DB|SQLITE_OPEN_READWRITE,0)==SQLITE_CANTOPEN&&!effects&&o->sealed);
    } else if(!strcmp(argv[1],"leases")) {
        assert(gitgo_vfs_lease(257,2)==SQLITE_MISUSE&&o->leases==1);
        assert(gitgo_vfs_lease(257,-1)==SQLITE_MISUSE&&o->leases==1);
        for(int i=0;i<FILES;i++){assert(gitgo_vfs_lease(257,1)==0);assert(gitgo_vfs_lease(257,0)==0);}
        assert(gitgo_vfs_lease(257,1)==SQLITE_BUSY&&o->leases==1&&o->starts==FILES);
    } else assert(0);
    before=effects;
    if(o->occupied){assert(gitgo_vfs_stats(257,stats,12)==0);assert(effects==before);}
    printf("native SQLite protocol fault passed: %s\n",argv[1]);return 0;
}
