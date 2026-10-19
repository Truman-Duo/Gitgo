// Audit only. This program never boots a guest or enables tool execution.
import Foundation
import Hypervisor
import Virtualization

var report: [String: Any] = [
    "schema_version": 1,
    "virtualization_framework_supported": VZVirtualMachine.isSupported,
    "guest_os_boot_tested": false,
    "native_macos_guest_ready": false,
    "production_backend_enabled": false,
]
#if arch(arm64)
report["native_macos_guest_api_available"] = true
let created = hv_vm_create(nil)
#elseif arch(x86_64)
// Hypervisor supports Intel, but VZ's macOS guest platform is Apple silicon only.
report["native_macos_guest_api_available"] = false
let created = hv_vm_create(hv_vm_options_t(HV_VM_DEFAULT))
#else
#error("Unsupported macOS architecture")
#endif
report["hypervisor_create_status"] = UInt32(bitPattern: Int32(truncatingIfNeeded: created))
report["hypervisor_vm_created"] = created == HV_SUCCESS
if created == HV_SUCCESS {
    let destroyed = hv_vm_destroy()
    report["hypervisor_destroy_status"] = UInt32(bitPattern: Int32(truncatingIfNeeded: destroyed))
    if destroyed != HV_SUCCESS {
        // Exiting also releases the VM, but cleanup failure must fail the audit.
        fputs("Hypervisor VM cleanup failed\n", stderr)
        exit(1)
    }
}
let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data([10]))
