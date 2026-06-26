#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>

@interface AppDelegate : NSObject <NSApplicationDelegate>
@property (strong) NSTask *viewer;
@property (strong) NSURL *url;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)note {
    NSString *home = NSHomeDirectory();
    NSString *support = [home stringByAppendingPathComponent:
        @"Library/Application Support/arXiv Hub"];
    NSString *python = [support stringByAppendingPathComponent:@"venv/bin/python"];
    NSString *script = [support stringByAppendingPathComponent:
        @"app/scripts/launch_viewer.py"];
    NSString *profile = [support stringByAppendingPathComponent:@"profile.toml"];

    NSFileManager *fm = [NSFileManager defaultManager];
    if (![fm isExecutableFileAtPath:python] || ![fm fileExistsAtPath:script]) {
        [self failWithTitle:@"arXiv Hub is not installed."
                    message:@"Run Install arXiv Hub.command first."];
        return;
    }

    NSTask *pk = [[NSTask alloc] init];
    pk.executableURL = [NSURL fileURLWithPath:@"/usr/bin/pkill"];
    pk.arguments = @[@"-f", @"launch_viewer.py"];
    @try { [pk launch]; [pk waitUntilExit]; } @catch (NSException *e) {}

    NSString *log = [NSTemporaryDirectory() stringByAppendingPathComponent:
        [NSString stringWithFormat:@"arxivhub-%d.log", getpid()]];
    [@"" writeToFile:log atomically:NO encoding:NSUTF8StringEncoding error:nil];
    NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:log];

    NSTask *viewer = [[NSTask alloc] init];
    viewer.executableURL = [NSURL fileURLWithPath:python];
    viewer.arguments = @[@"-u", script, @"--profile", profile, @"--no-open"];
    viewer.standardOutput = fh;
    viewer.standardError = fh;
    viewer.terminationHandler = ^(NSTask *t) {
        dispatch_async(dispatch_get_main_queue(), ^{ [NSApp terminate:nil]; });
    };
    @try {
        [viewer launch];
    } @catch (NSException *e) {
        [self failWithTitle:@"arXiv Hub failed to start."
                    message:@"Could not start the viewer process."];
        return;
    }
    self.viewer = viewer;

    __weak AppDelegate *weakSelf = self;
    dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
        NSString *prefix = @"Daily Preprint Viewer: ";
        NSURL *found = nil;
        for (int i = 0; i < 60 && !found; i++) {
            [NSThread sleepForTimeInterval:0.25];
            NSString *contents = [NSString stringWithContentsOfFile:log
                encoding:NSUTF8StringEncoding error:nil];
            for (NSString *line in [contents componentsSeparatedByString:@"\n"]) {
                NSString *tr = [line stringByTrimmingCharactersInSet:
                    NSCharacterSet.whitespaceCharacterSet];
                if ([tr hasPrefix:prefix]) {
                    found = [NSURL URLWithString:[tr substringFromIndex:prefix.length]];
                    break;
                }
            }
        }
        [fm removeItemAtPath:log error:nil];
        dispatch_async(dispatch_get_main_queue(), ^{
            if (found) {
                weakSelf.url = found;
                [[NSWorkspace sharedWorkspace] openURL:found];
            } else {
                [weakSelf failWithTitle:@"arXiv Hub failed to start."
                                message:@"Check that the app is installed correctly."];
            }
        });
    });
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender
                    hasVisibleWindows:(BOOL)flag {
    if (self.url) {
        [[NSWorkspace sharedWorkspace] openURL:self.url];
    }
    return YES;
}

- (void)failWithTitle:(NSString *)title message:(NSString *)message {
    [self.viewer terminate];
    NSAlert *a = [[NSAlert alloc] init];
    a.messageText = title;
    a.informativeText = message;
    [a runModal];
    [NSApp terminate:nil];
}

@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        AppDelegate *delegate = [[AppDelegate alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
