use super::Request;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::c_void;
use std::mem::{size_of, zeroed};
use std::ptr::{null, null_mut};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};
use windows_sys::Win32::{
    Foundation::*,
    Security::Authorization::*,
    Security::Isolation::*,
    Security::*,
    Storage::FileSystem::*,
    System::{JobObjects::*, Threading::*},
};

type Result<T> = std::result::Result<T, u32>;
const INVALID: u32 = 87;
const MAX_RESOURCES: usize = 4096;
const REPARSE_DENIED: u32 = 0x20000001;
const HARDLINK_DENIED: u32 = 0x20000002;

// These types have different allocators/destructors. None is interchangeable.
struct KernelHandle(usize);
impl KernelHandle {
    fn raw(&self) -> HANDLE {
        self.0 as HANDLE
    }
    fn close(&mut self) -> Result<()> {
        if self.0 != 0 {
            check(unsafe { CloseHandle(self.raw()) })?;
            self.0 = 0;
        }
        Ok(())
    }
}
impl Drop for KernelHandle {
    fn drop(&mut self) {
        let _ = self.close();
    } // Best effort, NEVER cleanup evidence.
}
struct Sid(*mut c_void);
impl Drop for Sid {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                FreeSid(self.0);
            }
        }
    }
}
struct Attributes {
    storage: Vec<usize>,
    initialized: bool,
}
impl Attributes {
    fn new() -> Result<Self> {
        let mut bytes = 0;
        unsafe {
            InitializeProcThreadAttributeList(null_mut(), 3, 0, &mut bytes);
        }
        if bytes == 0 || bytes > 1024 * 1024 {
            return Err(INVALID);
        }
        let mut value = Self {
            storage: vec![0; bytes.div_ceil(size_of::<usize>())],
            initialized: false,
        };
        check(unsafe { InitializeProcThreadAttributeList(value.raw(), 3, 0, &mut bytes) })?;
        value.initialized = true;
        Ok(value)
    }
    fn raw(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        self.storage.as_mut_ptr().cast()
    }
    fn set(&mut self, key: usize, value: *const c_void, bytes: usize) -> Result<()> {
        check(unsafe {
            UpdateProcThreadAttribute(self.raw(), 0, key, value, bytes, null_mut(), null())
        })
    }
}
impl Drop for Attributes {
    fn drop(&mut self) {
        if self.initialized {
            unsafe {
                DeleteProcThreadAttributeList(self.raw());
            }
        }
    }
}

struct Job {
    handle: KernelHandle,
    launched: bool,
    retired: bool,
}
struct Resources {
    next: u64,
    jobs: BTreeMap<u64, Job>,
    processes: BTreeMap<u64, KernelHandle>,
    files: BTreeMap<u64, KernelHandle>,
    retired_files: BTreeMap<u64, u64>,
}
static RESOURCES: OnceLock<Mutex<Resources>> = OnceLock::new();
fn resources() -> &'static Mutex<Resources> {
    RESOURCES.get_or_init(|| {
        Mutex::new(Resources {
            next: 1,
            jobs: BTreeMap::new(),
            processes: BTreeMap::new(),
            files: BTreeMap::new(),
            retired_files: BTreeMap::new(),
        })
    })
}
impl Resources {
    fn file_retired(&self, token: u64) -> bool {
        self.retired_files
            .range(..=token)
            .next_back()
            .is_some_and(|(_, end)| token <= *end)
    }
    fn remember_file_retirement(&mut self, token: u64) {
        let (mut start, mut end) = (token, token);
        if let Some((&left, &right)) = self.retired_files.range(..token).next_back()
            && right.checked_add(1) == Some(token)
        {
            start = left;
            self.retired_files.remove(&left);
        }
        if let Some((&right, &last)) = self.retired_files.range(token..).next()
            && token.checked_add(1) == Some(right)
        {
            end = last;
            self.retired_files.remove(&right);
        }
        self.retired_files.insert(start, end);
    }
    fn token(&mut self) -> Result<u64> {
        // Retired IDs are never reused. Bound retained state rather than dropping
        // tombstones and accidentally accepting a stale cleanup/launch request.
        if self.jobs.len() + self.processes.len() >= MAX_RESOURCES || self.files.len() >= 100_256 {
            return Err(8);
        }
        let token = self.next;
        self.next = self.next.checked_add(1).ok_or(8u32)?;
        Ok(token)
    }
}
fn check(ok: i32) -> Result<()> {
    if ok == 0 {
        Err(unsafe { GetLastError() })
    } else {
        Ok(())
    }
}
fn wide(value: &str) -> Result<Vec<u16>> {
    if value.contains('\0') || value.encode_utf16().count() >= 32767 {
        return Err(INVALID);
    }
    Ok(value.encode_utf16().chain([0]).collect())
}
fn job_name(value: &str) -> Result<Vec<u16>> {
    if value.len() != 32
        || !value
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Err(INVALID);
    }
    wide(&format!("Local\\Gitgo.Publish.{value}"))
}
fn accounting(job: HANDLE) -> Result<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION> {
    let mut info = unsafe { zeroed() };
    check(unsafe {
        QueryInformationJobObject(
            job,
            JobObjectBasicAccountingInformation,
            (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
            size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            null_mut(),
        )
    })?;
    Ok(info)
}
fn create_job(name: &[u16], processes: u32, memory: u64, cpu: u32) -> Result<KernelHandle> {
    if !(1..=1024).contains(&processes)
        || memory < 1024 * 1024
        || memory > usize::MAX as u64
        || !(1..=86400).contains(&cpu)
    {
        return Err(INVALID);
    }
    unsafe {
        SetLastError(0);
    }
    let raw = unsafe {
        CreateJobObjectW(
            null(),
            if name.is_empty() {
                null()
            } else {
                name.as_ptr()
            },
        )
    };
    let error = unsafe { GetLastError() };
    if raw.is_null() {
        return Err(error);
    }
    let handle = KernelHandle(raw as usize);
    if error == ERROR_ALREADY_EXISTS {
        return Err(error);
    }
    let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { zeroed() };
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_JOB_TIME;
    limits.BasicLimitInformation.ActiveProcessLimit = processes;
    limits.BasicLimitInformation.PerJobUserTimeLimit = i64::from(cpu) * 10_000_000;
    limits.JobMemoryLimit = memory as usize;
    check(unsafe {
        SetInformationJobObject(
            raw,
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of_val(&limits) as u32,
        )
    })?;
    let processors = unsafe { GetActiveProcessorCount(0xffff) };
    if processors == 0 {
        return Err(INVALID);
    }
    let rate = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION {
        ControlFlags: JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
        Anonymous: JOBOBJECT_CPU_RATE_CONTROL_INFORMATION_0 {
            CpuRate: (10000 / processors).max(1),
        },
    };
    check(unsafe {
        SetInformationJobObject(
            raw,
            JobObjectCpuRateControlInformation,
            (&rate as *const JOBOBJECT_CPU_RATE_CONTROL_INFORMATION).cast(),
            size_of_val(&rate) as u32,
        )
    })?;
    Ok(handle)
}

type DeriveSid = unsafe extern "system" fn(*const u16, *mut *mut c_void) -> i32;
fn derive_profile_sid(profile: &[u16], derive: DeriveSid) -> Result<Sid> {
    let mut sid = Sid(null_mut());
    let status = unsafe { derive(profile.as_ptr(), &mut sid.0) };
    if status < 0 {
        return Err(status as u32);
    }
    if sid.0.is_null() {
        return Err(INVALID);
    }
    Ok(sid)
}

#[allow(clippy::too_many_arguments)]
fn launch(
    job: HANDLE,
    profile: &str,
    executable: Option<String>,
    command: &str,
    cwd: &str,
    environment: Vec<(String, String)>,
    handles: &[usize],
    flags: u32,
) -> Result<(KernelHandle, u32)> {
    if profile.is_empty()
        || command.is_empty()
        || cwd.is_empty()
        || handles.len() < 3
        // stdio + <=16 installation leases + <=16 metadata-only image pins.
        || handles.len() > 35
        || handles.iter().any(|h| *h == 0 || *h == usize::MAX)
        || handles.iter().collect::<BTreeSet<_>>().len() != handles.len()
        || flags & !(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW) != 0
    {
        return Err(INVALID);
    }
    let profile = wide(profile)?;
    let mut command = wide(command)?;
    let cwd = wide(cwd)?;
    let executable = executable.map(|s| wide(&s)).transpose()?;
    let mut keys = BTreeSet::new();
    let mut env = String::new();
    for (key, value) in environment {
        if key.is_empty()
            || key.contains(['=', '\0'])
            || value.contains('\0')
            || !keys.insert(key.to_uppercase())
        {
            return Err(INVALID);
        }
        env.push_str(&key);
        env.push('=');
        env.push_str(&value);
        env.push('\0');
    }
    let mut environment: Vec<u16> = env.encode_utf16().collect();
    if environment.len() > 1024 * 1024 {
        return Err(INVALID);
    }
    environment.push(0);
    if environment.len() == 1 {
        environment.push(0);
    }
    for handle in handles {
        let mut flags = 0;
        check(unsafe { GetHandleInformation(*handle as HANDLE, &mut flags) })?;
        if flags & HANDLE_FLAG_INHERIT == 0 {
            return Err(INVALID);
        }
    }
    let sid = derive_profile_sid(&profile, DeriveAppContainerSidFromAppContainerName)?;
    let caps = SECURITY_CAPABILITIES {
        AppContainerSid: sid.0,
        Capabilities: null_mut(),
        CapabilityCount: 0,
        Reserved: 0,
    };
    let mut attrs = Attributes::new()?;
    let inherited: Vec<HANDLE> = handles.iter().map(|h| *h as HANDLE).collect();
    let jobs = [job];
    attrs.set(
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES as usize,
        (&caps as *const SECURITY_CAPABILITIES).cast(),
        size_of_val(&caps),
    )?;
    attrs.set(
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
        inherited.as_ptr().cast(),
        size_of_val(inherited.as_slice()),
    )?;
    attrs.set(
        PROC_THREAD_ATTRIBUTE_JOB_LIST as usize,
        jobs.as_ptr().cast(),
        size_of_val(&jobs),
    )?;
    let mut startup: STARTUPINFOEXW = unsafe { zeroed() };
    startup.StartupInfo.cb = size_of_val(&startup) as u32;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = inherited[0];
    startup.StartupInfo.hStdOutput = inherited[1];
    startup.StartupInfo.hStdError = inherited[2];
    startup.lpAttributeList = attrs.raw();
    let mut info: PROCESS_INFORMATION = unsafe { zeroed() };
    check(unsafe {
        CreateProcessW(
            executable.as_ref().map_or(null(), |s| s.as_ptr()),
            command.as_mut_ptr(),
            null(),
            null(),
            1,
            flags | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
            environment.as_ptr().cast(),
            cwd.as_ptr(),
            &startup.StartupInfo,
            &mut info,
        )
    })?;
    // Thread and temporary attributes/SID never cross FFI. The process handle
    // remains owned here; Python receives a borrowed raw value plus a token.
    let _thread = KernelHandle(info.hThread as usize);
    Ok((KernelHandle(info.hProcess as usize), info.dwProcessId))
}

pub fn dispatch(request: Request) -> Value {
    match perform(request) {
        Ok(value) => value,
        Err(error) => json!({"ok": false, "error": match error {
            REPARSE_DENIED => "Refusing a reparse point in the configuration tree",
            HARDLINK_DENIED => "Refusing a multiply-linked configuration file",
            _ => "windows_native_failure"
        }, "winerror": error}),
    }
}
fn perform(request: Request) -> Result<Value> {
    let mut resources = resources().lock().map_err(|_| ERROR_INVALID_STATE)?;
    match request {
        Request::JobCreate {
            recovery_id,
            processes,
            memory,
            cpu_seconds,
        } => {
            let name = if recovery_id.is_empty() {
                Vec::new()
            } else {
                job_name(&recovery_id)?
            };
            let token = resources.token()?;
            let handle = create_job(&name, processes, memory, cpu_seconds)?;
            resources.jobs.insert(
                token,
                Job {
                    handle,
                    launched: false,
                    retired: false,
                },
            );
            Ok(json!({"ok": true, "token": token}))
        }
        Request::JobRecover { recovery_id } => {
            let name = job_name(&recovery_id)?;
            let token = resources.token()?;
            let handle = unsafe { OpenJobObjectW(0x0004 | 0x0008, 0, name.as_ptr()) }; // QUERY | TERMINATE
            if handle.is_null() {
                let error = unsafe { GetLastError() };
                if error == ERROR_FILE_NOT_FOUND {
                    return Ok(json!({"ok": true, "absent": true}));
                }
                return Err(error);
            }
            resources.jobs.insert(
                token,
                Job {
                    handle: KernelHandle(handle as usize),
                    launched: true,
                    retired: false,
                },
            );
            Ok(json!({"ok": true, "token": token, "absent": false}))
        }
        Request::JobQuery { token } => {
            let job = resources.jobs.get(&token).ok_or(ERROR_INVALID_HANDLE)?;
            if job.retired {
                return Err(ERROR_INVALID_HANDLE);
            }
            let info = accounting(job.handle.raw())?;
            let mut rate: JOBOBJECT_CPU_RATE_CONTROL_INFORMATION = unsafe { zeroed() };
            check(unsafe {
                QueryInformationJobObject(
                    job.handle.raw(),
                    JobObjectCpuRateControlInformation,
                    (&mut rate as *mut JOBOBJECT_CPU_RATE_CONTROL_INFORMATION).cast(),
                    size_of_val(&rate) as u32,
                    null_mut(),
                )
            })?;
            Ok(json!({"ok": true, "active": info.ActiveProcesses,
                "cpu_100ns": info.TotalUserTime + info.TotalKernelTime,
                "cpu_flags": rate.ControlFlags, "cpu_rate": unsafe { rate.Anonymous.CpuRate }}))
        }
        Request::JobRetire { token } => {
            let job = resources.jobs.get_mut(&token).ok_or(ERROR_INVALID_HANDLE)?;
            if job.retired {
                return Ok(json!({"ok": true, "complete": true}));
            }
            check(unsafe { TerminateJobObject(job.handle.raw(), 1) })?;
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                if accounting(job.handle.raw())?.ActiveProcesses == 0 {
                    job.handle.close()?;
                    job.retired = true;
                    return Ok(json!({"ok": true, "complete": true}));
                }
                if Instant::now() >= deadline {
                    return Ok(json!({"ok": true, "complete": false}));
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        }
        Request::ProcessRelease { token } => {
            // Keep a zero-valued tombstone: repeated release is idempotent and
            // never closes a subsequently reused OS handle.
            resources
                .processes
                .get_mut(&token)
                .ok_or(ERROR_INVALID_HANDLE)?
                .close()?;
            Ok(json!({"ok": true}))
        }
        Request::AclOpen { path, access } => {
            if ![
                0x01020089, 0x010E0089, 0x010200A0, 0x20089, 0xE0089, 0x200A0,
            ]
            .contains(&access)
            {
                return Err(INVALID);
            }
            let path = wide(&path)?;
            let token = resources.token()?;
            let handle = unsafe {
                CreateFileW(
                    path.as_ptr(),
                    access,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    null(),
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                    null_mut(),
                )
            };
            if handle == INVALID_HANDLE_VALUE {
                return Err(unsafe { GetLastError() });
            }
            let owned = KernelHandle(handle as usize);
            let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
            check(unsafe { GetFileInformationByHandle(handle, &mut info) })?;
            if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(REPARSE_DENIED);
            }
            if info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0 && info.nNumberOfLinks != 1 {
                return Err(HARDLINK_DENIED);
            }
            resources.files.insert(token, owned);
            Ok(json!({"ok": true, "token": token, "handle": handle as usize}))
        }
        Request::AclRelease { token } => {
            // Retain the closed token so a lost FFI response can be retried
            // without closing any subsequently reused OS handle.
            if resources.file_retired(token) {
                return Ok(json!({"ok": true}));
            }
            resources
                .files
                .get_mut(&token)
                .ok_or(ERROR_INVALID_HANDLE)?
                .close()?;
            resources.files.remove(&token);
            resources.remember_file_retirement(token);
            Ok(json!({"ok": true}))
        }
        Request::Launch {
            token,
            profile,
            executable,
            command,
            cwd,
            environment,
            handles,
            flags,
        } => {
            let process_token = resources.token()?; // Capacity BEFORE any effect.
            let job = resources.jobs.get_mut(&token).ok_or(ERROR_INVALID_HANDLE)?;
            if job.retired || job.launched {
                return Err(ERROR_INVALID_STATE);
            }
            // Even a failed/ambiguous FFI launch cannot be replayed in this scope.
            job.launched = true;
            let (process, pid) = launch(
                job.handle.raw(),
                &profile,
                executable,
                &command,
                &cwd,
                environment,
                &handles,
                flags,
            )?;
            let raw = process.0;
            resources.processes.insert(process_token, process);
            Ok(
                json!({"ok": true, "process_token": process_token, "process_handle": raw, "pid": pid}),
            )
        }
        Request::Capabilities {}
        | Request::LinuxCgroupCreate { .. }
        | Request::LinuxCgroupRecover { .. }
        | Request::LinuxCgroupQuery { .. }
        | Request::LinuxCgroupKill { .. }
        | Request::LinuxCgroupRetire { .. }
        | Request::ReadRoot { .. }
        | Request::ReadOpen { .. }
        | Request::ReadClose { .. }
        | Request::ReadHash { .. }
        | Request::ReadSize { .. } => Err(INVALID),
    }
}

#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQuerySecurityObject(
        handle: HANDLE,
        information: u32,
        descriptor: *mut c_void,
        length: u32,
        needed: *mut u32,
    ) -> i32;
    fn RtlNtStatusToDosError(status: i32) -> u32;
    fn NtSetSecurityObject(handle: HANDLE, information: u32, descriptor: *const c_void) -> i32;
}

struct LocalAllocation(*mut c_void);
impl Drop for LocalAllocation {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.0);
        }
    }
}
struct BackupContext {
    file: HANDLE,
    context: *mut c_void,
}
impl BackupContext {
    fn finish(&mut self) -> Result<()> {
        let mut written = 0;
        check(unsafe { BackupWrite(self.file, null(), 0, &mut written, 1, 1, &mut self.context) })?;
        self.context = null_mut();
        Ok(())
    }
}
impl Drop for BackupContext {
    fn drop(&mut self) {
        // Explicit finish determines the operation result. Drop is only the
        // panic/early-return guard and cannot mark Python's recovery complete.
        if !self.context.is_null() {
            let _ = self.finish();
        }
    }
}

fn descriptor(raw: &[u8]) -> Result<Vec<u64>> {
    if raw.len() < 20
        || raw.len() > 1024 * 1024
        || raw[0] != 1
        || u16::from_le_bytes([raw[2], raw[3]]) & 0x8000 == 0
    {
        return Err(INVALID);
    }
    // Check every pointed-to span before passing any pointer to Windows. This
    // is structural memory validation; Python still owns descriptor semantics.
    for (index, sid) in [(4, true), (8, true), (12, false), (16, false)] {
        let offset =
            u32::from_le_bytes(raw[index..index + 4].try_into().map_err(|_| INVALID)?) as usize;
        if offset == 0 {
            continue;
        }
        if offset < 20
            || !offset.is_multiple_of(4)
            || offset.checked_add(8).is_none_or(|n| n > raw.len())
        {
            return Err(INVALID);
        }
        let size = if sid {
            if raw[offset] != 1 || raw[offset + 1] > 15 {
                return Err(INVALID);
            }
            8 + 4 * usize::from(raw[offset + 1])
        } else {
            usize::from(u16::from_le_bytes([raw[offset + 2], raw[offset + 3]]))
        };
        if size < 8 || offset.checked_add(size).is_none_or(|n| n > raw.len()) {
            return Err(INVALID);
        }
    }
    let mut aligned = vec![0u64; raw.len().div_ceil(8)];
    unsafe {
        std::ptr::copy_nonoverlapping(raw.as_ptr(), aligned.as_mut_ptr().cast(), raw.len());
    }
    if unsafe { IsValidSecurityDescriptor(aligned.as_mut_ptr().cast()) } == 0 {
        return Err(INVALID);
    }
    Ok(aligned)
}

pub fn acl(operation: u32, token: u64, input: &[u8], capacity: usize) -> Result<Vec<u8>> {
    if operation == 3 {
        let mut aligned = descriptor(input)?;
        let mut text = null_mut();
        let mut count = 0;
        check(unsafe {
            ConvertSecurityDescriptorToStringSecurityDescriptorW(
                aligned.as_mut_ptr().cast(),
                1,
                4 | 16,
                &mut text,
                &mut count,
            )
        })?;
        let _allocation = LocalAllocation(text.cast());
        if text.is_null() || count == 0 || count > 4 * 1024 * 1024 {
            return Err(INVALID);
        }
        let text = unsafe { std::slice::from_raw_parts(text, count as usize) };
        let text = text.strip_suffix(&[0]).unwrap_or(text);
        let bytes = String::from_utf16(text).map_err(|_| INVALID)?.into_bytes();
        if bytes.len() > capacity {
            return Err(ERROR_INSUFFICIENT_BUFFER);
        }
        return Ok(bytes);
    }
    let resources = resources().lock().map_err(|_| ERROR_INVALID_STATE)?;
    let file = resources.files.get(&token).ok_or(ERROR_INVALID_HANDLE)?;
    if file.0 == 0 {
        return Err(ERROR_INVALID_HANDLE);
    }
    if operation == 1 || operation == 4 {
        let information = if operation == 4 {
            1 | 2 | 4 | 16
        } else {
            0x1ff
        };
        if capacity < 1024 * 1024 || !input.is_empty() {
            return Err(INVALID);
        }
        for _ in 0..3 {
            let mut needed = 0;
            let status = unsafe {
                NtQuerySecurityObject(file.raw(), information, null_mut(), 0, &mut needed)
            };
            if status != -1073741789 {
                return Err(if status == 0 {
                    INVALID
                } else {
                    unsafe { RtlNtStatusToDosError(status) }
                });
            }
            if !(20..=1024 * 1024).contains(&needed) {
                return Err(INVALID);
            }
            let mut buffer = vec![0u64; (needed as usize).div_ceil(8)];
            let bytes = needed;
            let status = unsafe {
                NtQuerySecurityObject(
                    file.raw(),
                    information,
                    buffer.as_mut_ptr().cast(),
                    bytes,
                    &mut needed,
                )
            };
            if status == -1073741789 {
                continue;
            }
            if status != 0 {
                return Err(unsafe { RtlNtStatusToDosError(status) });
            }
            if needed > bytes || needed < 20 {
                return Err(INVALID);
            }
            let output = unsafe {
                std::slice::from_raw_parts(buffer.as_ptr().cast::<u8>(), needed as usize)
            }
            .to_vec();
            descriptor(&output)?;
            return Ok(output);
        }
        return Err(ERROR_RETRY);
    }
    if operation == 2 {
        descriptor(input)?;
        // SECURITY_DATA / STREAM_CONTAINS_SECURITY only: no caller stream IDs,
        // links, file contents, names or alternate streams can reach BackupWrite.
        let mut data = Vec::with_capacity(input.len() + 20);
        data.extend_from_slice(&3u32.to_le_bytes());
        data.extend_from_slice(&2u32.to_le_bytes());
        data.extend_from_slice(&(input.len() as i64).to_le_bytes());
        data.extend_from_slice(&0u32.to_le_bytes());
        data.extend_from_slice(input);
        let mut context = BackupContext {
            file: file.raw(),
            context: null_mut(),
        };
        let mut written = 0;
        let result = check(unsafe {
            BackupWrite(
                file.raw(),
                data.as_ptr(),
                data.len() as u32,
                &mut written,
                0,
                1,
                &mut context.context,
            )
        });
        let cleanup = context.finish();
        result?;
        cleanup?;
        if written as usize != data.len() {
            return Err(ERROR_WRITE_FAULT);
        }
        return Ok(Vec::new());
    }
    if operation == 5 {
        let aligned = descriptor(input)?;
        let status = unsafe { NtSetSecurityObject(file.raw(), 4 | 16, aligned.as_ptr().cast()) };
        if status != 0 {
            return Err(unsafe { RtlNtStatusToDosError(status) });
        }
        return Ok(Vec::new());
    }
    Err(INVALID)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn failed_sid_derivation_is_propagated_without_a_fallback() {
        unsafe extern "system" fn deny(_profile: *const u16, sid: *mut *mut c_void) -> i32 {
            unsafe {
                *sid = null_mut();
            }
            0x80070005u32 as i32
        }
        let result = derive_profile_sid(&wide("Gitgo.Test.Prepared").unwrap(), deny);
        assert!(matches!(result, Err(0x80070005)));
    }
    #[test]
    fn retirement_ranges_preserve_holes_and_compact_repeated_trees() {
        let mut value = Resources {
            next: 1,
            jobs: BTreeMap::new(),
            processes: BTreeMap::new(),
            files: BTreeMap::new(),
            retired_files: BTreeMap::new(),
        };
        for token in [5, 3, 4, 1] {
            value.remember_file_retirement(token);
        }
        assert!(!value.file_retired(0));
        assert!(!value.file_retired(2));
        assert!(!value.file_retired(6));
        value.remember_file_retirement(2);
        for token in 6..100_000 {
            value.remember_file_retirement(token);
        }
        assert_eq!(value.retired_files.len(), 1);
        assert!(value.file_retired(99_999));
        assert!(!value.file_retired(100_000));
    }
}
