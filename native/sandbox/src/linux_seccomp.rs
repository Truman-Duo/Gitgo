//! Fixed socket policy construction. Never installs a filter into the Host.
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::ffi::{CString, OsStr};
use std::fs::File;
use std::io::{self, Seek, SeekFrom};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::{ffi::OsStrExt, fs::MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

fn invalid(message: &'static str) -> io::Error {
    io::Error::other(message)
}
#[repr(C)]
struct Comparison {
    argument: u32,
    operation: i32,
    first: u64,
    second: u64,
}
impl Comparison {
    fn new(argument: u32, operation: i32, first: u64, second: u64) -> Self {
        Self {
            argument,
            operation,
            first,
            second,
        }
    }
}
struct Api {
    _image: File,
    handle: *mut libc::c_void,
    init: unsafe extern "C" fn(u32) -> *mut libc::c_void,
    release: unsafe extern "C" fn(*mut libc::c_void),
    attr: unsafe extern "C" fn(*mut libc::c_void, u32, u32) -> i32,
    resolve: unsafe extern "C" fn(*const libc::c_char) -> i32,
    rule: unsafe extern "C" fn(*mut libc::c_void, u32, i32, u32, *const Comparison) -> i32,
    export: unsafe extern "C" fn(*mut libc::c_void, i32) -> i32,
}
impl Drop for Api {
    fn drop(&mut self) {
        // A failed dlclose keeps the loader mapping alive, never frees memory
        // behind function pointers. No kernel isolation result relies on unload.
        unsafe {
            libc::dlclose(self.handle);
        }
    }
}
fn trusted_image(candidate: &Path, workspace: &Path) -> io::Result<File> {
    let path = candidate.canonicalize()?;
    if path.starts_with(workspace) {
        return Err(invalid("seccomp_image_inside_workspace"));
    }
    for component in path.ancestors() {
        let info = component.metadata()?;
        if info.uid() != 0 || info.mode() & 0o022 != 0 {
            return Err(invalid("untrusted_seccomp_image"));
        }
    }
    let file = File::open(&path)?;
    let opened = file.metadata()?;
    let checked = path.metadata()?;
    if !opened.is_file() || opened.dev() != checked.dev() || opened.ino() != checked.ino() {
        return Err(invalid("seccomp_image_changed"));
    }
    Ok(file)
}
impl Api {
    fn load(workspace: &Path) -> io::Result<Self> {
        #[cfg(target_arch = "x86_64")]
        let arch = "x86_64-linux-gnu";
        #[cfg(target_arch = "aarch64")]
        let arch = "aarch64-linux-gnu";
        #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
        let arch = "unsupported";
        let directories = [
            format!("/usr/lib/{arch}"),
            format!("/lib/{arch}"),
            "/usr/lib".into(),
            "/lib".into(),
            "/usr/lib64".into(),
            "/lib64".into(),
        ];
        let image = directories
            .iter()
            .find_map(|base| {
                trusted_image(&Path::new(base).join("libseccomp.so.2"), workspace).ok()
            })
            .ok_or_else(|| invalid("trusted_system_libseccomp_required"))?;
        let path = CString::new(format!("/proc/self/fd/{}", image.as_raw_fd())).unwrap();
        let handle = unsafe { libc::dlopen(path.as_ptr(), libc::RTLD_NOW | libc::RTLD_LOCAL) };
        if handle.is_null() {
            return Err(invalid("cannot_load_trusted_libseccomp"));
        }
        macro_rules! symbol {
            ($name:literal, $ty:ty) => {{
                let pointer = unsafe { libc::dlsym(handle, concat!($name, "\0").as_ptr().cast()) };
                if pointer.is_null() {
                    unsafe {
                        libc::dlclose(handle);
                    }
                    return Err(invalid("missing_libseccomp_symbol"));
                }
                unsafe { std::mem::transmute::<*mut libc::c_void, $ty>(pointer) }
            }};
        }
        Ok(Self {
            _image: image,
            handle,
            init: symbol!(
                "seccomp_init",
                unsafe extern "C" fn(u32) -> *mut libc::c_void
            ),
            release: symbol!("seccomp_release", unsafe extern "C" fn(*mut libc::c_void)),
            attr: symbol!(
                "seccomp_attr_set",
                unsafe extern "C" fn(*mut libc::c_void, u32, u32) -> i32
            ),
            resolve: symbol!(
                "seccomp_syscall_resolve_name",
                unsafe extern "C" fn(*const libc::c_char) -> i32
            ),
            rule: symbol!(
                "seccomp_rule_add_array",
                unsafe extern "C" fn(*mut libc::c_void, u32, i32, u32, *const Comparison) -> i32
            ),
            export: symbol!(
                "seccomp_export_bpf",
                unsafe extern "C" fn(*mut libc::c_void, i32) -> i32
            ),
        })
    }
}
struct Context<'a> {
    api: &'a Api,
    value: *mut libc::c_void,
}
impl Drop for Context<'_> {
    fn drop(&mut self) {
        unsafe {
            (self.api.release)(self.value);
        }
    }
}
fn check(status: i32) -> io::Result<()> {
    if status != 0 {
        Err(invalid("cannot_construct_socket_seccomp_policy"))
    } else {
        Ok(())
    }
}
impl Context<'_> {
    fn deny(&self, name: &str, comparisons: &[Comparison]) -> io::Result<()> {
        let name = CString::new(name).unwrap();
        let number = unsafe { (self.api.resolve)(name.as_ptr()) };
        if number == -1 {
            return Err(invalid("required_seccomp_syscall_unresolved"));
        }
        check(unsafe {
            (self.api.rule)(
                self.value,
                0x00050000 | libc::EPERM as u32,
                number,
                comparisons.len() as u32,
                if comparisons.is_empty() {
                    std::ptr::null()
                } else {
                    comparisons.as_ptr()
                },
            )
        })
    }
}
fn build(workspace: &[u8]) -> io::Result<File> {
    let workspace = PathBuf::from(OsStr::from_bytes(workspace)).canonicalize()?;
    let api = Api::load(&workspace)?;
    let value = unsafe { (api.init)(0x7fff0000) };
    if value.is_null() {
        return Err(invalid("cannot_allocate_seccomp_policy"));
    }
    let context = Context { api: &api, value };
    check(unsafe { (api.attr)(context.value, 2, 0x80000000) })?;
    context.deny("socket", &[Comparison::new(0, 2, libc::AF_INET as u64, 0)])?;
    for domain in (libc::AF_INET + 1)..libc::AF_INET6 {
        context.deny("socket", &[Comparison::new(0, 4, domain as u64, 0)])?;
    }
    context.deny("socket", &[Comparison::new(0, 6, libc::AF_INET6 as u64, 0)])?;
    context.deny(
        "socketpair",
        &[Comparison::new(0, 1, libc::AF_UNIX as u64, 0)],
    )?;
    for kind in 0..16 {
        if kind != libc::SOCK_STREAM {
            context.deny("socketpair", &[Comparison::new(1, 7, 0xf, kind as u64)])?;
        }
    }
    for name in ["io_uring_setup", "io_uring_enter", "io_uring_register"] {
        context.deny(name, &[])?;
    }
    let fd = unsafe {
        libc::memfd_create(
            c"gitgo-seccomp".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    check(unsafe { (api.export)(context.value, fd) })?;
    let length = file.metadata()?.len();
    if length == 0 || length > 65536 || length % 8 != 0 {
        return Err(invalid("invalid_seccomp_program_size"));
    }
    let seals = libc::F_SEAL_WRITE | libc::F_SEAL_GROW | libc::F_SEAL_SHRINK | libc::F_SEAL_SEAL;
    if unsafe { libc::fcntl(fd, libc::F_ADD_SEALS, seals) } != 0 {
        return Err(io::Error::last_os_error());
    }
    file.seek(SeekFrom::Start(0))?;
    Ok(file)
}
struct Registry {
    next: u64,
    files: BTreeMap<u64, File>,
}
fn registry() -> &'static Mutex<Registry> {
    static STATE: OnceLock<Mutex<Registry>> = OnceLock::new();
    STATE.get_or_init(|| {
        Mutex::new(Registry {
            next: 1,
            files: BTreeMap::new(),
        })
    })
}
fn failure(error: io::Error) -> Value {
    json!({"ok":false,"error":error.to_string(),"os_error":error.raw_os_error()})
}
pub fn create(workspace: &[u8]) -> Value {
    let result = (|| -> io::Result<Value> {
        let mut state = registry()
            .lock()
            .map_err(|_| invalid("seccomp_registry_poisoned"))?;
        if state.files.len() >= 128 || state.next == u64::MAX {
            return Err(invalid("seccomp_resource_budget_exhausted"));
        }
        let file = build(workspace)?;
        let fd = file.as_raw_fd();
        let token = state.next;
        state.next += 1;
        state.files.insert(token, file);
        Ok(json!({"ok":true,"token":token,"fd":fd}))
    })();
    result.unwrap_or_else(failure)
}
pub fn close(token: u64) -> Value {
    let result = (|| -> io::Result<Value> {
        let mut state = registry()
            .lock()
            .map_err(|_| invalid("seccomp_registry_poisoned"))?;
        if token == 0 || token >= state.next {
            return Err(invalid("unknown_seccomp_token"));
        }
        state.files.remove(&token);
        Ok(json!({"ok":true}))
    })();
    result.unwrap_or_else(failure)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exported_policy_is_sealed_and_not_inherited() {
        let file = build(b"/tmp").unwrap();
        let fd = file.as_raw_fd();
        assert_ne!(
            unsafe { libc::fcntl(fd, libc::F_GETFD) } & libc::FD_CLOEXEC,
            0
        );
        let seals = unsafe { libc::fcntl(fd, libc::F_GET_SEALS) };
        assert_eq!(seals & 0xf, 0xf);
        assert_eq!(unsafe { libc::write(fd, b"x".as_ptr().cast(), 1) }, -1);
        assert_eq!(io::Error::last_os_error().raw_os_error(), Some(libc::EPERM));
    }
    #[test]
    fn workspace_cannot_supply_the_system_library() {
        assert!(trusted_image(Path::new("/bin/sh"), Path::new("/")).is_err());
    }
}
