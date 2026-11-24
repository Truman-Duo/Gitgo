using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Threading;
using System.Windows.Forms;

internal static class DistributionEntry
{
    private static string Quote(string value)
    {
        // CommandLineToArgvW/CRT quoting, including trailing slashes.
        return "\"" + System.Text.RegularExpressions.Regex.Replace(value,
            @"(\\*)""|(\\+)$", match => match.Value.EndsWith("\"")
                ? new string('\\', match.Groups[1].Length * 2 + 1) + "\""
                : new string('\\', match.Length * 2)) + "\"";
    }

    private static Process Start(string program, params string[] arguments)
    {
        return Process.Start(new ProcessStartInfo(program, String.Join(" ", Array.ConvertAll(arguments, Quote)))
        {
            UseShellExecute = false,
            WorkingDirectory = Environment.CurrentDirectory,
        });
    }

    private static int Run(string program, params string[] arguments)
    {
        using (var child = Start(program, arguments))
        {
            child.WaitForExit();
            return child.ExitCode;
        }
    }

    private static string Workspace(string parent)
    {
        var directory = Path.Combine(parent, "Gitgo-" + Guid.NewGuid().ToString("N"));
        var security = new DirectorySecurity();
        security.SetAccessRuleProtection(true, false);
        var inheritance = InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit;
        foreach (var identity in new[] { WindowsIdentity.GetCurrent().User,
            new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null) })
            security.AddAccessRule(new FileSystemAccessRule(identity, FileSystemRights.FullControl,
                inheritance, PropagationFlags.None, AccessControlType.Allow));
        Directory.CreateDirectory(directory, security);
        return directory;
    }

    private static string Extract(string workspace, Action<int, int> progress = null)
    {
        var assembly = Assembly.GetExecutingAssembly();
        var package = Path.Combine(workspace, "package");
        using (var stream = assembly.GetManifestResourceStream("Gitgo.Payload"))
        using (var checksum = new StreamReader(assembly.GetManifestResourceStream("Gitgo.PayloadSha256")))
        using (var hasher = SHA256.Create())
        {
            var actual = BitConverter.ToString(hasher.ComputeHash(stream)).Replace("-", "");
            if (!String.Equals(actual, checksum.ReadLine().Trim(), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Embedded Gitgo archive failed its integrity check.");
            stream.Position = 0;
            using (var zip = new ZipArchive(stream, ZipArchiveMode.Read))
            {
                if (progress == null) zip.ExtractToDirectory(package);
                else ExtractWithProgress(zip, package, progress);
            }
        }
        return package;
    }

    private static void ExtractWithProgress(ZipArchive zip, string package, Action<int, int> progress)
    {
        var root = Path.GetFullPath(package) + Path.DirectorySeparatorChar;
        var total = zip.Entries.Count;
        Directory.CreateDirectory(package);
        progress(0, total);
        for (int index = 0; index < total; index++)
        {
            var entry = zip.Entries[index];
            if (entry.FullName.IndexOf(':') >= 0)
                throw new InvalidDataException("Gitgo archive contains an invalid path.");
            var path = Path.GetFullPath(Path.Combine(root, entry.FullName));
            if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Gitgo archive contains an invalid path.");
            if (String.IsNullOrEmpty(entry.Name))
            {
                if (entry.Length != 0) throw new InvalidDataException("Gitgo archive directory contains data.");
                Directory.CreateDirectory(path);
            }
            else
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                entry.ExtractToFile(path);
            }
            if ((index + 1) % 12 == 0 || index + 1 == total) progress(index + 1, total);
        }
    }

    private static string Host(string directory)
    {
        return Path.Combine(directory, "internal", "gitgo-host", "gitgo-host.exe");
    }

#if SETUP
    private static SetupResult RunCaptured(string program, string[] arguments, string launcher = null)
    {
        var info = new ProcessStartInfo(program, String.Join(" ", Array.ConvertAll(arguments, Quote))) {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true,
            RedirectStandardError = true, StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8, WorkingDirectory = Environment.CurrentDirectory
        };
        using (var child = Process.Start(info))
        {
            var stdout = child.StandardOutput.ReadToEndAsync();
            var stderr = child.StandardError.ReadToEndAsync();
            child.WaitForExit();
            return new SetupResult(child.ExitCode, stdout.Result, stderr.Result, launcher);
        }
    }

    private static SetupResult Install(string root, string desktop, string workspace, Action<SetupProgress> report)
    {
        var package = Path.Combine(workspace, "package");
        var ready = Path.Combine(workspace, "extracted");
        if (!File.Exists(ready))
        {
            report(new SetupProgress("verify"));
            if (Directory.Exists(package)) RemoveScratch(package);
            Extract(workspace, (completed, total) => report(new SetupProgress("extract", completed, total)));
            using (File.Create(ready)) { }
        }
        var install = Path.Combine(root, "program");
        var arguments = new System.Collections.Generic.List<string> {
            "--gitgo-internal-role", "managed-install", "--root", install,
            "--bin-dir", Path.Combine(root, "commands")
        };
        if (!String.IsNullOrEmpty(desktop))
            arguments.AddRange(new[] { "--desktop-folder", desktop });
        var registration = Path.Combine(install, "control", "registration.json");
        var repair = File.Exists(registration);
        if (repair) arguments.Add("--repair-entries");
        report(new SetupProgress(repair ? "repair" : "install"));
        var launcher = Path.Combine(install, "bootstrap", "gitgo-host", "gitgo-host.exe");
        var result = RunCaptured(Host(package), arguments.ToArray(), launcher);
        if (result.ExitCode == 0 && (!result.ManagedInstallConfirmed()
            || !File.Exists(launcher) || !File.Exists(Path.Combine(install, "active", "gitgo.exe"))
            || !File.Exists(registration)))
            return new SetupResult(2, "{\"code\":\"UPDATE_INSTALL_UNCONFIRMED\"}", result.Errors, launcher);
        return result;
    }

    private static int Execute(string[] args, string workspace, ref bool handoff)
    {
        var quiet = args.Length > 0 && args[0] == "--install-only";
        var localData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var defaultRoot = Path.Combine(localData, "GitgoInstalled");
        if (quiet)
        {
            if (args.Length > 2 && (args.Length != 4 || args[2] != "--desktop-folder"))
                throw new ArgumentException("Usage: GitgoSetup.exe [--install-only [directory [--desktop-folder directory]]]");
            var root = args.Length >= 2 ? Path.GetFullPath(args[1]) : defaultRoot;
            var desktop = args.Length == 4 ? Path.GetFullPath(args[3]) : null;
            var result = Install(root, desktop, workspace, delegate { });
            Console.Out.Write(result.Output);
            Console.Error.Write(result.Errors);
            if (result.ExitCode != 0) return result.ExitCode;
            var probe = RunCaptured(result.Launcher, new[] { "--gitgo-internal-role", "managed-launch", "--version" });
            Console.Out.Write(probe.Output);
            Console.Error.Write(probe.Errors);
            return probe.ExitCode;
        }
        if (args.Length != 0)
            throw new ArgumentException("Unknown Gitgo Setup argument.");
        SetupWizard.ConfigureTextScaling();
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using (var license = new StreamReader(Assembly.GetExecutingAssembly().GetManifestResourceStream("Gitgo.License")))
        using (var wizard = new SetupWizard(defaultRoot, license.ReadToEnd(),
            (root, desktop, report) => Install(root, desktop, workspace, report),
            launcher => Start(launcher, "--gitgo-internal-role", "managed-launch").Dispose(),
            Path.Combine(localData, "GitgoSetup", "last-attempt.json")))
        {
            Application.Run(wizard);
            return wizard.ExitCode;
        }
    }
#else
    private static int Execute(string[] args, string workspace, ref bool handoff)
    {
        var package = Extract(workspace);
        if (args.Length == 1 && Array.IndexOf(new[] { "--check-update", "--update", "--rollback" }, args[0]) >= 0)
        {
            if (args[0] == "--check-update")
                return Run(Host(package), "--gitgo-internal-role", "portable-update", "check");
            var ready = Path.Combine(workspace, "updater.ready");
            using (var me = Process.GetCurrentProcess())
            using (var updater = Start(Host(package), "--gitgo-internal-role", "portable-update",
                args[0] == "--update" ? "apply" : "rollback", "--parent", me.Id.ToString(), "--ready", ready))
            {
                for (int n = 0; n < 300 && !File.Exists(ready) && !updater.HasExited; n++)
                    Thread.Sleep(50);
                if (!File.Exists(ready))
                    throw new IOException("Gitgo could not prepare the update handoff.");
                handoff = true;
                return 0;
            }
        }
        // The launcher owns the temporary payload for the lifetime of this
        // terminal. Prevent a detached dashboard handoff from outliving it.
        var attached = new string[args.Length + 1];
        attached[0] = "--attached";
        Array.Copy(args, 0, attached, 1, args.Length);
        return Run(Path.Combine(package, "gitgo.exe"), attached);
    }
#endif

    // Explicit stack traversal never follows reparse points in the private
    // scratch namespace. Busy files may be retained for later cleanup.
    private static void RemoveScratch(string path)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) == 0)
        {
            foreach (var entry in Directory.EnumerateFileSystemEntries(path))
            {
                if ((File.GetAttributes(entry) & FileAttributes.Directory) == 0)
                    File.Delete(entry);
                else
                    RemoveScratch(entry);
            }
        }
        Directory.Delete(path);
    }

    [STAThread]
    public static int Main(string[] args)
    {
        string path = null;
        var handoff = false;
        try
        {
            path = Workspace(Path.GetTempPath());
            return Execute(args, path, ref handoff);
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Gitgo: " + error.Message);
#if SETUP
            if (args.Length == 0)
                MessageBox.Show(error.Message, "Gitgo Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
#endif
            return 1;
        }
        finally
        {
            if (path != null && !handoff)
            {
                try { RemoveScratch(path); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }
}
