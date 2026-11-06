//! Object ownership, not permission authority. Tokens never encode OS handles.
//! No content leaves this module until the final metadata check succeeds.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::sync::{Arc, Mutex, OnceLock};

#[cfg(unix)]
#[path = "safe_read_unix.rs"]
mod platform;
#[cfg(windows)]
#[path = "safe_read_windows.rs"]
mod platform;

pub const MAX_READ: usize = 16 * 1024 * 1024;
pub const MAX_PACKAGE: usize = 272 * 1024 * 1024;
pub const MAX_HASH: u64 = 512 * 1024 * 1024;
const MAX_LIVE: usize = 8192;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub volume: u64,
    pub id: u128,
    pub size: u64,
    pub links: u64,
    pub modified: i128,
    pub changed: i128,
    pub directory: bool,
    pub regular: bool,
}
pub fn boundary(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
fn identity(info: &Snapshot) -> Value {
    json!({"volume": info.volume, "file_id": info.id.to_string()})
}
fn regular(info: &Snapshot) -> io::Result<()> {
    if info.directory {
        return Err(io::Error::from(io::ErrorKind::IsADirectory));
    }
    if !info.regular || info.links != 1 {
        return Err(boundary("singly_linked_regular_file_required"));
    }
    Ok(())
}

pub struct Opened {
    pub file: File,
    // Metadata-only descriptors/ancestor locks outlive any readable object.
    pub _pins: Vec<File>,
    pub readable: bool,
    pub _root: Arc<platform::Root>,
}
enum Resource {
    Root(Arc<platform::Root>),
    File(Arc<Mutex<Opened>>),
}
struct Registry {
    next: u64,
    live: BTreeMap<u64, Resource>,
}
static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();
fn registry() -> io::Result<std::sync::MutexGuard<'static, Registry>> {
    REGISTRY
        .get_or_init(|| {
            Mutex::new(Registry {
                next: 1,
                live: BTreeMap::new(),
            })
        })
        .lock()
        .map_err(|_| boundary("read_registry_poisoned"))
}
fn insert(value: Resource) -> io::Result<u64> {
    let mut registry = registry()?;
    registry.insert(value)
}
impl Registry {
    fn insert(&mut self, value: Resource) -> io::Result<u64> {
        if self.live.len() >= MAX_LIVE || self.next == u64::MAX {
            return Err(boundary("read_resource_limit"));
        }
        let token = self.next;
        self.next += 1;
        self.live.insert(token, value);
        Ok(token)
    }
}
fn file(token: u64) -> io::Result<Arc<Mutex<Opened>>> {
    match registry()?.live.get(&token) {
        Some(Resource::File(value)) => Ok(Arc::clone(value)),
        _ => Err(boundary("invalid_read_file_token")),
    }
}
pub fn root(path: &[u8]) -> io::Result<Value> {
    let root = Arc::new(platform::Root::open(path)?);
    let info = platform::snapshot(&root.file)?;
    let token = insert(Resource::Root(root))?;
    Ok(json!({"ok": true, "token": token, "identity": identity(&info)}))
}
#[cfg(target_os = "linux")]
pub fn directory_descriptor(token: u64) -> io::Result<Value> {
    use std::os::fd::AsRawFd;
    let registry = registry()?;
    match registry.live.get(&token) {
        Some(Resource::Root(root)) => Ok(json!({"ok":true,"fd":root.file.as_raw_fd()})),
        _ => Err(boundary("invalid_read_root_token")),
    }
}
pub fn open(token: u64, path: &[u8], metadata_only: bool) -> io::Result<Value> {
    let root = match registry()?.live.get(&token) {
        Some(Resource::Root(value)) => Arc::clone(value),
        _ => return Err(boundary("invalid_read_root_token")),
    };
    let opened = platform::open(root, path, metadata_only)?;
    let info = platform::snapshot(&opened.file)?;
    regular(&info)?;
    let token = insert(Resource::File(Arc::new(Mutex::new(opened))))?;
    Ok(json!({"ok": true, "token": token, "identity": identity(&info)}))
}
pub fn close(token: u64) -> io::Result<Value> {
    // Issued tokens form a contiguous range. A retired token never names a
    // future object; duplicate retirement needs no unbounded tombstone table.
    let retired = {
        let mut registry = registry()?;
        if token == 0 || token >= registry.next {
            return Err(boundary("unknown_read_token"));
        }
        registry.live.remove(&token)
    };
    // An already admitted operation keeps its Arc until completion. Retiring
    // here bars new operations; no descriptor closes underneath a live read.
    drop(retired);
    Ok(json!({"ok": true}))
}
fn checked_before(opened: &Opened, limit: u64) -> io::Result<Snapshot> {
    if !opened.readable {
        return Err(boundary("metadata_pin_cannot_read"));
    }
    let info = platform::snapshot(&opened.file)?;
    regular(&info)?;
    if info.size > limit {
        return Err(boundary("read_size_limit"));
    }
    Ok(info)
}
fn checked_after(opened: &Opened, before: &Snapshot) -> io::Result<()> {
    if platform::snapshot(&opened.file)? != *before {
        return Err(boundary("file_changed_during_read"));
    }
    Ok(())
}
pub fn bytes(token: u64, limit: usize) -> io::Result<Vec<u8>> {
    if limit == 0 || limit > MAX_READ {
        return Err(boundary("invalid_read_limit"));
    }
    bounded_bytes(token, limit)
}
pub fn package_bytes(token: u64, limit: usize) -> io::Result<Vec<u8>> {
    if limit == 0 || limit > MAX_PACKAGE {
        return Err(boundary("invalid_package_read_limit"));
    }
    bounded_bytes(token, limit)
}
pub fn size(token: u64, limit: u64) -> io::Result<Value> {
    if limit == 0 || limit > MAX_HASH {
        return Err(boundary("invalid_read_limit"));
    }
    let resource = file(token)?;
    let opened = resource
        .lock()
        .map_err(|_| boundary("read_object_poisoned"))?;
    Ok(json!({"ok": true, "size": checked_before(&opened, limit)?.size}))
}
fn bounded_bytes(token: u64, limit: usize) -> io::Result<Vec<u8>> {
    let resource = file(token)?;
    let mut opened = resource
        .lock()
        .map_err(|_| boundary("read_object_poisoned"))?;
    read_checked(&mut opened, limit, |file, output| {
        file.take(limit as u64 + 1).read_to_end(output)?;
        Ok(())
    })
}
fn read_checked(
    opened: &mut Opened,
    limit: usize,
    consume: impl FnOnce(&mut File, &mut Vec<u8>) -> io::Result<()>,
) -> io::Result<Vec<u8>> {
    let before = checked_before(opened, limit as u64)?;
    // Reserve the observed size plus the growth sentinel. Geometric growth of
    // a 272 MiB package must not transiently allocate a 512 MiB Rust vector.
    let mut output = Vec::with_capacity(before.size.min(limit as u64) as usize + 1);
    // Read one extra byte to detect growth. No unvalidated prefix is exported.
    consume(&mut opened.file, &mut output)?;
    if output.len() > limit {
        return Err(boundary("read_size_limit"));
    }
    checked_after(opened, &before)?;
    Ok(output)
}
pub fn hash(token: u64, limit: u64) -> io::Result<Value> {
    if limit == 0 || limit > MAX_HASH {
        return Err(boundary("invalid_hash_limit"));
    }
    let resource = file(token)?;
    let mut opened = resource
        .lock()
        .map_err(|_| boundary("read_object_poisoned"))?;
    let before = checked_before(&opened, limit)?;
    opened.file.seek(SeekFrom::Start(0))?;
    let mut digest = Sha256::new();
    let mut count = 0u64;
    let mut buffer = [0u8; 65536];
    loop {
        let size = (limit + 1 - count).min(buffer.len() as u64) as usize;
        let n = match opened.file.read(&mut buffer[..size]) {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            result => result?,
        };
        if n == 0 {
            break;
        }
        count += n as u64;
        if count > limit {
            return Err(boundary("read_size_limit"));
        }
        digest.update(&buffer[..n]);
    }
    if count != before.size {
        return Err(boundary("file_changed_during_read"));
    }
    checked_after(&opened, &before)?;
    Ok(json!({"ok": true, "sha256": format!("{:x}", digest.finalize()), "size": count}))
}
pub fn response(result: io::Result<Value>) -> Value {
    result.unwrap_or_else(|error| {
        json!({"ok": false, "error": error.to_string(),
        "read_error": format!("{:?}", error.kind()), "os_error": error.raw_os_error()})
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::path::Path;
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    fn encoded(path: &Path) -> Vec<u8> {
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStrExt;
            path.as_os_str().as_bytes().to_vec()
        }
        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStrExt;
            path.as_os_str()
                .encode_wide()
                .flat_map(u16::to_le_bytes)
                .collect()
        }
    }
    pub(super) struct Tree(pub(super) std::path::PathBuf);
    impl Tree {
        pub(super) fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "gitgo-read-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            std::fs::write(path.join("file"), b"selected").unwrap();
            Self(path)
        }
        fn opened(&self) -> Opened {
            let root = Arc::new(platform::Root::open(&encoded(&self.0)).unwrap());
            platform::open(root, &encoded(Path::new("file")), false).unwrap()
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    #[test]
    fn same_length_change_with_changed_metadata_is_refused() {
        let tree = Tree::new();
        let mut opened = tree.opened();
        let original = platform::snapshot(&opened.file).unwrap();
        let result =
            read_checked(&mut opened, 16, |file, output| {
                // Actual second writer, at the exact interval between snapshots.
                let mut writer = std::fs::OpenOptions::new()
                    .write(true)
                    .open(tree.0.join("file"))
                    .unwrap();
                writer.write_all(b"attacker").unwrap();
                // NTFS writes in the same clock tick can keep both timestamps.
                // Test the promised metadata-change refusal deterministically;
                // this is not a claim of an immutable/atomic content snapshot.
                writer
                    .set_times(std::fs::FileTimes::new().set_modified(
                        std::time::UNIX_EPOCH + std::time::Duration::from_secs(86_400),
                    ))
                    .unwrap();
                writer.sync_all().unwrap();
                drop(writer);
                assert_ne!(platform::snapshot(file)?, original);
                file.read_to_end(output)?;
                Ok(())
            });
        assert!(result.is_err());
    }
    #[test]
    fn live_resource_budget_and_token_exhaustion_refuse_without_allocation() {
        let tree = Tree::new();
        let root = Arc::new(platform::Root::open(&encoded(&tree.0)).unwrap());
        let mut registry = Registry {
            next: 1,
            live: BTreeMap::new(),
        };
        for token in 1..=MAX_LIVE as u64 {
            assert_eq!(
                registry.insert(Resource::Root(Arc::clone(&root))).unwrap(),
                token
            );
        }
        let next = registry.next;
        assert!(registry.insert(Resource::Root(Arc::clone(&root))).is_err());
        assert_eq!(registry.next, next);
        assert_eq!(registry.live.len(), MAX_LIVE);
        registry.live.clear();
        registry.next = u64::MAX;
        assert!(registry.insert(Resource::Root(root)).is_err());
        assert!(registry.live.is_empty());
    }
    #[test]
    fn growth_during_read_is_not_a_successful_prefix() {
        let tree = Tree::new();
        let mut opened = tree.opened();
        assert!(
            read_checked(&mut opened, 8, |file, output| {
                let mut writer = std::fs::OpenOptions::new()
                    .append(true)
                    .open(tree.0.join("file"))
                    .unwrap();
                writer.write_all(b"oversized").unwrap();
                drop(writer);
                file.take(9).read_to_end(output)?;
                Ok(())
            })
            .is_err()
        );
    }
    #[test]
    fn admitted_owner_survives_retirement_but_token_does_not() {
        let tree = Tree::new();
        let token = insert(Resource::File(Arc::new(Mutex::new(tree.opened())))).unwrap();
        let admitted = file(token).unwrap();
        close(token).unwrap();
        assert!(file(token).is_err());
        assert_eq!(
            read_checked(&mut admitted.lock().unwrap(), 16, |file, output| {
                file.read_to_end(output)?;
                Ok(())
            })
            .unwrap(),
            b"selected"
        );
    }
    #[test]
    fn native_descriptors_are_not_inherited() {
        let tree = Tree::new();
        let opened = tree.opened();
        for file in [&opened.file, &opened._root.file]
            .into_iter()
            .chain(opened._pins.iter())
        {
            #[cfg(unix)]
            {
                use std::os::fd::AsRawFd;
                let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFD) };
                assert!(flags >= 0 && flags & libc::FD_CLOEXEC != 0);
            }
            #[cfg(windows)]
            {
                use std::os::windows::io::AsRawHandle;
                use windows_sys::Win32::Foundation::{GetHandleInformation, HANDLE_FLAG_INHERIT};
                let mut flags = 0;
                assert_ne!(
                    unsafe { GetHandleInformation(file.as_raw_handle(), &mut flags) },
                    0
                );
                assert_eq!(flags & HANDLE_FLAG_INHERIT, 0);
            }
        }
    }
}
