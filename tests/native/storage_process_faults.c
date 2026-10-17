/* Disposable native failure fixture, never part of the application DLL. */
#include <windows.h>
#include <psapi.h>
#include <stdint.h>
#include <stdio.h>
#include <wchar.h>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
static int fail_create_after,fail_query,fail_close_after,actual_closes,fail_resume_after,resume_calls;
static int fail_open_code,fail_times,enum_fault;
static HANDLE (WINAPI *real_open)(DWORD,BOOL,DWORD)=OpenProcess;
static BOOL (WINAPI *real_times)(HANDLE,LPFILETIME,LPFILETIME,LPFILETIME,LPFILETIME)=GetProcessTimes;
static BOOL (WINAPI *real_enum)(DWORD*,DWORD,DWORD*)=K32EnumProcesses;
static HANDLE WINAPI injected_open(DWORD access,BOOL inherit,DWORD pid) {
    if(fail_open_code){SetLastError((DWORD)fail_open_code);return NULL;}
    return real_open(access,inherit,pid);
}
static BOOL WINAPI injected_times(HANDLE process,LPFILETIME created,LPFILETIME exit,LPFILETIME kernel,LPFILETIME user) {
    if(fail_times){fail_times=0;SetLastError(ERROR_ACCESS_DENIED);return FALSE;}
    return real_times(process,created,exit,kernel,user);
}
static BOOL WINAPI injected_enum(DWORD *pids,DWORD cap,DWORD *bytes) {
    if(enum_fault==1){SetLastError(ERROR_ACCESS_DENIED);return FALSE;}
    if(enum_fault==2){*bytes=cap;return TRUE;}
    return real_enum(pids,cap,bytes);
}
static DWORD (WINAPI *real_resume)(HANDLE)=ResumeThread;
static DWORD WINAPI injected_resume(HANDLE thread) {
    DWORD previous=real_resume(thread);resume_calls++;
    if(previous!=(DWORD)-1&&fail_resume_after){fail_resume_after=0;SetLastError(ERROR_GEN_FAILURE);return (DWORD)-1;}
    return previous;
}
static BOOL (WINAPI *real_close)(HANDLE)=CloseHandle;
static BOOL (WINAPI *real_query)(HANDLE,JOBOBJECTINFOCLASS,LPVOID,DWORD,LPDWORD)=QueryInformationJobObject;
static BOOL (WINAPI *real_create)(LPCWSTR,LPWSTR,LPSECURITY_ATTRIBUTES,LPSECURITY_ATTRIBUTES,BOOL,DWORD,LPVOID,LPCWSTR,LPSTARTUPINFOW,LPPROCESS_INFORMATION)=CreateProcessW;
static BOOL WINAPI injected_close(HANDLE handle) {
    BOOL ok=real_close(handle);actual_closes++;
    if(ok&&fail_close_after){fail_close_after=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    return ok;
}
static BOOL WINAPI injected_query(HANDLE job,JOBOBJECTINFOCLASS kind,LPVOID out,DWORD size,LPDWORD length) {
    if(fail_query){fail_query=0;SetLastError(ERROR_ACCESS_DENIED);return FALSE;}
    return real_query(job,kind,out,size,length);
}
static BOOL WINAPI injected_create(LPCWSTR exe,LPWSTR command,LPSECURITY_ATTRIBUTES ps,
    LPSECURITY_ATTRIBUTES ts,BOOL inherit,DWORD flags,LPVOID env,LPCWSTR cwd,
    LPSTARTUPINFOW startup,LPPROCESS_INFORMATION info) {
    BOOL ok=real_create(exe,command,ps,ts,inherit,flags,env,cwd,startup,info);
    if(ok&&fail_create_after){fail_create_after=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
    return ok;
}
#define CloseHandle injected_close
#define QueryInformationJobObject injected_query
#define CreateProcessW injected_create
#define ResumeThread injected_resume
#define OpenProcess injected_open
#define GetProcessTimes injected_times
#define K32EnumProcesses injected_enum
#include "../../backend/native/storage_process_windows.c"

static DWORD retire_until_quiescent(uint64_t cookie) {
    unsigned attempt;DWORD result;
    for(attempt=0;attempt<1000;attempt++) {
        result=gitgo_process_retire(cookie,1);
        if(result!=ERROR_BUSY)return result;
        Sleep(10);
    }
    return ERROR_TIMEOUT;
}
int main(void) {
    wchar_t exe[32768],command[32768],cwd[32768];LPWCH env;
    uint64_t cookie,other[COUNT],stats[9],identity[3],created;
    FILETIME creation,exit,kernel,user;DWORD n;int i,baseline,ended;
    if(wcsstr(GetCommandLineW(),L" --child-gsp")){Sleep(60000);return 0;}
    n=GetModuleFileNameW(0,exe,32768);assert(n&&n<32768);
    assert(swprintf(command,32768,L"\"%ls\" --child-gsp",exe)>0);
    n=GetCurrentDirectoryW(32768,cwd);assert(n&&n<32768);
    env=GetEnvironmentStringsW();assert(env);
    assert(real_times(GetCurrentProcess(),&creation,&exit,&kernel,&user));
    created=((uint64_t)creation.dwHighDateTime<<32)|creation.dwLowDateTime;
    /* Permission/query/snapshot failure is never evidence of native absence. */
    assert(!gitgo_process_reserve(&cookie));
    assert(!gitgo_process_ended(cookie,GetCurrentProcessId(),created,&ended)&&!ended);
    fail_times=1;ended=1;
    assert(gitgo_process_ended(cookie,GetCurrentProcessId(),created,&ended)==ERROR_ACCESS_DENIED&&!ended);
    assert(!gitgo_process_retire(cookie,0));
    assert(!gitgo_process_reserve(&cookie));fail_open_code=ERROR_ACCESS_DENIED;ended=1;
    assert(gitgo_process_ended(cookie,GetCurrentProcessId(),created,&ended)==ERROR_ACCESS_DENIED&&!ended);
    fail_open_code=ERROR_INVALID_PARAMETER;
    assert(gitgo_process_ended(cookie,GetCurrentProcessId(),created,&ended)==ERROR_INVALID_PARAMETER&&!ended);
    enum_fault=1;
    assert(gitgo_process_ended(cookie,GetCurrentProcessId(),created,&ended)==ERROR_ACCESS_DENIED&&!ended);
    enum_fault=2;
    assert(gitgo_process_ended(cookie,GetCurrentProcessId(),created,&ended)==ERROR_INSUFFICIENT_BUFFER&&!ended);
    enum_fault=0;fail_open_code=0;assert(!gitgo_process_retire(cookie,0));
    /* Native creation happened but its response is injected as failure. The
     * Job and process handles must already be owned and safely terminable. */
    assert(!gitgo_process_reserve(&cookie));fail_create_after=1;
    assert(gitgo_process_start(cookie,exe,command,cwd,env)==ERROR_GEN_FAILURE);
    assert(!gitgo_process_status(cookie,stats,9)&&stats[1]&&stats[3]==1);
    assert(gitgo_process_start(cookie,exe,command,cwd,env)==ERROR_INVALID_PARAMETER);
    assert(!retire_until_quiescent(cookie));
    /* Resume can take effect before returning failure. Consume the attempt
     * before the call, keep exact ownership and never issue another resume. */
    assert(!gitgo_process_reserve(&cookie));
    assert(swprintf(command,32768,L"\"%ls\" --child-gsp",exe)>0);
    assert(!gitgo_process_start(cookie,exe,command,cwd,env));
    assert(!gitgo_process_identity(cookie,identity,3)&&!identity[2]);
    assert(gitgo_process_resume(cookie,(DWORD)identity[0],identity[1]+1)==ERROR_INVALID_PARAMETER);
    assert(!resume_calls);
    fail_resume_after=1;
    assert(gitgo_process_resume(cookie,(DWORD)identity[0],identity[1])==ERROR_GEN_FAILURE);
    assert(!gitgo_process_identity(cookie,identity,3)&&identity[2]);
    assert(gitgo_process_resume(cookie,(DWORD)identity[0],identity[1])==ERROR_INVALID_STATE);
    assert(resume_calls==1);
    assert(!retire_until_quiescent(cookie));
    /* A failed read-only Job query retains authority; a later real query can
     * resolve it. Never convert unavailable accounting into an empty Job. */
    assert(!gitgo_process_reserve(&cookie));
    assert(swprintf(command,32768,L"\"%ls\" --child-gsp",exe)>0);
    assert(!gitgo_process_start(cookie,exe,command,cwd,env));
    assert(gitgo_process_retire(cookie,0)==ERROR_BUSY);
    fail_query=1;assert(gitgo_process_retire(cookie,0)==ERROR_ACCESS_DENIED);
    assert(!gitgo_process_status(cookie,stats,9)&&stats[3]==1&&stats[5]>0);
    assert(!retire_until_quiescent(cookie));
    /* Close the OS handle, then inject a failed result. The old numeric value
     * is unknown forever and must not be closed again or free this slot. */
    assert(!gitgo_process_reserve(&cookie));
    assert(swprintf(command,32768,L"\"%ls\" --child-gsp",exe)>0);
    assert(!gitgo_process_start(cookie,exe,command,cwd,env));
    baseline=actual_closes;fail_close_after=1;
    assert(retire_until_quiescent(cookie)==ERROR_INVALID_STATE);
    assert(actual_closes==baseline+1);
    assert(!gitgo_process_status(cookie,stats,9)&&stats[3]==0&&stats[6]==1);
    for(i=0;i<3;i++)assert(gitgo_process_retire(cookie,1)==ERROR_INVALID_STATE);
    assert(actual_closes==baseline+1);
    for(i=0;i<COUNT-1;i++)assert(!gitgo_process_reserve(&other[i]));
    {uint64_t denied=123;assert(gitgo_process_reserve(&denied)==ERROR_NOT_ENOUGH_QUOTA&&!denied);}
    for(i=0;i<COUNT-1;i++)assert(!gitgo_process_retire(other[i],1));
    assert(FreeEnvironmentStringsW(env));
    printf("native process faults passed: creation-owned, resume-unknown-not-retried, query-retained, absence-not-inferred, unknown-close-not-retried; quarantined=1\n");
    /* One intentionally quarantined parent owner is reclaimed by this process
     * exiting. Its Job is already confirmed empty; no children remain. */
    return 0;
}
