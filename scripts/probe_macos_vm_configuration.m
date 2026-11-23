// Configuration validation only. Never installs or boots a guest OS.
#import <Foundation/Foundation.h>
#import <Virtualization/Virtualization.h>
#include <fcntl.h>
#include <unistd.h>

#if defined(__arm64__)
static VZVirtualMachineConfiguration *configuration(NSDictionary *metadata, NSURL *root, NSString *variant, NSError **error) {
    NSData *modelData = [[NSData alloc] initWithBase64EncodedString:metadata[@"hardware_model_data"] options:0];
    if (modelData.length == 0) return nil;
    VZMacHardwareModel *model = [[VZMacHardwareModel alloc] initWithDataRepresentation:modelData];
    if (!model || !model.supported) return nil;
    NSURL *auxURL = [root URLByAppendingPathComponent:[variant stringByAppendingString:@".aux"]];
    VZMacAuxiliaryStorage *aux = [[VZMacAuxiliaryStorage alloc] initCreatingStorageAtURL:auxURL hardwareModel:model options:0 error:error];
    if (!aux) return nil;
    VZMacPlatformConfiguration *platform = [[VZMacPlatformConfiguration alloc] init];
    platform.hardwareModel = model;
    platform.machineIdentifier = [[VZMacMachineIdentifier alloc] init];
    platform.auxiliaryStorage = aux;
    NSURL *diskURL = [root URLByAppendingPathComponent:[variant stringByAppendingString:@".raw"]];
    int descriptor = open(diskURL.fileSystemRepresentation, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (descriptor < 0) return nil;
    int sized = ftruncate(descriptor, (off_t)64 * 1024 * 1024 * 1024);
    int closed = close(descriptor);
    if (sized || closed) return nil;
    VZDiskImageStorageDeviceAttachment *disk = [[VZDiskImageStorageDeviceAttachment alloc] initWithURL:diskURL readOnly:NO error:error];
    if (!disk) return nil;
    VZVirtualMachineConfiguration *config = [[VZVirtualMachineConfiguration alloc] init];
    config.platform = platform;
    config.bootLoader = [[VZMacOSBootLoader alloc] init];
    config.CPUCount = [metadata[@"minimum_cpu_count"] unsignedIntegerValue];
    config.memorySize = [metadata[@"minimum_memory_bytes"] unsignedLongLongValue];
    config.storageDevices = @[[[VZVirtioBlockDeviceConfiguration alloc] initWithAttachment:disk]];
    VZMacGraphicsDeviceConfiguration *graphics = [[VZMacGraphicsDeviceConfiguration alloc] init];
    graphics.displays = @[[[VZMacGraphicsDisplayConfiguration alloc] initWithWidthInPixels:1280 heightInPixels:800 pixelsPerInch:80]];
    config.graphicsDevices = @[graphics];
    if ([variant isEqualToString:@"socket"]) config.socketDevices = @[[[VZVirtioSocketDeviceConfiguration alloc] init]];
    if ([variant isEqualToString:@"serial"]) {
        VZVirtioConsoleDeviceSerialPortConfiguration *port = [[VZVirtioConsoleDeviceSerialPortConfiguration alloc] init];
        port.attachment = [[VZFileHandleSerialPortAttachment alloc] initWithFileHandleForReading:[NSFileHandle fileHandleForReadingAtPath:@"/dev/null"] fileHandleForWriting:[NSFileHandle fileHandleForWritingAtPath:@"/dev/null"]];
        config.serialPorts = @[port];
    }
    if ([variant isEqualToString:@"directory"]) {
        VZVirtioFileSystemDeviceConfiguration *device = [[VZVirtioFileSystemDeviceConfiguration alloc] initWithTag:@"gitgo-empty-probe"];
        NSURL *shareURL = [root URLByAppendingPathComponent:@"empty-share" isDirectory:YES];
        if (![[NSFileManager defaultManager] createDirectoryAtURL:shareURL withIntermediateDirectories:NO attributes:@{NSFilePosixPermissions:@0700} error:error]) return nil;
        device.share = [[VZSingleDirectoryShare alloc] initWithDirectory:[[VZSharedDirectory alloc] initWithURL:shareURL readOnly:YES]];
        config.directorySharingDevices = @[device];
    }
    return config;
}
#endif
int main(int argc, const char *argv[]) {
    (void)argv; // Not consumed by the explicit unsupported-architecture branch.
    @autoreleasepool {
        if (argc != 3) { fputs("Expected metadata JSON and an existing private output directory\n", stderr); return 1; }
#if defined(__arm64__)
        if (![VZVirtualMachine isSupported]) { fputs("Virtualization unavailable in this execution context\n", stderr); return 2; }
        NSError *error = nil;
        NSData *data = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:argv[1]] options:0 error:&error];
        NSDictionary *metadata = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:&error] : nil;
        if (![metadata isKindOfClass:[NSDictionary class]] ||
            ![metadata[@"configuration_available"] isEqual:@YES] ||
            ![metadata[@"hardware_model_data"] isKindOfClass:[NSString class]] ||
            ![metadata[@"minimum_cpu_count"] isKindOfClass:[NSNumber class]] ||
            ![metadata[@"minimum_memory_bytes"] isKindOfClass:[NSNumber class]]) {
            fputs("Supported Apple restore metadata required\n", stderr); return 1;
        }
        NSString *template = [[NSString stringWithUTF8String:argv[2]] stringByAppendingPathComponent:@"configuration.XXXXXX"];
        char *temporary = strdup(template.fileSystemRepresentation);
        if (!temporary || !mkdtemp(temporary)) { free(temporary); fputs("Cannot create private probe directory\n", stderr); return 1; }
        NSURL *root = [NSURL fileURLWithFileSystemRepresentation:temporary isDirectory:YES relativeToURL:nil];
        free(temporary);
        NSMutableDictionary *variants = [NSMutableDictionary dictionary];
        BOOL allValid = YES;
        for (NSString *variant in @[@"base", @"socket", @"serial", @"directory"]) {
            error = nil;
            VZVirtualMachineConfiguration *config = configuration(metadata, root, variant, &error);
            BOOL valid = config && [config validateWithError:&error];
            allValid = allValid && valid;
            variants[variant] = @{@"configuration_valid": (valid ? @YES : @NO),
                @"error_domain": error.domain ?: @"", @"error_code": @(error.code),
                @"error_description": error.localizedDescription ?: @""};
        }
        NSDictionary *report = @{@"schema_version": @1, @"guest_boot_tested": @NO,
            @"production_backend_enabled": @NO, @"network_devices": @0,
            @"disk_images_sparse": @YES, @"variants": variants};
        NSData *output = [NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingSortedKeys error:&error];
        if (!output) return 1;
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
        return allValid ? 0 : 1;
#else
        fputs("macOS guests require Apple silicon\n", stderr);
        return 2;
#endif
    }
}
