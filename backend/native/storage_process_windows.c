/* Windows storage worker lifetime ABI. Does not grant admission or activate a format.
 * Native records exist before every handle-creation effect. Parent ownership
 * survives Python FFI result-conversion failure. No Python callbacks here.
 */
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdatomic.h>
#include <psapi.h>
#define API __declspec(dllexport)
#define COUNT 8
#define HANDLES 11
#define ATTR_BYTES 4096
enum { H_JOB,H_PROCESS,H_THREAD,H_IN_READ,H_IN_WRITE,H_OUT_READ,H_OUT_WRITE,H_ERR_READ,H_ERR_WRITE,H_OBSERVER,H_DIAGNOSTIC };
enum { H_EMPTY,H_LIVE,H_UNKNOWN };
typedef struct Held { HANDLE value;int phase; } Held;
typedef union AttrBuffer { max_align_t align;unsigned char bytes[ATTR_BYTES]; } AttrBuffer;
typedef struct Owner {
    SRWLOCK lock;
    uint64_t generation;
    int occupied,started,sealed,release_attempted,terminate_attempted,attrs_ready,resume_attempted;
    DWORD process_id,last_error,close_calls,observer_pid;
    uint64_t observed_creation;
    DWORD process_snapshot[16384];
    Held handles[HANDLES];
    PROCESS_INFORMATION created;
    AttrBuffer attrs;
} Owner;
static Owner owners[COUNT];
static SRWLOCK pool_lock;
static INIT_ONCE once=INIT_ONCE_STATIC_INIT;
static _Atomic DWORD module_pid;
static BOOL CALLBACK initialized(PINIT_ONCE init,PVOID arg,PVOID *context) {
    (void)init;(void)arg;(void)context;atomic_store(&module_pid,GetCurrentProcessId());return TRUE;
}
static Owner *get(uint64_t cookie) {
    unsigned i=(unsigned)(cookie&255);Owner *o;
    if(!i||i>COUNT||atomic_load(&module_pid)!=GetCurrentProcessId())return 0;
    o=&owners[i-1];AcquireSRWLockExclusive(&o->lock);
    if(!o->occupied||o->generation!=(cookie>>8)){ReleaseSRWLockExclusive(&o->lock);return 0;}
    /* Creation's output storage also belongs to the persistent native record.
     * If control failed before normal handoff, recover these exact handles;
     * EMPTY/UNKNOWN slots never reacquire a previously closed numeric value. */
    if(o->handles[H_PROCESS].phase==H_LIVE&&!o->handles[H_PROCESS].value)
        o->handles[H_PROCESS].value=o->created.hProcess;
    if(o->handles[H_THREAD].phase==H_LIVE&&!o->handles[H_THREAD].value)
        o->handles[H_THREAD].value=o->created.hThread;
    if(!o->process_id)o->process_id=o->created.dwProcessId;
    return o;
}
static DWORD done(Owner *o,DWORD error) {
    if(error)o->last_error=error;
    ReleaseSRWLockExclusive(&o->lock);return error;
}
static DWORD failed(void) { DWORD error=GetLastError();return error?error:ERROR_GEN_FAILURE; }
static int held_close(Owner *o,int index) {
    Held *h=&o->handles[index];
    if(!h->value)return h->phase!=H_UNKNOWN;
    if(h->phase==H_UNKNOWN)return 0;
    h->phase=H_UNKNOWN;o->close_calls++;
    if(!CloseHandle(h->value)){o->last_error=failed();o->sealed=1;return 0;}
    h->value=0;h->phase=H_EMPTY;return 1;
}
static int pipe_pair(Owner *o,int read_index,int write_index) {
    SECURITY_ATTRIBUTES sa={sizeof(sa),0,TRUE};BOOL ok;
    o->handles[read_index].phase=o->handles[write_index].phase=H_LIVE;
    ok=CreatePipe(&o->handles[read_index].value,&o->handles[write_index].value,&sa,16384);
    if(!ok)o->last_error=failed();
    return ok!=0;
}
API DWORD gitgo_process_reserve(uint64_t *cookie) {
    int i;Owner *o=0;uint64_t generation;
    if(!cookie)return ERROR_INVALID_PARAMETER;*cookie=0;
    InitOnceExecuteOnce(&once,initialized,0,0);
    AcquireSRWLockExclusive(&pool_lock);
    for(i=0;i<COUNT;i++)if(!owners[i].occupied&&owners[i].generation<(UINT64_MAX>>8)){o=&owners[i];break;}
    if(!o){ReleaseSRWLockExclusive(&pool_lock);return ERROR_NOT_ENOUGH_QUOTA;}
    AcquireSRWLockExclusive(&o->lock);generation=o->generation+1;
    memset((char*)o+offsetof(Owner,generation),0,sizeof(*o)-offsetof(Owner,generation));
    o->generation=generation;o->occupied=1;*cookie=(generation<<8)|(uint64_t)(i+1);
    ReleaseSRWLockExclusive(&o->lock);ReleaseSRWLockExclusive(&pool_lock);return 0;
}
/* A pseudo-handle observes this process; no Python-created handle debt exists.
 * This proves neither selected-runtime membership nor installation approval. */
API DWORD gitgo_process_current(uint64_t cookie,uint64_t *out,DWORD words,
                               wchar_t *image,DWORD image_cap,wchar_t *system,DWORD system_cap) {
    Owner *o=get(cookie);FILETIME created,exit,kernel,user;DWORD count,error=0;
    if(!o)return ERROR_INVALID_HANDLE;
    if(o->sealed||!out||words!=2||!image||image_cap!=32768||!system||system_cap!=513)
        return done(o,ERROR_INVALID_PARAMETER);
    memset(out,0,words*sizeof(*out));image[0]=system[0]=0;
    if(!GetProcessTimes(GetCurrentProcess(),&created,&exit,&kernel,&user)){error=failed();goto failed_query;}
    count=GetModuleFileNameW(NULL,image,image_cap);
    if(!count){error=failed();goto failed_query;}
    if(count>=image_cap){error=ERROR_INSUFFICIENT_BUFFER;goto failed_query;}
    count=GetSystemWindowsDirectoryW(system,system_cap);
    if(!count){error=failed();goto failed_query;}
    if(count>=system_cap){error=ERROR_INSUFFICIENT_BUFFER;goto failed_query;}
    out[0]=GetCurrentProcessId();out[1]=((uint64_t)created.dwHighDateTime<<32)|created.dwLowDateTime;
    if(!out[0]||!out[1]){error=ERROR_INVALID_DATA;goto failed_query;}
    return done(o,0);
failed_query:
    memset(out,0,words*sizeof(*out));image[0]=system[0]=0;o->sealed=1;
    return done(o,error);
}
API DWORD gitgo_process_start(uint64_t cookie,const wchar_t *executable,wchar_t *command,
                    const wchar_t *directory,wchar_t *environment) {
    Owner *o=get(cookie);JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    JOBOBJECT_CPU_RATE_CONTROL_INFORMATION cpu;
    STARTUPINFOEXW startup;SIZE_T size=0;
    HANDLE inherit[3],jobs[1];DWORD error=0,processors;BOOL ok;
    if(!o)return ERROR_INVALID_HANDLE;
    if(o->started||o->sealed||!executable||!command||!directory||!environment)
        return done(o,ERROR_INVALID_PARAMETER);
    /* Attempt is consumed even if creation later fails. No fresh launch retry. */
    o->started=1;
    o->handles[H_JOB].phase=H_LIVE;
    o->handles[H_JOB].value=CreateJobObjectW(0,0);
    if(!o->handles[H_JOB].value){error=failed();goto end;}
    memset(&limits,0,sizeof(limits));
    limits.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE|
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS|JOB_OBJECT_LIMIT_JOB_MEMORY|JOB_OBJECT_LIMIT_JOB_TIME;
    limits.BasicLimitInformation.ActiveProcessLimit=1;
    limits.BasicLimitInformation.PerJobUserTimeLimit.QuadPart=30LL*10000000;
    limits.JobMemoryLimit=256u*1024u*1024u;
    if(!SetInformationJobObject(o->handles[H_JOB].value,JobObjectExtendedLimitInformation,&limits,sizeof(limits))){error=failed();goto end;}
    processors=GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
    if(!processors){error=failed();goto end;}
    memset(&cpu,0,sizeof(cpu));cpu.ControlFlags=JOB_OBJECT_CPU_RATE_CONTROL_ENABLE|JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP;
    cpu.CpuRate=10000/processors;if(!cpu.CpuRate)cpu.CpuRate=1;
    if(!SetInformationJobObject(o->handles[H_JOB].value,JobObjectCpuRateControlInformation,&cpu,sizeof(cpu))){error=failed();goto end;}
    if(!pipe_pair(o,H_IN_READ,H_IN_WRITE)||!pipe_pair(o,H_OUT_READ,H_OUT_WRITE)||!pipe_pair(o,H_ERR_READ,H_ERR_WRITE)){error=o->last_error;goto end;}
    if(!SetHandleInformation(o->handles[H_IN_WRITE].value,HANDLE_FLAG_INHERIT,0)||
       !SetHandleInformation(o->handles[H_OUT_READ].value,HANDLE_FLAG_INHERIT,0)||
       !SetHandleInformation(o->handles[H_ERR_READ].value,HANDLE_FLAG_INHERIT,0)){error=failed();goto end;}
    InitializeProcThreadAttributeList(0,2,0,&size);
    if(!size||size>ATTR_BYTES){error=ERROR_INSUFFICIENT_BUFFER;goto end;}
    if(!InitializeProcThreadAttributeList((LPPROC_THREAD_ATTRIBUTE_LIST)o->attrs.bytes,2,0,&size)){error=failed();goto end;}
    o->attrs_ready=1;
    jobs[0]=o->handles[H_JOB].value;
    inherit[0]=o->handles[H_IN_READ].value;inherit[1]=o->handles[H_OUT_WRITE].value;inherit[2]=o->handles[H_ERR_WRITE].value;
    if(!UpdateProcThreadAttribute((LPPROC_THREAD_ATTRIBUTE_LIST)o->attrs.bytes,0,PROC_THREAD_ATTRIBUTE_JOB_LIST,jobs,sizeof(jobs),0,0)||
       !UpdateProcThreadAttribute((LPPROC_THREAD_ATTRIBUTE_LIST)o->attrs.bytes,0,PROC_THREAD_ATTRIBUTE_HANDLE_LIST,inherit,sizeof(inherit),0,0)){error=failed();goto end;}
    memset(&startup,0,sizeof(startup));
    startup.StartupInfo.cb=sizeof(startup);startup.StartupInfo.dwFlags=STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput=inherit[0];startup.StartupInfo.hStdOutput=inherit[1];startup.StartupInfo.hStdError=inherit[2];
    startup.lpAttributeList=(LPPROC_THREAD_ATTRIBUTE_LIST)o->attrs.bytes;
    /* Record slots already exist; the OS atomically assigns JOB_LIST before
     * running the process. Only the listed private pipe ends are inherited. */
    o->handles[H_PROCESS].phase=o->handles[H_THREAD].phase=H_LIVE;
    ok=CreateProcessW(executable,command,0,0,TRUE,
        /* DETACHED_PROCESS avoids allocating a console host at all. The three
         * explicitly inherited pipe handles still carry bounded private IPC. */
        EXTENDED_STARTUPINFO_PRESENT|CREATE_UNICODE_ENVIRONMENT|DETACHED_PROCESS|CREATE_SUSPENDED,
        environment,directory,&startup.StartupInfo,&o->created);
    error=ok?0:failed();
    o->handles[H_PROCESS].value=o->created.hProcess;o->handles[H_THREAD].value=o->created.hThread;
    o->process_id=o->created.dwProcessId;
    if(ok&&(!held_close(o,H_IN_READ)||!held_close(o,H_OUT_WRITE)||!held_close(o,H_ERR_WRITE)))error=o->last_error;
end:
    if(o->attrs_ready){DeleteProcThreadAttributeList((LPPROC_THREAD_ATTRIBUTE_LIST)o->attrs.bytes);o->attrs_ready=0;}
    if(error)o->sealed=1;
    return done(o,error);
}
static int process_identity(Owner *o,DWORD *pid,uint64_t *created) {
    FILETIME creation,exit,kernel,user;
    if(o->handles[H_PROCESS].phase!=H_LIVE||!o->handles[H_PROCESS].value)return 0;
    *pid=GetProcessId(o->handles[H_PROCESS].value);
    if(!*pid||!GetProcessTimes(o->handles[H_PROCESS].value,&creation,&exit,&kernel,&user))return 0;
    *created=((uint64_t)creation.dwHighDateTime<<32)|creation.dwLowDateTime;
    return *created!=0;
}
API DWORD gitgo_process_identity(uint64_t cookie,uint64_t *out,DWORD count) {
    Owner *o=get(cookie);DWORD pid=0;uint64_t created=0;
    if(!o)return ERROR_INVALID_HANDLE;
    if(!out||count!=3)return done(o,ERROR_INVALID_PARAMETER);
    memset(out,0,count*sizeof(*out));
    if(!process_identity(o,&pid,&created))return done(o,failed());
    out[0]=pid;out[1]=created;out[2]=o->resume_attempted;
    return done(o,0);
}
/* Recovery observation uses an owned native handle and exact creation time.
 * An access/query failure is UNKNOWN. This never sends termination or resume.
 * The one-process profile plus both parent's and worker's confirmed death is
 * a separate caller precondition before durable launch retirement. */
API DWORD gitgo_process_ended(uint64_t cookie,DWORD pid,uint64_t expected_creation,int *ended) {
    Owner *o=get(cookie);DWORD error,wait,bytes,i;FILETIME created,exit,kernel,user;uint64_t actual;
    if(!o)return ERROR_INVALID_HANDLE;
    if(!pid||!expected_creation||!ended||o->handles[H_OBSERVER].phase==H_UNKNOWN)
        return done(o,ERROR_INVALID_PARAMETER);
    *ended=0;
    if(o->observer_pid&&(o->observer_pid!=pid||o->observed_creation!=expected_creation))
        return done(o,ERROR_INVALID_STATE);
    o->observer_pid=pid;o->observed_creation=expected_creation;
    if(!o->handles[H_OBSERVER].value){
        o->handles[H_OBSERVER].phase=H_LIVE;
        o->handles[H_OBSERVER].value=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,FALSE,pid);
        if(!o->handles[H_OBSERVER].value){
            error=failed();
            /* An error code is not absence evidence. Require a complete,
             * bounded native process snapshot and actual absence from it. */
            if(error==ERROR_INVALID_PARAMETER){
                if(!K32EnumProcesses(o->process_snapshot,sizeof(o->process_snapshot),&bytes))return done(o,failed());
                if(bytes>=sizeof(o->process_snapshot)||bytes%sizeof(DWORD))return done(o,ERROR_INSUFFICIENT_BUFFER);
                for(i=0;i<bytes/sizeof(DWORD);i++)if(o->process_snapshot[i]==pid)return done(o,error);
                *ended=1;return done(o,0);
            }
            return done(o,error);
        }
        o->observer_pid=pid;o->observed_creation=expected_creation;
    }
    if(o->observer_pid!=pid||o->observed_creation!=expected_creation)return done(o,ERROR_INVALID_STATE);
    if(!GetProcessTimes(o->handles[H_OBSERVER].value,&created,&exit,&kernel,&user))return done(o,failed());
    actual=((uint64_t)created.dwHighDateTime<<32)|created.dwLowDateTime;
    if(actual!=expected_creation){*ended=1;return done(o,0);}
    wait=WaitForSingleObject(o->handles[H_OBSERVER].value,0);
    if(wait==WAIT_OBJECT_0){*ended=1;return done(o,0);}
    if(wait==WAIT_TIMEOUT)return done(o,0);
    return done(o,failed());
}
/* Caller must first persist this exact identity and its existing bootstrap
 * authority. This native API validates object identity, not disk durability. */
API DWORD gitgo_process_resume(uint64_t cookie,DWORD expected_pid,uint64_t expected_creation) {
    Owner *o=get(cookie);DWORD pid=0,result;uint64_t created=0;
    if(!o)return ERROR_INVALID_HANDLE;
    if(o->sealed||o->resume_attempted||o->handles[H_THREAD].phase!=H_LIVE||!o->handles[H_THREAD].value)
        return done(o,ERROR_INVALID_STATE);
    if(!process_identity(o,&pid,&created))return done(o,failed());
    if(pid!=expected_pid||created!=expected_creation)return done(o,ERROR_INVALID_PARAMETER);
    o->resume_attempted=1;
    result=ResumeThread(o->handles[H_THREAD].value);
    if(result!=1){o->sealed=1;return done(o,result==(DWORD)-1?failed():ERROR_INVALID_STATE);}
    if(!held_close(o,H_THREAD))return done(o,ERROR_INVALID_STATE);
    return done(o,0);
}
/* Nonblocking observation: no caller needs to wait inside a held owner lock. */
API DWORD gitgo_process_read(uint64_t cookie,int stderr_stream,void *buffer,DWORD cap,DWORD *count) {
    Owner *o=get(cookie);DWORD available=0,take;int i=stderr_stream?H_ERR_READ:H_OUT_READ;
    if(!o)return ERROR_INVALID_HANDLE;
    if(!count||!buffer||!cap||cap>4096)return done(o,ERROR_INVALID_PARAMETER);
    *count=0;
    if(o->handles[i].phase!=H_LIVE||!o->handles[i].value)return done(o,ERROR_INVALID_HANDLE);
    if(!PeekNamedPipe(o->handles[i].value,0,0,0,&available,0))return done(o,failed());
    take=available<cap?available:cap;
    if(take&&!ReadFile(o->handles[i].value,buffer,take,count,0))return done(o,failed());
    return done(o,0);
}
/* One bounded handoff byte; no business authority or general RPC. */
API DWORD gitgo_process_release(uint64_t cookie) {
    Owner *o=get(cookie);DWORD written=0;char value='R';
    if(!o)return ERROR_INVALID_HANDLE;
    if(o->sealed||o->release_attempted||o->handles[H_IN_WRITE].phase!=H_LIVE)return done(o,ERROR_INVALID_STATE);
    o->release_attempted=1;
    if(!WriteFile(o->handles[H_IN_WRITE].value,&value,1,&written,0)||written!=1){o->sealed=1;return done(o,failed());}
    return done(o,0);
}
API DWORD gitgo_process_status(uint64_t cookie,uint64_t *out,DWORD count) {
    Owner *o=get(cookie);JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info;int i;
    if(!o)return ERROR_INVALID_HANDLE;
    if(!out||count!=9)return done(o,ERROR_INVALID_PARAMETER);
    memset(out,0,count*sizeof(*out));out[0]=o->started;out[1]=o->sealed;out[2]=o->process_id;
    for(i=0;i<HANDLES;i++){if(o->handles[i].value)out[5]++;if(o->handles[i].phase==H_UNKNOWN)out[6]++;}
    out[7]=o->last_error;out[8]=o->close_calls;
    if(o->handles[H_JOB].value&&o->handles[H_JOB].phase==H_LIVE){
        if(!QueryInformationJobObject(o->handles[H_JOB].value,JobObjectBasicAccountingInformation,&info,sizeof(info),0))return done(o,failed());
        out[3]=info.ActiveProcesses;out[4]=info.TotalProcesses;
    }
    return done(o,0);
}
API DWORD gitgo_process_retire(uint64_t cookie,int terminate) {
    Owner *o;JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info;DWORD wait;int i;
    unsigned index=(unsigned)(cookie&255);uint64_t generation=cookie>>8;
    if(!index||index>COUNT||!generation||atomic_load(&module_pid)!=GetCurrentProcessId()||
       (terminate!=0&&terminate!=1))return ERROR_INVALID_PARAMETER;
    AcquireSRWLockExclusive(&pool_lock);o=&owners[index-1];AcquireSRWLockExclusive(&o->lock);
    /* Native slots advance only after confirmed retirement. This is a query
     * of that recorded terminal fact, never a second OS close or termination. */
    if(generation<o->generation||(generation==o->generation&&!o->occupied)){
        ReleaseSRWLockExclusive(&pool_lock);return done(o,0);
    }
    if(generation!=o->generation){
        ReleaseSRWLockExclusive(&pool_lock);return done(o,ERROR_INVALID_HANDLE);
    }
    /* Recover actual creation outputs even if the original call never returned. */
    if(o->handles[H_PROCESS].phase==H_LIVE&&!o->handles[H_PROCESS].value)
        o->handles[H_PROCESS].value=o->created.hProcess;
    if(o->handles[H_THREAD].phase==H_LIVE&&!o->handles[H_THREAD].value)
        o->handles[H_THREAD].value=o->created.hThread;
    o->sealed=1;
    if(o->handles[H_JOB].phase==H_UNKNOWN)goto unknown;
    if(o->handles[H_JOB].value){
        if(terminate&&!o->terminate_attempted){
            o->terminate_attempted=1;
            if(!TerminateJobObject(o->handles[H_JOB].value,84))o->last_error=failed();
        }
        if(!QueryInformationJobObject(o->handles[H_JOB].value,JobObjectBasicAccountingInformation,&info,sizeof(info),0)){
            DWORD error=failed();ReleaseSRWLockExclusive(&pool_lock);return done(o,error);
        }
        if(info.ActiveProcesses){ReleaseSRWLockExclusive(&pool_lock);return done(o,ERROR_BUSY);}
    }
    if(o->handles[H_PROCESS].value&&o->handles[H_PROCESS].phase==H_LIVE){
        wait=WaitForSingleObject(o->handles[H_PROCESS].value,0);
        if(wait!=WAIT_OBJECT_0){ReleaseSRWLockExclusive(&pool_lock);return done(o,wait==WAIT_TIMEOUT?ERROR_BUSY:failed());}
    }
    for(i=1;i<HANDLES;i++)if(!held_close(o,i))goto unknown;
    /* Keep the observable Job until every owned process and other handle ends. */
    if(!held_close(o,H_JOB))goto unknown;
    o->occupied=0;ReleaseSRWLockExclusive(&pool_lock);return done(o,0);
unknown:
    ReleaseSRWLockExclusive(&pool_lock);return done(o,ERROR_INVALID_STATE);
}

API DWORD gitgo_process_abi(void){return 1;}
