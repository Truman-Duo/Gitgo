// Audit only: allocate and retire an empty hypervisor, never boot a guest.
#import <Foundation/Foundation.h>
#import <Hypervisor/Hypervisor.h>
#import <Virtualization/Virtualization.h>

int main(void) {
    @autoreleasepool {
        NSMutableDictionary *report = [@{
            @"schema_version": @1,
            @"virtualization_framework_supported": ([VZVirtualMachine isSupported] ? @YES : @NO),
            @"guest_os_boot_tested": @NO,
            @"native_macos_guest_ready": @NO,
            @"production_backend_enabled": @NO,
            @"minimum_vm_memory_bytes": @([VZVirtualMachineConfiguration minimumAllowedMemorySize]),
            @"maximum_vm_memory_bytes": @([VZVirtualMachineConfiguration maximumAllowedMemorySize]),
            @"minimum_vm_cpu_count": @([VZVirtualMachineConfiguration minimumAllowedCPUCount]),
            @"maximum_vm_cpu_count": @([VZVirtualMachineConfiguration maximumAllowedCPUCount]),
        } mutableCopy];
#if defined(__arm64__)
        report[@"native_macos_guest_api_available"] = @YES;
        hv_return_t created = hv_vm_create(NULL);
#elif defined(__x86_64__)
        report[@"native_macos_guest_api_available"] = @NO;
        hv_return_t created = hv_vm_create(HV_VM_DEFAULT);
#else
#error Unsupported macOS architecture
#endif
        report[@"hypervisor_create_status"] = @((uint32_t)created);
        report[@"hypervisor_vm_created"] = (created == HV_SUCCESS ? @YES : @NO);
        if (created == HV_SUCCESS) {
            hv_return_t destroyed = hv_vm_destroy();
            report[@"hypervisor_destroy_status"] = @((uint32_t)destroyed);
            if (destroyed != HV_SUCCESS) {
                fputs("Hypervisor VM cleanup failed\n", stderr);
                return 1;
            }
        }
        NSError *error = nil;
        NSData *data = [NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingSortedKeys error:&error];
        if (!data) {
            fputs("Cannot encode virtualization audit\n", stderr);
            return 1;
        }
        [[NSFileHandle fileHandleWithStandardOutput] writeData:data];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
        return 0;
    }
}
