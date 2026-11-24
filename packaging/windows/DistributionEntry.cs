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

    private static string Extract(string workspace)
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
                zip.ExtractToDirectory(package);
        }
        return package;
    }

    private static string Host(string directory)
    {
        return Path.Combine(directory, "internal", "gitgo-host", "gitgo-host.exe");
    }

#if SETUP
    private static int Execute(string[] args, string workspace, ref bool handoff)
    {
        var quiet = args.Length > 0 && args[0] == "--install-only";
        if ((quiet && args.Length > 2) || (!quiet && args.Length != 0))
            throw new ArgumentException("Usage: GitgoSetup.exe [--install-only [directory]]");
        var root = quiet && args.Length == 2 ? Path.GetFullPath(args[1])
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "GitgoInstalled");
        var install = Path.Combine(root, "program");
        var commands = Path.Combine(root, "commands");
        if (!quiet && MessageBox.Show("Install Gitgo for this Windows user?\r\n\r\n" + root,
                "Gitgo Setup", MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK)
            return 0;
        if (!File.Exists(Path.Combine(install, "control", "registration.json")))
        {
            var status = Run(Host(Extract(workspace)), "--gitgo-internal-role", "managed-install",
                "--root", install, "--bin-dir", commands);
            if (status != 0)
                return status;
        }
        var launcher = Path.Combine(install, "bootstrap", "gitgo-host", "gitgo-host.exe");
        return quiet ? Run(launcher, "--gitgo-internal-role", "managed-launch", "--version")
            : Run(launcher, "--gitgo-internal-role", "managed-launch");
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
        return Run(Path.Combine(package, "gitgo.exe"), args);
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
