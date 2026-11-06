use super::{Opened, Snapshot, boundary, regular};
use std::ffi::{CString, OsStr};
use std::fs::File;
use std::io;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::{ffi::OsStrExt, fs::MetadataExt};
use std::path::{Component, Path};
use std::sync::Arc;

pub struct Root {
    pub file: File,
}
fn name(raw: &[u8]) -> io::Result<CString> {
    if raw.is_empty() || raw.len() > 32768 {
        return Err(boundary("invalid_read_path"));
    }
    CString::new(raw).map_err(|_| boundary("invalid_read_path"))
}
fn owned(fd: i32) -> io::Result<File> {
    if fd < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { File::from_raw_fd(fd) })
    }
}
pub fn snapshot(file: &File) -> io::Result<Snapshot> {
    let m = file.metadata()?;
    Ok(Snapshot {
        volume: m.dev(),
        id: m.ino() as u128,
        size: m.size(),
        links: m.nlink(),
        modified: m.mtime() as i128 * 1_000_000_000 + m.mtime_nsec() as i128,
        changed: m.ctime() as i128 * 1_000_000_000 + m.ctime_nsec() as i128,
        directory: m.is_dir(),
        regular: m.is_file(),
    })
}
fn relative(raw: &[u8]) -> io::Result<CString> {
    let path = Path::new(OsStr::from_bytes(raw));
    if path.components().count() > 256
        || path
            .components()
            .any(|p| !matches!(p, Component::Normal(_)))
    {
        return Err(boundary("invalid_relative_read_path"));
    }
    name(raw)
}
#[cfg(target_os = "linux")]
fn openat(directory: i32, name: &CString, flags: i32, resolve: u64) -> io::Result<File> {
    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
    {
        let _ = (directory, name, flags, resolve);
        Err(boundary("unsupported_linux_read_abi"))
    }
    #[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
    {
        #[repr(C)]
        struct How {
            flags: u64,
            mode: u64,
            resolve: u64,
        }
        let how = How {
            flags: flags as u64,
            mode: 0,
            resolve,
        };
        owned(unsafe {
            libc::syscall(
                libc::SYS_openat2,
                directory,
                name.as_ptr(),
                &how,
                std::mem::size_of::<How>(),
            )
        } as i32)
    }
}
#[cfg(target_os = "linux")]
impl Root {
    pub fn open(raw: &[u8]) -> io::Result<Self> {
        absolute(raw)?;
        Ok(Self {
            file: openat(
                libc::AT_FDCWD,
                &name(raw)?,
                libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC,
                0x04 | 0x02,
            )?,
        })
    }
}
fn absolute(raw: &[u8]) -> io::Result<()> {
    let path = Path::new(OsStr::from_bytes(raw));
    if !path.is_absolute()
        || path.parent().is_none()
        || path.components().any(|p| matches!(p, Component::ParentDir))
    {
        return Err(boundary("concrete_absolute_read_root_required"));
    }
    Ok(())
}
#[cfg(target_os = "linux")]
pub fn open(root: Arc<Root>, raw: &[u8], metadata_only: bool) -> io::Result<Opened> {
    let pin = openat(
        root.file.as_raw_fd(),
        &relative(raw)?,
        libc::O_PATH | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        0x08 | 0x01 | 0x04 | 0x02,
    )?;
    let selected = snapshot(&pin)?;
    regular(&selected)?;
    if metadata_only {
        return Ok(Opened {
            file: pin,
            _pins: vec![],
            readable: false,
            _root: root,
        });
    }
    let path = name(format!("/proc/self/fd/{}", pin.as_raw_fd()).as_bytes())?;
    let file = owned(unsafe {
        libc::open(
            path.as_ptr(),
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NONBLOCK,
        )
    })?;
    if snapshot(&file)? != selected {
        return Err(boundary("readable_identity_changed"));
    }
    Ok(Opened {
        file,
        _pins: vec![pin],
        readable: true,
        _root: root,
    })
}

#[cfg(target_os = "macos")]
const BENEATH: i32 = 0x1000;
#[cfg(target_os = "macos")]
const UNIQUE: i32 = 0x2000;
#[cfg(target_os = "macos")]
const EVTONLY: i32 = 0x8000;
#[cfg(target_os = "macos")]
const FLAGS: i32 = libc::O_CLOEXEC | 0x20000000 | libc::O_NONBLOCK;
#[cfg(target_os = "macos")]
fn openat(directory: i32, name: &CString, flags: i32) -> io::Result<File> {
    owned(unsafe { libc::openat(directory, name.as_ptr(), flags) })
}
#[cfg(target_os = "macos")]
impl Root {
    pub fn open(raw: &[u8]) -> io::Result<Self> {
        absolute(raw)?;
        let mut path = Path::new(OsStr::from_bytes(raw)).to_path_buf();
        for alias in ["var", "tmp", "etc"] {
            let prefix = Path::new("/").join(alias);
            if let Ok(suffix) = path.strip_prefix(&prefix) {
                let metadata = prefix.symlink_metadata()?;
                if !metadata.is_symlink()
                    || metadata.uid() != 0
                    || prefix.read_link()? != Path::new("private").join(alias)
                {
                    return Err(boundary("darwin_system_alias_unavailable"));
                }
                path = Path::new("/private").join(alias).join(suffix);
                break;
            }
        }
        let file = openat(
            libc::AT_FDCWD,
            &name(path.as_os_str().as_bytes())?,
            FLAGS | libc::O_DIRECTORY | EVTONLY,
        )?;
        probe(&file, BENEATH)?;
        Ok(Self { file })
    }
}
#[cfg(target_os = "macos")]
fn probe(file: &File, beneath: i32) -> io::Result<()> {
    match openat(
        file.as_raw_fd(),
        &name(b"..")?,
        FLAGS | libc::O_DIRECTORY | EVTONLY | beneath,
    ) {
        Err(error) if matches!(error.raw_os_error(), Some(107) | Some(libc::EXDEV)) => (),
        _ => return Err(boundary("darwin_beneath_not_enforced")),
    }
    let within = openat(
        file.as_raw_fd(),
        &name(b".")?,
        FLAGS | libc::O_DIRECTORY | EVTONLY | beneath,
    )?;
    if snapshot(&within)? != snapshot(file)? {
        return Err(boundary("darwin_probe_identity_changed"));
    }
    Ok(())
}
#[cfg(target_os = "macos")]
pub fn open(root: Arc<Root>, raw: &[u8], metadata_only: bool) -> io::Result<Opened> {
    let name = relative(raw)?;
    let pin = openat(
        root.file.as_raw_fd(),
        &name,
        FLAGS | BENEATH | UNIQUE | EVTONLY,
    )?;
    let selected = snapshot(&pin)?;
    regular(&selected)?;
    if selected.volume != snapshot(&root.file)?.volume {
        return Err(boundary("read_cross_device"));
    }
    if metadata_only {
        return Ok(Opened {
            file: pin,
            _pins: vec![],
            readable: false,
            _root: root,
        });
    }
    let file = openat(
        root.file.as_raw_fd(),
        &name,
        FLAGS | BENEATH | UNIQUE | libc::O_NOCTTY,
    )?;
    if snapshot(&file)? != selected {
        return Err(boundary("readable_identity_changed"));
    }
    Ok(Opened {
        file,
        _pins: vec![pin],
        readable: true,
        _root: root,
    })
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    #[test]
    fn ignored_beneath_is_refused() {
        let root = Root::open(b"/private/tmp").unwrap();
        assert!(probe(&root.file, 0).is_err());
    }
}
