//! Unprivileged NT relative opens. No backup semantics, ACL edits or token privileges.
use super::{Opened, Snapshot, boundary};
use std::ffi::{OsStr, OsString, c_void};
use std::fs::File;
use std::io;
use std::mem::{size_of, zeroed};
use std::os::windows::{
    ffi::{OsStrExt, OsStringExt},
    io::{AsRawHandle, FromRawHandle},
};
use std::path::{Component, Path, PathBuf, Prefix};
use std::ptr::{null, null_mut};
use std::sync::Arc;
use windows_sys::Win32::{Foundation::HANDLE, Storage::FileSystem::*};

pub struct Root {
    pub file: File,
}
#[repr(C)]
struct Unicode {
    length: u16,
    maximum: u16,
    buffer: *mut u16,
}
#[repr(C)]
struct Attributes {
    length: u32,
    parent: HANDLE,
    name: *mut Unicode,
    flags: u32,
    security: *mut c_void,
    qos: *mut c_void,
}
#[repr(C)]
struct IoStatus {
    status: usize,
    information: usize,
}
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtCreateFile(
        handle: *mut HANDLE,
        access: u32,
        attributes: *mut Attributes,
        status: *mut IoStatus,
        allocation: *const i64,
        file_attributes: u32,
        sharing: u32,
        disposition: u32,
        options: u32,
        ea: *const c_void,
        ea_length: u32,
    ) -> i32;
    fn RtlNtStatusToDosError(status: i32) -> u32;
}
fn decode(raw: &[u8]) -> io::Result<PathBuf> {
    if raw.is_empty() || raw.len() > 65532 || !raw.len().is_multiple_of(2) {
        return Err(boundary("invalid_windows_read_path"));
    }
    let wide: Vec<_> = raw
        .chunks_exact(2)
        .map(|c| u16::from_le_bytes([c[0], c[1]]))
        .collect();
    if wide.contains(&0) {
        return Err(boundary("invalid_windows_read_path"));
    }
    Ok(PathBuf::from(OsString::from_wide(&wide)))
}
fn validate(parts: &[&OsStr]) -> io::Result<()> {
    if parts.is_empty() || parts.len() > 256 {
        return Err(boundary("invalid_relative_read_path"));
    }
    for part in parts {
        let name = part.to_string_lossy();
        let base = name.split('.').next().unwrap_or("").to_uppercase();
        let reserved = matches!(
            base.as_str(),
            "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
        ) || ["COM", "LPT"].iter().any(|prefix| {
            base.strip_prefix(prefix)
                .is_some_and(|tail| tail.chars().count() == 1 && "123456789¹²³".contains(tail))
        });
        if reserved
            || name.ends_with([' ', '.'])
            || name.chars().any(|c| c < ' ' || ":*?\"<>|".contains(c))
        {
            return Err(boundary("ambiguous_windows_read_path"));
        }
    }
    Ok(())
}
fn nt_open(name: &OsStr, parent: HANDLE, directory: bool, readable: bool) -> io::Result<File> {
    let mut wide: Vec<u16> = name.encode_wide().collect();
    let length = wide.len() * 2;
    if length == 0 || length > 65532 || wide.contains(&0) {
        return Err(boundary("invalid_windows_read_path"));
    }
    wide.push(0);
    let mut string = Unicode {
        length: length as u16,
        maximum: length as u16 + 2,
        buffer: wide.as_mut_ptr(),
    };
    let mut attributes = Attributes {
        length: size_of::<Attributes>() as u32,
        parent,
        name: &mut string,
        flags: 0x40 | 0x1000,
        security: null_mut(),
        qos: null_mut(),
    };
    let mut status = IoStatus {
        status: 0,
        information: 0,
    };
    let mut handle = null_mut();
    let result = unsafe {
        NtCreateFile(
            &mut handle,
            0x100080
                | if directory {
                    0x20
                } else if readable {
                    1
                } else {
                    0
                },
            &mut attributes,
            &mut status,
            null(),
            0,
            1 | 2,
            1,
            0x200000 | 0x20 | if directory { 1 } else { 0 },
            null(),
            0,
        )
    };
    if result < 0 {
        return Err(io::Error::from_raw_os_error(
            unsafe { RtlNtStatusToDosError(result) } as i32,
        ));
    }
    Ok(unsafe { File::from_raw_handle(handle) })
}
pub fn snapshot(file: &File) -> io::Result<Snapshot> {
    let handle = file.as_raw_handle();
    if unsafe { GetFileType(handle) } != FILE_TYPE_DISK {
        return Err(boundary("read_requires_disk_object"));
    }
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
    let mut id: FILE_ID_INFO = unsafe { zeroed() };
    let mut basic: FILE_BASIC_INFO = unsafe { zeroed() };
    if unsafe { GetFileInformationByHandle(handle, &mut info) } == 0
        || unsafe {
            GetFileInformationByHandleEx(
                handle,
                FileIdInfo,
                (&mut id as *mut FILE_ID_INFO).cast(),
                size_of::<FILE_ID_INFO>() as u32,
            )
        } == 0
        || unsafe {
            GetFileInformationByHandleEx(
                handle,
                FileBasicInfo,
                (&mut basic as *mut FILE_BASIC_INFO).cast(),
                size_of::<FILE_BASIC_INFO>() as u32,
            )
        } == 0
    {
        return Err(io::Error::last_os_error());
    }
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(boundary("read_reparse_object_denied"));
    }
    let directory = info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0;
    Ok(Snapshot {
        volume: id.VolumeSerialNumber,
        id: u128::from_le_bytes(id.FileId.Identifier),
        size: (info.nFileSizeHigh as u64) << 32 | info.nFileSizeLow as u64,
        links: info.nNumberOfLinks as u64,
        modified: basic.LastWriteTime as i128,
        changed: basic.ChangeTime as i128,
        directory,
        regular: !directory,
    })
}
fn check_directory(file: &File) -> io::Result<()> {
    if !snapshot(file)?.directory {
        return Err(io::Error::from(io::ErrorKind::NotADirectory));
    }
    Ok(())
}
impl Root {
    pub fn open(raw: &[u8]) -> io::Result<Self> {
        let path = decode(raw)?;
        let mut components = path.components();
        if !matches!(components.next(), Some(Component::Prefix(p)) if matches!(p.kind(), Prefix::Disk(_)))
            || !matches!(components.next(), Some(Component::RootDir))
        {
            return Err(boundary("local_absolute_read_root_required"));
        }
        let parts: Vec<_> = components
            .map(|part| match part {
                Component::Normal(name) => Ok(name),
                _ => Err(boundary("invalid_read_root")),
            })
            .collect::<io::Result<_>>()?;
        validate(&parts)?;
        let mut name = OsString::from("\\??\\");
        name.push(path);
        let file = nt_open(&name, null_mut(), true, false)?;
        check_directory(&file)?;
        Ok(Self { file })
    }
}
pub fn open(root: Arc<Root>, raw: &[u8], metadata_only: bool) -> io::Result<Opened> {
    let path = decode(raw)?;
    let parts: Vec<_> = Path::new(&path)
        .components()
        .map(|part| match part {
            Component::Normal(name) => Ok(name),
            _ => Err(boundary("invalid_relative_read_path")),
        })
        .collect::<io::Result<_>>()?;
    validate(&parts)?;
    let mut parent = root.file.as_raw_handle();
    let mut pins = Vec::new();
    for part in &parts[..parts.len() - 1] {
        let file = nt_open(part, parent, true, false)?;
        check_directory(&file)?;
        parent = file.as_raw_handle();
        pins.push(file);
    }
    let file = nt_open(parts[parts.len() - 1], parent, false, !metadata_only)?;
    super::regular(&snapshot(&file)?)?;
    Ok(Opened {
        file,
        _pins: pins,
        readable: !metadata_only,
        _root: root,
    })
}
