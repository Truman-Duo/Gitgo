//! Native resource ownership only. Python remains the permission/journal authority.
//! C callers supply live, non-overlapping buffers for the duration of one call.
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicBool, Ordering};

pub const ABI: u32 = 1;
pub const MAX_REQUEST: usize = 1024 * 1024;
pub const RESPONSE_CAPACITY: usize = 65536;
static SEALED: AtomicBool = AtomicBool::new(false);

#[derive(Deserialize)]
#[serde(tag = "op", deny_unknown_fields)]
pub enum Request {
    #[serde(rename = "capabilities")]
    Capabilities {},
    #[serde(rename = "job_create")]
    JobCreate {
        recovery_id: String,
        processes: u32,
        memory: u64,
        cpu_seconds: u32,
    },
    #[serde(rename = "job_recover")]
    JobRecover { recovery_id: String },
    #[serde(rename = "job_query")]
    JobQuery { token: u64 },
    #[serde(rename = "job_retire")]
    JobRetire { token: u64 },
    #[serde(rename = "process_release")]
    ProcessRelease { token: u64 },
    #[serde(rename = "acl_open")]
    AclOpen { path: String, access: u32 },
    #[serde(rename = "acl_release")]
    AclRelease { token: u64 },
    #[serde(rename = "launch")]
    Launch {
        token: u64,
        profile: String,
        executable: Option<String>,
        command: String,
        cwd: String,
        environment: Vec<(String, String)>,
        handles: Vec<usize>,
        flags: u32,
    },
}

fn dispatch(raw: &[u8]) -> Value {
    if SEALED.load(Ordering::Acquire) {
        return json!({"ok": false, "error": "native_boundary_sealed"});
    }
    let request = match serde_json::from_slice::<Request>(raw) {
        Ok(value) => value,
        Err(_) => return json!({"ok": false, "error": "invalid_request"}),
    };
    if matches!(request, Request::Capabilities {}) {
        return json!({"ok": true, "abi": ABI, "platform": std::env::consts::OS,
            "windows_appcontainer": cfg!(windows), "linux_execution": false,
            "macos_execution": false, "authority": "host-existing-permission-broker"});
    }
    #[cfg(windows)]
    {
        windows::dispatch(request)
    }
    #[cfg(not(windows))]
    {
        json!({"ok": false, "error": "unsupported_platform"})
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn gitgo_native_abi() -> u32 {
    ABI
}

/// A response buffer is mandatory BEFORE any effect. Never retry a request to
/// discover its size: launch is non-replayable. Panic seals this module instance;
/// existing Python recovery debt remains authoritative after host failure.
///
/// # Safety
/// All pointers must reference valid, disjoint buffers of their declared sizes.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn gitgo_native_call(
    input: *const u8,
    length: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    if input.is_null()
        || output.is_null()
        || written.is_null()
        || length == 0
        || length > MAX_REQUEST
        || capacity < RESPONSE_CAPACITY
    {
        return 1;
    }
    unsafe {
        *written = 0;
    }
    let result = std::panic::catch_unwind(|| {
        let request = unsafe { std::slice::from_raw_parts(input, length) };
        serde_json::to_vec(&dispatch(request))
    });
    match result {
        Ok(Ok(bytes)) if bytes.len() <= capacity => {
            unsafe {
                std::ptr::copy_nonoverlapping(bytes.as_ptr(), output, bytes.len());
                *written = bytes.len();
            }
            0
        }
        _ => {
            SEALED.store(true, Ordering::Release);
            2
        }
    }
}

#[cfg(any(windows, test))]
mod registry;
#[cfg(windows)]
mod windows;

/// Bounded descriptor transport; no native allocation pointer escapes the ABI.
/// # Safety
/// Valid, non-overlapping buffers and a live `written` pointer are required.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn gitgo_native_acl(
    operation: u32,
    token: u64,
    input: *const u8,
    length: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    if written.is_null()
        || length > MAX_REQUEST
        || capacity > 8 * MAX_REQUEST
        || (length > 0 && input.is_null())
        || (capacity > 0 && output.is_null())
    {
        return 87;
    }
    unsafe {
        *written = 0;
    }
    if SEALED.load(Ordering::Acquire) {
        return 5023;
    }
    #[cfg(windows)]
    {
        match std::panic::catch_unwind(|| {
            let input = if length == 0 {
                &[]
            } else {
                unsafe { std::slice::from_raw_parts(input, length) }
            };
            windows::acl(operation, token, input, capacity)
        }) {
            Ok(Ok(bytes)) if bytes.len() <= capacity => {
                if !bytes.is_empty() {
                    unsafe {
                        std::ptr::copy_nonoverlapping(bytes.as_ptr(), output, bytes.len());
                    }
                }
                unsafe {
                    *written = bytes.len();
                }
                0
            }
            Ok(Err(error)) => error,
            _ => {
                SEALED.store(true, Ordering::Release);
                5023
            }
        }
    }
    #[cfg(not(windows))]
    {
        let _ = (operation, token);
        50
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_request_schema() {
        for raw in [
            br#"{"op":"capabilities","allow":true}"#.as_slice(),
            br#"{"op":"launch","token":0}"#,
            br#"{"op":"unknown"}"#,
        ] {
            assert_eq!(dispatch(raw)["error"], "invalid_request");
        }
    }
    #[test]
    fn ffi_checks_output_before_dispatch() {
        let raw = br#"{"op":"capabilities"}"#;
        let mut output = [0u8; RESPONSE_CAPACITY];
        let mut size = 999;
        unsafe {
            assert_eq!(
                gitgo_native_call(raw.as_ptr(), raw.len(), output.as_mut_ptr(), 1, &mut size),
                1
            );
            assert_eq!(size, 999);
            assert_eq!(
                gitgo_native_call(
                    raw.as_ptr(),
                    raw.len(),
                    output.as_mut_ptr(),
                    output.len(),
                    &mut size
                ),
                0
            );
        }
        let result: Value = serde_json::from_slice(&output[..size]).unwrap();
        assert_eq!(result["abi"], ABI);
        assert_eq!(result["ok"], true);
    }
    #[test]
    #[cfg(not(windows))]
    fn unsupported_is_not_execution_success() {
        assert_eq!(
            dispatch(br#"{"op":"job_retire","token":1}"#)["error"],
            "unsupported_platform"
        );
    }
}
