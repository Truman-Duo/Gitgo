//! Delegated cgroup-v2 object ownership. Never creates a delegation or grants authority.
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::ffi::{CString, OsStr};
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::{ffi::OsStrExt, fs::MetadataExt};
use std::path::{Component, Path};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

const MAX_GROUPS: usize = 1024;
const CGROUP2_MAGIC: libc::c_long = 0x63677270;
fn invalid(message: &'static str) -> io::Error {
    io::Error::other(message)
}
fn cstring(raw: &[u8]) -> io::Result<CString> {
    if raw.is_empty() || raw.len() > 32768 {
        return Err(invalid("invalid_cgroup_path"));
    }
    CString::new(raw).map_err(|_| invalid("invalid_cgroup_path"))
}
fn owned(fd: i32) -> io::Result<File> {
    if fd < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { File::from_raw_fd(fd) })
    }
}
fn open_at(parent: i32, name: &[u8], flags: i32) -> io::Result<File> {
    #[repr(C)]
    struct How {
        flags: u64,
        mode: u64,
        resolve: u64,
    }
    let name = cstring(name)?;
    let how = How {
        flags: (flags | libc::O_CLOEXEC | libc::O_NOFOLLOW) as u64,
        mode: 0,
        resolve: 0x02
            | 0x04
            | if parent == libc::AT_FDCWD {
                0
            } else {
                0x08 | 0x01
            },
    };
    owned(unsafe {
        libc::syscall(
            libc::SYS_openat2,
            parent,
            name.as_ptr(),
            &how,
            std::mem::size_of::<How>(),
        ) as i32
    })
}
fn filesystem(file: &File) -> io::Result<()> {
    let mut info = std::mem::MaybeUninit::<libc::statfs>::uninit();
    if unsafe { libc::fstatfs(file.as_raw_fd(), info.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { info.assume_init() }.f_type != CGROUP2_MAGIC {
        return Err(invalid("cgroup2_filesystem_required"));
    }
    Ok(())
}
fn root(raw: &[u8]) -> io::Result<File> {
    let path = Path::new(OsStr::from_bytes(raw));
    if !path.starts_with("/sys/fs/cgroup")
        || path == Path::new("/sys/fs/cgroup")
        || path
            .components()
            .any(|c| !matches!(c, Component::RootDir | Component::Normal(_)))
    {
        return Err(invalid("dedicated_cgroup_root_required"));
    }
    let file = open_at(libc::AT_FDCWD, raw, libc::O_RDONLY | libc::O_DIRECTORY)?;
    filesystem(&file)?;
    Ok(file)
}
fn control(directory: &File, name: &str, write: bool) -> io::Result<File> {
    let file = open_at(
        directory.as_raw_fd(),
        name.as_bytes(),
        if write {
            libc::O_WRONLY
        } else {
            libc::O_RDONLY
        },
    )?;
    filesystem(&file)?;
    if !file.metadata()?.is_file() {
        return Err(invalid("invalid_cgroup_control"));
    }
    Ok(file)
}
fn text(directory: &File, name: &str) -> io::Result<String> {
    let mut value = String::new();
    control(directory, name, false)?
        .take(65537)
        .read_to_string(&mut value)?;
    if value.len() > 65536 {
        return Err(invalid("cgroup_control_exceeds_bound"));
    }
    Ok(value)
}
fn write(directory: &File, name: &str, value: &str) -> io::Result<()> {
    // cgroup control writes must be one complete kernel operation, not a partial retry.
    let count = control(directory, name, true)?.write(value.as_bytes())?;
    if count != value.len() {
        return Err(invalid("partial_cgroup_control_write"));
    }
    Ok(())
}
fn counter(value: &str, key: &str) -> io::Result<u64> {
    let mut selected = None;
    for line in value.lines() {
        let mut fields = line.split_whitespace();
        if fields.next() == Some(key) {
            let parsed = fields
                .next()
                .ok_or_else(|| invalid("invalid_cgroup_counter"))?
                .parse::<u64>()
                .map_err(|_| invalid("invalid_cgroup_counter"))?;
            if fields.next().is_some() || selected.replace(parsed).is_some() {
                return Err(invalid("invalid_cgroup_counter"));
            }
        }
    }
    selected.ok_or_else(|| invalid("missing_cgroup_counter"))
}
fn group_name(id: &str) -> io::Result<CString> {
    if id.len() != 32
        || !id
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return Err(invalid("invalid_cgroup_recovery_identity"));
    }
    cstring(format!("gitgo-{id}").as_bytes())
}
struct Group {
    root: File,
    directory: File,
    name: CString,
    procs: Option<File>,
    monitor: Option<Monitor>,
}
impl Group {
    fn kill(&self) -> io::Result<()> {
        write(&self.directory, "cgroup.kill", "1")
    }
    fn usage(&self) -> io::Result<u64> {
        counter(&text(&self.directory, "cpu.stat")?, "usage_usec")
    }
    fn populated(&self) -> io::Result<bool> {
        match counter(&text(&self.directory, "cgroup.events")?, "populated")? {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(invalid("invalid_cgroup_population")),
        }
    }
    fn retire(&mut self) -> io::Result<bool> {
        self.kill()?;
        let deadline = Instant::now() + Duration::from_secs(3);
        while self.populated()? {
            if Instant::now() >= deadline {
                return Ok(false);
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        if let Some(monitor) = &mut self.monitor {
            monitor.stop()?;
        }
        // Never unlink a replacement selected through a reused name.
        let current = open_at(
            self.root.as_raw_fd(),
            self.name.as_bytes(),
            libc::O_RDONLY | libc::O_DIRECTORY,
        )?;
        let a = current.metadata()?;
        let b = self.directory.metadata()?;
        if a.dev() != b.dev() || a.ino() != b.ino() {
            return Err(invalid("cgroup_object_changed"));
        }
        if unsafe {
            libc::unlinkat(
                self.root.as_raw_fd(),
                self.name.as_ptr(),
                libc::AT_REMOVEDIR,
            )
        } != 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(true)
    }
}
struct Registry {
    next: u64,
    groups: BTreeMap<u64, Group>,
}
fn registry() -> &'static Mutex<Registry> {
    static STATE: OnceLock<Mutex<Registry>> = OnceLock::new();
    STATE.get_or_init(|| {
        Mutex::new(Registry {
            next: 1,
            groups: BTreeMap::new(),
        })
    })
}
fn failure(error: io::Error) -> Value {
    json!({"ok":false,"error":error.to_string(),"os_error":error.raw_os_error()})
}
/// Allocation has bounded ownership; the existing Host recovery gate remains
/// authoritative. A native token never grants permission.
pub fn create(raw: &[u8], id: &str, memory: u64, processes: u32) -> Value {
    let result = (|| -> io::Result<Value> {
        if memory == 0 || memory > i64::MAX as u64 || processes == 0 || processes > i32::MAX as u32
        {
            return Err(invalid("invalid_cgroup_budget"));
        }
        let mut state = registry()
            .lock()
            .map_err(|_| invalid("cgroup_registry_poisoned"))?;
        if state.groups.len() >= MAX_GROUPS || state.next == u64::MAX {
            return Err(invalid("cgroup_budget_exhausted"));
        }
        let root = root(raw)?;
        let controllers = text(&root, "cgroup.subtree_control")?;
        if !["cpu", "memory", "pids"]
            .iter()
            .all(|key| controllers.split_whitespace().any(|item| item == *key))
        {
            return Err(invalid("cgroup_controllers_required"));
        }
        let name = group_name(id)?;
        if unsafe { libc::mkdirat(root.as_raw_fd(), name.as_ptr(), 0o700) } != 0 {
            return Err(io::Error::last_os_error());
        }
        // If this open fails, retain an explicit unconfirmed allocation outcome;
        // never report cleanup or remove a path whose identity was not observed.
        let directory = match open_at(
            root.as_raw_fd(),
            name.as_bytes(),
            libc::O_RDONLY | libc::O_DIRECTORY,
        ) {
            Ok(file) => file,
            Err(error) => {
                return Ok(
                    json!({"ok":false,"error":error.to_string(),"cleanup_complete":false,"allocation_unknown":true}),
                );
            }
        };
        let group = Group {
            root,
            directory,
            name,
            procs: None,
            monitor: None,
        };
        let token = state.next;
        state.next += 1;
        state.groups.insert(token, group);
        let group = &state.groups[&token];
        let configured = (|| -> io::Result<()> {
            control(&group.directory, "cgroup.kill", true)?;
            for (name, value) in [
                ("memory.max", memory.to_string()),
                ("memory.swap.max", "0".into()),
                ("memory.oom.group", "1".into()),
                ("pids.max", processes.to_string()),
                ("cpu.max", "100000 100000".into()),
            ] {
                write(&group.directory, name, &value)?;
            }
            group.usage()?;
            group.populated()?;
            Ok(())
        })();
        if let Err(error) = configured {
            return Ok(
                json!({"ok":false,"error":error.to_string(),"token":token,"cleanup_complete":false}),
            );
        }
        Ok(json!({"ok":true,"token":token}))
    })();
    result.unwrap_or_else(failure)
}
pub fn query(token: u64) -> Value {
    let result = (|| -> io::Result<Value> {
        let state = registry()
            .lock()
            .map_err(|_| invalid("cgroup_registry_poisoned"))?;
        let group = state
            .groups
            .get(&token)
            .ok_or_else(|| invalid("unknown_cgroup_token"))?;
        Ok(
            json!({"ok":true,"usage_usec":group.usage()?,"populated":group.populated()?,
            "failure":group.monitor.as_ref().map_or(0, |m|m.failure.load(Ordering::Acquire))}),
        )
    })();
    result.unwrap_or_else(failure)
}
pub fn kill(token: u64) -> Value {
    let result = (|| -> io::Result<Value> {
        let state = registry()
            .lock()
            .map_err(|_| invalid("cgroup_registry_poisoned"))?;
        state
            .groups
            .get(&token)
            .ok_or_else(|| invalid("unknown_cgroup_token"))?
            .kill()?;
        Ok(json!({"ok":true}))
    })();
    result.unwrap_or_else(failure)
}
pub fn retire(token: u64) -> Value {
    let result = (|| -> io::Result<Value> {
        let mut state = registry()
            .lock()
            .map_err(|_| invalid("cgroup_registry_poisoned"))?;
        let Some(group) = state.groups.get_mut(&token) else {
            return if token > 0 && token < state.next {
                Ok(json!({"ok":true,"complete":true}))
            } else {
                Err(invalid("unknown_cgroup_token"))
            };
        };
        let complete = group.retire()?;
        if complete {
            state.groups.remove(&token);
        }
        Ok(json!({"ok":true,"complete":complete}))
    })();
    result.unwrap_or_else(failure)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_accounting_is_not_zero_usage_or_empty_tree() {
        for value in [
            "",
            "other 0",
            "populated nope",
            "populated 0 1",
            "populated 0\npopulated 1",
        ] {
            assert!(counter(value, "populated").is_err());
        }
        assert_eq!(counter("populated 1\nfrozen 0\n", "populated").unwrap(), 1);
    }
    #[test]
    fn recovery_name_cannot_select_another_resource() {
        for id in [
            "",
            "../host",
            "0000000000000000000000000000000/",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ] {
            assert!(group_name(id).is_err());
        }
        assert!(group_name("0123456789abcdef0123456789abcdef").is_ok());
    }
    #[test]
    fn non_cgroup_and_undelegated_roots_are_refused() {
        for raw in [
            b"/tmp".as_slice(),
            b"/sys/fs/cgroup",
            b"/sys/fs/cgroup/../cgroup",
            b"relative",
        ] {
            assert!(root(raw).is_err());
        }
        assert!(filesystem(&File::open("/tmp").unwrap()).is_err());
    }
}

/// Recovery is called only after the existing Host journal/lease checks.
/// An absent exact name is retired; a present object is pinned before any kill.
pub fn recover(raw: &[u8], id: &str) -> Value {
    let result = (|| -> io::Result<Value> {
        let mut state = registry()
            .lock()
            .map_err(|_| invalid("cgroup_registry_poisoned"))?;
        let root = root(raw)?;
        let name = group_name(id)?;
        let root_id = root.metadata()?;
        for (token, group) in &state.groups {
            let existing = group.root.metadata()?;
            if group.name == name
                && existing.dev() == root_id.dev()
                && existing.ino() == root_id.ino()
            {
                return Ok(json!({"ok":true,"absent":false,"token":token}));
            }
        }
        if state.groups.len() >= MAX_GROUPS || state.next == u64::MAX {
            return Err(invalid("cgroup_budget_exhausted"));
        }
        let directory = match open_at(
            root.as_raw_fd(),
            name.as_bytes(),
            libc::O_RDONLY | libc::O_DIRECTORY,
        ) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return Ok(json!({"ok":true,"absent":true}));
            }
            Err(error) => return Err(error),
        };
        filesystem(&directory)?;
        let group = Group {
            root,
            directory,
            name,
            procs: None,
            monitor: None,
        };
        group.populated()?;
        control(&group.directory, "cgroup.kill", true)?;
        let token = state.next;
        state.next += 1;
        state.groups.insert(token, group);
        Ok(json!({"ok":true,"absent":false,"token":token}))
    })();
    result.unwrap_or_else(failure)
}

/// Borrow only the exact membership object for bubblewrap's explicit FD mount.
/// Rust retains ownership until observed tree retirement; no directory is exported.
pub fn procs(token: u64) -> Value {
    let result = (|| -> io::Result<Value> {
        let mut state = registry()
            .lock()
            .map_err(|_| invalid("cgroup_registry_poisoned"))?;
        let group = state
            .groups
            .get_mut(&token)
            .ok_or_else(|| invalid("unknown_cgroup_token"))?;
        if group.procs.is_none() {
            let file = open_at(group.directory.as_raw_fd(), b"cgroup.procs", libc::O_PATH)?;
            filesystem(&file)?;
            if !file.metadata()?.is_file() {
                return Err(invalid("invalid_cgroup_control"));
            }
            group.procs = Some(file);
        }
        Ok(json!({"ok":true,"fd":group.procs.as_ref().unwrap().as_raw_fd()}))
    })();
    result.unwrap_or_else(failure)
}

#[cfg(test)]
mod real_tests {
    use super::*;
    #[test]
    fn delegated_kernel_group_limits_and_retirement() {
        // Dedicated delegated Linux CI sets this variable; ordinary unit tests
        // do not claim real resource acceptance in its absence.
        let Some(raw) = std::env::var_os("GITGO_SANDBOX_CGROUP_ROOT") else {
            return;
        };
        let id = "45a00000000000000000000000000001";
        let result = create(raw.as_bytes(), id, 64 * 1024 * 1024, 8);
        assert_eq!(result["ok"], true, "{result}");
        let token = result["token"].as_u64().unwrap();
        let path = Path::new(&raw).join(format!("gitgo-{id}"));
        assert_eq!(
            std::fs::read_to_string(path.join("memory.max"))
                .unwrap()
                .trim(),
            "67108864"
        );
        assert_eq!(
            std::fs::read_to_string(path.join("memory.swap.max"))
                .unwrap()
                .trim(),
            "0"
        );
        assert_eq!(
            std::fs::read_to_string(path.join("memory.oom.group"))
                .unwrap()
                .trim(),
            "1"
        );
        assert_eq!(
            std::fs::read_to_string(path.join("pids.max"))
                .unwrap()
                .trim(),
            "8"
        );
        assert_eq!(
            std::fs::read_to_string(path.join("cpu.max"))
                .unwrap()
                .trim(),
            "100000 100000"
        );
        assert_eq!(query(token)["populated"], false);
        assert_eq!(recover(raw.as_bytes(), id)["token"], token);
        assert_eq!(retire(token)["complete"], true);
        assert!(!path.exists());
        assert_eq!(retire(token)["complete"], true);
        assert_eq!(recover(raw.as_bytes(), id)["absent"], true);
        assert_eq!(query(token)["ok"], false);
        assert_eq!(retire(u64::MAX)["ok"], false);
    }
}

struct Monitor {
    stop: Arc<AtomicBool>,
    failure: Arc<AtomicU8>,
    thread: Option<std::thread::JoinHandle<()>>,
    join_failed: bool,
}
impl Monitor {
    fn stop(&mut self) -> io::Result<()> {
        if self.join_failed {
            return Err(invalid("cgroup_monitor_cleanup_unknown"));
        }
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take()
            && thread.join().is_err()
        {
            self.join_failed = true;
            return Err(invalid("cgroup_monitor_cleanup_unknown"));
        }
        Ok(())
    }
}
pub fn watch(token: u64, pid: u32, cpu_seconds: u32) -> Value {
    let result = (|| -> io::Result<Value> {
        if pid == 0 || pid > i32::MAX as u32 || cpu_seconds == 0 {
            return Err(invalid("invalid_cgroup_monitor_budget"));
        }
        let mut state = registry()
            .lock()
            .map_err(|_| invalid("cgroup_registry_poisoned"))?;
        let group = state
            .groups
            .get_mut(&token)
            .ok_or_else(|| invalid("unknown_cgroup_token"))?;
        if group.monitor.is_some() {
            return Err(invalid("cgroup_monitor_already_started"));
        }
        // The Host supplies its just-created, unreaped Popen child before poll.
        // pidfd is retained for observation, never used to signal an unrelated PID.
        let descriptor = unsafe { libc::syscall(libc::SYS_pidfd_open, pid as i32, 0) as i32 };
        if descriptor < 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ESRCH) {
                group.kill()?;
                return Ok(json!({"ok":true,"leader_exited":true}));
            }
            return Err(error);
        }
        let pidfd = owned(descriptor)?;
        let directory = group.directory.try_clone()?;
        let stop = Arc::new(AtomicBool::new(false));
        let failure = Arc::new(AtomicU8::new(0));
        let worker_stop = stop.clone();
        let worker_failure = failure.clone();
        let thread = std::thread::Builder::new()
            .name("gitgo-cgroup".into())
            .spawn(move || {
                let monitored = std::panic::catch_unwind(|| -> io::Result<()> {
                    while !worker_stop.load(Ordering::Acquire) {
                        let mut event = libc::pollfd {
                            fd: pidfd.as_raw_fd(),
                            events: libc::POLLIN,
                            revents: 0,
                        };
                        let status = unsafe { libc::poll(&mut event, 1, 50) };
                        if status < 0 {
                            let error = io::Error::last_os_error();
                            if error.kind() == io::ErrorKind::Interrupted {
                                continue;
                            }
                            return Err(error);
                        }
                        if event.revents != 0 {
                            if event.revents & (libc::POLLERR | libc::POLLNVAL) != 0 {
                                return Err(invalid("cgroup_pidfd_observation_failed"));
                            }
                            write(&directory, "cgroup.kill", "1")?;
                            return Ok(());
                        }
                        let usage = counter(&text(&directory, "cpu.stat")?, "usage_usec")?;
                        if usage >= u64::from(cpu_seconds) * 1_000_000 {
                            worker_failure.store(1, Ordering::Release);
                            write(&directory, "cgroup.kill", "1")?;
                            return Ok(());
                        }
                    }
                    Ok(())
                });
                if !matches!(monitored, Ok(Ok(()))) {
                    worker_failure.store(2, Ordering::Release);
                    // Unknown accounting fails closed. The normal retirement path
                    // still independently checks whole-tree exit and retries kill.
                    let _ = write(&directory, "cgroup.kill", "1");
                }
            })?;
        group.monitor = Some(Monitor {
            stop,
            failure,
            thread: Some(thread),
            join_failed: false,
        });
        Ok(json!({"ok":true,"leader_exited":false}))
    })();
    result.unwrap_or_else(failure)
}

#[cfg(test)]
mod monitor_tests {
    use super::*;
    #[test]
    fn invalid_observer_inputs_refuse_before_any_effect() {
        assert_eq!(watch(1, 0, 1)["ok"], false);
        assert_eq!(watch(1, u32::MAX, 1)["ok"], false);
        assert_eq!(watch(1, 1, 0)["ok"], false);
    }
    #[test]
    fn panicked_monitor_retirement_does_not_turn_successful_on_retry() {
        let mut monitor = Monitor {
            stop: Arc::new(AtomicBool::new(false)),
            failure: Arc::new(AtomicU8::new(0)),
            thread: Some(std::thread::spawn(|| panic!("injected monitor failure"))),
            join_failed: false,
        };
        assert!(monitor.stop().is_err());
        assert!(monitor.stop().is_err());
        assert!(monitor.join_failed);
    }
}
