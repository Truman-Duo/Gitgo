// Fetch Apple's supported restore metadata only; never download or boot a guest.
#import <Foundation/Foundation.h>
#import <Virtualization/Virtualization.h>
#import <dispatch/dispatch.h>

static void emit(NSDictionary *report) {
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingSortedKeys error:&error];
    if (!data) { fputs("Cannot encode guest requirement audit\n", stderr); exit(1); }
    [[NSFileHandle fileHandleWithStandardOutput] writeData:data];
    [[NSFileHandle fileHandleWithStandardOutput] writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
}

int main(void) {
    @autoreleasepool {
#if defined(__arm64__)
        if (![VZVirtualMachine isSupported]) {
            emit(@{@"schema_version": @1, @"framework_supported": @NO,
                   @"guest_boot_tested": @NO, @"production_backend_enabled": @NO});
            return 2;
        }
        [VZMacOSRestoreImage fetchLatestSupportedWithCompletionHandler:^(VZMacOSRestoreImage *image, NSError *error) {
            dispatch_async(dispatch_get_main_queue(), ^{
                if (error || !image) {
                    emit(@{@"schema_version": @1, @"metadata_fetch_succeeded": @NO,
                        @"error_domain": error.domain ?: @"unknown", @"error_code": @(error.code),
                        @"guest_boot_tested": @NO, @"production_backend_enabled": @NO});
                    exit(1);
                }
                VZMacOSConfigurationRequirements *requirements = image.mostFeaturefulSupportedConfiguration;
                NSMutableDictionary *report = [@{
                    @"schema_version": @1, @"metadata_fetch_succeeded": @YES,
                    @"guest_boot_tested": @NO, @"production_backend_enabled": @NO,
                    @"restore_url": image.URL.absoluteString,
                    @"build_version": image.buildVersion,
                    @"os_major": @(image.operatingSystemVersion.majorVersion),
                    @"os_minor": @(image.operatingSystemVersion.minorVersion),
                    @"os_patch": @(image.operatingSystemVersion.patchVersion),
                    @"configuration_available": (requirements != nil ? @YES : @NO),
                } mutableCopy];
                if (requirements) {
                    report[@"minimum_cpu_count"] = @(requirements.minimumSupportedCPUCount);
                    report[@"minimum_memory_bytes"] = @(requirements.minimumSupportedMemorySize);
                    report[@"hardware_model_supported"] = (requirements.hardwareModel.supported ? @YES : @NO);
                    report[@"hardware_model_data"] = [requirements.hardwareModel.dataRepresentation base64EncodedStringWithOptions:0];
                }
                emit(report);
                exit(requirements ? 0 : 2);
            });
        }];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 90 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
            fputs("Apple guest metadata query timed out\n", stderr);
            exit(1);
        });
        dispatch_main();
#else
        emit(@{@"schema_version": @1, @"macos_guest_supported": @NO,
               @"guest_boot_tested": @NO, @"production_backend_enabled": @NO});
        return 2;
#endif
    }
}
