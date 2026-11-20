// Development-only VM lifecycle experiment, NOT a Gitgo execution backend.
// No network device, directory share, guest command or production capability.
#import <Foundation/Foundation.h>
#import <Virtualization/Virtualization.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <unistd.h>

static void emit(NSString *event, NSDictionary *fields) {
    NSMutableDictionary *record = [fields mutableCopy];
    record[@"event"] = event;
    record[@"schema_version"] = @1;
    record[@"production_backend_enabled"] = @NO;
    record[@"guest_command_execution_tested"] = @NO;
    NSData *data = [NSJSONSerialization dataWithJSONObject:record options:NSJSONWritingSortedKeys error:nil];
    if (!data) { fputs("Cannot encode lifecycle event\n", stderr); exit(1); }
    [[NSFileHandle fileHandleWithStandardOutput] writeData:data];
    [[NSFileHandle fileHandleWithStandardOutput] writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
}

#if defined(__arm64__)
static NSError *failure(NSString *reason) {
    return [NSError errorWithDomain:@"GitgoGuestExperiment" code:1
                          userInfo:@{NSLocalizedDescriptionKey: reason}];
}

static BOOL privateFile(NSURL *url, BOOL directory, NSError **error) {
    struct stat info;
    if (lstat(url.fileSystemRepresentation, &info) != 0 || info.st_uid != geteuid() ||
        (info.st_mode & 0077) != 0 ||
        (directory ? !S_ISDIR(info.st_mode) : (!S_ISREG(info.st_mode) || info.st_nlink != 1))) {
        *error = failure(@"Expected a private, owned, non-link experiment object");
        return NO;
    }
    return YES;
}

static BOOL saveMetadata(NSURL *root, NSDictionary *metadata, NSError **error) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:metadata options:NSJSONWritingSortedKeys error:error];
    if (!data) return NO;
    // This experiment's metadata is not an authorization or recovery ledger.
    NSURL *url = [root URLByAppendingPathComponent:@"configuration.json"];
    if (![data writeToURL:url options:NSDataWritingAtomic error:error]) return NO;
    return chmod(url.fileSystemRepresentation, 0600) == 0;
}

static BOOL validMetadata(NSDictionary *metadata) {
    if (![metadata isKindOfClass:[NSDictionary class]] || metadata.count != 7) return NO;
    for (NSString *key in @[@"hardware_model", @"machine_identifier", @"phase", @"build_version"]) {
        if (![metadata[key] isKindOfClass:[NSString class]] ||
            [metadata[key] length] == 0 || [metadata[key] length] > 32768) return NO;
    }
    for (NSString *key in @[@"schema_version", @"cpu_count", @"memory_bytes"]) {
        if (![metadata[key] isKindOfClass:[NSNumber class]] ||
            CFGetTypeID((__bridge CFTypeRef)metadata[key]) == CFBooleanGetTypeID() ||
            CFNumberIsFloatType((__bridge CFNumberRef)metadata[key])) return NO;
    }
    return [metadata[@"schema_version"] isEqual:@1] &&
        [metadata[@"cpu_count"] unsignedIntegerValue] >= 2 &&
        [metadata[@"cpu_count"] unsignedIntegerValue] <= [VZVirtualMachineConfiguration maximumAllowedCPUCount] &&
        [metadata[@"memory_bytes"] unsignedLongLongValue] >= 4ULL * 1024 * 1024 * 1024 &&
        [metadata[@"memory_bytes"] unsignedLongLongValue] <= 8ULL * 1024 * 1024 * 1024;
}

static VZVirtualMachineConfiguration *makeConfiguration(NSURL *root, NSDictionary *metadata, NSError **error) {
    if (!validMetadata(metadata) || !privateFile(root, YES, error)) return nil;
    NSData *modelData = [[NSData alloc] initWithBase64EncodedString:metadata[@"hardware_model"] options:0];
    NSData *identifierData = [[NSData alloc] initWithBase64EncodedString:metadata[@"machine_identifier"] options:0];
    if (modelData.length == 0 || identifierData.length == 0) {
        *error = failure(@"Invalid encoded experiment identity"); return nil;
    }
    VZMacHardwareModel *model = [[VZMacHardwareModel alloc] initWithDataRepresentation:modelData];
    VZMacMachineIdentifier *identifier = [[VZMacMachineIdentifier alloc] initWithDataRepresentation:identifierData];
    if (!model || !model.supported || !identifier) {
        *error = failure(@"Unsupported or invalid experiment hardware identity");
        return nil;
    }
    NSURL *auxURL = [root URLByAppendingPathComponent:@"auxiliary.bin"];
    NSURL *diskURL = [root URLByAppendingPathComponent:@"disk.raw"];
    if (!privateFile(auxURL, NO, error) || !privateFile(diskURL, NO, error)) return nil;
    VZMacPlatformConfiguration *platform = [[VZMacPlatformConfiguration alloc] init];
    platform.hardwareModel = model;
    platform.machineIdentifier = identifier;
    platform.auxiliaryStorage = [[VZMacAuxiliaryStorage alloc] initWithURL:auxURL];
    VZDiskImageStorageDeviceAttachment *disk = [[VZDiskImageStorageDeviceAttachment alloc]
        initWithURL:diskURL readOnly:NO error:error];
    if (!disk) return nil;
    VZVirtualMachineConfiguration *configuration = [[VZVirtualMachineConfiguration alloc] init];
    configuration.platform = platform;
    configuration.bootLoader = [[VZMacOSBootLoader alloc] init];
    configuration.CPUCount = [metadata[@"cpu_count"] unsignedIntegerValue];
    configuration.memorySize = [metadata[@"memory_bytes"] unsignedLongLongValue];
    configuration.storageDevices = @[[[VZVirtioBlockDeviceConfiguration alloc] initWithAttachment:disk]];
    VZMacGraphicsDeviceConfiguration *graphics = [[VZMacGraphicsDeviceConfiguration alloc] init];
    graphics.displays = @[[[VZMacGraphicsDisplayConfiguration alloc]
        initWithWidthInPixels:1280 heightInPixels:800 pixelsPerInch:80]];
    configuration.graphicsDevices = @[graphics];
    configuration.socketDevices = @[[[VZVirtioSocketDeviceConfiguration alloc] init]];
    // Explicitly prohibit accidental use of host networking/shares in this probe.
    configuration.networkDevices = @[];
    configuration.directorySharingDevices = @[];
    if (![configuration validateWithError:error]) return nil;
    return configuration;
}

@interface Experiment : NSObject <VZVirtualMachineDelegate>
@property(nonatomic, strong) VZVirtualMachine *machine;
@property(nonatomic, strong) VZMacOSInstaller *installer;
@property(nonatomic, strong) NSURL *root;
@property(nonatomic, strong) NSMutableDictionary *metadata;
@property(nonatomic, strong) dispatch_source_t progressTimer;
@property(nonatomic, strong) dispatch_source_t termSignal;
@property(nonatomic, strong) dispatch_source_t intSignal;
@property(nonatomic) BOOL installing;
@property(nonatomic) BOOL stopping;
@property(nonatomic) BOOL started;
@property(nonatomic) int outcome;
@property(nonatomic) int ownershipFD;
- (void)stopWithReason:(NSString *)reason;
@end

@implementation Experiment
- (void)stopWithReason:(NSString *)reason {
    if (self.stopping) return;
    self.stopping = YES;
    if (self.installing) {
        // Apple explicitly forbids stopping a VM during installation.
        [self.installer.progress cancel];
        emit(@"installation_cancel_requested", @{@"cleanup_confirmed": @NO});
        return;
    }
    if (self.machine.state == VZVirtualMachineStateStopped) {
        emit(@"stopped", @{@"reason": reason, @"cleanup_confirmed": @YES});
        exit(self.outcome);
    }
    if (!self.machine.canStop) {
        emit(@"stop_unavailable", @{@"reason": reason, @"cleanup_confirmed": @NO,
                                    @"state": @(self.machine.state)});
        exit(1);
    }
    [self.machine stopWithCompletionHandler:^(NSError *error) {
        BOOL stopped = !error && self.machine.state == VZVirtualMachineStateStopped;
        emit(@"forced_stop", @{@"reason": reason, @"cleanup_confirmed": (stopped ? @YES : @NO),
            @"state": @(self.machine.state), @"error": error.localizedDescription ?: @""});
        exit(stopped ? self.outcome : 1);
    }];
}
- (void)guestDidStopVirtualMachine:(VZVirtualMachine *)machine {
    if (self.installing) return;
    emit(@"guest_stopped", @{@"state": @(machine.state),
        @"cleanup_confirmed": (machine.state == VZVirtualMachineStateStopped ? @YES : @NO)});
    exit(machine.state == VZVirtualMachineStateStopped ? self.outcome : 1);
}
- (void)virtualMachine:(VZVirtualMachine *)machine didStopWithError:(NSError *)error {
    emit(@"vm_error", @{@"state": @(machine.state), @"cleanup_confirmed": @NO,
                        @"error": error.localizedDescription ?: @""});
    exit(1);
}
@end

static Experiment *active;

static int lockExperiment(NSURL *root) {
    NSURL *lockURL = [root URLByAppendingPathComponent:@"owner.lock"];
    int fd = open(lockURL.fileSystemRepresentation, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    struct stat info;
    if (fd < 0) return -1;
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_nlink != 1 ||
        info.st_uid != geteuid() || (info.st_mode & 0077) != 0 || flock(fd, LOCK_EX | LOCK_NB) != 0) {
        close(fd); return -1;
    }
    return fd;
}

static void prepareSignals(Experiment *experiment) {
    signal(SIGTERM, SIG_IGN);
    signal(SIGINT, SIG_IGN);
    experiment.termSignal = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGTERM, 0, dispatch_get_main_queue());
    experiment.intSignal = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGINT, 0, dispatch_get_main_queue());
    dispatch_source_set_event_handler(experiment.termSignal, ^{ [experiment stopWithReason:@"sigterm"]; });
    dispatch_source_set_event_handler(experiment.intSignal, ^{ [experiment stopWithReason:@"sigint"]; });
    dispatch_resume(experiment.termSignal);
    dispatch_resume(experiment.intSignal);
}

static void installImage(NSURL *imageURL, NSURL *root) {
    [VZMacOSRestoreImage loadFileURL:imageURL completionHandler:^(VZMacOSRestoreImage *image, NSError *loadError) {
        dispatch_async(dispatch_get_main_queue(), ^{
            NSError *error = loadError;
            VZMacOSConfigurationRequirements *requirements = image.mostFeaturefulSupportedConfiguration;
            if (error || !image || !image.supported || !requirements) {
                emit(@"restore_image_rejected", @{@"error": error.localizedDescription ?: @"Unsupported image"});
                exit(1);
            }
            // Never overwrite or resume an existing installation implicitly.
            if (mkdir(root.fileSystemRepresentation, 0700) != 0) {
                emit(@"experiment_directory_rejected", @{@"error": @"A new private directory is required"});
                exit(1);
            }
            VZMacMachineIdentifier *identifier = [[VZMacMachineIdentifier alloc] init];
            NSURL *auxURL = [root URLByAppendingPathComponent:@"auxiliary.bin"];
            VZMacAuxiliaryStorage *aux = [[VZMacAuxiliaryStorage alloc]
                initCreatingStorageAtURL:auxURL hardwareModel:requirements.hardwareModel options:0 error:&error];
            if (!aux || chmod(auxURL.fileSystemRepresentation, 0600) != 0) {
                emit(@"auxiliary_creation_failed", @{@"error": error.localizedDescription ?: @"Permissions"});
                exit(1);
            }
            NSURL *diskURL = [root URLByAppendingPathComponent:@"disk.raw"];
            int fd = open(diskURL.fileSystemRepresentation, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
            if (fd < 0) { emit(@"disk_creation_failed", @{}); exit(1); }
            int sized = ftruncate(fd, (off_t)64 * 1024 * 1024 * 1024);
            int closed = close(fd);
            if (sized || closed) { emit(@"disk_sizing_failed", @{}); exit(1); }
            NSMutableDictionary *metadata = [@{
                @"schema_version": @1, @"phase": @"created", @"build_version": image.buildVersion,
                @"cpu_count": @(requirements.minimumSupportedCPUCount),
                @"memory_bytes": @(requirements.minimumSupportedMemorySize),
                @"hardware_model": [requirements.hardwareModel.dataRepresentation base64EncodedStringWithOptions:0],
                @"machine_identifier": [identifier.dataRepresentation base64EncodedStringWithOptions:0],
            } mutableCopy];
            if (!saveMetadata(root, metadata, &error)) { emit(@"metadata_write_failed", @{}); exit(1); }
            VZVirtualMachineConfiguration *configuration = makeConfiguration(root, metadata, &error);
            if (!configuration) {
                emit(@"configuration_rejected", @{@"error": error.localizedDescription ?: @"Invalid configuration"});
                exit(1);
            }
            active = [[Experiment alloc] init];
            active.ownershipFD = lockExperiment(root);
            if (active.ownershipFD < 0) { emit(@"experiment_busy_or_invalid", @{}); exit(1); }
            active.root = root;
            active.metadata = metadata;
            active.machine = [[VZVirtualMachine alloc] initWithConfiguration:configuration];
            active.machine.delegate = active;
            active.installer = [[VZMacOSInstaller alloc] initWithVirtualMachine:active.machine restoreImageURL:imageURL];
            active.installing = YES;
            prepareSignals(active);
            emit(@"installation_started", @{@"build_version": image.buildVersion,
                @"memory_bytes": @(configuration.memorySize), @"cpu_count": @(configuration.CPUCount),
                @"network_devices": @(configuration.networkDevices.count), @"directory_shares": @0});
            active.progressTimer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
            dispatch_source_set_timer(active.progressTimer, dispatch_time(DISPATCH_TIME_NOW, 20 * NSEC_PER_SEC), 20 * NSEC_PER_SEC, NSEC_PER_SEC);
            dispatch_source_set_event_handler(active.progressTimer, ^{
                emit(@"installation_progress", @{@"fraction": @(active.installer.progress.fractionCompleted)});
            });
            dispatch_resume(active.progressTimer);
            [active.installer installWithCompletionHandler:^(NSError *installationError) {
                active.installing = NO;
                active.stopping = NO;
                dispatch_source_cancel(active.progressTimer);
                if (installationError) {
                    active.outcome = 1;
                    emit(@"installation_failed", @{@"error": installationError.localizedDescription ?: @"",
                        @"cleanup_confirmed": @NO, @"state": @(active.machine.state)});
                    // Incomplete configuration remains marked created; never used by boot.
                    if (active.machine.canStop) [active stopWithReason:@"installation_failed"];
                    else exit(1);
                    return;
                }
                active.metadata[@"phase"] = @"installed";
                NSError *writeError = nil;
                if (!saveMetadata(root, active.metadata, &writeError)) {
                    emit(@"metadata_write_failed", @{@"cleanup_confirmed": @NO});
                    active.outcome = 1;
                    [active stopWithReason:@"metadata_write_failed"];
                    return;
                }
                emit(@"installation_completed", @{@"guest_setup_completed": @NO,
                    @"state": @(active.machine.state)});
                [active stopWithReason:@"installation_completed"];
            }];
        });
    }];
}

static void boot(NSURL *root) {
    NSError *error = nil;
    NSURL *metadataURL = [root URLByAppendingPathComponent:@"configuration.json"];
    if (!privateFile(root, YES, &error) || !privateFile(metadataURL, NO, &error)) {
        emit(@"experiment_directory_rejected", @{}); exit(1);
    }
    NSData *data = [NSData dataWithContentsOfURL:metadataURL options:0 error:&error];
    NSDictionary *metadata = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:&error] : nil;
    if (!validMetadata(metadata) || ![metadata[@"phase"] isEqual:@"installed"]) {
        emit(@"installation_incomplete", @{}); exit(1);
    }
    VZVirtualMachineConfiguration *configuration = makeConfiguration(root, metadata, &error);
    if (!configuration) {
        emit(@"configuration_rejected", @{@"error": error.localizedDescription ?: @"Invalid configuration"}); exit(1);
    }
    active = [[Experiment alloc] init];
    active.ownershipFD = lockExperiment(root);
    if (active.ownershipFD < 0) { emit(@"experiment_busy_or_invalid", @{}); exit(1); }
    active.machine = [[VZVirtualMachine alloc] initWithConfiguration:configuration];
    active.machine.delegate = active;
    prepareSignals(active);
    [active.machine startWithCompletionHandler:^(NSError *startError) {
        if (startError) {
            emit(@"start_failed", @{@"error": startError.localizedDescription ?: @"", @"cleanup_confirmed": @NO});
            exit(1);
        }
        active.started = YES;
        emit(@"framework_started", @{@"state": @(active.machine.state),
            @"guest_setup_completed": @NO, @"network_devices": @0, @"directory_shares": @0});
        // A framework start is NOT proof of a usable OS or command isolation.
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 45 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
            [active stopWithReason:@"bounded_boot_probe"];
        });
    }];
}
#endif

int main(int argc, const char *argv[]) {
    (void)argv; // Not consumed by the explicit unsupported-architecture branch.
    @autoreleasepool {
        if (argc < 2) {
            fputs("Development experiment: install RESTORE_IPSW NEW_PRIVATE_DIRECTORY | boot EXISTING_DIRECTORY\n", stderr);
            return 1;
        }
#if defined(__arm64__)
        if (![VZVirtualMachine isSupported]) { emit(@"framework_unavailable", @{}); return 2; }
        NSString *mode = [NSString stringWithUTF8String:argv[1]];
        if ([mode isEqual:@"install"] && argc == 4) {
            installImage([NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[2]]],
                         [NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[3]]]);
        } else if ([mode isEqual:@"boot"] && argc == 3) {
            boot([NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[2]]]);
        } else { fputs("Invalid experiment arguments\n", stderr); return 1; }
        dispatch_main();
#else
        emit(@"macos_guest_requires_apple_silicon", @{});
        return 2;
#endif
    }
}
